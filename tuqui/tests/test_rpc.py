import json
import secrets

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiRpc(HttpCase):
    """End-to-end coverage of /tuqui/rpc — one test per allowed operation
    plus the perimeter (auth, allowlist, acting_user, error mapping)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        client = cls.env["tuqui.oauth.client"].sudo()._get_singleton()
        if not client:
            client, _ = cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
        # Rotate so we hold a known plaintext secret for the test run.
        plain = secrets.token_urlsafe(48)
        salt = secrets.token_hex(16)
        client.write(
            {
                "client_secret_hash": client._hash_secret(plain, salt),
                "client_secret_salt": salt,
            }
        )
        cls.client_id = client.client_id
        cls.client_secret = plain

        # Non-admin user used to exercise AccessError → 403 access_denied.
        cls.basic_user = cls.env["res.users"].create(
            {
                "name": "Tuqui Basic Test",
                "login": "tuqui_basic_test",
                "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
            }
        )

    def _db_headers(self):
        # Multiple databases can match the configured dbfilter; pin every
        # request to the test DB so route resolution doesn't 404.
        return {"X-Odoo-Database": self.env.cr.dbname}

    def _get_token(self):
        resp = self.url_open(
            "/tuqui/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers=self._db_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["access_token"]

    def _rpc(self, operation, params, acting_user="admin", token=None, status=None):
        token = token or self._get_token()
        resp = self.url_open(
            "/tuqui/rpc",
            data=json.dumps({"operation": operation, "params": params}),
            headers={
                **self._db_headers(),
                "Authorization": f"Bearer {token}",
                "X-Tuqui-Acting-User": acting_user,
                "Content-Type": "application/json",
            },
        )
        if status is not None:
            self.assertEqual(resp.status_code, status, resp.text)
        return resp

    # ---------- Happy paths (one per operation) ----------

    def test_search_read(self):
        resp = self._rpc(
            "search_read",
            {"model": "res.partner", "domain": [], "fields": ["name"], "limit": 2},
            status=200,
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["data"], list)
        self.assertLessEqual(len(body["data"]), 2)
        self.assertEqual(body["record_count"], len(body["data"]))

    def test_read(self):
        partner = self.env["res.partner"].search([], limit=1)
        self.assertTrue(partner)
        resp = self._rpc(
            "read",
            {"model": "res.partner", "ids": [partner.id], "fields": ["name"]},
            status=200,
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"][0]["id"], partner.id)

    def test_fields_get(self):
        resp = self._rpc(
            "fields_get",
            {"model": "res.partner", "allfields": ["name"], "attributes": ["string", "type"]},
            status=200,
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIn("name", body["data"])
        self.assertEqual(body["data"]["name"]["type"], "char")

    def test_name_search(self):
        resp = self._rpc(
            "name_search",
            {"model": "res.partner", "name": "", "limit": 5},
            status=200,
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        # Each row is [id, display_name]
        for row in body["data"]:
            self.assertEqual(len(row), 2)
            self.assertIsInstance(row[0], int)
            self.assertIsInstance(row[1], str)

    def test_read_group(self):
        resp = self._rpc(
            "read_group",
            {
                "model": "res.partner",
                "domain": [],
                "fields": ["id:count"],
                "groupby": ["parent_id"],
            },
            status=200,
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["data"], list)

    def test_name_get(self):
        partner = self.env["res.partner"].search([], limit=1)
        resp = self._rpc(
            "name_get",
            {"model": "res.partner", "ids": [partner.id]},
            status=200,
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"][0][0], partner.id)
        self.assertEqual(body["data"][0][1], partner.display_name)

    def test_model_list(self):
        resp = self._rpc("model_list", {}, status=200)
        body = resp.json()
        self.assertTrue(body["ok"])
        models = {row["model"] for row in body["data"]}
        # res.partner is read-accessible to admin
        self.assertIn("res.partner", models)

    # ---------- Negative paths (perimeter) ----------

    def test_missing_bearer(self):
        resp = self.url_open(
            "/tuqui/rpc",
            data=json.dumps({"operation": "search_read", "params": {"model": "res.partner"}}),
            headers={**self._db_headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "unauthorized")

    def test_invalid_bearer(self):
        resp = self.url_open(
            "/tuqui/rpc",
            data=json.dumps({"operation": "search_read", "params": {}}),
            headers={
                **self._db_headers(),
                "Authorization": "Bearer not.a.real.jwt",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(resp.status_code, 401)

    def test_operation_not_allowed(self):
        resp = self._rpc("unlink", {"model": "res.partner", "ids": [1]}, status=403)
        self.assertEqual(resp.json()["error"]["code"], "operation_not_allowed")

    def test_unknown_acting_user(self):
        resp = self._rpc(
            "search_read",
            {"model": "res.partner"},
            acting_user="ghost",
            status=400,
        )
        self.assertEqual(resp.json()["error"]["code"], "unknown_acting_user")

    def test_validation_missing_model(self):
        resp = self._rpc("search_read", {}, status=400)
        self.assertEqual(resp.json()["error"]["code"], "validation_error")

    def test_validation_unknown_model(self):
        resp = self._rpc(
            "search_read",
            {"model": "does.not.exist"},
            status=400,
        )
        self.assertEqual(resp.json()["error"]["code"], "validation_error")

    def test_internal_error_does_not_leak_exception_details(self):
        """A 500 must surface a generic message — no SQL fragments, no
        ValueError repr — even when the underlying exception is descriptive."""
        # Grouping by a non-stored computed field raises ValueError inside
        # the ORM ('Cannot convert <field> to SQL because it is not stored').
        resp = self._rpc(
            "read_group",
            {
                "model": "res.partner",
                "domain": [],
                "fields": [],
                "groupby": ["company_type"],  # non-stored selection
            },
            status=500,
        )
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "internal_error")
        msg = body["error"]["message"]
        # Must be the generic copy, not the underlying ORM message.
        for fragment in ("Cannot convert", "SQL", "company_type", "ValueError", "Traceback"):
            self.assertNotIn(fragment, msg, f"500 response leaked {fragment!r}: {msg!r}")

    def test_access_denied_for_basic_user(self):
        """Basic (non-system) user should not be able to read ir.config_parameter
        — the dispatcher must surface the AccessError as 403 access_denied."""
        resp = self._rpc(
            "search_read",
            {"model": "ir.config_parameter", "fields": ["key"]},
            acting_user=self.basic_user.login,
            status=403,
        )
        self.assertEqual(resp.json()["error"]["code"], "access_denied")
