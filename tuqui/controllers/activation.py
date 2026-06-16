"""Activation trigger — mints a nonce + redirects to the Tuqui frontend."""

import json
import logging
import urllib.parse

from odoo import _, fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import Response, request

from .health import _PROTOCOL_VERSION, _module_version

_LOG = logging.getLogger(__name__)


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
        # Re-activation is allowed from both 'pending' (never activated) and
        # 'disconnected' (admin or Tuqui tore the link down). Only a live
        # 'active' connection must be explicitly disconnected first.
        if oauth_client and oauth_client.state == "active":
            raise UserError(_("Tuqui is already activated. Disconnect first to re-activate."))

        # Lazy-create the singleton on first activation; otherwise rotate the
        # secret so the plaintext we hand to Tuqui isn't whatever leftover
        # value a previous abandoned attempt left behind.
        if not oauth_client:
            oauth_client, plain_secret = env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
        else:
            plain_secret = oauth_client._rotate_secret_silent()

        nonce, _expires_at = (
            env["tuqui.activation.nonce"]
            .sudo()
            ._issue(
                client_id=oauth_client.client_id,
                client_secret_plaintext=plain_secret,
                acting_user_login=env.user.login,
            )
        )

        frontend_url = env["tuqui.oauth.client"].sudo()._get_activation_frontend_url().rstrip("/")
        companion_url = request.httprequest.host_url.rstrip("/")

        # The Tuqui frontend reads nonce + companion_url from the query
        # string and POSTs back to /tuqui/activation/exchange with the
        # nonce only — the rest is metadata for UX, not auth. instance_name
        # carries the company name so Tuqui can pre-fill a new workspace.
        params = {"nonce": nonce, "companion_url": companion_url}
        instance_name = env.company.name
        if instance_name:
            params["instance_name"] = instance_name
        query = urllib.parse.urlencode(params)
        redirect_to = f"{frontend_url}?{query}"

        _LOG.info(
            "tuqui.activation.start: nonce minted, redirecting (companion_url=%s)",
            companion_url,
        )
        return Response("", status=302, headers={"Location": redirect_to})

    @http.route(
        "/tuqui/activation/exchange",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,
    )
    def exchange(self, **_kwargs):
        """Redeem a single-use nonce for the activation credentials.

        Auth is the nonce itself — single-use, 5-minute TTL, sent only
        via the redirect from /start to the Tuqui frontend (HTTPS to a
        trusted origin). The exchange marks the row consumed in the same
        UPDATE that nullifies the plaintext secret, so a leaked nonce
        post-consumption can't replay.

        On success the OAuth client transitions ``pending → active`` —
        Tuqui having received the credentials is the moment activation
        is "real" from this module's perspective. If Tuqui's downstream
        wiring fails, the admin sees ``state=active`` but no traffic
        in ``last_seen_at`` and can disconnect to retry.

        Response shape::

            {
                "client_id":          "...",
                "client_secret":      "...",
                "companion_url":      "https://erp.example.com",
                "acting_user_login":  "admin",
                "module_version":     "19.0.0.3.0",
                "protocol_version":   "2.0"
            }
        """
        env = request.env

        try:
            body = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return _json_error("bad_request", "Body must be valid JSON", status=400)

        nonce = (body.get("nonce") or "").strip()
        if not nonce:
            return _json_error("bad_request", "Missing 'nonce'", status=400)

        row = env["tuqui.activation.nonce"].sudo().search([("nonce", "=", nonce)], limit=1)
        if not row:
            return _json_error("not_found", "Unknown activation nonce", status=404)

        if row.consumed_at:
            return _json_error("gone", "Activation nonce already consumed", status=410)

        if row.expires_at and row.expires_at < fields.Datetime.now():
            return _json_error("gone", "Activation nonce has expired", status=410)

        # Snapshot the fields we need to return before _consume nulls
        # the plaintext.
        client_id = row.client_id
        client_secret = row.client_secret_plaintext
        acting_user_login = row.acting_user_login

        row._consume()

        # Tuqui now holds valid creds → the connection is active, whatever the
        # prior state. Re-activation after a disconnect reaches here with
        # state='disconnected'; guarding on 'pending' would leave Odoo wrongly
        # showing "not connected" while Tuqui works fine. mark_active is idempotent.
        oauth_client = env["tuqui.oauth.client"].sudo()._get_singleton()
        if oauth_client:
            oauth_client.mark_active()

        companion_url = request.httprequest.host_url.rstrip("/")
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "companion_url": companion_url,
            "acting_user_login": acting_user_login,
            "module_version": _module_version(env),
            "protocol_version": _PROTOCOL_VERSION,
        }
        _LOG.info(
            "tuqui.activation.exchange: nonce redeemed, client_id=%s",
            client_id,
        )
        return Response(
            json.dumps(payload),
            content_type="application/json",
            status=200,
        )


def _json_error(code: str, message: str, *, status: int) -> Response:
    return Response(
        json.dumps({"error": {"code": code, "message": message}}),
        content_type="application/json",
        status=status,
    )
