"""Tests for the embed SSO handoff: nonce model + /sso/exchange endpoint.

Mirrors ``tuqui/tests/test_activation.py``. All data is built with ``.create()``
/ the model's own mint method — no demo data.
"""

import json

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged


@tagged("post_install", "-at_install", "tuqui_assistant")
class TestTuquiAssistantSsoNonceModel(TransactionCase):
    """Unit-level coverage of tuqui.assistant.sso.nonce primitives."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Nonce = cls.env["tuqui.assistant.sso.nonce"]
        # An active companion singleton with a client_id is the precondition for
        # minting an SSO nonce (issue_for_current_user hard-cuts otherwise).
        client, _secret = cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
        client.write({"state": "active", "workspace_id_external": "test-workspace"})
        cls.oauth_client = client

    # ---- embed_bootstrap ----

    def test_embed_bootstrap_connected_when_active_with_slug(self):
        boot = self.Nonce.embed_bootstrap()
        self.assertTrue(boot["connected"], "active companion + slug → connected")
        self.assertEqual(boot["slug"], "test-workspace")
        self.assertTrue(boot["base_url"], "base_url must always be present")
        # Exact contract: the payload is resolved from local state only, with no
        # HTTP call to Tuqui (this test used to reach the network for the plan flag).
        self.assertEqual(set(boot), {"connected", "base_url", "slug"})

    def test_embed_bootstrap_not_connected_without_slug(self):
        self.oauth_client.write({"workspace_id_external": False})
        boot = self.Nonce.embed_bootstrap()
        self.assertFalse(boot["connected"], "no slug → not connected (panel shows prompt)")

    def test_embed_bootstrap_not_connected_when_disconnected(self):
        self.oauth_client.write({"state": "disconnected"})
        boot = self.Nonce.embed_bootstrap()
        self.assertFalse(boot["connected"], "disconnected companion → not connected")

    # ---- issue_for_current_user ----

    def test_issue_binds_to_current_uid_and_client(self):
        data = self.Nonce.issue_for_current_user()
        self.assertGreater(len(data["nonce"]), 40, "nonce must be url-safe and unguessable")
        self.assertEqual(data["client_id"], self.oauth_client.client_id)
        self.assertEqual(data["expires_in"], 90)

        row = self.Nonce.sudo().search([("nonce", "=", data["nonce"])], limit=1)
        self.assertTrue(row)
        self.assertEqual(row.odoo_uid, self.env.uid, "nonce must bind to env.uid, never the caller's choice")
        self.assertEqual(row.client_id, self.oauth_client.client_id)
        self.assertFalse(row.consumed_at)
        self.assertGreater(row.expires_at, fields.Datetime.now())

    def test_issue_raises_when_companion_not_active(self):
        self.oauth_client.write({"state": "disconnected"})
        with self.assertRaises(UserError):
            self.Nonce.issue_for_current_user()

    # ---- redeem ----

    def test_redeem_happy_returns_uid_and_client(self):
        data = self.Nonce.issue_for_current_user()
        result = self.Nonce.redeem(data["nonce"])
        self.assertEqual(result, {"odoo_uid": self.env.uid, "client_id": self.oauth_client.client_id})

        row = self.Nonce.sudo().search([("nonce", "=", data["nonce"])], limit=1)
        self.assertTrue(row.consumed_at, "redeem must stamp consumed_at")

    def test_redeem_replay_is_atomic_single_use(self):
        """A second redeem of the same nonce must return None.

        The consume is a single guarded ``UPDATE ... WHERE consumed_at IS NULL``,
        so even two requests that both pass the existence check can't both win —
        only the first UPDATE matches the predicate. This in-transaction replay
        exercises that guard (a true two-cursor race is impractical in a
        TransactionCase, but the atomicity lives in the one SQL statement).
        """
        data = self.Nonce.issue_for_current_user()
        self.assertIsNotNone(self.Nonce.redeem(data["nonce"]))
        self.assertIsNone(self.Nonce.redeem(data["nonce"]), "replayed nonce must not redeem twice")

    def test_redeem_expired_returns_none(self):
        data = self.Nonce.issue_for_current_user()
        row = self.Nonce.sudo().search([("nonce", "=", data["nonce"])], limit=1)
        row.write({"expires_at": fields.Datetime.subtract(fields.Datetime.now(), minutes=1)})
        self.assertIsNone(self.Nonce.redeem(data["nonce"]), "expired nonce must not redeem")

        row.invalidate_recordset(["consumed_at"])
        self.assertFalse(row.consumed_at, "an expired (unredeemed) nonce must NOT be marked consumed")

    def test_redeem_unknown_returns_none(self):
        self.assertIsNone(self.Nonce.redeem("not-a-real-nonce"))

    def test_redeem_blank_returns_none(self):
        self.assertIsNone(self.Nonce.redeem(""))
        self.assertIsNone(self.Nonce.redeem(None))

    # ---- gc cron ----

    def test_gc_drops_only_aged_rows(self):
        fresh = self.Nonce.issue_for_current_user()["nonce"]
        aged = self.Nonce.issue_for_current_user()["nonce"]
        aged_row = self.Nonce.sudo().search([("nonce", "=", aged)])
        aged_row.write({"expires_at": fields.Datetime.subtract(fields.Datetime.now(), days=30)})

        deleted = self.Nonce.sudo()._gc_old_nonces()
        self.assertGreaterEqual(deleted, 1)

        remaining = self.Nonce.sudo().search([]).mapped("nonce")
        self.assertIn(fresh, remaining)
        self.assertNotIn(aged, remaining)


@tagged("post_install", "-at_install", "tuqui_assistant")
class TestTuquiAssistantSsoExchange(HttpCase):
    """End-to-end coverage of ``POST /tuqui_assistant/sso/exchange``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Nonce = cls.env["tuqui.assistant.sso.nonce"]
        client, _secret = cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
        client.write({"state": "active", "workspace_id_external": "test-workspace"})
        cls.oauth_client = client

    def _db_headers(self):
        return {"X-Odoo-Database": self.env.cr.dbname}

    def _post_exchange(self, body, *, expect_status=None):
        resp = self.url_open(
            "/tuqui_assistant/sso/exchange",
            data=json.dumps(body),
            headers={**self._db_headers(), "Content-Type": "application/json"},
        )
        if expect_status is not None:
            self.assertEqual(resp.status_code, expect_status, resp.text)
        return resp

    def _mint(self):
        """Mint a nonce via the model (binds to the current env.uid)."""
        return self.Nonce.issue_for_current_user()["nonce"]

    def test_exchange_happy_path(self):
        nonce = self._mint()
        resp = self._post_exchange({"nonce": nonce}, expect_status=200)
        body = resp.json()
        self.assertEqual(body["odoo_uid"], self.env.uid)
        self.assertEqual(body["client_id"], self.oauth_client.client_id)

        row = self.Nonce.sudo().search([("nonce", "=", nonce)], limit=1)
        row.invalidate_recordset(["consumed_at"])
        self.assertTrue(row.consumed_at, "exchange must consume the nonce")

    def test_exchange_rejects_replayed_nonce(self):
        nonce = self._mint()
        self._post_exchange({"nonce": nonce}, expect_status=200)
        resp = self._post_exchange({"nonce": nonce}, expect_status=410)
        self.assertEqual(resp.json()["error"]["code"], "invalid_nonce")

    def test_exchange_rejects_expired_nonce(self):
        nonce = self._mint()
        row = self.Nonce.sudo().search([("nonce", "=", nonce)])
        row.write({"expires_at": fields.Datetime.subtract(fields.Datetime.now(), minutes=1)})
        resp = self._post_exchange({"nonce": nonce}, expect_status=410)
        self.assertEqual(resp.json()["error"]["code"], "invalid_nonce")

    def test_exchange_410_for_unknown_nonce(self):
        # This endpoint intentionally returns a single 410 for unknown / consumed
        # / expired — no distinction leaked (unlike activation's 404 for unknown).
        resp = self._post_exchange({"nonce": "not-a-real-nonce"}, expect_status=410)
        self.assertEqual(resp.json()["error"]["code"], "invalid_nonce")

    def test_exchange_400_for_missing_nonce(self):
        resp = self._post_exchange({}, expect_status=400)
        self.assertEqual(resp.json()["error"]["code"], "bad_request")

    def test_exchange_400_for_invalid_json(self):
        resp = self.url_open(
            "/tuqui_assistant/sso/exchange",
            data="this-is-not-json",
            headers={**self._db_headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "bad_request")
