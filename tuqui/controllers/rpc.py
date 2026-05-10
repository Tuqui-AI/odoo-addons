import json
import logging

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Response, request

from .oauth import verify_access_token


_LOG = logging.getLogger(__name__)

# Hardcoded perimeter — operations the module is willing to execute on
# behalf of Tuqui. Model-level allowlist lives in Tuqui (already enforced
# server-side). Any operation outside this set is rejected here as a
# defense-in-depth measure (e.g. should Tuqui be compromised).
_ALLOWED_OPERATIONS = frozenset(
    {
        "search_read",
        "read_group",
        "name_search",
        "fields_get",
        "model_list",
        "read",
        "name_get",
    }
)


def _json_response(body, status=200):
    return Response(
        json.dumps(body, default=str),
        content_type="application/json",
        status=status,
    )


def _ok(data, record_count=None):
    body = {"ok": True, "data": data}
    if record_count is not None:
        body["record_count"] = record_count
    return _json_response(body, status=200)


def _error(code, message, status=400):
    return _json_response({"ok": False, "error": {"code": code, "message": message}}, status=status)


def _bearer_token():
    auth = request.httprequest.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip()


def _resolve_acting_user(env, login):
    """Look up by ``res.users.login`` (NOT email — duplicates exist in some instances)."""
    if not login:
        return env["res.users"]
    return env["res.users"].sudo().search([("login", "=", login), ("active", "=", True)], limit=1)


def _log_access(env, operation, acting_user, model_name, record_count, success, error_code=None):
    try:
        env["tuqui.access.log"].sudo().log(
            operation=operation,
            acting_user_id=acting_user.id if acting_user else None,
            model_name=model_name,
            record_count=record_count,
            success=success,
            error_code=error_code,
        )
    except Exception:  # noqa: BLE001
        # Logging is best-effort — never fail an RPC call because we couldn't log it.
        _LOG.exception("tuqui.access.log: failed to record access")


def _require_model(env, model_name):
    """Return the recordset for ``model_name`` or raise ValidationError."""
    if not model_name or not isinstance(model_name, str):
        raise ValidationError("Param 'model' is required")
    if model_name not in env:
        raise ValidationError(f"Unknown model: {model_name!r}")
    return env[model_name]


# ---------- Per-operation dispatch ----------
#
# Each handler receives (env_user, params) where env_user is request.env modified
# with `with_user(acting_user)` so the call respects the acting user's groups.
# Handlers return (data, record_count). Exceptions propagate to the wrapper.


def _op_search_read(env_user, params):
    model_name = params.get("model")
    model = _require_model(env_user, model_name)
    domain = params.get("domain") or []
    fields = params.get("fields") or []
    limit = params.get("limit")
    offset = params.get("offset") or 0
    order = params.get("order")
    rows = model.search_read(domain, fields, offset=offset, limit=limit, order=order)
    return rows, len(rows)


def _op_read(env_user, params):
    model = _require_model(env_user, params.get("model"))
    ids = params.get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise ValidationError("Param 'ids' must be a list of integers")
    fields = params.get("fields") or None
    rows = model.browse(ids).read(fields) if fields else model.browse(ids).read()
    return rows, len(rows)


def _op_fields_get(env_user, params):
    model = _require_model(env_user, params.get("model"))
    allfields = params.get("allfields") or None
    attributes = params.get("attributes") or None
    data = model.fields_get(allfields=allfields, attributes=attributes)
    return data, len(data)


def _op_name_search(env_user, params):
    model = _require_model(env_user, params.get("model"))
    name = params.get("name") or ""
    # Accept legacy 'args' alias for the search domain (Odoo 18 → 19 renamed
    # the parameter to 'domain'). Tuqui clients may send either.
    domain = params.get("domain")
    if domain is None:
        domain = params.get("args")
    operator = params.get("operator") or "ilike"
    limit = params.get("limit") or 100
    rows = model.name_search(name=name, domain=domain, operator=operator, limit=limit)
    # name_search returns list of (id, display_name) tuples → JSON serialises tuples as lists.
    return rows, len(rows)


def _op_read_group(env_user, params):
    model = _require_model(env_user, params.get("model"))
    domain = params.get("domain") or []
    fields = params.get("fields") or []
    groupby = params.get("groupby") or []
    offset = params.get("offset") or 0
    limit = params.get("limit")
    orderby = params.get("orderby") or False
    lazy = params.get("lazy")
    if lazy is None:
        lazy = True
    rows = model.read_group(
        domain=domain,
        fields=fields,
        groupby=groupby,
        offset=offset,
        limit=limit,
        orderby=orderby,
        lazy=lazy,
    )
    return rows, len(rows)


def _op_name_get(env_user, params):
    """Compatibility shim: ``name_get`` was removed in Odoo 17+ in favour of ``display_name``.

    We replicate the legacy return shape (``[[id, display_name], ...]``) so
    Tuqui clients that still call it keep working until they migrate.
    """
    model = _require_model(env_user, params.get("model"))
    ids = params.get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise ValidationError("Param 'ids' must be a list of integers")
    records = model.browse(ids)
    rows = [[rec.id, rec.display_name] for rec in records]
    return rows, len(rows)


def _op_model_list(env_user, _params):
    """List models the acting user has read access to (via ``ir.model.access``).

    Returns ``[{model, name, transient}, ...]``. Uses the public
    ``_get_allowed_models('read')`` API so we don't reach into ACL internals.
    """
    allowed = env_user["ir.model.access"]._get_allowed_models("read")
    if not allowed:
        return [], 0
    ir_model = env_user["ir.model"].sudo().search([("model", "in", list(allowed))])
    rows = [
        {"model": rec.model, "name": rec.name, "transient": rec.transient}
        for rec in ir_model
    ]
    rows.sort(key=lambda r: r["model"])
    return rows, len(rows)


_DISPATCH = {
    "search_read": _op_search_read,
    "read": _op_read,
    "fields_get": _op_fields_get,
    "name_search": _op_name_search,
    "read_group": _op_read_group,
    "name_get": _op_name_get,
    "model_list": _op_model_list,
}


class TuquiRpc(http.Controller):
    """Generic RPC bridge: read-style operations against the Odoo ORM."""

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
        token = _bearer_token()
        if not token:
            return _error("unauthorized", "Missing bearer token", status=401)
        payload = verify_access_token(env, token)
        if not payload:
            return _error("unauthorized", "Invalid or expired token", status=401)

        try:
            body = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return _error("bad_request", "Body must be valid JSON", status=400)

        operation = body.get("operation")
        params = body.get("params") or {}
        if not operation or not isinstance(operation, str):
            return _error("bad_request", "Missing 'operation'", status=400)
        if operation not in _ALLOWED_OPERATIONS:
            _log_access(env, operation, None, params.get("model"), 0, False, "operation_not_allowed")
            return _error("operation_not_allowed", f"Operation '{operation}' is not exposed", status=403)

        acting_login = request.httprequest.headers.get("X-Tuqui-Acting-User") or ""
        acting_user = _resolve_acting_user(env, acting_login)
        if not acting_user:
            _log_access(env, operation, None, params.get("model"), 0, False, "unknown_acting_user")
            return _error(
                "unknown_acting_user",
                f"Unknown acting_user login: {acting_login!r}",
                status=400,
            )

        handler = _DISPATCH.get(operation)
        if handler is None:
            # Defensive — _ALLOWED_OPERATIONS and _DISPATCH must stay in sync.
            _log_access(env, operation, acting_user, params.get("model"), 0, False, "not_implemented")
            return _error("not_implemented", f"Operation '{operation}' has no handler", status=501)

        env_user = env(user=acting_user.id)
        model_name = params.get("model")
        try:
            data, record_count = handler(env_user, params)
        except AccessError as exc:
            _log_access(env, operation, acting_user, model_name, 0, False, "access_denied")
            return _error("access_denied", str(exc), status=403)
        except (ValidationError, UserError) as exc:
            _log_access(env, operation, acting_user, model_name, 0, False, "validation_error")
            return _error("validation_error", str(exc), status=400)
        except Exception:  # noqa: BLE001
            # Log the full traceback server-side; never leak exception
            # details (str/repr/traceback) to the client — they may carry
            # SQL fragments, internal paths, or user data.
            _LOG.exception("tuqui.rpc: unhandled error in operation %s", operation)
            _log_access(env, operation, acting_user, model_name, 0, False, "internal_error")
            return _error("internal_error", "An internal error occurred.", status=500)

        _log_access(env, operation, acting_user, model_name, record_count, True)
        return _ok(data, record_count=record_count)
