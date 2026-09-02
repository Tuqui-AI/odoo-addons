import json
import logging
import time
from collections.abc import Mapping

import psycopg2
from odoo import SUPERUSER_ID, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Response, request
from odoo.models import BaseModel
from odoo.service.model import get_public_method
from odoo.tools.json import json_default as odoo_json_default

from .oauth import verify_access_token

_LOG = logging.getLogger(__name__)

# Cost guard for /tuqui/rpc. Every Tuqui caller — the Tuqui chat, MCP / Tuqui
# Connect, and the insights cron — runs its query SYNCHRONOUSLY inside a shared
# provider worker. Without this, a pathological domain could pin a worker for
# minutes and OOM the pod (#70305). Cap the per-request SQL runtime at the DB
# level, using the budget the client itself declares for this call
# (CompanionTransport always sends one — see _statement_timeout_ms).
_DEFAULT_STATEMENT_TIMEOUT_MS = 120_000


def _statement_timeout_ms(client_timeout_ms=None) -> int:
    """Return the per-request SQL timeout in ms (falls back to
    _DEFAULT_STATEMENT_TIMEOUT_MS if client_timeout_ms is missing/invalid)."""
    try:
        client_ms = int(client_timeout_ms)
    except (TypeError, ValueError):
        client_ms = 0
    return client_ms if client_ms > 0 else _DEFAULT_STATEMENT_TIMEOUT_MS


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

# These sets mirror the method names Tuqui posts to this gateway — both the
# typed surface of CompanionTransport (see
# tuqui_core/integrations/odoo/transports/companion.py) and the untyped call
# sites that go through its generic `execute()` / `execute_method()`. THE
# UNTYPED ONES ARE THE TRAP: the contract test used to mirror only the typed
# surface, which is exactly how `get_view` and `check_access_rights` slipped
# through. Keep all three in sync. Asymmetry to remember when a method is added:
#   * a READ not recognized here → ``execute`` → REFUSED on the two restricted
#     paths (read-only member, connection), and mislabelled in the audit log.
#     Refused is the part that bites: `get_view` degraded silently and
#     `check_access_rights` broke the embedded "create a record" flow outright.
#   * a WRITE not listed → also ``execute`` → still gated the same way, so only
#     its audit row gets mislabelled.
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
        # ── Metadata ──────────────────────────────────────────────────────
        # None of these read records. They answer "what does this model look
        # like / may this user do X", and Odoo scopes the view ones to the
        # acting user: `get_view` returns the arch pruned to their groups.
        #
        # The access helpers are a different animal and get their own gate
        # below (`_ONLY_AS_A_USER`): they answer ABOUT the caller, so as
        # superuser they answer about nobody.
        #
        # Falling into `execute` is not just a wrong audit label — it makes the
        # call REFUSED on the two paths that only allow reads (a read-only
        # companion, which is the default right after activation, and the
        # connection path). `get_view` was found that way: the search-view
        # field ranking degraded silently on every read-only companion.
        # `check_access_rights` was found the same way and fails loudly: the
        # embedded chat could not offer to create a record and told the user
        # their permissions could not be verified.
        "get_view",
        # `check_access_rights` is @api.deprecated in 19 — it stays here because
        # the deployed backend still calls it, but the caller should move to
        # `has_access`. Two traps in that migration:
        #   * Odoo's own deprecation message says "use check_access() instead",
        #     and that is bad advice for an RPC client: `check_access` is
        #     @api.private in 19, so `get_public_method` refuses it. Verified.
        #   * It is not a rename either. `has_access` is a RECORD method, so
        #     `_dispatch` pops args[0] as ids: it takes `[[], "read"]`, not
        #     `["read"]`. On an empty recordset it answers the model-level
        #     question — measured identical to `check_access_rights` for
        #     read/create on 18 and 19.
        "check_access_rights",
        "get_views",
        "has_access",
        "has_group",
        "has_groups",
        "get_metadata",
        "get_property_definition",
        # web_* are the web client's read entrypoints; they don't match the
        # search/read prefix because of that prefix of their own.
        "web_read",
        "web_search_read",
        "web_read_group",
    }
)
_READ_PREFIXES = ("search", "read")

# Reads that only mean something when there IS an acting user. They answer
# about `env.user`, and the connection path runs as SUPERUSER — where the
# answer is not just useless, it is WRONG in the dangerous direction:
#
#     def has_access(self, operation):
#         return self.env.su or not self._check_access(operation)
#
# As superuser that is an unconditional True, for any operation, `unlink`
# included. A caller would read "yes, allowed" and act on it. `has_group`
# is the same shape: `res.users` only allows the cross-user question when
# `env.su`, so on that path a token holder could enumerate anyone's groups.
#
# So they classify as `read` (which is the honest audit label) and are still
# refused on the connection path, with a reason of their own that says why.
_ONLY_AS_A_USER = frozenset({"check_access_rights", "has_access", "has_group", "has_groups"})


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


def _evaluate_policy(read_only: bool, method: str, op_type: str, *, is_connection: bool):
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
    4. Member path: when the connection is flagged ``read_only``, anything that
       can mutate is refused with ``read_only_mode``.
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
        # …and not even every read: the ones that answer about the caller
        # answer about nobody here. See `_ONLY_AS_A_USER`.
        if method in _ONLY_AS_A_USER:
            return False, "requires_acting_user"
        return True, None
    if read_only and op_type in ("write", "execute"):
        return False, "read_only_mode"
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
_POLICY_DENY_403 = frozenset(
    {
        "method_blocked",
        "private_method_blocked",
        "connection_read_only",
        "read_only_mode",
        "requires_acting_user",
    }
)


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
      traffic). The call runs as SUPERUSER (``with_user(SUPERUSER_ID)``).
      Because the superuser bypasses record rules, this path is locked to reads
      UNCONDITIONALLY: any write / execute / private / blocked op is refused
      (``connection_read_only``). A stolen token can therefore only ever read
      on this path.

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
    def rpc(self, **_kwargs):  # noqa: C901
        env = request.env

        # ─── Auth ──────────────────────────────────────────────────────────
        token = _bearer_token()
        if not token:
            return _error("unauthorized", "Missing bearer token", status=401)
        if not verify_access_token(env, token):
            return _error("unauthorized", "Invalid or expired token", status=401)

        # ─── Path detection (header only, no body needed) ─────────────────
        acting_uid = request.httprequest.headers.get("X-Tuqui-Acting-Uid") or ""
        is_connection = not acting_uid
        read_only = False if is_connection else env["tuqui.oauth.client"].sudo()._is_read_only()

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
        # Client-declared budget for THIS call (ms) — see _statement_timeout_ms.
        client_timeout_ms = body.get("client_timeout_ms")

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
        # acting_uid / is_connection already resolved above (path detection).
        # Presence of the acting-uid header picks the path:
        #   * MEMBER PATH (uid present) → resolve + vet the member, run with_user.
        #   * CONNECTION PATH (no uid)  → run as superuser, locked to reads.
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
        allowed, denied_reason = _evaluate_policy(read_only, method, operation_type, is_connection=is_connection)
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
        #
        # The acting identity must be adopted REQUEST-wide (update_env), not
        # just on the dispatched recordset (with_user). ir.http pins the
        # transaction's default_env to request.env on every web request, and
        # deferred computes run under THAT env at flush time. This route is
        # auth="none", so request.env has no user — any compute touching
        # self.env.user (e.g. enterprise's _compute_signing_user calls
        # env.user.has_group on every account.move) died with "Expected
        # singleton: res.users()", making invoice creation impossible via
        # companion (#70932). update_env swaps request.env AND re-points
        # default_env, so dispatch and flush both run as the acting user —
        # exactly like a native authenticated RPC.
        request.update_env(user=SUPERUSER_ID if is_connection else acting_user.id)
        env = request.env
        recordset = env[model_name]
        if context:
            recordset = recordset.with_context(**context)

        started = time.monotonic()
        timeout_ms = _statement_timeout_ms(client_timeout_ms)
        try:
            if timeout_ms:
                # SET LOCAL caps the SQL runtime for THIS request's transaction
                # only (reset on commit/rollback). Applied right before dispatch
                # so auth/policy queries above are unaffected. This is the guard
                # that stops a heavy query from pinning a worker into an OOM
                # (#70305) — for every caller: chat, MCP / Tuqui Connect, insights.
                env.cr.execute("SET LOCAL statement_timeout = %s", (timeout_ms,))
            result = _dispatch(recordset, method, args, kwargs)
            # Flush now, INSIDE the try. @api.constrains, stored computes and
            # SQL constraints otherwise run at commit — after this handler has
            # returned — where a failure bypasses every except below: Odoo
            # rolls back the whole request (audit row included) and answers
            # with its opaque HTML error page, so the caller never sees the
            # business message and cannot self-correct (#70932: creating an
            # account.move with the AR localization). Near-free for reads
            # (nothing pending to flush).
            env.cr.flush()
        except AccessError as exc:
            # Roll back before answering — every error branch below does. The
            # ORM may have already flushed rows before the failure; since we
            # swallow the exception and return a well-formed response, Odoo
            # would otherwise COMMIT the request and silently persist the very
            # change whose validation failed. The rollback also reopens an
            # aborted transaction so the audit INSERT below runs on a clean
            # cursor.
            env.cr.rollback()
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="access_denied", duration_ms=duration_ms)
            return _error("access_denied", str(exc), status=403)
        except (ValidationError, UserError) as exc:
            env.cr.rollback()
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="validation_error", duration_ms=duration_ms)
            return _error("validation_error", str(exc), status=400)
        except psycopg2.IntegrityError as exc:
            # A flushed INSERT/UPDATE hit a database constraint (NOT NULL,
            # CHECK, unique…). Native Odoo RPC maps these to a ValidationError,
            # so mirror that: recoverable validation_error/400 with the
            # constraint text — the caller fixes the payload and retries,
            # instead of giving up on an opaque fatal 500. The message only
            # describes the row the caller itself just tried to write.
            _LOG.info("tuqui.rpc: IntegrityError in %s.%s", model_name, method, exc_info=True)
            env.cr.rollback()
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="validation_error", duration_ms=duration_ms)
            return _error("validation_error", str(exc).strip(), status=400)
        except psycopg2.errors.QueryCanceled:
            # The query blew past the cost guard; Postgres aborted it. Roll back
            # so the audit-log INSERT below runs on a clean cursor, then free the
            # worker instead of letting the query pin it (#70305). The explicit
            # error also tells the caller how to self-correct.
            env.cr.rollback()
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="query_timeout", duration_ms=duration_ms)
            return _error(
                "query_timeout",
                f"The query exceeded the {timeout_ms // 1000}s limit and was cancelled. "
                "Narrow the domain (e.g. a date range), request fewer fields, "
                "or use an aggregate (read_group) instead of fetching every record.",
                status=400,
            )
        except ValueError as exc:  # noqa: BLE001
            # Odoo raises a plain ValueError when a query references a field it
            # can't push down to SQL — typically ordering/grouping/filtering by
            # a non-stored computed field (sales_count, qty_available, …). That
            # is a recoverable caller mistake, not a server fault, so return
            # validation_error/400: the client treats it as correctable and can
            # pivot (aggregate with read_group, use a stored field) instead of
            # giving up. Odoo's message is just the model/field name and reason
            # — no SQL, paths or data — so it is safe to relay.
            #
            # Log with the traceback: unlike UserError (routine business
            # validation, message already relayed), a ValueError out of the ORM
            # is diagnostic — WHERE it was raised is the whole story (finding
            # the "Expected singleton" of #70932 required exactly this).
            _LOG.info("tuqui.rpc: ValueError in %s.%s", model_name, method, exc_info=True)
            env.cr.rollback()
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="validation_error", duration_ms=duration_ms)
            # The read_group pivot hint only fits the SQL-pushdown flavor of
            # ValueError; gluing it onto unrelated ones ("Expected singleton:
            # …") sends the caller chasing a field problem that doesn't exist.
            message = str(exc)
            if "to SQL" in message or "not stored" in message:
                message += (
                    ". This field is likely computed/non-stored and cannot be "
                    "used to sort, group or filter at the database level — use "
                    "a stored field or aggregate the underlying model with read_group."
                )
            return _error("validation_error", message, status=400)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("tuqui.rpc: unhandled error in %s.%s", model_name, method)
            env.cr.rollback()
            duration_ms = int((time.monotonic() - started) * 1000)
            emit(policy_allowed=True, success=False, error_code="internal_error", duration_ms=duration_ms)
            return _error("internal_error", str(exc), status=500)

        duration_ms = int((time.monotonic() - started) * 1000)
        result_count = _result_count(result, method)
        emit(policy_allowed=True, success=True, duration_ms=duration_ms, result_count=result_count)
        return _ok(result)
