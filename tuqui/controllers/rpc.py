import json
import logging

from odoo import http
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


def _error(code, message, status=400):
    return _json_response({"ok": False, "error": {"code": code, "message": message}}, status=status)


def _bearer_token():
    auth = request.httprequest.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip()


def _resolve_acting_user(env, login):
    """Look up by ``res.users.login`` (NOT email — duplicates exist in some instances).

    Returns the user record or empty recordset.
    """
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


class TuquiRpc(http.Controller):
    """Generic RPC bridge: read-style operations against the Odoo ORM."""

    @http.route(
        "/tuqui/rpc",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
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

        # NOTE: full RPC dispatch is implemented in a follow-up commit.
        # This first cut validates auth + acting_user + operation perimeter
        # so the integration handshake can be exercised end-to-end while the
        # actual ORM call shapes are built incrementally.
        _log_access(env, operation, acting_user, params.get("model"), 0, True, "not_implemented")
        return _error(
            "not_implemented",
            f"Operation '{operation}' wiring is pending — auth perimeter validated.",
            status=501,
        )
