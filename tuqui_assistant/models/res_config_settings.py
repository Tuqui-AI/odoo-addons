from odoo import _, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    def action_tuqui_sync_chat_members(self):
        """Refresh who has Tuqui chat, now, instead of waiting for the cron.

        For the admin who just assigned seats in Tuqui and has the person next to
        them. Synchronous on purpose: the whole point is the immediate answer, and
        the caller is one human clicking one button.

        On success it reloads — that is what re-issues ``session_info``, so an admin
        who just granted a seat to themselves sees their own icon appear without
        hunting for a refresh. On failure it says so instead of pretending, because
        the sync keeps the previous state and the screen would otherwise look
        identical to a successful no-op.
        """
        if self.env["res.users"]._tuqui_sync_chat_access():
            return {"type": "ir.actions.client", "tag": "reload"}
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "warning",
                "title": _("Tuqui did not answer"),
                "message": _("Chat access is unchanged — the last known state is kept. Try again in a moment."),
                "sticky": False,
            },
        }
