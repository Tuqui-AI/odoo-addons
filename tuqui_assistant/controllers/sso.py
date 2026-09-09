"""SSO nonce exchange for the embedded Tuqui SPA (ADR 0001 / spec §2.2).

Mirrors ``tuqui/controllers/activation.py``: the exchange endpoint is
authenticated by the single-use nonce itself (auth='none'), not by a session
or a secret — the module never holds the OAuth client secret in plaintext.
Tuqui (server-side) calls this after the SPA forwards the nonce, gets back the
``odoo_uid``, and maps it to a workspace member to mint a session token.
"""

import json
import logging

from odoo import http
from odoo.http import Response, request

_LOG = logging.getLogger(__name__)


class TuquiAssistantSso(http.Controller):
    @http.route(
        "/tuqui_assistant/sso/exchange",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,
    )
    def exchange(self, **_kwargs):
        """Redeem a single-use SSO nonce for the bound ``odoo_uid``.

        Auth is the nonce itself (short TTL, single-use). Response::

            { "odoo_uid": 42, "client_id": "..." }
        """
        try:
            body = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return _json_error("bad_request", "Body must be valid JSON", status=400)

        nonce = (body.get("nonce") or "").strip()
        if not nonce:
            return _json_error("bad_request", "Missing 'nonce'", status=400)

        result = request.env["tuqui.assistant.sso.nonce"].sudo().redeem(nonce)
        if result is None:
            # Unknown / consumed / expired — single response, no distinction leaked.
            return _json_error("invalid_nonce", "Unknown, consumed or expired nonce", status=410)

        _LOG.info("tuqui_assistant.sso.exchange: nonce redeemed for uid=%s", result["odoo_uid"])
        return Response(json.dumps(result), content_type="application/json", status=200)


def _json_error(code: str, message: str, *, status: int) -> Response:
    return Response(
        json.dumps({"error": {"code": code, "message": message}}),
        content_type="application/json",
        status=status,
    )
