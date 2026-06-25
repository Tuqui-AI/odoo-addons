from odoo import _, fields, models

# Settings shows the connection state read-only; transitions happen through the
# actions below. Reuse the canonical selection from the client model so the two
# never drift when a state is added.
from .tuqui_oauth_client import _STATE_SELECTION


class ResConfigSettings(models.TransientModel):
    """Tuqui block under Settings > General Settings > Integrations.

    The whole admin surface of the module lives here: connection state +
    activation, security policy, and the entry points to the rules table
    and the access log. There are no Tuqui menus — the module is
    connection infrastructure, not an app the user navigates.
    """

    _inherit = "res.config.settings"

    tuqui_state = fields.Selection(
        _STATE_SELECTION,
        compute="_compute_tuqui_status",
        string="Tuqui Connection State",
    )
    tuqui_last_seen_at = fields.Datetime(
        compute="_compute_tuqui_status",
        string="Tuqui Last Activity",
    )
    tuqui_access_count_7d = fields.Integer(
        compute="_compute_tuqui_status",
        string="Tuqui Accesses (7 days)",
    )
    tuqui_access_summary = fields.Char(
        compute="_compute_tuqui_status",
        string="Tuqui Activity Summary",
    )

    def _compute_tuqui_status(self):
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        count = client.access_count_7d if client else 0
        # Pre-rendered so the view shows a clean sentence instead of an inline
        # <field> that breaks the line mid-phrase. Pluralized by hand — Odoo's
        # backend i18n has no plural form helper.
        summary = _("%s access in the last 7 days", count) if count == 1 else _("%s accesses in the last 7 days", count)
        for rec in self:
            rec.tuqui_state = client.state if client else "pending"
            rec.tuqui_last_seen_at = client.last_seen_at if client else False
            rec.tuqui_access_count_7d = count
            rec.tuqui_access_summary = summary

    # ---------- Actions (thin proxies to the singletons) ----------

    def action_tuqui_activate(self):
        """Navigate to the activation route in the same tab — it mints the
        nonce + secret, captures the Settings page URL from the Referer
        header, and redirects on to the Tuqui frontend with a return_url so
        the browser lands back here once the handshake completes. Works from
        both 'pending' and 'disconnected' (the route itself rejects 'active').

        Same tab so Tuqui can redirect the admin back to this exact Settings
        page after activation and the connected state is visible without a
        manual reload."""
        return {
            "type": "ir.actions.act_url",
            "url": "/tuqui/activation/start",
            "target": "self",
        }

    def action_tuqui_open_workspace(self):
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        return client.action_open_tuqui()

    def action_tuqui_rotate_secret(self):
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        return client.action_rotate_secret()

    def action_tuqui_disconnect(self):
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        client.action_disconnect()
        # Reload so the block re-renders in its disconnected shape.
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_tuqui_open_access_log(self):
        return self.env["ir.actions.act_window"]._for_xml_id("tuqui.action_tuqui_access_log")
