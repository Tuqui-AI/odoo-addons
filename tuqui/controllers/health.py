import json

from odoo import http, release
from odoo.http import Response

_PROTOCOL_VERSION = "2.0"

# Capabilities advertised by this module to a Tuqui backend during handshake.
# The 2.0 surface is a single generic execute_kw gateway behind a local
# policy engine — new capabilities are added on the Tuqui side by calling
# new ORM methods, without shipping a new module version.
#
# ``policy.read_only`` is deliberately NOT in this list: Tuqui reads it as the
# current STATE of the read-only switch, not as "this module implements the
# read-only policy". Advertising it unconditionally (as this module did up to
# 18.0.1.5.0) made every database look permanently read-only to Tuqui, which
# locked the write whitelist and the tool approvals in its settings even for
# customers who had turned the switch off here. It is appended per request in
# ``health()`` below, only while the switch is actually on.
_CAPABILITIES = [
    "rpc.execute_kw",
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
        caps = list(_CAPABILITIES)
        if env["tuqui.oauth.client"].sudo()._is_read_only():
            caps.append("policy.read_only")
        body = {
            "ok": True,
            "module": "tuqui",
            "module_version": _module_version(env),
            "protocol_version": _PROTOCOL_VERSION,
            "odoo_version": release.version,
            "capabilities": caps,
        }
        return Response(
            json.dumps(body),
            content_type="application/json",
            status=200,
        )
