from odoo import _, api, fields, models

# Settings shows the connection state read-only; transitions happen through the
# actions below. Reuse the canonical selection from the client model so the two
# never drift when a state is added.
from .tuqui_oauth_client import _STATE_SELECTION


class ResConfigSettings(models.TransientModel):
    """Tuqui block under Settings > General Settings > Integrations.

    The whole admin surface of the module lives here: connection state +
    activation, security policy, and the entry point to the access log.
    There are no Tuqui menus — the module is connection infrastructure,
    not an app the user navigates.
    """

    _inherit = "res.config.settings"

    # Read-only display values derived from the tuqui.oauth.client singleton.
    # Populated in get_values() (not via compute): res.config.settings loads the
    # form through an onchange, which only recomputes computed fields whose
    # @api.depends are triggered. These depend on another model, with no field
    # path on this record to depend on, so a no-dependency compute never fires on
    # load and the fields render blank. get_values() is the lifecycle hook
    # res.config.settings guarantees to run on every form load.
    tuqui_state = fields.Selection(
        _STATE_SELECTION,
        string="Tuqui Connection State",
    )
    tuqui_last_seen_at = fields.Datetime(
        string="Tuqui Last Activity",
    )
    tuqui_access_count_7d = fields.Integer(
        string="Tuqui Accesses (7 days)",
    )
    tuqui_access_summary = fields.Char(
        string="Tuqui Activity Summary",
    )
    tuqui_read_only = fields.Boolean(
        string="Tuqui Read-only Mode",
        help=(
            "When enabled, Tuqui can read but never create, update, delete "
            "or run methods on this database. Reads still follow each user's "
            "own Odoo permissions. Private methods and ORM escape hatches are "
            "always blocked regardless of this flag. Note: read-only is "
            "enforced by classifying the method name (not at the cursor "
            "level), so a mutating method whose name starts with read/search "
            "could be classified as a read."
        ),
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        count = client.access_count_7d if client else 0
        # Pre-rendered so the view shows a clean sentence instead of an inline
        # <field> that breaks the line mid-phrase. Pluralized by hand — Odoo's
        # backend i18n has no plural form helper.
        summary = _("%s access in the last 7 days", count) if count == 1 else _("%s accesses in the last 7 days", count)
        res.update(
            tuqui_state=client.state if client else "pending",
            tuqui_last_seen_at=client.last_seen_at if client else False,
            tuqui_access_count_7d=count,
            tuqui_access_summary=summary,
            tuqui_read_only=bool(client.read_only) if client else False,
        )
        return res

    def set_values(self):
        super().set_values()
        # read_only lives on the OAuth singleton, which only exists once the
        # database is activated — there's nothing to gate before then, so a
        # missing singleton just means the toggle has no effect yet.
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        if client and client.read_only != self.tuqui_read_only:
            client.write({"read_only": self.tuqui_read_only})

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

    def action_tuqui_disconnect(self):
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        client.action_disconnect()
        # Reload so the block re-renders in its disconnected shape.
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_tuqui_open_access_log(self):
        return self.env["ir.actions.act_window"]._for_xml_id("tuqui.action_tuqui_access_log")
