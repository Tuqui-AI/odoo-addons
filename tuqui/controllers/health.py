import json

from odoo import http, release
from odoo.http import Response


_PROTOCOL_VERSION = "2.0"

# Capabilities advertised by this module to a Tuqui backend during handshake.
# The 2.0 surface is a single generic execute_kw gateway behind a local
# policy engine — new capabilities are added on the Tuqui side by calling
# new ORM methods, without shipping a new module version.
_CAPABILITIES = [
    "rpc.execute_kw",
    "policy.default",
    "policy.advanced",
    "policy.private_exact_allow",
    "access_log",
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
