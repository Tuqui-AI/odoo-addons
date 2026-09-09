import difflib
import json
import logging
import re
import time
from collections import Counter
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

# These sets mirror the typed method names Tuqui's CompanionTransport posts to
# this gateway — see tuqui_core/integrations/odoo/transports/companion.py and
# the contract test ``test_classify_covers_companion_transport_surface``. Keep
# all three in sync. Asymmetry to remember when the transport gains a method:
#   * a READ it sends but not recognized here → ``execute`` → wrongly refused on
#     a read_only connection (that was the formatted_read_group bug).
#   * a WRITE not listed → also ``execute`` → still blocked under read_only
#     (safe); only its audit row gets mislabelled.
# This classifier is the coarse read_only edge gate + audit label, NOT the
# authorization boundary: writes are really gated by the backend whitelist
# (workspace_write_models) and the acting user's Odoo ACL.
_WRITE_METHODS = frozenset({"create", "write", "unlink", "copy", "name_create"})
# Reads that don't begin with the search/read prefix must be listed explicitly
# (e.g. ``formatted_read_group``, the Odoo 19 grouped read).
_READ_METHODS = frozenset(
    {
        "name_search",
        "fields_get",
        "default_get",
        "formatted_read_group",
        # View metadata. It reads no records — Odoo's own `get_view` starts by
        # checking read access on the model and returns the arch already pruned
        # to the acting user's groups. Left out of this set it classified as
        # `execute`, which is not just a wrong audit label: it made the call
        # refused on both restricted paths, so the search-view field ranking
        # silently degraded on every read-only companion (the default right
        # after activation) and on the connection path.
        "get_view",
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
       can mutate is refused with ``connection_read_only`` regardless of the
       ``read_only`` flag. Keeps the blast radius of a stolen token to
       read-only even on workspace-level/system traffic.
    4. Member path: when the connection is flagged ``read_only``, anything that
       can mutate (``write`` / ``execute``) is refused; reads pass.
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
    # Model-level methods (``@api.model`` / ``@api.model_create_multi``) take no
    # leading ids arg; record methods do. Odoo tags the former with
    # ``method._api in ('model', 'model_create')`` — the same marker
    # odoo.service.model.call_kw branches on.
    if getattr(method, "_api", None) in ("model", "model_create"):
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


# ─── Model name suggestions ──────────────────────────────────────────
# A caller that guesses a model name gets nothing back today, so it guesses
# again: prod logged one session trying `adhoc.pull.request`, `pull.request`
# and `adhoc.pull` within four seconds, for a model actually named
# `saas.pull.request`. The registry is right here on the failure path, so
# answer the question the caller is really asking — what is it called?
#
# Every threshold below was set against the real corpus — the 60-odd names
# production asked for and did not get, scored over a real registry. See
# ``test_suggests_the_model_production_actually_wanted``: it is the reason
# these are the numbers they are, and the thing to re-run before touching one.

_MAX_MODEL_SUGGESTIONS = 3
# Stricter than difflib's usual 0.5, because this pool only has to catch the
# typos that share no whole word (`saleorder` → `sale.order`, ratio 0.95). The
# guesses that ARE far apart textually — `adhoc.pull` scores 0.44 against
# `saas.pull.request` — come in through the word pool instead. Below 0.65 the
# corpus starts pairing unrelated names of similar shape (`ir.property` →
# `kpi.provider`, ratio 0.61); above it nothing real is lost.
_MODEL_MATCH_CUTOFF = 0.7
_MODEL_FUZZY_POOL = 12
# How close to the best candidate a second or third name has to be to be worth
# saying at all. Two axes because they answer different objections: segment
# score keeps the module prefix from dragging unrelated models in, ratio keeps
# a sibling of the right model from riding on the same shared segment.
_MODEL_KEEP_SEGMENTS = 0.5
_MODEL_KEEP_RATIO = 0.8
# Absolute floor, on top of the relative gate: a candidate has to be either
# textually close or share a word that means something. Without it, sharing a
# crowded prefix is enough to get named — `ir.property` came back as "did you
# mean ir.cron", which only shares `ir` with 18 other models. Silence is a
# better answer than a model that does something else.
_MODEL_MIN_RATIO = 0.6
# In units of 1/occurrences, so it only means anything when ``known`` is the
# COMPLETE registry. Scoring the corpus over a hand-trimmed registry inflates
# every weight — roughly 5x for a fifth of the models — and quietly lifts
# candidates over this line: the same corpus read 54/61 trimmed and 49/61
# whole, and the extra "passes" were confident wrong answers, not silence.
# Production always passes the whole registry; the test fixture must too.
_MODEL_MIN_SEGMENTS = 0.25
# Hard ceiling on the name we are willing to score. Both difflib passes are
# linear in the length of the name, and a long name made of many real words
# makes EVERY model a word-sharing candidate, so cost grows faster than
# linearly: measured over the 1368-model registry, 12 chars costs 7.9 ms, 4 KB
# costs 3.4 s and 29 KB does not finish inside 30 s. `model` is only validated
# as a non-empty string, so without this a handful of POSTs with a valid token
# pins every worker — and the statement_timeout guard cannot help, since this
# is pure Python and runs before the cursor budget is set. Twice the longest
# name in a real registry (56 chars) is all the headroom this needs.
_MAX_SUGGESTIBLE_NAME = 128


def _segments(name):
    """Split a model name into its words, on dots AND underscores.

    Odoo mixes both separators inside one name — `saas.database.custom_domain`,
    `helpdesk.ticket.customer_note`, `saas.odoo.major_version` — and the caller
    that guesses one usually writes the other. Splitting on dots alone made
    `saas.database.custom.domain` share three words with the model it wanted
    and lose to `saas.database.egress.domain`, which shared the same three.
    """
    return set(re.split(r"[._]", name))


def _last_word(name):
    """The final word of a model name — the one that says what it is."""
    return re.split(r"[._]", name)[-1]


def _segment_weights(known):
    """Map each word to how much matching it says about intent.

    Rarity is the whole point. Guessing `adhoc.pull` shares `adhoc` with every
    model of the module and `pull` with almost nothing, so a plain count of
    shared words ranks `adhoc.product` above `saas.pull.request` — the common
    prefix outvoting the one word that carries the meaning.
    """
    counts = Counter(segment for name in known for segment in _segments(name))
    return {segment: 1.0 / count for segment, count in counts.items()}


def _rank_model_candidates(bad_name, known):
    """Model names closest to ``bad_name``, most likely intent first.

    Two pools, because neither alone covers the guesses prod actually makes:
    names sharing a dotted segment (`adhoc.pull` → `saas.pull.request`, too far
    apart textually for difflib) and difflib's own matches (`crm.leed` →
    `crm.lead`, a typo that shares no whole segment).

    Only candidates comparable to the best one survive. Filling the list up to
    the cap makes the message actively worse: `adhoc.pull.request` scores 2.00
    against `saas.pull.request` and 0.25 against `adhoc.module`, which shares
    nothing but the module prefix — offering it invites the caller to try a
    model that has no relation to what it asked for. Real ambiguity does
    survive: `account.moves` still returns `account.move` AND
    `account.move.line`.
    """
    wanted = _segments(bad_name)
    weights = _segment_weights(known)
    head_word = _last_word(bad_name)
    pool = {name for name in known if wanted & _segments(name)}
    pool.update(difflib.get_close_matches(bad_name, known, n=_MODEL_FUZZY_POOL, cutoff=_MODEL_MATCH_CUTOFF))
    pool.discard(bad_name)

    scored = sorted(
        (
            (
                sum(weights[segment] for segment in wanted & _segments(name)),
                # Tie-break only. The last word names the thing and the ones
                # before it qualify it, so between two candidates that share
                # the SAME words, the one that IS that thing wins:
                # `stock.orderpoint` shares both its words with
                # `stock.orderpoint.snooze` (a wizard about orderpoints) and
                # with `stock.warehouse.orderpoint` (the orderpoint itself).
                # Folded into the score instead, it was a wash — a generic
                # ending like `request` or `client` then outvoted a rare shared
                # word and cost as many cases as it won.
                _last_word(name) == head_word,
                difflib.SequenceMatcher(None, bad_name, name).ratio(),
                name,
            )
            for name in pool
        ),
        # Ties broken by name so the message is reproducible: the pool is a
        # set, and a test asserting on the second suggestion would flake.
        key=lambda candidate: (-candidate[0], not candidate[1], -candidate[2], candidate[3]),
    )
    scored = [(segments, ratio, name) for segments, _head, ratio, name in scored]
    scored = [
        candidate for candidate in scored if candidate[1] >= _MODEL_MIN_RATIO or candidate[0] >= _MODEL_MIN_SEGMENTS
    ]
    if not scored:
        return []
    best_segments, best_ratio, _ = scored[0]
    return [
        name
        for segments, ratio, name in scored
        if segments >= _MODEL_KEEP_SEGMENTS * best_segments and ratio >= _MODEL_KEEP_RATIO * best_ratio
    ]


def _suggest_models(env, bad_name, acting_user):
    """Existing model names close to ``bad_name`` that the caller may read.

    A name longer than ``_MAX_SUGGESTIBLE_NAME`` is refused outright, before
    any scoring: that is the cost guard, and no real model name comes close.

    Abstract models are dropped (`mail.thread` helps nobody) and the rest are
    filtered by the acting user's model-level ACL, so the suggestion never
    sends the caller at something it will only get a 403 from. The connection
    path runs as superuser and needs no filtering.

    Args:
        env: Request environment, before the acting identity is adopted.
        bad_name: The model name the caller asked for and that does not exist.
        acting_user: The vetted member, or None on the connection path.

    Returns:
        Up to ``_MAX_MODEL_SUGGESTIONS`` model names, best guess first. Empty
        when nothing is close enough, or when the name is absurdly long.
    """
    if len(bad_name) > _MAX_SUGGESTIBLE_NAME:
        return []
    access = env["ir.model.access"]
    if acting_user is not None:
        access = access.with_user(acting_user)
    suggestions = []
    for name in _rank_model_candidates(bad_name, list(env.registry)):
        if env[name]._abstract:
            continue
        if acting_user is not None and not access.check(name, "read", raise_exception=False):
            continue
        suggestions.append(name)
        if len(suggestions) == _MAX_MODEL_SUGGESTIONS:
            break
    return suggestions


def _unknown_model_message(env, bad_name, acting_user):
    """The 400 body for a model that does not exist, with a way forward.

    Best-effort: any failure while ranking falls back to the bare message
    rather than turning a recoverable 400 into a 500.
    """
    try:
        suggestions = _suggest_models(env, bad_name, acting_user)
    except Exception:  # noqa: BLE001 — a suggestion is never worth an error
        # Roll back like every other error branch on this route. The ACL check
        # runs SQL, and a failed statement leaves the cursor aborted: we would
        # answer a well-formed 400 and then have Odoo's own commit raise, so
        # the caller gets the opaque 500 HTML page instead — the exact failure
        # mode #70932 was about. Callers of this helper must not have written
        # their audit row yet, or the rollback would discard it.
        env.cr.rollback()
        _LOG.warning("tuqui.rpc: could not build model suggestions for %r", bad_name, exc_info=True)
        suggestions = []
    if suggestions:
        return f"Unknown model: {bad_name!r}. Did you mean: {', '.join(repr(name) for name in suggestions)}?"
    return (
        f"Unknown model: {bad_name!r}. No installed model has a similar name — "
        "search 'ir.model' on its 'model' field to find the right one."
    )


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
_POLICY_DENY_403 = frozenset({"method_blocked", "private_method_blocked", "read_only_mode", "connection_read_only"})


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
      Writes here stay governed by the connection's ``read_only`` flag.

    * CONNECTION PATH — request has NO acting uid (workspace-level / system
      traffic). The call runs as SUPERUSER (``sudo()``). Because sudo bypasses
      record rules, this path is locked to reads UNCONDITIONALLY: any write /
      execute / private / blocked op is refused (``connection_read_only``),
      independent of the ``read_only`` flag. A stolen token can therefore only
      ever read on this path.

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
        read_only = env["tuqui.oauth.client"].sudo()._is_read_only()
        allowed, denied_reason = _evaluate_policy(read_only, method, operation_type, is_connection=is_connection)
        if not allowed:
            emit(policy_allowed=False, policy_denied_reason=denied_reason, success=False)
            status = 403 if denied_reason in _POLICY_DENY_403 else 400
            return _error(denied_reason, f"Call blocked by policy: {denied_reason}", status=status)

        # ─── Dispatch ──────────────────────────────────────────────────────
        if model_name not in env:
            # Build the message BEFORE the audit row: on failure it rolls the
            # cursor back, which would otherwise take the row with it.
            message = _unknown_model_message(env, model_name, acting_user)
            emit(policy_allowed=True, success=False, error_code="validation_error")
            return _error("validation_error", message, status=400)

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
