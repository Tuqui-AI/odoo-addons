"""Tests for the chat-access sync that gates the systray icon.

Spec: ``systray-solo-para-usuarios-con-chat``. All data is built with
``.create()`` — no demo data. The HTTP call to Tuqui is patched at
``requests.get``, which is also how we assert the thing that matters most about
cost: ONE request per run, never one per user.

The failure mode worth protecting is not "the sync is wrong" but "the sync
revoked everyone because Tuqui hiccupped", so most of these are about what does
NOT get written.
"""

import json
from unittest.mock import patch

from odoo.tests import HttpCase, TransactionCase, tagged


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@tagged("post_install", "-at_install", "tuqui_assistant")
class TestTuquiChatAccessSync(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Users = cls.env["res.users"]
        client, _secret = cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
        client.write({"state": "active", "workspace_id_external": "test-workspace"})
        cls.oauth_client = client

    def _user(self, login, *, has_chat=False):
        user = self.Users.create({"name": login, "login": login})
        # A SaaS-managed base forces new users inactive and ignores ``active`` in
        # the create values, so activating takes a separate write. Without it,
        # test_archived_user_loses_the_flag would pass on a fixture that was
        # archived from birth — testing nothing.
        user.write({"active": True})
        if has_chat:
            user.tuqui_has_chat = True
        return user

    def _sync(self, payload=None, *, status_code=200, side_effect=None):
        """Run the sync with a patched Tuqui, returning (result, call_count)."""
        kwargs = {"side_effect": side_effect} if side_effect else {"return_value": _Resp(payload, status_code)}
        with patch("odoo.addons.tuqui_assistant.models.res_users.requests.get", **kwargs) as mocked:
            result = self.Users._tuqui_sync_chat_access()
        return result, mocked.call_count

    # ---- The happy path ----

    def test_grants_and_revokes_from_the_roster(self):
        granted = self._user("tuqui-grant@example.com")
        revoked = self._user("tuqui-revoke@example.com", has_chat=True)

        result, calls = self._sync({"odoo_uids": [granted.id]})

        self.assertTrue(result)
        self.assertEqual(calls, 1, "one HTTP call per run, not one per user")
        self.assertTrue(granted.tuqui_has_chat)
        self.assertFalse(revoked.tuqui_has_chat)

    def test_one_call_regardless_of_how_many_users(self):
        """The whole reason for a roster endpoint instead of a per-user check."""
        users = [self._user(f"tuqui-many-{i}@example.com") for i in range(8)]

        _result, calls = self._sync({"odoo_uids": [user.id for user in users]})

        self.assertEqual(calls, 1)
        self.assertTrue(all(user.tuqui_has_chat for user in users))

    def test_empty_roster_revokes_everyone(self):
        user = self._user("tuqui-lonely@example.com", has_chat=True)

        result, _calls = self._sync({"odoo_uids": []})

        self.assertTrue(result)
        self.assertFalse(user.tuqui_has_chat)

    def test_unknown_uid_is_ignored(self):
        """Tuqui may name a uid this database doesn't have (restored backup)."""
        result, _calls = self._sync({"odoo_uids": [999999]})

        self.assertTrue(result, "an unknown uid is not an error")

    def test_archived_user_loses_the_flag(self):
        user = self._user("tuqui-archived@example.com", has_chat=True)
        user.active = False

        self._sync({"odoo_uids": []})

        self.assertFalse(
            user.with_context(active_test=False).tuqui_has_chat,
            "an archived user must be reachable by the revoke pass",
        )

    # ---- Fail-open: nobody loses the icon over a network problem ----

    def test_unreachable_tuqui_keeps_the_last_known_state(self):
        seated = self._user("tuqui-keeps@example.com", has_chat=True)

        result, _calls = self._sync(side_effect=RuntimeError("connection refused"))

        self.assertFalse(result, "the sync reports it could not refresh")
        self.assertTrue(seated.tuqui_has_chat, "a network error must not revoke anyone")

    def test_server_error_keeps_the_last_known_state(self):
        seated = self._user("tuqui-500@example.com", has_chat=True)

        result, _calls = self._sync({"odoo_uids": []}, status_code=500)

        self.assertFalse(result)
        self.assertTrue(seated.tuqui_has_chat)

    def test_rate_limited_keeps_the_last_known_state(self):
        """Being throttled is not evidence that somebody lost their seat."""
        seated = self._user("tuqui-429@example.com", has_chat=True)

        result, _calls = self._sync({"odoo_uids": []}, status_code=429)

        self.assertFalse(result)
        self.assertTrue(seated.tuqui_has_chat)

    def test_malformed_payload_keeps_the_last_known_state(self):
        seated = self._user("tuqui-garbage@example.com", has_chat=True)

        for payload in ({}, {"odoo_uids": None}, {"odoo_uids": "7"}, {"odoo_uids": [True]}, {"odoo_uids": ["7"]}):
            with self.subTest(payload=payload):
                result, _calls = self._sync(payload)
                self.assertFalse(result, f"{payload!r} must not be trusted")
                self.assertTrue(seated.tuqui_has_chat)

    # ---- A disconnected companion is local truth, not a failure ----

    def test_disconnected_companion_revokes_without_calling_tuqui(self):
        seated = self._user("tuqui-disconnected@example.com", has_chat=True)
        self.oauth_client.write({"state": "disconnected"})

        result, calls = self._sync({"odoo_uids": [seated.id]})

        self.assertTrue(result)
        self.assertEqual(calls, 0, "nothing to ask: we know locally that nobody has access")
        self.assertFalse(seated.tuqui_has_chat)

    def test_action_disconnect_revokes_immediately(self):
        """Waiting for the cron would leave 15 minutes of icons leading nowhere."""
        seated = self._user("tuqui-onduty@example.com", has_chat=True)

        self.oauth_client.action_disconnect()

        self.assertFalse(seated.tuqui_has_chat)

    # ---- Triggers, so nobody waits 15 minutes ----

    def test_mark_active_triggers_the_cron_without_calling_tuqui(self):
        """It runs inside the activation handshake: an HTTP call here would put a
        round-trip on that path and, if it raised, break the activation."""
        self.oauth_client.write({"state": "pending"})
        cron = self.env.ref("tuqui_assistant.ir_cron_sync_chat_members")
        before = self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)])

        with patch("odoo.addons.tuqui_assistant.models.res_users.requests.get") as mocked:
            self.oauth_client.mark_active(workspace_id_external="test-workspace")

        self.assertEqual(mocked.call_count, 0, "activation must not depend on Tuqui answering")
        self.assertGreater(
            self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)]),
            before,
            "activation should schedule the sync for the next worker wake",
        )

    def test_settings_button_reloads_on_success_and_warns_on_failure(self):
        settings = self.env["res.config.settings"].create({})

        with patch(
            "odoo.addons.tuqui_assistant.models.res_users.requests.get",
            return_value=_Resp({"odoo_uids": []}),
        ):
            self.assertEqual(settings.action_tuqui_sync_chat_members()["tag"], "reload")

        with patch(
            "odoo.addons.tuqui_assistant.models.res_users.requests.get",
            side_effect=RuntimeError("down"),
        ):
            action = settings.action_tuqui_sync_chat_members()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "warning")


@tagged("post_install", "-at_install", "tuqui_assistant")
class TestTuquiChatAccessSessionInfo(HttpCase):
    """What the systray actually reads, over a real request.

    ``session_info`` needs a bound ``request``, so this can't be a
    ``TransactionCase`` — and going through the route is the more honest test
    anyway: it exercises the whole ``ir.http`` override chain (this database
    stacks eight of them) exactly as a page load does.
    """

    # Reuses the admin instead of creating a fixture user, and that is not
    # laziness: on a SaaS-managed base (which is where this module lives) a
    # created user comes out with active=False — ``active`` in the create values
    # is ignored — and authenticating as one fails with "Login failed". The admin
    # authenticates fine, so the login goes through it.
    _PASSWORD = "chat-access-probe-password"

    def setUp(self):
        super().setUp()
        self.user = self.env.ref("base.user_admin")
        self.user.password = self._PASSWORD

    def _session_info(self):
        self.authenticate(self.user.login, self._PASSWORD)
        resp = self.url_open(
            "/web/session/get_session_info",
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["result"]

    def test_session_info_exposes_the_flag_for_a_seated_user(self):
        self.user.tuqui_has_chat = True

        self.assertIs(self._session_info()["tuqui_has_chat"], True)

    def test_session_info_is_false_without_a_seat(self):
        self.user.tuqui_has_chat = False

        self.assertIs(
            self._session_info()["tuqui_has_chat"],
            False,
            "always present and always a boolean, so the JS side has one thing to check",
        )
