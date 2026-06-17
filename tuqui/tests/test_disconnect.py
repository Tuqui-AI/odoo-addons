"""Coverage of what disconnect does to OAuth *access* (token + signing key).

The state-machine transitions (pending → active → disconnected and back) live
in ``test_activation.py``. This suite covers the consequences that make the
disconnect real instead of cosmetic:

* ``action_disconnect`` rotates the signing key, so every outstanding access
  token stops verifying immediately.
* ``/tuqui/oauth/token`` refuses to mint new tokens while ``disconnected``
  (but still serves ``pending`` — the pre-activation state of the direct-paste
  flow — and ``active``).
* ``/tuqui/oauth/revoke`` (Tuqui-initiated teardown) goes through the same
  ``action_disconnect`` path, so both ends behave identically.
* Re-activation via ``/tuqui/activation/exchange`` restores token issuance.
"""

import json
import secrets

from odoo.addons.tuqui.controllers.oauth import _issue_access_token, verify_access_token
from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiDisconnect(HttpCase):
    """Disconnect cuts Tuqui off — outstanding tokens die and new ones are refused."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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

    def setUp(self):
        super().setUp()
        # Every test starts from a live, active connection.
        self.client.write({"state": "active"})

    # ─── HTTP helpers ────────────────────────────────────────────────

    def _db_headers(self):
        return {"X-Odoo-Database": self.env.cr.dbname}

    def _post_token(self, *, expect_status=None):
        resp = self.url_open(
            "/tuqui/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers=self._db_headers(),
        )
        if expect_status is not None:
            self.assertEqual(resp.status_code, expect_status, resp.text)
        return resp

    def _rpc_with_token(self, token):
        """A minimal read through the gateway, authenticated by ``token``.

        No acting-uid header → connection path (superuser, read-only); a plain
        search_read is a read so it passes when the token is valid.
        """
        return self.url_open(
            "/tuqui/rpc",
            data=json.dumps(
                {
                    "model": "res.company",
                    "method": "search_read",
                    "args": [[]],
                    "kwargs": {"fields": ["name"], "limit": 1},
                }
            ),
            headers={
                **self._db_headers(),
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    # ─── token state gate ────────────────────────────────────────────

    def test_token_issued_when_active(self):
        self.assertIn("access_token", self._post_token(expect_status=200).json())

    def test_token_issued_when_pending(self):
        # 'pending' is a live, pre-activation state (direct-paste flow): a
        # state gate of ``!= active`` would wrongly refuse first activation.
        self.client.write({"state": "pending"})
        self.assertIn("access_token", self._post_token(expect_status=200).json())

    @mute_logger("odoo.addons.tuqui.controllers.oauth")
    def test_token_refused_when_disconnected(self):
        self.client.action_disconnect()
        body = self._post_token(expect_status=401).json()
        self.assertEqual(body["error"], "invalid_client")
        self.assertEqual(body.get("error_description"), "client_disconnected")

    # ─── key rotation invalidates outstanding tokens ─────────────────

    def test_disconnect_rotates_signing_key(self):
        """A token that verified before disconnect must not verify after."""
        token = _issue_access_token(self.env, self.client_id)
        self.assertIsNotNone(verify_access_token(self.env, token))
        self.client.action_disconnect()
        self.assertIsNone(
            verify_access_token(self.env, token),
            "the signing key must rotate on disconnect so outstanding tokens die",
        )

    @mute_logger("odoo.addons.tuqui.controllers.rpc")
    def test_disconnect_cuts_off_live_token_end_to_end(self):
        token = self._post_token(expect_status=200).json()["access_token"]
        self.assertEqual(self._rpc_with_token(token).status_code, 200)
        self.client.action_disconnect()
        self.assertEqual(
            self._rpc_with_token(token).status_code,
            401,
            "the gateway must reject a token issued before disconnect",
        )

    # ─── /revoke mirrors the manual button ───────────────────────────

    @mute_logger("odoo.addons.tuqui.controllers.rpc", "odoo.addons.tuqui.controllers.oauth")
    def test_revoke_rotates_key_and_disconnects(self):
        token = self._post_token(expect_status=200).json()["access_token"]
        resp = self.url_open(
            "/tuqui/oauth/revoke",
            data={"client_id": self.client_id, "client_secret": self.client_secret},
            headers=self._db_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.client.invalidate_recordset()
        self.assertEqual(self.client.state, "disconnected")
        # Outstanding token dead, and no fresh tokens while disconnected.
        self.assertEqual(self._rpc_with_token(token).status_code, 401)
        self._post_token(expect_status=401)

    # ─── reactivation restores access ────────────────────────────────

    @mute_logger("odoo.addons.tuqui.controllers.oauth")
    def test_reactivation_restores_token_issuance(self):
        self.client.action_disconnect()
        self._post_token(expect_status=401)

        # Re-activate via the redirect /exchange, which marks the client active
        # from any prior state.
        nonce, _ = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(
                client_id=self.client_id,
                client_secret_plaintext=self.client_secret,
                acting_user_login="admin",
            )
        )
        ex = self.url_open(
            "/tuqui/activation/exchange",
            data=json.dumps({"nonce": nonce}),
            headers={**self._db_headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(ex.status_code, 200, ex.text)

        token = self._post_token(expect_status=200).json()["access_token"]
        self.assertEqual(self._rpc_with_token(token).status_code, 200)
