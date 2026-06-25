import json
import logging
import time
from collections.abc import Mapping

from odoo import SUPERUSER_ID, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Response, request
from odoo.models import BaseModel
from odoo.service.model import get_public_method
from odoo.tools.json import json_default as odoo_json_default

from .oauth import verify_access_token

_LOG = logging.getLogger(__name__)


# ─── Absolute blocks ─────────────────────────────────────────────────
# Methods that bypass ACL or reach into the ORM internals. Hardcoded
# block regardless of policy mode — these aren't business operations,
# they're escape hatches that have no place behind an RPC. Even an
# admin authoring an allow rule for them should be a friction event.

_ABSOLUTE_METHOD_BLOCKS = frozenset({"sudo", "with_user", "with_env", "with_company"})
_ABSOLUTE_METHOD_PREFIXES = ("flush", "invalidate")


def _is_absolutely_blocked(method: str) -> bool:
    if method in _ABSOLUTE_METHOD_BLOCKS:
        return True
    # Dunders (``__class__``, ``__getattribute__``, ``__reduce__``, …) are
    # Python introspection slots, not business methods. No RPC has a
    # legitimate use for them, so they are hard-blocked at the gate.
    if method.startswith("__") and method.endswith("__"):
        return True
    return method.startswith(_ABSOLUTE_METHOD_PREFIXES)


# ─── Classification ──────────────────────────────────────────────────
# Each inbound method gets one of four operation types. The classifier
# is intentionally pattern-light: most cases fall into private/_-prefix,
# explicit write set, or the read prefix family. Business methods and
# actions land in `execute` and only get bouncing by explicit rules.

# These sets mirror the typed method names Tuqui's CompanionTransport posts to
# this gateway — see tuqui_core/integrations/odoo/transports/companion.py and
# the contract test ``test_classify_covers_companion_transport_surface``. Keep
# all three in sync. Asymmetry to remember when the transport gains a method:
#   * a READ not recognized here → ``execute`` → mislabelled in the audit log
#     (that was the formatted_read_group bug; explicit entry in _READ_METHODS fixes it).
#   * a WRITE not listed → also ``execute`` → only its audit row gets mislabelled.
# This classifier is the audit label, NOT the authorization boundary: writes
# are really gated by the backend whitelist (workspace_write_models) and the
# acting user's Odoo ACL.
_WRITE_METHODS = frozenset({"create", "write", "unlink", "copy", "name_create"})
# Reads that don't begin with the search/read prefix must be listed explicitly
# (e.g. ``formatted_read_group``, the Odoo 19 grouped read).
_READ_METHODS = frozenset(
    {
        "name_search",
        "fields_get",
        "default_get",
        "formatted_read_group",
    }
)
_READ_PREFIXES = ("search", "read")


# L1: read_only is enforced by method-NAME classification, not at the cursor
# level. A mutating method whose name starts with ``read``/``search`` (or is
# listed in ``_READ_METHODS``) would classify as ``read`` and slip past the
# read_only gate — and on the connection path it would run as superuser. No
# core Odoo method does this, but a custom client module could; a cursor-level
# read-only guard was deemed disproportionate vs. the LOCKED auth design.
def _classify(method: str) -> str:
    if method.startswith("_"):
        return "private_execute"
    if method in _WRITE_METHODS:
        return "write"
    if method in _READ_METHODS or method.startswith(_READ_PREFIXES):
        return "read"
    return "execute"


# ─── Policy gate ─────────────────────────────────────────────────────


def _evaluate_policy(method: str, op_type: str, *, is_connection: bool):
    """Return ``(allowed, denied_reason)``.

    ``allowed=True`` means the call may proceed to the ORM (where the acting
    user's own Odoo ACL is the real per-call check on the member path).
    ``denied_reason`` is set only when ``allowed=False``. Gates, in order:

    1. Absolute blocks — ``sudo`` / ``with_*`` / ``flush*`` / ``invalidate*``
       and dunders bypass or sidestep the ACL; blocked unconditionally.
    2. Private (``_``-prefixed) methods — internal ORM surface, never exposed.
    3. Connection path (``is_connection=True``): runs as SUPERUSER with no
       record rules, so it is locked to reads UNCONDITIONALLY — anything that
       can mutate is refused with ``connection_read_only``. Keeps the blast
       radius of a stolen token to read-only on workspace-level/system traffic.
    """
    if _is_absolutely_blocked(method):
        return False, "method_blocked"
    if op_type == "private_execute":
        return False, "private_method_blocked"
    if is_connection:
        # Only read-classified ops may run as superuser. Anything else
        # (write / execute) is refused here — sudo must never mutate.
        if op_type != "read":
            return False, "connection_read_only"
        return True, None
    return True, None


# ─── Dispatch ────────────────────────────────────────────────────────
# Mirrors odoo.service.model.call_kw — same args[0]=ids semantics for
# record methods, same context popping, same recordset → ids result
# adaptation. Method resolution delegates to Odoo's own
# ``get_public_method`` (the same guard ``call_kw`` uses), so the gateway
# honors the platform's ``@api.private`` / classmethod / unsafe-attribute
# contracts and can't drift from them. ``_UNSAFE_ATTRIBUTES`` (Python
# introspection slots like ``mro``, ``f_code``) and public-named private
# methods (``init``, ``mapped``, ``new``, …) are refused here as
# ``AccessError`` → 403 ``access_denied``, in parity with native RPC. The
# policy gate upstream still refuses ``_``-prefixed / sudo / with_* /
# flush* / invalidate* / dunders with their own explicit codes first.


def _dispatch(model, method_name: str, args: list, kwargs: dict):
    """Invoke ``method_name`` on ``model`` with execute_kw-style args.

    Method resolution delegates to Odoo's own ``get_public_method`` (the same
    guard ``call_kw`` uses) so the gateway honors ``@api.private`` /
    classmethod / unsafe-attribute contracts and can't drift from the platform.
    The policy gate already refused ``_``-prefixed / sudo / with_* / flush* /
    invalidate* / dunders upstream with explicit codes; anything that reaches
    here and is still non-public-callable (``init``, ``mapped``, ``new``, …) is
    refused as AccessError → 403 ``access_denied`` (parity with native RPC).
    """
    try:
        method = get_public_method(model, method_name)
    except AccessError:
        raise  # rpc() maps AccessError → access_denied (403)
    except AttributeError:
        # Unknown / non-callable method → preserve the prior 400 mapping.
        raise ValidationError(f"Model {model._name!r} has no callable method {method_name!r}")

    args = list(args)
    if getattr(method, "_api_model", False):
        recs = model
    else:
        if not args:
            raise ValidationError(f"Record method {method_name!r} requires a list of ids as the first positional arg")
        ids = args[0]
        args = args[1:]
        if isinstance(ids, int):
            ids = [ids]
        if not isinstance(ids, (list, tuple)):
            raise ValidationError(f"First positional arg for record method {method_name!r} must be a list of ids")
        recs = model.browse(ids)

    kwargs = dict(kwargs)
    inner_context = kwargs.pop("context", None) or {}
    if inner_context:
        recs = recs.with_context(**inner_context)

    result = method(recs, *args, **kwargs)

    # Adapt result to a JSON-safe shape — match what odoo.service.model.call_kw does
    # so Tuqui's CompanionTransport gets the same data shape as JsonRpcTransport.
    if method_name == "create":
        # @api.model_create_multi keeps original args (no ids pop); args[0] is the vals.
        original_vals = args[0] if args else None
        if isinstance(original_vals, Mapping):
            result = result.id
        else:
            result = result.ids
    elif isinstance(result, BaseModel):
        result = result.ids

    return result


# ─── HTTP helpers ────────────────────────────────────────────────────


def _serializer(obj):
    """JSON fallback: recordsets → ids, then delegate to Odoo's date/lazy serializer."""
    if isinstance(obj, BaseModel):
        return obj.ids
    return odoo_json_default(obj)


def _json_response(body, status=200):
    return Response(
        json.dumps(body, default=_serializer),
        content_type="application/json",
        status=status,
    )


def _ok(data):
    return _json_response({"ok": True, "data": data}, status=200)


def _error(code, message, status=400):
    return _json_response({"ok": False, "error": {"code": code, "message": message}}, status=status)


def _bearer_token():
    auth = request.httprequest.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip()


def _resolve_acting_member(env, uid):
    """Resolve + vet the workspace member a member-path ORM call impersonates.

    The only acting header is ``X-Tuqui-Acting-Uid`` — the stable per-member
    Odoo user id. The call runs under that member's own ACL via
    ``with_user(member)`` so companion behaves like the native per-user
    JSON-RPC path.

    Returns ``(member, denied_reason)``:

    - ``(<active internal user>, None)`` when the uid vets clean.
    - ``(env['res.users'], 'forbidden_acting_user')`` when the uid is the
      superuser, a share (portal/public) user, or doesn't resolve to an
      active user. We never widen privilege: an unparseable/unknown uid is a
      refusal, not a silent fallback to the connection identity.
    """
    empty = env["res.users"]
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return empty, "forbidden_acting_user"
    # SUPERUSER_ID would bypass every record rule — never impersonable.
    if uid_int == SUPERUSER_ID:
        return empty, "forbidden_acting_user"
    member = env["res.users"].sudo().search([("id", "=", uid_int), ("active", "=", True)], limit=1)
    if not member:
        return empty, "forbidden_acting_user"
    # share == True is a portal/public user — not an internal member; refuse
    # so the per-member path only ever runs as a real workspace member.
    if member.share:
        return empty, "forbidden_acting_user"
    return member, None


def _result_count(result, method: str) -> int:
    """Best-effort "records affected" count for the audit log.

    Sized results (lists, tuples, dicts, recordsets) → ``len()``. Scalar
    int results (``create`` returning a single id, ``copy``) → ``1`` so
    the audit shows a meaningful count instead of zero. ``search_count``
    is special-cased: the int it returns IS the count, so we surface it
    directly. ``None``/``False`` (typical for action methods that don't
    return data) → ``0``.
    """
    if result is None or result is False:
        return 0
    if isinstance(result, BaseModel):
        return len(result)
    if isinstance(result, (list, tuple, dict)):
        return len(result)
    if isinstance(result, int):
        if method == "search_count":
            return result
        return 1
    return 0


def _log(
    env,
    *,
    method,
    model_name,
    operation_type,
    acting_user,
    policy_allowed,
    policy_denied_reason,
    success,
    error_code,
    duration_ms,
    result_count,
):
    """Best-effort audit log write. Never fails the calling RPC."""
    try:
        env["tuqui.access.log"].sudo().log(
            method=method,
            model_name=model_name,
            operation_type=operation_type,
            acting_user_id=acting_user.id if acting_user else None,
            policy_allowed=policy_allowed,
            policy_denied_reason=policy_denied_reason,
            success=success,
            error_code=error_code,
            duration_ms=duration_ms,
            result_count=result_count,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("tuqui.access.log: failed to record access")


def _bind_logger(env, *, method, model_name, operation_type, acting_user):
    """Bind the audit-log context invariant across one request and return an
    ``emit(...)`` closure for the per-outcome fields. Pure convenience over
    ``_log`` — same arguments, same values, no behavior change."""

    def emit(
        *,
        policy_allowed,
        success,
        policy_denied_reason=None,
        error_code=None,
        duration_ms=0,
        result_count=0,
    ):
        _log(
            env,
            method=method,
            model_name=model_name,
            operation_type=operation_type,
            acting_user=acting_user,
            policy_allowed=policy_allowed,
            policy_denied_reason=policy_denied_reason,
            success=success,
            error_code=error_code,
            duration_ms=duration_ms,
            result_count=result_count,
        )

    return emit


# Policy-deny reasons that should surface as HTTP 403. Anything else
# from the gate (currently nothing) would surface as 400.
_POLICY_DENY_403 = frozenset({"method_blocked", "private_method_blocked", "connection_read_only"})


class TuquiRpc(http.Controller):
    """Generic ``execute_kw``-style gateway, behind OAuth + policy engine.

    Body shape::

        {
            "model":   "res.partner",
            "method":  "search_read",
            "args":    [[]],
            "kwargs":  {"fields": ["name"], "limit": 5},
            "context": {"lang": "es_AR"}
        }

    There is no hardcoded allowlist of operations — Odoo's own ACL (via the
    acting user) is the authorization model, so adding new capabilities to
    Tuqui doesn't require shipping a new module version to clients. Two
    request paths, picked by the presence of ``X-Tuqui-Acting-Uid``:

    * MEMBER PATH — request carries ``X-Tuqui-Acting-Uid`` (a workspace
      member's res.users id). The call runs through ``with_user(member)`` so
      Odoo's ACL is the per-call privilege check — identical to the native
      per-user path. The uid is vetted first: superuser, share/portal users
      and unknown/inactive ids are refused (``forbidden_acting_user``).

    * CONNECTION PATH — request has NO acting uid (workspace-level / system
      traffic). The call runs as SUPERUSER (``sudo()``). Because sudo bypasses
      record rules, this path is locked to reads UNCONDITIONALLY: any write /
      execute / private / blocked op is refused (``connection_read_only``).
      A stolen token can therefore only ever read on this path.

    Defense in depth, applied to both paths:

    1. OAuth ``client_credentials`` bearer (verified upstream).
    2. Absolute blocks on ``sudo`` / ``with_*`` / ``flush*`` /
       ``invalidate*`` and dunders — escape hatches that bypass ACL.
    3. Private (``_``-prefixed) methods are always refused.
    """

    @http.route(
        "/tuqui/rpc",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,
    )
    def rpc(self, **_kwargs):
        env = request.env

        # ─── Auth ──────────────────────────────────────────────────────────
        token = _bearer_token()
        if not token:
            return _error("unauthorized", "Missing bearer token", status=401)
        if not verify_access_token(env, token):
            return _error("unauthorized", "Invalid or expired token", status=401)

        # ─── Body ──────────────────────────────────────────────────────────
        try:
            body = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return _error("bad_request", "Body must be valid JSON", status=400)

        model_name = body.get("model")
        method = body.get("method")
        args = body.get("args") or []
        kwargs = body.get("kwargs") or {}
        context = body.get("context") or {}

        if not isinstance(model_name, str) or not model_name:
            return _error("bad_request", "Param 'model' is required", status=400)
        if not isinstance(method, str) or not method:
            return _error("bad_request", "Param 'method' is required", status=400)
        if not isinstance(args, list):
            return _error("bad_request", "Param 'args' must be a list", status=400)
        if not isinstance(kwargs, dict):
            return _error("bad_request", "Param 'kwargs' must be an object", status=400)
        if not isinstance(context, dict):
            return _error("bad_request", "Param 'context' must be an object", status=400)

        # The outer ``context`` field is applied first via with_context()
        # on the recordset, then ``_dispatch`` pops any context inside
        # kwargs and layers it on top — inner wins on key conflicts,
        # matching Odoo's own call_kw semantics.
        operation_type = _classify(method)

        # ─── Acting user ───────────────────────────────────────────────────
        # Presence of the acting-uid header picks the path:
        #   * MEMBER PATH (uid present) → resolve + vet the member, run with_user.
        #   * CONNECTION PATH (no uid)  → run as superuser, locked to reads.
        acting_uid = request.httprequest.headers.get("X-Tuqui-Acting-Uid") or ""
        is_connection = not acting_uid
        if is_connection:
            acting_user = None
        else:
            acting_user, acting_denied = _resolve_acting_member(env, acting_uid)
            if acting_denied:
                # Fires before the member is resolved → acting_user is None.
                _log(
                    env,
                    method=method,
                    model_name=model_name,
                    operation_type=operation_type,
                    acting_user=None,
                    policy_allowed=False,
                    policy_denied_reason=acting_denied,
                    success=False,
                    error_code=None,
                    duration_ms=0,
                    result_count=0,
                )
                return _error(
                    acting_denied,
                    f"Acting user refused (uid={acting_uid!r})",
                    status=403,
                )

        # acting_user is now settled for both paths (None on the connection
        # path); bind the audit context once and emit per-outcome below.
        emit = _bind_logger(
            env,
            method=method,
            model_name=model_name,
            operation_type=operation_type,
            acting_user=acting_user,
        )

        # ─── Policy gate ───────────────────────────────────────────────────
        allowed, denied_reason = _evaluate_policy(method, operation_type, is_connection=is_connection)
        if not allowed:
            emit(policy_allowed=False, policy_denied_reason=denied_reason, success=False)
            status = 403 if denied_reason in _POLICY_DENY_403 else 400
            return _error(denied_reason, f"Call blocked by policy: {denied_reason}", status=status)

        # ─── Dispatch ──────────────────────────────────────────────────────
        if model_name not in env:
            emit(policy_allowed=True, success=False, error_code="validation_error")
            return _error("validation_error", f"Unknown model: {model_name!r}", status=400)

        # Member path runs under the member's ACL; connection path runs as
        # SUPERUSER (already gated read-only above).
        if is_connection:
            recordset = env[model_name].sudo()
        else:
            recordset = env[model_name].with_user(acting_user)
        if context:
            recordset = recordset.with_context(**context)

        started = time.monotonic()
        try:
            result = _dispatch(recordset, method, args, kwargs)
        except AccessError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="access_denied", duration_ms=duration_ms)
            return _error("access_denied", str(exc), status=403)
        except (ValidationError, UserError) as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="validation_error", duration_ms=duration_ms)
            return _error("validation_error", str(exc), status=400)
        except Exception:  # noqa: BLE001
            # Server-side: full traceback. Client-side: generic copy.
            # Never leak SQL, paths, repr(exc), or anything from the
            # underlying call into the response body.
            _LOG.exception("tuqui.rpc: unhandled error in %s.%s", model_name, method)
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="internal_error", duration_ms=duration_ms)
            return _error("internal_error", "An internal error occurred.", status=500)

        duration_ms = int((time.monotonic() - started) * 1000)
        result_count = _result_count(result, method)
        emit(policy_allowed=True, success=True, duration_ms=duration_ms, result_count=result_count)
        return _ok(result)
