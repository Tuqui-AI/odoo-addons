"""Tests for the OAuth client form refinements: access count + Go to Tuqui."""

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiOAuthClient(TransactionCase):
    """Computed fields and action helpers on tuqui.oauth.client."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.OAuth = cls.env["tuqui.oauth.client"].sudo()
        cls.AccessLog = cls.env["tuqui.access.log"].sudo()
        cls.client = cls.OAuth._get_or_create_singleton()[0]

    def _log(self, *, method="search_read", days_ago=0):
        rec = self.AccessLog.log(method=method, operation_type="read")
        if days_ago:
            # create_date is a magic field — the ORM silently drops writes to
            # it, so backdate straight in SQL and invalidate the cached value.
            backdated = fields.Datetime.subtract(fields.Datetime.now(), days=days_ago)
            self.env.cr.execute(
                "UPDATE tuqui_access_log SET create_date = %s WHERE id = %s",
                (backdated, rec.id),
            )
            rec.invalidate_recordset(["create_date"])
        return rec

    def test_access_count_7d_counts_recent(self):
        self.AccessLog.search([]).unlink()
        self._log()
        self._log(days_ago=3)
        self._log(days_ago=7)
        self.client.invalidate_recordset()
        self.assertEqual(self.client.access_count_7d, 3)

    def test_access_count_7d_excludes_old(self):
        self.AccessLog.search([]).unlink()
        self._log()
        self._log(days_ago=8)
        self._log(days_ago=30)
        self.client.invalidate_recordset()
        self.assertEqual(self.client.access_count_7d, 1)

    def _set_base_url(self, value):
        self.env["ir.config_parameter"].sudo().set_param("tuqui.base_url", value)

    def test_open_tuqui_with_workspace_links_to_w_slug(self):
        self._set_base_url("https://tuqui.com")
        self.client.write({"workspace_id_external": "acme-sa"})
        action = self.client.action_open_tuqui()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertEqual(action["target"], "new")
        self.assertEqual(action["url"], "https://tuqui.com/w/acme-sa")

    def test_open_tuqui_without_workspace_falls_back_to_base(self):
        self._set_base_url("https://tuqui.com")
        self.client.write({"workspace_id_external": False})
        action = self.client.action_open_tuqui()
        self.assertEqual(action["url"], "https://tuqui.com")

    def test_open_tuqui_defaults_to_production_url_without_param(self):
        self.env["ir.config_parameter"].sudo().search([("key", "=", "tuqui.base_url")]).unlink()
        self.client.write({"workspace_id_external": "acme-sa"})
        action = self.client.action_open_tuqui()
        self.assertEqual(action["url"], "https://tuqui.com/w/acme-sa")

    def test_open_tuqui_strips_trailing_slash_and_url_encodes_slug(self):
        self._set_base_url("https://tuqui.com/")
        self.client.write({"workspace_id_external": "team a/b"})
        action = self.client.action_open_tuqui()
        self.assertEqual(action["url"], "https://tuqui.com/w/team%20a%2Fb")
