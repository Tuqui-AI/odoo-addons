import json

from odoo import http, release
from odoo.http import Response


_PROTOCOL_VERSION = "1.0"

# Capabilities advertised by this module to a Tuqui backend during handshake.
# Read-style operations only; new capabilities (rag_search, etc.) are added
# through dedicated semantic endpoints, not by widening the generic RPC.
_CAPABILITIES = [
    "rpc.search_read",
    "rpc.read_group",
    "rpc.name_search",
    "rpc.fields_get",
    "rpc.model_list",
    "rpc.read",
    "rpc.name_get",
]


def _module_version(env):
    rec = env["ir.module.module"].sudo().search([("name", "=", "tuqui")], limit=1)
    return rec.installed_version or rec.latest_version or "unknown"


class TuquiHealth(http.Controller):
    """Health probe + capability advertisement.

    Public endpoint (auth='none'). Returns enough metadata for a Tuqui
    backend to decide whether the module is reachable and what protocol
    version / capability set it speaks.
    """

    @http.route("/tuqui/health", type="http", auth="none", methods=["GET"], csrf=False)
    def health(self, **_kwargs):
        env = http.request.env
        body = {
            "ok": True,
            "module": "tuqui",
            "module_version": _module_version(env),
            "protocol_version": _PROTOCOL_VERSION,
            "odoo_version": release.version,
            "capabilities": _CAPABILITIES,
            "db_name": http.request.db,
        }
        return Response(
            json.dumps(body),
            content_type="application/json",
            status=200,
        )
