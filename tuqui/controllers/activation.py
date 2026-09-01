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
    2. This route (auth='user', group_system required) ensures a fresh
       activation nonce holding the plaintext secret for up to 10 minutes
       (reusing a still-valid unconsumed nonce instead of rotating the
       secret on every click), and 302s to the configured Tuqui frontend
       URL with ``?nonce=...`` and ``?companion_url=...`` in the query
       string. The redirect carries ``Referrer-Policy: no-referrer`` so
       the nonce in the URL never leaks via the Referer header.
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

        # Lazy-create the singleton on first activation; keep the freshly minted
        # secret so we reuse it below instead of rotating it again right away.
        created_secret = None
        if not oauth_client:
            oauth_client, created_secret = env["tuqui.oauth.client"].sudo()._get_or_create_singleton()

        # Idempotent /start: if a still-valid, unconsumed nonce already exists
        # for this client, reuse it (same nonce) instead of rotating the secret
        # again. Rotating on every click would invalidate the secret Tuqui is
        # about to exchange and let a stray double-click break a live handshake.
        existing = (
            env["tuqui.activation.nonce"]
            .sudo()
            .search(
                [
                    ("client_id", "=", oauth_client.client_id),
                    ("consumed_at", "=", False),
                    ("expires_at", ">", fields.Datetime.now()),
                ],
                order="id desc",
                limit=1,
            )
        )
        if existing:
            nonce = existing.nonce
        else:
            # No reusable nonce. Reuse the secret from a just-created singleton;
            # otherwise rotate so the plaintext we hand to Tuqui isn't a leftover
            # from an abandoned attempt. Then mint a fresh nonce holding it.
            plain_secret = created_secret or oauth_client._rotate_secret_silent()
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
        companion_url = env["tuqui.oauth.client"].sudo()._get_companion_url()

        # The Tuqui frontend reads nonce + companion_url from the query
        # string and POSTs back to /tuqui/activation/exchange with the
        # nonce only — the rest is metadata for UX, not auth. instance_name
        # carries the company name so Tuqui can pre-fill a new workspace.
        params = {"nonce": nonce, "companion_url": companion_url}
        instance_name = env.company.name
        if instance_name:
            params["instance_name"] = instance_name

        # The activating admin's email. Tuqui pre-fills its login form with it
        # when the admin isn't logged into Tuqui yet (activation from Odoo while
        # logged out) so they don't retype it. It's the admin's own email, sent
        # over the same no-referrer redirect as the nonce; omitted if unset.
        user_email = env.user.email
        if user_email:
            params["email"] = user_email

        # Capture the Settings page URL from the Referer header so Tuqui can
        # redirect the admin back after a successful activation — the settings
        # page then reloads and shows the connected state without a manual F5.
        # We read the raw Referer before we overwrite Referrer-Policy on the
        # outgoing 302, so this is the page the admin was on, not the /start URL.
        # Graceful fallback: if Referer is absent or doesn't belong to this
        # Odoo origin (e.g. triggered programmatically), we omit return_url
        # and Tuqui falls back to its default in-app navigate — never breaks.
        referrer = request.httprequest.referrer or ""
        if referrer:
            try:
                ref = urllib.parse.urlparse(referrer)
                own = urllib.parse.urlparse(companion_url)
                if (ref.scheme, ref.netloc) == (own.scheme, own.netloc):
                    params["return_url"] = referrer
            except ValueError:
                pass  # Malformed referrer — omit return_url silently

        query = urllib.parse.urlencode(params)
        redirect_to = f"{frontend_url}?{query}"

        _LOG.info(
            "tuqui.activation.start: nonce ready, redirecting (companion_url=%s)",
            companion_url,
        )
        # Referrer-Policy: no-referrer so the nonce in the redirect URL is never
        # leaked to the Tuqui frontend (or any further hop) via the Referer header.
        return Response(
            "",
            status=302,
            headers={
                "Location": redirect_to,
                "Referrer-Policy": "no-referrer",
            },
        )

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

        Auth is the nonce itself — single-use, 10-minute TTL, sent only
        via the redirect from /start to the Tuqui frontend (HTTPS to a
        trusted origin). The exchange marks the row consumed in the same
        UPDATE that nullifies the plaintext secret, so a leaked nonce
        post-consumption can't replay.

        On success the activation is staged: ``workspace_id_external`` is stored
        and ``activation_pending`` is set on the OAuth client. State flips to
        ``active`` only on the first successful ``POST /tuqui/oauth/token`` —
        the proof that Tuqui completed its own workspace wiring and can
        actually use the credentials.

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

        # workspace_slug is optional — older Tuqui versions that don't send it
        # are tolerated; we store it when present and leave it untouched when not.
        workspace_slug = body.get("workspace_slug")
        if not isinstance(workspace_slug, str) or not workspace_slug:
            workspace_slug = None

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

        # Stage the activation: store the workspace identifier (when present)
        # and set activation_pending so /token knows to flip state to 'active'
        # on the first successful mint. We don't mark active here — that flip
        # happens only when Tuqui proves it can use the credentials, which is
        # the first successful POST /tuqui/oauth/token call.
        oauth_client = env["tuqui.oauth.client"].sudo()._get_singleton()
        if oauth_client:
            vals = {"activation_pending": True}
            if workspace_slug:
                vals["workspace_id_external"] = workspace_slug
            oauth_client.write(vals)

        companion_url = env["tuqui.oauth.client"].sudo()._get_companion_url()
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            # The other half of the pair: Tuqui verifies event signatures with
            # it. Travels the same one-shot channel as the secret, and for the
            # same reason \u2014 this is the only moment both sides are in contact
            # with a human who authorised the connection.
            "event_signing_key": oauth_client.sudo().event_signing_key if oauth_client else None,
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
