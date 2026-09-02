"""Tests for ``/tuqui/companion/signing-key`` (task #72545).

The route exists so a database that connected before signed events existed can
get a key without tearing the connection down. What matters:

* only a caller holding a valid access token gets it — the same credential that
  can already read the whole ERP through the gateway;
* it mints when there is none, and returns the same key forever after. Rotating
  would invalidate whatever is sitting unsent in ``tuqui.event``.
"""

import secrets

from odoo.tests import HttpCase, tagged


def _rotate_oauth_secret(env):
    client = env["tuqui.oauth.client"].sudo()._get_singleton()
    if not client:
        client, _ = env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
    plain = secrets.token_urlsafe(48)
    salt = secrets.token_hex(16)
    client.write({"client_secret_hash": client._hash_secret(plain, salt), "client_secret_salt": salt})
    return client.client_id, plain


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiSigningKeyRoute(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client_id, cls.client_secret = _rotate_oauth_secret(cls.env)

    def setUp(self):
        super().setUp()
        self.client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        self.client.write({"state": "active", "event_signing_key": False})

    def _token(self):
        resp = self.url_open(
            "/tuqui/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"X-Odoo-Database": self.env.cr.dbname},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["access_token"]

    def _get(self, token=None):
        headers = {"X-Odoo-Database": self.env.cr.dbname}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.url_open("/tuqui/companion/signing-key", headers=headers)

    # ─── Who may ask ─────────────────────────────────────────────────────────

    def test_without_a_token_it_hands_out_nothing(self):
        resp = self._get()
        assert resp.status_code == 401
        assert "signing" not in resp.text.lower() or "event_signing_key" not in resp.json()

    def test_a_bogus_token_hands_out_nothing(self):
        resp = self._get(token="not.a.token")
        assert resp.status_code == 401

    def test_a_disconnected_database_hands_out_nothing(self):
        token = self._token()
        self.client.write({"state": "disconnected"})
        try:
            resp = self._get(token=token)
            assert resp.status_code in (401, 403)
        finally:
            self.client.write({"state": "active"})

    # ─── What it hands out ───────────────────────────────────────────────────

    def test_it_mints_a_key_when_there_is_none(self):
        resp = self._get(token=self._token())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["minted"] is True
        assert body["event_signing_key"]
        self.client.invalidate_recordset()
        assert self.client.event_signing_key == body["event_signing_key"]

    def test_asking_twice_returns_the_same_key(self):
        """Rotating on every ask would invalidate the events already queued."""
        token = self._token()
        first = self._get(token=token).json()
        second = self._get(token=token).json()
        assert first["event_signing_key"] == second["event_signing_key"]
        assert second["minted"] is False
