"""Activation trigger — mints a nonce + redirects to the Tuqui frontend."""

import logging
import urllib.parse

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import Response, request


_LOG = logging.getLogger(__name__)


# Default landing URL when no admin override is set via
# ir.config_parameter ``tuqui.activation.frontend_url``. Dev environments
# typically point this at a local Vite/Next server (e.g.
# http://localhost:5173/activate).
_DEFAULT_FRONTEND_URL = "https://tuqui.com/activate"
_FRONTEND_URL_PARAM = "tuqui.activation.frontend_url"


class TuquiActivation(http.Controller):
    """Server-side trigger for the 2-leg activation handshake.

    The flow:

    1. Admin clicks "Activate Tuqui" in the OAuth client form — the
       button returns an ``act_url`` action that opens this endpoint in
       the same tab.
    2. This route (auth='user', group_system required) rotates the
       client_secret on the OAuth singleton, mints a fresh activation
       nonce holding the plaintext secret for up to 5 minutes, and
       302s to the configured Tuqui frontend URL with ``?nonce=...``
       and ``?companion_url=...`` in the query string.
    3. The Tuqui frontend exchanges the nonce for the credentials via
       ``POST /tuqui/activation/exchange`` (see ``exchange``) and then
       calls its own ``/api/onboarding/companion/activate`` to wire the
       Tuqui workspace to this Odoo instance.

    State guard: the OAuth singleton must be in ``'pending'`` (never
    activated yet, or disconnected). Re-activating an active workspace
    requires an explicit disconnect — keeps the flow non-idempotent on
    purpose so a stray click never replaces a live connection.
    """

    @http.route(
        "/tuqui/activation/start",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
        readonly=False,
    )
    def start(self, **_kwargs):
        env = request.env
        if not env.user.has_group("base.group_system"):
            raise AccessError(_("Only Odoo administrators can activate Tuqui."))

        oauth_client = env["tuqui.oauth.client"].sudo()._get_singleton()
        if oauth_client and oauth_client.state != "pending":
            raise UserError(
                _("Tuqui is already activated. Disconnect first to re-activate.")
            )

        # Lazy-create the singleton on first activation; otherwise rotate the
        # secret so the plaintext we hand to Tuqui isn't whatever leftover
        # value a previous abandoned attempt left behind.
        if not oauth_client:
            oauth_client, plain_secret = (
                env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
            )
        else:
            plain_secret = oauth_client._rotate_secret_silent()

        nonce, _expires_at = env["tuqui.activation.nonce"].sudo()._issue(
            client_id=oauth_client.client_id,
            client_secret_plaintext=plain_secret,
        )

        frontend_url = (
            env["ir.config_parameter"]
            .sudo()
            .get_param(_FRONTEND_URL_PARAM, _DEFAULT_FRONTEND_URL)
        ).rstrip("/")
        companion_url = request.httprequest.host_url.rstrip("/")

        # The Tuqui frontend reads nonce + companion_url from the query
        # string and POSTs back to /tuqui/activation/exchange with the
        # nonce only — the rest is metadata for UX, not auth.
        query = urllib.parse.urlencode(
            {"nonce": nonce, "companion_url": companion_url}
        )
        redirect_to = f"{frontend_url}?{query}"

        _LOG.info(
            "tuqui.activation.start: nonce minted, redirecting (companion_url=%s)",
            companion_url,
        )
        return Response("", status=302, headers={"Location": redirect_to})
