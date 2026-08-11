from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Populate the new chat-access flag right after the upgrade.

    ``post_init_hook`` covers fresh installs but never runs on upgrade, and this
    is the version that introduces the systray gating: without a sync here, every
    user on an already-installed instance would lose the icon until the first
    cron fires — the gate defaults to False.

    Triggers the cron instead of syncing inline for the same reason as the install
    hook: an upgrade must not hang on (or fail because of) outbound HTTP.
    """
    if not version:
        return  # fresh install — post_init_hook already triggered it
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref("tuqui_assistant.ir_cron_sync_chat_members", raise_if_not_found=False)
    if cron:
        cron.sudo()._trigger()
