"""Contract of the public ``/tuqui/health`` endpoint.

``/tuqui/health`` is ``auth='none'`` — Tuqui pings it pre-auth during cold-start
detect to confirm the module is present and negotiate the protocol. This suite
pins the body it advertises, and (per F2) asserts that ``db_name`` is NOT in it:
the database name is a sensitive fingerprint and must not be exposed on a public
endpoint. The assertion doubles as a regression guard so nobody reintroduces it.
"""

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiHealth(HttpCase):
    """The health probe is public and advertises a stable contract."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()[0]

    def _db_headers(self):
        return {"X-Odoo-Database": self.env.cr.dbname}

    def _capabilities(self):
        resp = self.url_open("/tuqui/health", headers=self._db_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["capabilities"]

    def test_health_is_public_and_advertises_contract(self):
        resp = self.url_open("/tuqui/health", headers=self._db_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIs(body["ok"], True)
        self.assertEqual(body["module"], "tuqui")
        self.assertEqual(body["protocol_version"], "2.0")
        self.assertTrue(body["module_version"])
        self.assertTrue(body["odoo_version"])
        for cap in ("rpc.execute_kw", "access_log"):
            self.assertIn(cap, body["capabilities"])
        # F2: the database name must NOT be exposed on the public endpoint.
        self.assertNotIn("db_name", body)

    def test_read_only_capability_reflects_the_switch(self):
        """``policy.read_only`` is the STATE of the switch, not a feature flag.

        Advertising it unconditionally is what made Tuqui show "writing is
        disabled" — and lock the write whitelist — on databases whose admin had
        already turned read-only off here.
        """
        self.client.write({"read_only": True})
        self.assertIn("policy.read_only", self._capabilities())

        self.client.write({"read_only": False})
        self.assertNotIn("policy.read_only", self._capabilities())
