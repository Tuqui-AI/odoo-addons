"""Tests for the Tuqui block in General Settings.

The settings transient reflects the connection state and proxies the
lifecycle actions (activate, open workspace, rotate secret, disconnect).
"""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiResConfigSettings(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Settings = cls.env["res.config.settings"]
        cls.OAuth = cls.env["tuqui.oauth.client"].sudo()

    def _settings(self):
        return self.Settings.create({})

    def test_state_defaults_to_pending_without_singleton(self):
        self.OAuth.search([]).unlink()
        settings = self._settings()
        self.assertEqual(settings.tuqui_state, "pending")
        self.assertFalse(settings.tuqui_last_seen_at)
        self.assertEqual(settings.tuqui_access_count_7d, 0)

    def test_state_reflects_active_singleton(self):
        client = self.OAuth._get_or_create_singleton()[0]
        client.mark_active(workspace_id_external="acme")
        settings = self._settings()
        self.assertEqual(settings.tuqui_state, "active")

    def test_activate_action_targets_start_route(self):
        settings = self._settings()
        action = settings.action_tuqui_activate()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertEqual(action["url"], "/tuqui/activation/start")
        # Same tab so the start route can capture the Settings Referer and
        # Tuqui can redirect the admin back here after activation.
        self.assertEqual(action["target"], "self")

    def test_open_access_log_action_resolves(self):
        settings = self._settings()
        log_action = settings.action_tuqui_open_access_log()
        self.assertEqual(log_action["res_model"], "tuqui.access.log")

    def test_disconnect_proxies_to_singleton(self):
        client = self.OAuth._get_or_create_singleton()[0]
        client.mark_active()
        settings = self._settings()
        settings.action_tuqui_disconnect()
        self.assertEqual(client.state, "disconnected")
