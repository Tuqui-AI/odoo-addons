import json
import secrets

from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger

# ─── Helpers shared by the HTTP suite ────────────────────────────────


def _rotate_oauth_secret(env):
    """Reset the OAuth singleton secret to a known plaintext for the run."""
    client = env["tuqui.oauth.client"].sudo()._get_singleton()
    if not client:
        client, _ = env["tuqui.oauth.client"].sudo()._get_or_create_singleton()
    plain = secrets.token_urlsafe(48)
    salt = secrets.token_hex(16)
    client.write(
        {
            "client_secret_hash": client._hash_secret(plain, salt),
            "client_secret_salt": salt,
        }
    )
    return client.client_id, plain


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiRpcGateway(HttpCase):
    """End-to-end coverage of the 2.0 ``/tuqui/rpc`` gateway.

    Each test starts from a known baseline (default policy mode, no rules,
    fresh access log) and asserts a specific behavior. Tests mutate policy
    state freely — the HttpCase savepoint rolls back at class teardown so
    nothing leaks across test classes.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client_id, cls.client_secret = _rotate_oauth_secret(cls.env)
        cls.basic_user = cls.env["res.users"].create(
            {
                "name": "Tuqui Basic Test",
                "login": "tuqui_basic_test",
                "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
            }
        )

    # ─── Test bookkeeping ────────────────────────────────────────────

    def setUp(self):
        super().setUp()
        # Reset the connection to a known baseline: read-only off.
        self.client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        if self.client.read_only:
            self.client.write({"read_only": False})

    # ─── HTTP helpers ────────────────────────────────────────────────

    def _db_headers(self):
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

    def _rpc(
        self,
        model=None,
        method=None,
        args=None,
        kwargs=None,
        context=None,
        acting_user="admin",
        acting_uid=None,
        token=None,
        body_override=None,
        expect_status=None,
    ):
        """Invoke ``/tuqui/rpc`` and return the response.

        Pass ``body_override`` to send a malformed body for perimeter tests;
        otherwise the body is built from the named params. Pass ``acting_uid``
        to impersonate by Odoo user id (``X-Tuqui-Acting-Uid``) instead of by
        login — the per-member path.
        """
        token = token or self._get_token()
        if body_override is not None:
            body = body_override
        else:
            body = {"model": model, "method": method}
            if args is not None:
                body["args"] = args
            if kwargs is not None:
                body["kwargs"] = kwargs
            if context is not None:
                body["context"] = context
        headers = {
            **self._db_headers(),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if acting_uid is not None:
            headers["X-Tuqui-Acting-Uid"] = str(acting_uid)
        else:
            headers["X-Tuqui-Acting-User"] = acting_user
        resp = self.url_open(
            "/tuqui/rpc",
            data=json.dumps(body),
            headers=headers,
        )
        if expect_status is not None:
            self.assertEqual(resp.status_code, expect_status, resp.text)
        return resp

    def _latest_log(self, **filters):
        domain = [(k, "=", v) for k, v in filters.items()]
        return self.env["tuqui.access.log"].sudo().search(domain, order="id desc", limit=1)

    # ─── Default mode classification ─────────────────────────────────

    def test_default_mode_routes_read_write_execute(self):
        """Default mode: reads/writes/executes pass to the ORM (subject to ACL)."""
        # read
        resp = self._rpc(
            "res.partner", "search_read", args=[[]], kwargs={"fields": ["name"], "limit": 2}, expect_status=200
        )
        self.assertTrue(resp.json()["ok"])

        # write — admin has create rights on res.partner
        resp = self._rpc("res.partner", "create", args=[{"name": "TC_create"}], expect_status=200)
        new_id = resp.json()["data"]
        self.assertIsInstance(new_id, int)

        # execute — action_archive on the record we just created
        resp = self._rpc("res.partner", "action_archive", args=[[new_id]], expect_status=200)
        self.assertTrue(resp.json()["ok"])

    def test_default_mode_blocks_private(self):
        resp = self._rpc("res.partner", "_compute_display_name", args=[[1]], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "private_method_blocked")

    def test_absolute_blocks(self):
        for method in ("sudo", "with_user", "with_env", "with_company"):
            resp = self._rpc("res.partner", method, args=[], expect_status=403)
            self.assertEqual(
                resp.json()["error"]["code"],
                "method_blocked",
                f"{method} should be hardblocked",
            )
        for method in ("flush_recordset", "invalidate_cache", "flush_all", "invalidate_model"):
            resp = self._rpc("res.partner", method, args=[], expect_status=403)
            self.assertEqual(resp.json()["error"]["code"], "method_blocked")
        for method in ("__class__", "__getattribute__", "__reduce__"):
            resp = self._rpc("res.partner", method, args=[], expect_status=403)
            self.assertEqual(
                resp.json()["error"]["code"],
                "method_blocked",
                f"dunder {method} should be hardblocked",
            )

    # ─── Read-only mode ──────────────────────────────────────────────

    def test_read_only_mode_blocks_writes_and_executes_but_allows_reads(self):
        """read_only=True: create/execute are refused; reads still pass —
        including ``formatted_read_group``, which doesn't match the search/read
        prefix and would be misclassified as ``execute`` without the explicit
        entry in ``_READ_METHODS``."""
        self.client.write({"read_only": True})

        # write blocked
        resp = self._rpc("res.partner", "create", args=[{"name": "blocked"}], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "read_only_mode")

        # execute (arbitrary business method) blocked
        resp = self._rpc("res.partner", "action_archive", args=[[1]], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "read_only_mode")

        # plain read works
        resp = self._rpc("res.partner", "search_read", args=[[]], kwargs={"limit": 1}, expect_status=200)
        self.assertTrue(resp.json()["ok"])

        # grouped read works too — formatted_read_group is a read, not an execute
        resp = self._rpc(
            "res.partner",
            "formatted_read_group",
            args=[[], ["is_company"], ["__count"]],
            expect_status=200,
        )
        self.assertTrue(resp.json()["ok"])

    def test_read_only_mode_still_hard_blocks_private_and_escape_hatches(self):
        """The unconditional blocks take precedence over the read_only reason."""
        self.client.write({"read_only": True})
        resp = self._rpc("res.partner", "_compute_display_name", args=[[1]], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "private_method_blocked")
        resp = self._rpc("res.partner", "sudo", args=[], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "method_blocked")

    # ─── Perimeter ───────────────────────────────────────────────────

    def test_missing_bearer(self):
        resp = self.url_open(
            "/tuqui/rpc",
            data=json.dumps({"model": "res.partner", "method": "search_read"}),
            headers={**self._db_headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "unauthorized")

    def test_invalid_bearer(self):
        resp = self.url_open(
            "/tuqui/rpc",
            data=json.dumps({"model": "res.partner", "method": "search_read"}),
            headers={
                **self._db_headers(),
                "Authorization": "Bearer not.a.real.jwt",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(resp.status_code, 401)

    def test_bad_request_shapes(self):
        # Missing model
        resp = self._rpc(method="search_read", expect_status=400)
        self.assertEqual(resp.json()["error"]["code"], "bad_request")
        # Missing method
        resp = self._rpc(model="res.partner", expect_status=400)
        self.assertEqual(resp.json()["error"]["code"], "bad_request")
        # args not list
        resp = self._rpc(
            model="res.partner",
            method="search_read",
            body_override={"model": "res.partner", "method": "search_read", "args": "not-a-list"},
            expect_status=400,
        )
        self.assertEqual(resp.json()["error"]["code"], "bad_request")
        # Unknown model
        resp = self._rpc("does.not.exist", "search_read", args=[[]], expect_status=400)
        self.assertEqual(resp.json()["error"]["code"], "validation_error")

    def test_unknown_acting_user(self):
        resp = self._rpc("res.partner", "search_read", args=[[]], acting_user="ghost", expect_status=400)
        self.assertEqual(resp.json()["error"]["code"], "unknown_acting_user")

    def test_unknown_acting_uid(self):
        resp = self._rpc("res.partner", "search_read", args=[[]], acting_uid=99999999, expect_status=400)
        self.assertEqual(resp.json()["error"]["code"], "unknown_acting_user")

    # ─── Per-member impersonation by uid ─────────────────────────────

    def test_acting_uid_impersonates_that_user(self):
        """X-Tuqui-Acting-Uid runs the call under that user's own ACL.

        The basic user can't read ir.config_parameter → AccessError → 403,
        proving the call ran as the uid we sent and not as admin.
        """
        resp = self._rpc(
            "ir.config_parameter",
            "search_read",
            args=[[]],
            kwargs={"fields": ["key"]},
            acting_uid=self.basic_user.id,
            expect_status=403,
        )
        self.assertEqual(resp.json()["error"]["code"], "access_denied")
        # And the audit log attributes the call to that user, not admin.
        log = self._latest_log(method="search_read", model_name="ir.config_parameter")
        self.assertEqual(log.acting_user_id.id, self.basic_user.id)

    # ─── Error mapping ───────────────────────────────────────────────

    def test_access_error_maps_to_403(self):
        """Basic user without system rights can't read ir.config_parameter — AccessError → 403."""
        resp = self._rpc(
            "ir.config_parameter",
            "search_read",
            args=[[]],
            kwargs={"fields": ["key"]},
            acting_user=self.basic_user.login,
            expect_status=403,
        )
        self.assertEqual(resp.json()["error"]["code"], "access_denied")

    @mute_logger("odoo.addons.tuqui.controllers.rpc")
    def test_internal_error_does_not_leak_details(self):
        """A 500 must surface a generic message — no SQL, no ValueError repr.

        The controller is expected to call ``_LOG.exception(...)`` server-side
        before returning the generic response; muting the logger here keeps
        runbot's red-on-any-ERROR signal honest (this isn't a real failure,
        it's the test verifying the failure path).
        """
        # Grouping by a non-stored field raises ValueError inside the ORM.
        # We use ``formatted_read_group`` (the Odoo 19 replacement) rather than
        # the deprecated ``read_group``: the latter emits a DeprecationWarning
        # through the ``py.warnings`` logger, which ``@mute_logger`` above does
        # not silence (it targets the controller logger), dirtying the runbot
        # log. ``_read_group`` is not an option here — its ``_`` prefix makes
        # the gateway refuse it as a private method (403), so it never reaches
        # the ORM error path this test exercises. Args: (domain, groupby,
        # aggregates) — same signature the CompanionTransport sends.
        resp = self._rpc(
            "res.partner",
            "formatted_read_group",
            args=[[], ["company_type"], []],  # group by non-stored selection
            expect_status=500,
        )
        body = resp.json()
        self.assertEqual(body["error"]["code"], "internal_error")
        msg = body["error"]["message"]
        for leak in ("Cannot convert", "SQL", "company_type", "ValueError", "Traceback"):
            self.assertNotIn(leak, msg, f"500 leaked {leak!r}: {msg!r}")

    # ─── Access log ──────────────────────────────────────────────────

    def test_access_log_records_successful_call(self):
        self._rpc("res.partner", "search_read", args=[[]], kwargs={"limit": 1}, expect_status=200)
        log = self._latest_log(method="search_read", model_name="res.partner")
        self.assertTrue(log)
        self.assertEqual(log.operation_type, "read")
        self.assertTrue(log.policy_allowed)
        self.assertTrue(log.success)
        self.assertFalse(log.policy_denied_reason)
        self.assertFalse(log.error_code)
        self.assertGreaterEqual(log.duration_ms, 0)

    def test_access_log_records_policy_denial(self):
        self._rpc("res.partner", "sudo", args=[], expect_status=403)
        log = self._latest_log(method="sudo")
        self.assertFalse(log.policy_allowed)
        self.assertFalse(log.success)
        self.assertEqual(log.policy_denied_reason, "method_blocked")

    def test_access_log_records_runtime_error(self):
        self._rpc(
            "ir.config_parameter",
            "search_read",
            args=[[]],
            kwargs={"fields": ["key"]},
            acting_user=self.basic_user.login,
            expect_status=403,
        )
        log = self._latest_log(method="search_read", model_name="ir.config_parameter")
        self.assertTrue(log.policy_allowed)  # gate passed, runtime failed
        self.assertFalse(log.success)
        self.assertEqual(log.error_code, "access_denied")

    def test_access_log_result_count_semantic(self):
        # create dict → result_count=1
        self._rpc("res.partner", "create", args=[{"name": "rc_single"}], expect_status=200)
        log = self._latest_log(method="create")
        self.assertEqual(log.result_count, 1)

        # create batch → result_count=N
        self._rpc("res.partner", "create", args=[[{"name": "rc_a"}, {"name": "rc_b"}]], expect_status=200)
        log = self._latest_log(method="create")
        self.assertEqual(log.result_count, 2)

        # search_count → result_count=the count itself
        resp = self._rpc("res.partner", "search_count", args=[[]], expect_status=200)
        count = resp.json()["data"]
        log = self._latest_log(method="search_count")
        self.assertEqual(log.result_count, count)
