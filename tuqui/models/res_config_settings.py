from odoo import _, api, fields, models

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
    tuqui_policy_mode = fields.Selection(
        [("default", "Default"), ("advanced", "Advanced")],
        string="Tuqui Security Policy",
        default="default",
        help=(
            "Default: private (_-prefixed) methods always blocked, plus "
            "hardcoded absolute blocks (sudo, with_*, flush*, invalidate*, "
            "dunders); everything else goes through the acting user's own "
            "Odoo access rights. Advanced: an allow/deny rules table is "
            "consulted on every call — deny wins."
        ),
    )
    tuqui_allow_private_methods = fields.Boolean(
        string="Allow Private Methods",
        help=(
            "Advanced mode only. When enabled, a private method can be "
            "called IF an exact (no wildcards) allow rule matches it."
        ),
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

    @api.model
    def get_values(self):
        res = super().get_values()
        policy = self.env["tuqui.rpc.policy"]._get_singleton()
        res.update(
            tuqui_policy_mode=policy.policy_mode,
            tuqui_allow_private_methods=policy.allow_private_methods,
        )
        return res

    def set_values(self):
        super().set_values()
        policy = self.env["tuqui.rpc.policy"]._get_singleton()
        # The flag only exists in advanced mode; force it off otherwise so
        # switching back to default never trips the policy's constraint on
        # a value the UI was hiding.
        allow_private = self.tuqui_policy_mode == "advanced" and self.tuqui_allow_private_methods
        vals = {}
        if policy.policy_mode != self.tuqui_policy_mode:
            vals["policy_mode"] = self.tuqui_policy_mode
        if policy.allow_private_methods != allow_private:
            vals["allow_private_methods"] = allow_private
        if vals:
            policy.write(vals)

    # ---------- Actions (thin proxies to the singletons) ----------

    def action_tuqui_activate(self):
        """Open the activation route in a new tab — it mints the nonce +
        secret and redirects on to the Tuqui frontend. Works from both
        'pending' and 'disconnected' (the route itself rejects 'active').

        New tab so the admin keeps the Odoo settings open behind the
        activation flow instead of navigating away from the database."""
        return {
            "type": "ir.actions.act_url",
            "url": "/tuqui/activation/start",
            "target": "new",
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

    def action_tuqui_apply_read_only_preset(self):
        return self.env["tuqui.rpc.policy"]._get_singleton().action_apply_read_only_preset()

    def action_tuqui_open_rules(self):
        return self.env["ir.actions.act_window"]._for_xml_id("tuqui.action_tuqui_rpc_rule")

    def action_tuqui_open_access_log(self):
        return self.env["ir.actions.act_window"]._for_xml_id("tuqui.action_tuqui_access_log")
