"""Negative paths of the ``/tuqui/oauth/token`` client_credentials grant.

``test_disconnect.py`` covers the *state* gate (disconnected refuses, pending /
active issue). This suite covers the RFC 6749 §4.4 request validation that runs
*before* the state gate — the grant-type, missing-credential and bad-credential
errors — so a malformed or unauthenticated token request can't slip through.

Responses asserted against ``controllers/oauth.py::token``:

* ``grant_type`` ≠ ``client_credentials`` → 400 ``unsupported_grant_type``
* missing ``client_id`` or ``client_secret`` → 400 ``invalid_request``
* unknown ``client_id`` / wrong ``client_secret`` → 401 ``invalid_client``
"""

import secrets

from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiOAuthToken(HttpCase):
    """The token endpoint validates the grant before minting anything."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Singleton with a known plaintext secret (mirror of test_disconnect).
        client = cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()[0]
        plain = secrets.token_urlsafe(48)
        salt = secrets.token_hex(16)
        client.write(
            {
                "client_secret_hash": client._hash_secret(plain, salt),
                "client_secret_salt": salt,
                "state": "active",
            }
        )
        cls.client = client
        cls.client_id = client.client_id
        cls.client_secret = plain

    # ─── HTTP helpers ────────────────────────────────────────────────

    def _db_headers(self):
        return {"X-Odoo-Database": self.env.cr.dbname}

    def _post_token(self, *, expect_status=None, **fields):
        resp = self.url_open(
            "/tuqui/oauth/token",
            data=fields,
            headers=self._db_headers(),
        )
        if expect_status is not None:
            self.assertEqual(resp.status_code, expect_status, resp.text)
        return resp

    # ─── grant_type ──────────────────────────────────────────────────

    def test_unsupported_grant_type(self):
        """Only ``client_credentials`` is supported; anything else → 400."""
        body = self._post_token(
            grant_type="authorization_code",
            client_id=self.client_id,
            client_secret=self.client_secret,
            expect_status=400,
        ).json()
        self.assertEqual(body["error"], "unsupported_grant_type")

    def test_missing_grant_type(self):
        """A request with no grant_type is not client_credentials → 400."""
        body = self._post_token(
            client_id=self.client_id,
            client_secret=self.client_secret,
            expect_status=400,
        ).json()
        self.assertEqual(body["error"], "unsupported_grant_type")

    # ─── missing credentials ─────────────────────────────────────────

    def test_missing_client_id(self):
        body = self._post_token(
            grant_type="client_credentials",
            client_secret=self.client_secret,
            expect_status=400,
        ).json()
        self.assertEqual(body["error"], "invalid_request")

    def test_missing_client_secret(self):
        body = self._post_token(
            grant_type="client_credentials",
            client_id=self.client_id,
            expect_status=400,
        ).json()
        self.assertEqual(body["error"], "invalid_request")

    # ─── bad credentials ─────────────────────────────────────────────

    def test_unknown_client_id(self):
        """A client_id that doesn't match the singleton → 401 invalid_client."""
        body = self._post_token(
            grant_type="client_credentials",
            client_id="not-the-real-client",
            client_secret=self.client_secret,
            expect_status=401,
        ).json()
        self.assertEqual(body["error"], "invalid_client")

    @mute_logger("odoo.addons.tuqui.controllers.oauth")
    def test_wrong_client_secret(self):
        """Right client_id, wrong secret → 401 invalid_client.

        The controller logs ``_LOG.info`` on invalid credentials; mute it so
        runbot's red-on-any-ERROR signal stays honest.
        """
        body = self._post_token(
            grant_type="client_credentials",
            client_id=self.client_id,
            client_secret="wrong-secret",
            expect_status=401,
        ).json()
        self.assertEqual(body["error"], "invalid_client")
