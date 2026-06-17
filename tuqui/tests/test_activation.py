"""Tests for the activation handoff: nonce model, /exchange endpoint, gc cron."""

import json
import urllib.parse

from odoo import fields
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiActivationNonceModel(TransactionCase):
    """Unit-level coverage of tuqui.activation.nonce primitives."""

    def test_issue_persists_plaintext_and_returns_nonce(self):
        nonce, expires_at = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(
                client_id="test-client-id",
                client_secret_plaintext="plain-secret",
                acting_user_login="admin",
            )
        )
        self.assertGreater(len(nonce), 40, "Nonce should be url-safe and unguessable")
        self.assertGreater(expires_at, fields.Datetime.now(), "expires_at must be in the future")

        row = self.env["tuqui.activation.nonce"].sudo().search([("nonce", "=", nonce)], limit=1)
        self.assertTrue(row)
        self.assertEqual(row.client_secret_plaintext, "plain-secret")
        self.assertEqual(row.client_id, "test-client-id")
        self.assertEqual(row.acting_user_login, "admin")
        self.assertFalse(row.consumed_at)

    def test_consume_nulls_plaintext_and_sets_timestamp(self):
        nonce, _ = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(client_id="cid", client_secret_plaintext="secret", acting_user_login="admin")
        )
        row = self.env["tuqui.activation.nonce"].sudo().search([("nonce", "=", nonce)], limit=1)

        row._consume()
        row.invalidate_recordset()
        self.assertFalse(row.client_secret_plaintext, "Plaintext must be NULL after consume")
        self.assertTrue(row.consumed_at, "consumed_at must be set after consume")

    def test_gc_old_nonces_drops_only_aged_rows(self):
        """The cron purges nonces whose expires_at is older than the retention window."""
        # Fresh nonce: should survive.
        fresh_nonce, _ = (
            self.env["tuqui.activation.nonce"].sudo()._issue(client_id="fresh", client_secret_plaintext="fresh-secret")
        )

        # Aged nonce: backdated way past retention.
        aged_nonce, _ = (
            self.env["tuqui.activation.nonce"].sudo()._issue(client_id="aged", client_secret_plaintext="aged-secret")
        )
        aged_row = self.env["tuqui.activation.nonce"].sudo().search([("nonce", "=", aged_nonce)])
        aged_row.write({"expires_at": fields.Datetime.subtract(fields.Datetime.now(), days=30)})

        deleted = self.env["tuqui.activation.nonce"].sudo()._gc_old_nonces()
        self.assertGreaterEqual(deleted, 1)

        remaining = self.env["tuqui.activation.nonce"].sudo().search([])
        self.assertIn(fresh_nonce, remaining.mapped("nonce"))
        self.assertNotIn(aged_nonce, remaining.mapped("nonce"))


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiActivationExchange(HttpCase):
    """End-to-end coverage of ``POST /tuqui/activation/exchange``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Ensure there's a singleton OAuth client to flip pending→active.
        cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
        cls.env["tuqui.oauth.client"].sudo()._get_singleton().write({"state": "pending"})

    def _db_headers(self):
        return {"X-Odoo-Database": self.env.cr.dbname}

    def _post_exchange(self, body, *, expect_status=None):
        resp = self.url_open(
            "/tuqui/activation/exchange",
            data=json.dumps(body),
            headers={**self._db_headers(), "Content-Type": "application/json"},
        )
        if expect_status is not None:
            self.assertEqual(resp.status_code, expect_status, resp.text)
        return resp

    def test_exchange_happy_path(self):
        """A valid, unconsumed, unexpired nonce returns all six credential fields."""
        client_id = self.env["tuqui.oauth.client"].sudo()._get_singleton().client_id
        nonce, _ = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(
                client_id=client_id,
                client_secret_plaintext="plain-secret-for-test",
                acting_user_login="admin",
            )
        )
        # HttpCase propagates writes to the HTTP-server view via the test
        # cursor's savepoint; no explicit commit is required (and would be
        # rejected — the test cursor forbids commit/rollback to keep
        # rollback semantics intact at test teardown).

        resp = self._post_exchange({"nonce": nonce}, expect_status=200)
        body = resp.json()
        self.assertEqual(body["client_id"], client_id)
        self.assertEqual(body["client_secret"], "plain-secret-for-test")
        self.assertEqual(body["acting_user_login"], "admin")
        self.assertEqual(body["protocol_version"], "2.0")
        self.assertIn("companion_url", body)
        self.assertIn("module_version", body)

        # Side effects: nonce row is consumed + plaintext nulled, OAuth client is active.
        row = self.env["tuqui.activation.nonce"].sudo().search([("nonce", "=", nonce)], limit=1)
        row.invalidate_recordset()
        self.assertTrue(row.consumed_at)
        self.assertFalse(row.client_secret_plaintext)
        oauth_client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        oauth_client.invalidate_recordset()
        self.assertEqual(oauth_client.state, "active")

    def test_exchange_rejects_replayed_nonce(self):
        client_id = self.env["tuqui.oauth.client"].sudo()._get_singleton().client_id
        nonce, _ = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(client_id=client_id, client_secret_plaintext="replay-target")
        )

        self._post_exchange({"nonce": nonce}, expect_status=200)
        resp = self._post_exchange({"nonce": nonce}, expect_status=410)
        self.assertEqual(resp.json()["error"]["code"], "gone")
        self.assertIn("consumed", resp.json()["error"]["message"].lower())

    def test_exchange_rejects_expired_nonce(self):
        client_id = self.env["tuqui.oauth.client"].sudo()._get_singleton().client_id
        nonce, _ = (
            self.env["tuqui.activation.nonce"].sudo()._issue(client_id=client_id, client_secret_plaintext="will-expire")
        )
        row = self.env["tuqui.activation.nonce"].sudo().search([("nonce", "=", nonce)])
        row.write({"expires_at": fields.Datetime.subtract(fields.Datetime.now(), minutes=1)})

        resp = self._post_exchange({"nonce": nonce}, expect_status=410)
        self.assertEqual(resp.json()["error"]["code"], "gone")
        self.assertIn("expired", resp.json()["error"]["message"].lower())

    def test_exchange_404_for_unknown_nonce(self):
        resp = self._post_exchange({"nonce": "not-a-real-nonce"}, expect_status=404)
        self.assertEqual(resp.json()["error"]["code"], "not_found")

    def test_exchange_400_for_missing_nonce(self):
        resp = self._post_exchange({}, expect_status=400)
        self.assertEqual(resp.json()["error"]["code"], "bad_request")

    def test_exchange_400_for_invalid_json(self):
        resp = self.url_open(
            "/tuqui/activation/exchange",
            data="this-is-not-json",
            headers={**self._db_headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "bad_request")

    def test_reactivation_cycle(self):
        """Full lifecycle: activate → disconnect → re-activate.

        Re-activation reaches /exchange with state='disconnected'. The client
        must flip back to 'active' — a transition guarded on 'pending' would
        leave Odoo wrongly showing "not connected" while Tuqui works. Regression
        guard for the from-state-coupled transitions in start()/exchange().
        """
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        client_id = client.client_id

        def _activate(secret):
            nonce, _ = (
                self.env["tuqui.activation.nonce"]
                .sudo()
                ._issue(client_id=client_id, client_secret_plaintext=secret, acting_user_login="admin")
            )
            self._post_exchange({"nonce": nonce}, expect_status=200)
            client.invalidate_recordset()

        # 1. First activation: pending → active.
        _activate("secret-1")
        self.assertEqual(client.state, "active")

        # 2. Disconnect (manual button / Tuqui-side revoke both land here).
        client.action_disconnect()
        client.invalidate_recordset()
        self.assertEqual(client.state, "disconnected")

        # 3. Re-activation: disconnected → active (the bug this guards against).
        _activate("secret-2")
        self.assertEqual(client.state, "active", "exchange must re-activate from 'disconnected', not only 'pending'")

    # The 'active' branch raises UserError, which the http framework logs at
    # WARNING (odoo.http) — expected here, muted so it doesn't redden runbot.
    @mute_logger("odoo.http")
    def test_start_rejects_only_active(self):
        """/start is allowed from 'pending' and 'disconnected'; only a live
        'active' connection is rejected. Authenticated admin GET → 302 redirect
        to the Tuqui frontend when allowed; non-redirect when blocked."""
        self.authenticate("admin", "admin")
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()

        # disconnected → re-activation allowed (redirects to frontend).
        client.write({"state": "disconnected"})
        resp = self.url_open("/tuqui/activation/start", allow_redirects=False, headers=self._db_headers())
        self.assertEqual(resp.status_code, 302, "re-activation from 'disconnected' must be allowed")

        # active → blocked (must disconnect first); no redirect.
        client.write({"state": "active"})
        resp = self.url_open("/tuqui/activation/start", allow_redirects=False, headers=self._db_headers())
        self.assertNotEqual(resp.status_code, 302, "an 'active' link must not re-activate")

    def test_start_sets_no_referrer_header(self):
        """The 302 redirect must carry Referrer-Policy: no-referrer so the nonce
        in the URL never leaks via the Referer header to the Tuqui frontend."""
        self.authenticate("admin", "admin")
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        client.write({"state": "pending"})
        resp = self.url_open("/tuqui/activation/start", allow_redirects=False, headers=self._db_headers())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")

    def test_start_reuses_unconsumed_nonce_without_rotating_secret(self):
        """Two /start calls in a row must reuse the still-valid nonce and leave
        the client_secret hash untouched — rotating on every click would break a
        double-clicked or refreshed handshake mid-flight."""
        self.authenticate("admin", "admin")
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        client.write({"state": "pending"})

        def _start_nonce():
            resp = self.url_open("/tuqui/activation/start", allow_redirects=False, headers=self._db_headers())
            self.assertEqual(resp.status_code, 302, resp.text)
            location = resp.headers["Location"]
            query = urllib.parse.urlparse(location).query
            return urllib.parse.parse_qs(query)["nonce"][0]

        # First call mints a nonce and (since none existed) rotates the secret.
        nonce_1 = _start_nonce()
        client.invalidate_recordset()
        hash_after_first = client.client_secret_hash

        # Second call must REUSE the same nonce and NOT rotate the secret again.
        nonce_2 = _start_nonce()
        client.invalidate_recordset()
        hash_after_second = client.client_secret_hash

        self.assertEqual(nonce_1, nonce_2, "an unconsumed, unexpired nonce must be reused, not re-minted")
        self.assertEqual(
            hash_after_first,
            hash_after_second,
            "the client_secret must not rotate while a valid nonce is outstanding",
        )
