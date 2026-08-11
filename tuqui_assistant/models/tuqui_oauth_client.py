from odoo import models


class TuquiOauthClient(models.Model):
    _inherit = "tuqui.oauth.client"

    def mark_active(self, workspace_id_external=None):
        """Kick off a chat-access sync as soon as the companion goes live.

        Without this the first batch of people waits up to 15 minutes for the cron
        before their icon appears, right after an admin connected Tuqui — the
        worst possible moment to look broken.

        Deliberately a cron trigger and NOT the sync itself: ``mark_active`` runs
        inside the ``/tuqui/oauth/token`` request, with Tuqui waiting on the other
        end of the activation handshake. An HTTP call to Tuqui from here would put
        a round-trip on that critical path and, if it raised, would abort the token
        mint and break the activation outright. ``_trigger`` is also transactional:
        if the activation rolls back, the trigger goes with it.
        """
        res = super().mark_active(workspace_id_external=workspace_id_external)
        self._tuqui_trigger_chat_sync()
        return res

    def action_disconnect(self):
        """Drop everyone's chat access the moment the companion is disconnected.

        Synchronous, unlike the activation path: with no live companion the sync
        makes no HTTP call at all (it clears from local state), so there is nothing
        to keep off the request path — and waiting for the cron would leave up to
        15 minutes of icons that lead nowhere.
        """
        res = super().action_disconnect()
        self.env["res.users"]._tuqui_sync_chat_access()
        return res

    def _tuqui_trigger_chat_sync(self):
        """Ask the cron to run on the next worker wake instead of on schedule."""
        cron = self.env.ref("tuqui_assistant.ir_cron_sync_chat_members", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
