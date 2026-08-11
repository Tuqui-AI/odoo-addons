# Tuqui Assistant — superficie embebida (OWL) + capa de identidad/SSO companion.
from . import models
from . import controllers


def post_init_hook(env):
    """Leave the chat-access roster populated right after install.

    Otherwise a fresh install shows nobody the icon until the first cron fires,
    up to 15 minutes of "we installed it and nothing happened".

    Triggers the cron rather than syncing inline: an install must not depend on
    outbound HTTP (it would also fire during test runs, where external requests
    are forbidden), and a trigger runs on the next worker wake anyway. The
    upgrade path does the same from ``migrations/``, since this hook only runs on
    install.
    """
    cron = env.ref("tuqui_assistant.ir_cron_sync_chat_members", raise_if_not_found=False)
    if cron:
        cron.sudo()._trigger()
