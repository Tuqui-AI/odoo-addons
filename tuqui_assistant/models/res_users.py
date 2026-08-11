import logging

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# The cron reconciles every 15 minutes, so one slow answer is never worth
# holding a worker for long. Generous enough for a cold Tuqui dyno.
_SYNC_TIMEOUT_SECONDS = 10


class ResUsers(models.Model):
    _inherit = "res.users"

    tuqui_has_chat = fields.Boolean(
        string="Tuqui chat access",
        default=False,
        readonly=True,
        copy=False,
        help=(
            "Whether this user has a Tuqui chat seat, as last reported by Tuqui. "
            "Refreshed by the 'Tuqui Assistant: sync chat members' scheduled action; "
            "the systray icon is shown only to users who have it. A cache, not a "
            "setting: editing it by hand is undone by the next sync."
        ),
    )

    # ─── Sync ────────────────────────────────────────────────────────

    @api.model
    def _tuqui_sync_chat_access(self) -> bool:
        """Refresh ``tuqui_has_chat`` for every user from Tuqui's roster.

        ONE HTTP request per Odoo instance, never one per user: Tuqui answers with
        the whole roster of uids that hold a chat seat and we diff it against what
        we have. That is what keeps this at ~96 requests a day whether the
        instance has 20 users or 2000 (spec ``systray-solo-para-usuarios-con-chat``
        §E).

        **Fail-open.** If Tuqui can't be reached, answers non-200, or answers
        something we don't recognise, nothing is written and the last known state
        stands. Nobody loses the icon over a network problem — same criterion the
        panel's bootstrap has always used. A disconnected companion is a different
        thing: that is local truth, not a failure, so the flags are cleared.

        Returns:
            True when the state was refreshed (including the cleared-because-
            disconnected case), False when Tuqui could not be trusted and nothing
            was touched.
        """
        oauth_client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        if not (oauth_client and oauth_client.state == "active" and oauth_client.client_id):
            # No live companion means nobody reaches Tuqui through this Odoo, and
            # we know that locally — no request needed, and no reason to keep
            # showing an icon that leads nowhere.
            self._tuqui_apply_chat_access(set())
            return True

        url = f"{oauth_client._get_tuqui_base_url()}/api/companion/chat-members"
        try:
            resp = requests.get(
                url,
                params={"client_id": oauth_client.client_id},
                timeout=_SYNC_TIMEOUT_SECONDS,
            )
            # Any non-200 keeps the last known state, 429 included: being
            # throttled is not evidence that somebody lost their seat.
            resp.raise_for_status()
            uids = resp.json()["odoo_uids"]
            # bool is a subclass of int, and JSON true would sail through a plain
            # isinstance check straight into browse().
            if not isinstance(uids, list) or any(not isinstance(uid, int) or isinstance(uid, bool) for uid in uids):
                raise ValueError(f"unexpected odoo_uids payload: {uids!r}")
        except Exception as exc:  # noqa: BLE001 - fail-open, must never raise
            _logger.warning("Tuqui chat-members sync failed, keeping last known state: %s", exc)
            return False

        self._tuqui_apply_chat_access(set(uids))
        return True

    @api.model
    def _tuqui_apply_chat_access(self, uids: set) -> None:
        """Write only what changed, so the sync is a no-op on a quiet instance.

        ``active_test=False`` on purpose: an archived Odoo user who kept the flag
        has to lose it too, and they wouldn't come back from a default search.

        Args:
            uids: Odoo user ids that currently hold a chat seat.
        """
        users = self.env["res.users"].sudo().with_context(active_test=False)

        flagged = users.search([("tuqui_has_chat", "=", True)])
        to_revoke = flagged.filtered(lambda user: user.id not in uids)
        # exists() because Tuqui's roster can name a uid this database no longer
        # has (deleted user, restored backup).
        to_grant = users.browse(sorted(uids)).exists().filtered(lambda user: not user.tuqui_has_chat)

        if to_revoke:
            to_revoke.write({"tuqui_has_chat": False})
        if to_grant:
            to_grant.write({"tuqui_has_chat": True})
        if to_revoke or to_grant:
            _logger.info(
                "Tuqui chat access synced: %s granted, %s revoked",
                len(to_grant),
                len(to_revoke),
            )
