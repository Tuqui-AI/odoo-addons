"""Tests for the Tuqui block in General Settings.

The settings transient is the only admin surface of the module (no
menus): it has to reflect the connection state, round-trip the read-only
flag to the tuqui.oauth.client singleton, and proxy the lifecycle
actions correctly.
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

    def test_read_only_roundtrip(self):
        client = self.OAuth._get_or_create_singleton()[0]
        # read_only now defaults ON; the round-trip toggles it off and back on.
        self.assertTrue(client.read_only)

        settings = self._settings()
        settings.tuqui_read_only = False
        settings.set_values()
        self.assertFalse(client.read_only)

        # And back on.
        settings = self._settings()
        settings.tuqui_read_only = True
        settings.set_values()
        self.assertTrue(client.read_only)

    def test_get_values_reads_read_only_from_singleton(self):
        client = self.OAuth._get_or_create_singleton()[0]
        client.write({"read_only": True})
        values = self.Settings.get_values()
        self.assertTrue(values["tuqui_read_only"])

    def test_set_values_without_singleton_is_noop(self):
        """Toggling read-only before activation has nothing to write to."""
        self.OAuth.search([]).unlink()
        settings = self._settings()
        settings.tuqui_read_only = True
        settings.set_values()  # must not raise / must not create a client
        self.assertFalse(self.OAuth.search([]))

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
