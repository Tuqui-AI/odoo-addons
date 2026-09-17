"""Contract of the public ``/tuqui/health`` endpoint.

``/tuqui/health`` is ``auth='none'`` — Tuqui pings it pre-auth during cold-start
detect to confirm the module is present and negotiate the protocol. This suite
pins the body it advertises, and (per F2) asserts that ``db_name`` is NOT in it:
the database name is a sensitive fingerprint and must not be exposed on a public
endpoint. The assertion doubles as a regression guard so nobody reintroduces it.

``warnings`` is the other half: what breaks in silence has to be visible
somewhere, and this is the endpoint an administrator (and Tuqui) already look
at. Same rule as ``db_name``, though: the text says what is wrong and what to
do, never which models the database has.
"""

from datetime import timedelta

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiHealth(HttpCase):
    """The health probe is public and advertises a stable contract."""

    def _db_headers(self):
        return {"X-Odoo-Database": self.env.cr.dbname}

    def _body(self):
        self.env.flush_all()
        resp = self.url_open("/tuqui/health", headers=self._db_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _enable_search(self, model="res.partner", field_name="name"):
        """Enable relevance search on a model, as an administrator would."""
        Config = self.env["search.relevant.config"]
        field = self.env["ir.model.fields"].search([("model", "=", model), ("name", "=", field_name)], limit=1)
        config = Config.with_context(active_test=False).search([("model", "=", model)], limit=1)
        if config:
            config.write({"active": True, "field_ids": [(6, 0, field.ids)]})
            return config
        return Config.create({"model_id": self.env["ir.model"]._get_id(model), "field_ids": [(6, 0, field.ids)]})

    def test_health_is_public_and_advertises_contract(self):
        resp = self.url_open("/tuqui/health", headers=self._db_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIs(body["ok"], True)
        self.assertEqual(body["module"], "tuqui")
        self.assertEqual(body["protocol_version"], "2.0")
        self.assertTrue(body["module_version"])
        self.assertTrue(body["odoo_version"])
        for cap in ("rpc.execute_kw", "access_log", "search_relevant"):
            self.assertIn(cap, body["capabilities"])
        self.assertEqual(body["warnings"], [], "nothing enabled, nothing to warn about")
        # F2: the database name must NOT be exposed on the public endpoint.
        self.assertNotIn("db_name", body)

    def test_a_search_index_that_fell_behind_is_warned_about(self):
        """The cron that keeps the index current can stop without a trace: Odoo
        deactivates it after repeated failures, or no worker runs crons. From
        then on every search answers ``partial`` and nobody is told."""
        config = self._enable_search()
        self.assertEqual(self._body()["warnings"], [], "a fresh index warns about nothing")

        config.sync_watermark = self.env.cr.now() - timedelta(hours=2)
        (warning,) = self._body()["warnings"]
        self.assertIn("relevance search index", warning)
        self.assertIn("Scheduled Actions", warning, "the warning says what to do")
        self.assertNotIn(config.model, warning, "and never which models this database has")

    def test_a_scheduled_action_that_was_turned_off_is_warned_about(self):
        self._enable_search()
        self.env.ref("base_search_relevant.ir_cron_search_relevant_index").active = False
        (warning,) = self._body()["warnings"]
        self.assertIn("is off", warning)
