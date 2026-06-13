"""Tests for the Tuqui block in General Settings.

The settings transient is the only admin surface of the module (no
menus): it has to reflect the connection state, round-trip the security
policy to the tuqui.rpc.policy singleton, and proxy the lifecycle
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
        cls.Policy = cls.env["tuqui.rpc.policy"]

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

    def test_policy_mode_roundtrip(self):
        policy = self.Policy._get_singleton()
        self.assertEqual(policy.policy_mode, "default")

        settings = self._settings()
        settings.tuqui_policy_mode = "advanced"
        settings.tuqui_allow_private_methods = True
        settings.set_values()

        self.assertEqual(policy.policy_mode, "advanced")
        self.assertTrue(policy.allow_private_methods)

        # And back — the constraint clears allow_private outside advanced,
        # so set_values must write both fields in one call.
        settings = self._settings()
        settings.tuqui_policy_mode = "default"
        settings.tuqui_allow_private_methods = False
        settings.set_values()
        self.assertEqual(policy.policy_mode, "default")
        self.assertFalse(policy.allow_private_methods)

    def test_get_values_reads_policy_singleton(self):
        policy = self.Policy._get_singleton()
        policy.write({"policy_mode": "advanced", "allow_private_methods": True})
        values = self.Settings.get_values()
        self.assertEqual(values["tuqui_policy_mode"], "advanced")
        self.assertTrue(values["tuqui_allow_private_methods"])

    def test_activate_action_targets_start_route(self):
        settings = self._settings()
        action = settings.action_tuqui_activate()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertEqual(action["url"], "/tuqui/activation/start")
        self.assertEqual(action["target"], "new")

    def test_open_rules_and_access_log_actions_resolve(self):
        settings = self._settings()
        rules_action = settings.action_tuqui_open_rules()
        self.assertEqual(rules_action["res_model"], "tuqui.rpc.rule")
        log_action = settings.action_tuqui_open_access_log()
        self.assertEqual(log_action["res_model"], "tuqui.access.log")

    def test_disconnect_proxies_to_singleton(self):
        client = self.OAuth._get_or_create_singleton()[0]
        client.mark_active()
        settings = self._settings()
        settings.action_tuqui_disconnect()
        self.assertEqual(client.state, "disconnected")
