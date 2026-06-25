import secrets

import requests
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# SSO nonces are machine-to-machine and redeemed immediately by the SPA right
# after the panel opens — no human round-trip — so the TTL is tight (seconds),
# unlike the activation nonce (10 min for the admin to log in / pick a workspace).
_NONCE_TTL_SECONDS = 90

# Housekeeping window for the GC cron — not a security knob (security is
# single-use + the short TTL above).
_GC_RETENTION_DAYS = 1


class TuquiAssistantSsoNonce(models.Model):
    """Short-lived nonce for the embed SSO handoff (ADR 0001 / spec §2.2).

    Mirrors ``tuqui.activation.nonce`` but for per-user single-sign-on into the
    embedded Tuqui SPA instead of workspace activation:

    1. When the user opens the Tuqui panel, the OWL side calls
       :meth:`issue_for_current_user` (RPC, runs as the logged-in user) — it
       mints a nonce bound to ``odoo_uid = env.uid`` and the companion
       ``client_id``. The uid comes from the session, never from the caller, so
       a user can only ever SSO as themselves.
    2. The panel posts ``{client_id, nonce}`` to the iframe via postMessage; the
       SPA sends them to Tuqui's ``/api/companion/embed-token``.
    3. Tuqui redeems the nonce via ``POST /tuqui_assistant/sso/exchange`` (auth
       by the nonce itself, single-use), gets back ``{odoo_uid}``, maps it to a
       workspace member and mints a short-lived session token.

    No secret is ever transported: the module never holds the OAuth client
    secret in plaintext, and the nonce is the only credential in flight.
    """

    _name = "tuqui.assistant.sso.nonce"
    _description = "Tuqui Assistant SSO Nonce"
    _order = "id desc"
    _rec_name = "nonce"

    nonce = fields.Char(required=True, index=True, readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True, index=True)
    consumed_at = fields.Datetime(readonly=True)
    odoo_uid = fields.Integer(
        required=True,
        readonly=True,
        help="res.users id the SSO token will be minted for (captured from env.uid at mint).",
    )
    client_id = fields.Char(
        required=True,
        readonly=True,
        help="Companion OAuth client_id this nonce is bound to (anti cross-workspace replay).",
    )

    _nonce_unique = models.Constraint(
        "unique(nonce)",
        # Translatable, env-aware message. ``models.Constraint`` accepts a callable
        # ``message(env, diagnostics)`` (odoo/orm/table_objects.py
        # ``Constraint.get_error_message``) that is invoked at *raise* time, so the
        # text is resolved in the offending user's language. We use the callable
        # form rather than a bare module-level ``_(...)``: calling ``_()`` at class
        # definition has no language context yet and logs
        # "no translation language detected" at boot. ``env._`` inside the lambda
        # is still picked up by the i18n extractor.
        lambda env, _diagnostics: env._("Tuqui SSO nonce must be unique."),
    )

    @api.model
    def embed_bootstrap(self) -> dict:
        """Everything the panel needs to mount the iframe, resolved from companion.

        Public (RPC-callable from the OWL panel) but reads the admin-only
        ``tuqui.oauth.client`` via ``sudo()``. Replaces the old
        ``tuqui_assistant.spa_url`` config param: the Tuqui base URL comes from
        the connector's ``tuqui.base_url`` (default ``https://tuqui.com``) and
        the workspace slug from the activated companion connection — so the embed
        always points at the same workspace the rest of Tuqui is wired to.

        Returns ``{connected, base_url, slug}``. ``connected`` is False unless the
        companion is ``active`` AND a workspace slug is known; the panel shows a
        "connect Tuqui" prompt instead of the iframe when it's False.
        """
        oauth_client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        base_url = oauth_client._get_tuqui_base_url() if oauth_client else "https://tuqui.com"
        connected = bool(oauth_client and oauth_client.state == "active" and oauth_client.workspace_id_external)

        chat_enabled = False
        if connected:
            try:
                client_id = oauth_client.client_id
                url = f"{base_url}/api/companion/bootstrap?client_id={client_id}"
                resp = requests.get(url, timeout=3)
                resp.raise_for_status()
                chat_enabled = resp.json().get("chat_enabled", False)
            except Exception:
                chat_enabled = True  # fail-open: no bloquear si Tuqui no responde

        return {
            "connected": connected,
            "base_url": base_url,
            "slug": oauth_client.workspace_id_external if oauth_client else False,
            "chat_enabled": chat_enabled,
        }

    @api.model
    def issue_for_current_user(self) -> dict:
        """Mint a single-use SSO nonce for the *current* Odoo user.

        Public (RPC-callable from the OWL panel). Binds the nonce to
        ``self.env.uid`` — the caller cannot mint a nonce for anyone else.
        Returns the data the panel needs to hand to the iframe.

        Raises:
            UserError: if the companion isn't currently ``active`` — a hard cut so
                a disconnected companion immediately stops the embed chat (the
                module knows its own state locally; we don't wait for Tuqui's
                best-effort teardown).
        """
        oauth_client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        if not oauth_client or oauth_client.state != "active" or not oauth_client.client_id:
            raise UserError(_("Tuqui is not connected to this Odoo (companion). Please activate the connection first."))

        nonce = secrets.token_urlsafe(48)
        expires_at = fields.Datetime.add(fields.Datetime.now(), seconds=_NONCE_TTL_SECONDS)
        self.sudo().create(
            {
                "nonce": nonce,
                "expires_at": expires_at,
                "odoo_uid": self.env.uid,
                "client_id": oauth_client.client_id,
            }
        )
        return {
            "nonce": nonce,
            "client_id": oauth_client.client_id,
            "expires_in": _NONCE_TTL_SECONDS,
        }

    @api.model
    def redeem(self, nonce: str) -> dict | None:
        """Redeem a nonce → ``{"odoo_uid": int, "client_id": str}`` or None.

        Single-use and **atomic**: the consume is a single guarded
        ``UPDATE ... WHERE id=%s AND consumed_at IS NULL`` that also enforces the
        TTL inline. Concurrent replays can't both win — only the first UPDATE
        matches the ``consumed_at IS NULL`` predicate and gets a row back; the
        loser sees zero rows and is treated as already-consumed. Returns None for
        unknown / already-consumed / expired nonces (caller maps to HTTP 410).

        A naive search → check ``consumed_at`` → write would let two requests
        both pass the Python check before either marks the row, replaying the SSO
        handoff; the DB predicate closes that window.
        """
        nonce = (nonce or "").strip()
        if not nonce:
            return None
        # Resolve the row id cheaply; existence/empty handled here, but the
        # consume + TTL gate live in the atomic UPDATE below (single source of
        # truth, race-free).
        row = self.sudo().search([("nonce", "=", nonce)], limit=1)
        if not row:
            return None
        # The guard below reads consumed_at/expires_at via raw SQL, so any pending
        # ORM writes to this row must hit the DB first — otherwise the predicate
        # would see stale column values (the ORM defers UPDATEs until flush).
        self.env["tuqui.assistant.sso.nonce"].flush_model(["consumed_at", "expires_at"])
        self.env.cr.execute(
            """
            UPDATE tuqui_assistant_sso_nonce
               SET consumed_at = now() AT TIME ZONE 'UTC'
             WHERE id = %s
               AND consumed_at IS NULL
               AND (expires_at IS NULL OR expires_at > (now() AT TIME ZONE 'UTC'))
         RETURNING odoo_uid, client_id
            """,
            (row.id,),
        )
        claimed = self.env.cr.fetchone()
        # Keep the ORM cache in sync with the raw UPDATE (consumed_at changed).
        row.invalidate_recordset(["consumed_at"])
        if not claimed:
            # Lost the race, already consumed, or expired — single None result,
            # no distinction leaked to the caller.
            return None
        odoo_uid, client_id = claimed
        return {"odoo_uid": odoo_uid, "client_id": client_id}

    @api.model
    def _gc_old_nonces(self) -> int:
        """Purge nonces past the retention window (wired to a daily cron)."""
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=_GC_RETENTION_DAYS)
        old = self.sudo().search([("expires_at", "<", cutoff)])
        count = len(old)
        if old:
            old.unlink()
        return count
