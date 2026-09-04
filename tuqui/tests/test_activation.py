"""Tests for the activation handoff: nonce model, /exchange endpoint, gc cron."""

import json
import secrets
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
        # Ensure there's a singleton OAuth client to stage activation against.
        cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
        cls.env["tuqui.oauth.client"].sudo()._get_singleton().write({"state": "pending", "activation_pending": False})

    def setUp(self):
        super().setUp()
        # Each test starts from a clean singleton — HttpCase rolls back only at
        # class teardown, so writes would otherwise leak between tests.
        self.env["tuqui.oauth.client"].sudo()._get_singleton().write(
            {"state": "pending", "workspace_id_external": False, "activation_pending": False}
        )

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

    def test_the_exchange_hands_back_the_pair_that_was_minted_together(self):
        """A rotation between issuing a nonce and redeeming it must not split the
        credentials.

        `/start` rotates the secret and the signing key together, so an older
        nonce that is still redeemable holds a secret from the previous
        rotation. Reading the key live at exchange time would pair that old
        secret with the new key, and Tuqui would store a pair no database ever
        held.
        """
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        nonce, _ = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(
                client_id=client.client_id,
                client_secret_plaintext="secret-of-the-first-attempt",
                event_signing_key="key-of-the-first-attempt",
            )
        )

        # A second activation attempt, abandoned. It rotates both.
        client._rotate_secret_silent()
        client.invalidate_recordset()
        self.assertNotEqual(client.event_signing_key, "key-of-the-first-attempt")

        body = self._post_exchange({"nonce": nonce}, expect_status=200).json()
        self.assertEqual(body["client_secret"], "secret-of-the-first-attempt")
        self.assertEqual(body["event_signing_key"], "key-of-the-first-attempt")

    def test_consuming_a_nonce_wipes_both_credentials(self):
        """The key lives under the same rules as the secret beside it: the row
        stops being useful to anyone who reads the table afterwards."""
        client_id = self.env["tuqui.oauth.client"].sudo()._get_singleton().client_id
        nonce, _ = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(client_id=client_id, client_secret_plaintext="s", event_signing_key="k")
        )
        self._post_exchange({"nonce": nonce}, expect_status=200)

        row = self.env["tuqui.activation.nonce"].sudo().search([("nonce", "=", nonce)])
        self.assertFalse(row.client_secret_plaintext)
        self.assertFalse(row.event_signing_key)

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

        # Side effects: nonce consumed + plaintext nulled; activation staged
        # (activation_pending=True, state unchanged until first /token call).
        row = self.env["tuqui.activation.nonce"].sudo().search([("nonce", "=", nonce)], limit=1)
        row.invalidate_recordset()
        self.assertTrue(row.consumed_at)
        self.assertFalse(row.client_secret_plaintext)
        oauth_client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        oauth_client.invalidate_recordset()
        self.assertTrue(oauth_client.activation_pending)
        self.assertEqual(oauth_client.state, "pending")

    def test_exchange_with_workspace_slug_stores_it(self):
        """When Tuqui sends workspace_slug the field is persisted and
        action_open_tuqui builds a direct /w/<slug> deep-link."""
        client_id = self.env["tuqui.oauth.client"].sudo()._get_singleton().client_id
        nonce, _ = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(
                client_id=client_id,
                client_secret_plaintext="plain-secret-slug-test",
                acting_user_login="admin",
            )
        )

        self._post_exchange({"nonce": nonce, "workspace_slug": "my-workspace"}, expect_status=200)

        oauth_client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        oauth_client.invalidate_recordset()
        self.assertEqual(oauth_client.workspace_id_external, "my-workspace")
        self.assertTrue(oauth_client.activation_pending)
        self.assertEqual(oauth_client.state, "pending")

        # action_open_tuqui must build <base>/w/my-workspace, not the bare base.
        action = oauth_client.action_open_tuqui()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertIn("/w/my-workspace", action["url"])

    def test_exchange_without_workspace_slug_leaves_field_empty(self):
        """An exchange body that omits workspace_slug (backward-compat: older
        Tuqui) must not crash and must leave workspace_id_external unset."""
        client_id = self.env["tuqui.oauth.client"].sudo()._get_singleton().client_id
        nonce, _ = (
            self.env["tuqui.activation.nonce"]
            .sudo()
            ._issue(
                client_id=client_id,
                client_secret_plaintext="plain-secret-no-slug",
                acting_user_login="admin",
            )
        )

        resp = self._post_exchange({"nonce": nonce}, expect_status=200)
        self.assertEqual(resp.status_code, 200, resp.text)

        oauth_client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        oauth_client.invalidate_recordset()
        self.assertFalse(oauth_client.workspace_id_external)
        self.assertTrue(oauth_client.activation_pending)
        self.assertEqual(oauth_client.state, "pending")

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

        Activation is a two-step handshake: /exchange stages the credentials
        (activation_pending=True) and the first /token mint completes it
        (state → 'active'). Re-activation from 'disconnected' follows the same
        path — /exchange sets activation_pending which lets /token through the
        disconnected guard and then flips state to 'active'.
        """
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        client_id = client.client_id

        def _activate(secret):
            # Mirror the real /start flow: write the secret hash before issuing
            # the nonce so /token can verify the same plaintext Tuqui would receive.
            salt = secrets.token_hex(8)
            client.write(
                {
                    "client_secret_hash": client._hash_secret(secret, salt),
                    "client_secret_salt": salt,
                }
            )
            nonce, _ = (
                self.env["tuqui.activation.nonce"]
                .sudo()
                ._issue(client_id=client_id, client_secret_plaintext=secret, acting_user_login="admin")
            )
            self._post_exchange({"nonce": nonce}, expect_status=200)
            client.invalidate_recordset()
            self.assertTrue(client.activation_pending, "exchange must set activation_pending")
            # First token mint: proves Tuqui wired successfully → state flips to active.
            resp = self.url_open(
                "/tuqui/oauth/token",
                data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
                headers=self._db_headers(),
            )
            self.assertEqual(resp.status_code, 200, f"first token after exchange must succeed: {resp.text}")
            client.invalidate_recordset()

        # 1. First activation: pending → active.
        _activate("secret-1")
        self.assertEqual(client.state, "active")

        # 2. Disconnect (manual button / Tuqui-side revoke both land here).
        client.action_disconnect()
        client.invalidate_recordset()
        self.assertEqual(client.state, "disconnected")

        # 3. Re-activation: disconnected → active via exchange + token.
        _activate("secret-2")
        self.assertEqual(client.state, "active", "first token after re-exchange must flip disconnected → active")

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

    def test_start_forwards_admin_email_for_login_prefill(self):
        """/start forwards the activating admin's email so the Tuqui frontend can
        pre-fill its login form when the admin isn't logged into Tuqui yet."""
        self.authenticate("admin", "admin")
        self.env.ref("base.user_admin").write({"email": "admin-prefill@example.com"})
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        client.write({"state": "pending"})
        resp = self.url_open("/tuqui/activation/start", allow_redirects=False, headers=self._db_headers())
        self.assertEqual(resp.status_code, 302, resp.text)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["Location"]).query)
        self.assertEqual(query.get("email", [None])[0], "admin-prefill@example.com")

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

    def test_start_roundtrips_same_origin_referer_as_return_url(self):
        """/start captures the Settings page URL from the Referer header and
        forwards it to Tuqui as return_url so the admin lands back on Settings
        after activation — but only when the Referer shares this Odoo's
        (companion_url) origin. A cross-origin Referer is dropped so it can't be
        abused as an open redirect."""
        self.authenticate("admin", "admin")
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        client.write({"state": "pending"})

        def _start(referer=None):
            headers = self._db_headers()
            if referer is not None:
                headers = {**headers, "Referer": referer}
            resp = self.url_open("/tuqui/activation/start", allow_redirects=False, headers=headers)
            self.assertEqual(resp.status_code, 302, resp.text)
            query = urllib.parse.urlparse(resp.headers["Location"]).query
            return urllib.parse.parse_qs(query)

        # Baseline call tells us this Odoo's own origin (host_url).
        own = urllib.parse.urlparse(_start()["companion_url"][0])
        own_origin = f"{own.scheme}://{own.netloc}"

        # Same-origin Referer → forwarded verbatim as return_url.
        settings_url = f"{own_origin}/odoo/settings"
        self.assertEqual(
            _start(settings_url).get("return_url", [None])[0],
            settings_url,
            "a same-origin Referer must be forwarded as return_url",
        )

        # Cross-origin Referer → dropped (open-redirect guard).
        self.assertNotIn(
            "return_url",
            _start("https://evil.example.com/phish"),
            "a cross-origin Referer must never become return_url",
        )


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiActivationStartAdminGate(HttpCase):
    """Security regression: /tuqui/activation/start is admin-only.

    Minting the activation nonce is the only path to the plaintext credentials
    and to flipping the OAuth client to ``state='active'``. The route is
    ``auth='user'`` AND raises ``AccessError`` unless the caller is in
    ``base.group_system`` (see controllers/activation.py). These tests pin that
    gate so a refactor can't silently let a non-admin mint a nonce.

    Refusal signal mirrors ``test_start_rejects_only_active``: a blocked caller
    must NOT 302-redirect into the activation handshake. An ``AccessError`` from
    an ``auth='user'`` http route surfaces as 403 Forbidden, so we also assert
    that for the authenticated (non-admin) cases.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A singleton in 'pending' would otherwise 302 an admin through — keep one
        # around so the only thing standing between these callers and a redirect
        # is the admin gate itself, not a missing/active client.
        cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
        cls.env["tuqui.oauth.client"].sudo()._get_singleton().write({"state": "pending"})

        # A regular INTERNAL user (NOT in base.group_system). A password is set so
        # self.authenticate() can log in as them. No demo data — created in-test.
        cls.internal_user = cls.env["res.users"].create(
            {
                "name": "Tuqui Activation Non-Admin",
                "login": "tuqui_activation_non_admin",
                "password": "tuqui_activation_non_admin",
                "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
            }
        )
        # A share (portal) user — also outside group_system, exercises the
        # non-internal branch of the same gate.
        cls.portal_user = cls.env["res.users"].create(
            {
                "name": "Tuqui Activation Portal",
                "login": "tuqui_activation_portal",
                "password": "tuqui_activation_portal",
                "group_ids": [(6, 0, [cls.env.ref("base.group_portal").id])],
            }
        )

    def setUp(self):
        super().setUp()
        # Keep the singleton 'pending' between tests — HttpCase only rolls back at
        # class teardown, so a prior test's write would otherwise leak.
        self.env["tuqui.oauth.client"].sudo()._get_singleton().write({"state": "pending"})

    def _db_headers(self):
        return {"X-Odoo-Database": self.env.cr.dbname}

    def _start_status(self):
        resp = self.url_open("/tuqui/activation/start", allow_redirects=False, headers=self._db_headers())
        return resp

    # The AccessError raised by the gate is logged at WARNING by odoo.http —
    # expected here, muted so it doesn't redden runbot.
    @mute_logger("odoo.http")
    def test_start_refuses_regular_internal_user(self):
        """A logged-in internal user without base.group_system must be refused —
        AccessError → 403, and crucially NOT a 302 into the handshake."""
        self.assertTrue(
            not self.internal_user.has_group("base.group_system"),
            "test precondition: the user must NOT be an Odoo administrator",
        )
        self.authenticate("tuqui_activation_non_admin", "tuqui_activation_non_admin")
        resp = self._start_status()
        self.assertNotEqual(resp.status_code, 302, "a non-admin internal user must not reach the activation handshake")
        self.assertEqual(resp.status_code, 403, "the admin gate raises AccessError → 403 Forbidden")

    @mute_logger("odoo.http")
    def test_start_refuses_portal_user(self):
        """A share (portal) user is likewise outside group_system → refused."""
        self.authenticate("tuqui_activation_portal", "tuqui_activation_portal")
        resp = self._start_status()
        self.assertNotEqual(resp.status_code, 302, "a portal user must not reach the activation handshake")
        self.assertEqual(resp.status_code, 403, "the admin gate raises AccessError → 403 Forbidden")

    @mute_logger("odoo.http")
    def test_start_redirects_for_admin(self):
        """Control: an admin IS allowed through (302) — proves the refusals above
        are the gate doing its job, not a singleton/state misconfiguration."""
        self.authenticate("admin", "admin")
        resp = self._start_status()
        self.assertEqual(resp.status_code, 302, resp.text)
