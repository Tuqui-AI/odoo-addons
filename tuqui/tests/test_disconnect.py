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
* ``action_disconnect`` fires a best-effort, credential-free hint to Tuqui
  AFTER the local teardown, and never lets that call break the teardown.
"""

import json
import secrets
from unittest.mock import patch

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
        # action_disconnect now defers its Tuqui hint to cr.postcommit. HttpCase
        # shares one cursor across the class, so a callback queued by a prior
        # test (one that didn't run postcommit) would otherwise leak into this
        # test's assertions. Start each test with an empty queue.
        self.env.cr.postcommit.clear()
        # action_disconnect fires a best-effort hint to Tuqui via requests.post.
        # Stub it across the suite so no test reaches the real https://tuqui.com —
        # the network behaviour is asserted explicitly in the dedicated tests
        # below via their own patches (which then run cr.postcommit).
        patcher = patch(
            "odoo.addons.tuqui.models.tuqui_oauth_client.requests.post",
            return_value=None,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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

    @mute_logger("odoo.addons.tuqui.controllers.rpc")
    def test_revoke_with_wrong_secret_does_not_disconnect(self):
        """A leaked access token alone must not tear down the connection.

        ``/oauth/revoke`` is authenticated by the ``client_secret``; a wrong
        secret → 401 ``invalid_client`` and the teardown never runs: the
        singleton stays ``active`` and the signing key is intact, so a token
        minted before the failed revoke still verifies.
        """
        token = self._post_token(expect_status=200).json()["access_token"]
        self.assertEqual(self._rpc_with_token(token).status_code, 200)

        resp = self.url_open(
            "/tuqui/oauth/revoke",
            data={"client_id": self.client_id, "client_secret": "wrong-secret"},
            headers=self._db_headers(),
        )
        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(resp.json()["error"], "invalid_client")

        # Nothing was torn down: state intact and the signing key never rotated.
        self.client.invalidate_recordset()
        self.assertEqual(self.client.state, "active")
        self.assertIsNotNone(
            verify_access_token(self.env, token),
            "a failed revoke must not rotate the signing key",
        )
        self.assertEqual(
            self._rpc_with_token(token).status_code,
            200,
            "the pre-revoke token must still work after a failed revoke",
        )

    # ─── reactivation restores access ────────────────────────────────

    @mute_logger("odoo.addons.tuqui.controllers.oauth")
    def test_reactivation_restores_token_issuance(self):
        self.client.action_disconnect()
        self._post_token(expect_status=401)

        # Re-activate: /exchange stages activation_pending=True, then /token
        # completes it (mark_active is called on the first successful mint).
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

    # ─── disconnect hint to Tuqui (option A) ─────────────────────────

    def test_disconnect_notifies_tuqui_after_teardown(self):
        """The hint POSTs {client_id} to the right URL, deferred to a POST-COMMIT
        callback: it must not fire during action_disconnect (that would let
        Tuqui's re-probe write this row before our own flush commits ->
        SerializationFailure) and, once it does fire, the teardown is durable so
        the re-probe sees the disconnected (401) state."""
        observed = {}

        def fake_post(url, json=None, timeout=None):
            # Capture the state the model is in at the moment of the call, so
            # we can assert the teardown already ran (ordering matters).
            self.client.invalidate_recordset()
            observed["url"] = url
            observed["json"] = json
            observed["timeout"] = timeout
            observed["state_at_call"] = self.client.state
            observed["key_dead"] = verify_access_token(self.env, token) is None
            return None

        token = _issue_access_token(self.env, self.client_id)
        self.assertIsNotNone(verify_access_token(self.env, token))

        with patch(
            "odoo.addons.tuqui.models.tuqui_oauth_client.requests.post",
            side_effect=fake_post,
        ) as mocked:
            self.client.action_disconnect()
            # Regression guard for the SerializationFailure fix: the hint is
            # registered on cr.postcommit, so NOTHING is sent until the
            # transaction commits. An inline POST here is what raced the flush.
            mocked.assert_not_called()
            self.env.cr.postcommit.run()

        mocked.assert_called_once()
        self.assertTrue(observed["url"].endswith("/api/onboarding/companion/disconnected"))
        self.assertEqual(observed["json"], {"client_id": self.client_id})
        self.assertEqual(observed["timeout"], 4)
        # Teardown ran first: state flipped and signing key rotated before the POST.
        self.assertEqual(observed["state_at_call"], "disconnected")
        self.assertTrue(observed["key_dead"], "the signing key must rotate before Tuqui is notified")

    @mute_logger("odoo.addons.tuqui.models.tuqui_oauth_client")
    def test_disconnect_completes_when_notify_raises(self):
        """A failing hint must not break the local teardown: state still flips
        to disconnected, the signing key still rotates, and nothing raises."""
        token = _issue_access_token(self.env, self.client_id)
        self.assertIsNotNone(verify_access_token(self.env, token))

        with patch(
            "odoo.addons.tuqui.models.tuqui_oauth_client.requests.post",
            side_effect=RuntimeError("tuqui unreachable"),
        ) as mocked:
            # action_disconnect only registers the post-commit hint — it must not
            # raise. The hint itself (which blows up) runs on commit and swallows.
            self.client.action_disconnect()
            self.env.cr.postcommit.run()

        mocked.assert_called_once()
        self.client.invalidate_recordset()
        self.assertEqual(self.client.state, "disconnected")
        self.assertIsNone(
            verify_access_token(self.env, token),
            "the signing key must rotate even when the Tuqui hint fails",
        )

    def test_disconnect_skips_notify_when_never_activated(self):
        """A client that never activated (pending, no workspace) has nothing
        for Tuqui to re-probe — the hint is skipped entirely."""
        self.client.write({"state": "pending", "workspace_id_external": False})

        with patch(
            "odoo.addons.tuqui.models.tuqui_oauth_client.requests.post",
        ) as mocked:
            self.client.action_disconnect()

        mocked.assert_not_called()
        self.client.invalidate_recordset()
        self.assertEqual(self.client.state, "disconnected")
