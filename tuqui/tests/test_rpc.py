import json
import secrets

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged

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
        # Reset policy + rules to a known baseline. Order matters because
        # of the @api.constrains: clear allow_private before switching mode.
        self.env["tuqui.rpc.rule"].sudo().search([]).unlink()
        policy = self.env["tuqui.rpc.policy"]._get_singleton()
        if policy.allow_private_methods:
            policy.write({"allow_private_methods": False})
        if policy.policy_mode != "default":
            policy.write({"policy_mode": "default"})
        self.policy = policy

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
        token=None,
        body_override=None,
        expect_status=None,
    ):
        """Invoke ``/tuqui/rpc`` and return the response.

        Pass ``body_override`` to send a malformed body for perimeter tests;
        otherwise the body is built from the named params.
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
        resp = self.url_open(
            "/tuqui/rpc",
            data=json.dumps(body),
            headers={
                **self._db_headers(),
                "Authorization": f"Bearer {token}",
                "X-Tuqui-Acting-User": acting_user,
                "Content-Type": "application/json",
            },
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

    # ─── Advanced mode rules ─────────────────────────────────────────

    def test_advanced_mode_allow_by_default_for_non_private(self):
        """No rules → reads/writes/executes pass (advanced doesn't tighten non-private)."""
        self.policy.write({"policy_mode": "advanced"})
        resp = self._rpc("res.partner", "search_read", args=[[]], kwargs={"limit": 1}, expect_status=200)
        self.assertTrue(resp.json()["ok"])

    def test_advanced_mode_deny_wins(self):
        self.policy.write({"policy_mode": "advanced"})
        # Allow rule that would match
        self.env["tuqui.rpc.rule"].sudo().create(
            {
                "name": "allow read",
                "effect": "allow",
                "model_pattern": "res.partner",
                "method_pattern": "search_read",
                "operation_type": "read",
            }
        )
        # And a deny rule that also matches — deny should win.
        self.env["tuqui.rpc.rule"].sudo().create(
            {
                "name": "deny everything on res.partner",
                "effect": "deny",
                "model_pattern": "res.partner",
                "method_pattern": "*",
                "operation_type": "any",
            }
        )
        resp = self._rpc("res.partner", "search_read", args=[[]], kwargs={"limit": 1}, expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "deny_rule_matched")

    def test_advanced_mode_private_requires_allow_private_and_exact_rule(self):
        """Both the toggle AND an exact allow rule are required for private."""
        self.policy.write({"policy_mode": "advanced"})
        # Toggle off: blocked even with an exact rule, before reaching rule eval.
        self.env["tuqui.rpc.rule"].sudo().create(
            {
                "name": "allow specific private",
                "effect": "allow",
                "model_pattern": "res.partner",
                "method_pattern": "_compute_display_name",
                "operation_type": "private_execute",
            }
        )
        resp = self._rpc("res.partner", "_compute_display_name", args=[[1]], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "private_method_blocked")

        # Toggle on but no rule → still blocked (no_allow_rule).
        self.env["tuqui.rpc.rule"].sudo().search([]).unlink()
        self.policy.write({"allow_private_methods": True})
        resp = self._rpc("res.partner", "_compute_display_name", args=[[1]], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "no_allow_rule")

        # Toggle on AND exact rule → succeeds.
        self.env["tuqui.rpc.rule"].sudo().create(
            {
                "name": "allow specific private 2",
                "effect": "allow",
                "model_pattern": "res.partner",
                "method_pattern": "_compute_display_name",
                "operation_type": "private_execute",
            }
        )
        resp = self._rpc("res.partner", "_compute_display_name", args=[[1]], expect_status=200)
        self.assertTrue(resp.json()["ok"])

    # ─── Preset ──────────────────────────────────────────────────────

    def test_preset_creates_three_deny_rules_idempotent(self):
        self.policy.write({"policy_mode": "advanced"})
        self.policy.action_apply_read_only_preset()
        rules = self.env["tuqui.rpc.rule"].sudo().search([])
        self.assertEqual(len(rules), 3)
        op_types = {r.operation_type for r in rules}
        self.assertEqual(op_types, {"write", "execute", "private_execute"})
        self.assertTrue(all(r.effect == "deny" for r in rules))

        # Idempotent: second apply doesn't add duplicates.
        self.policy.action_apply_read_only_preset()
        self.assertEqual(self.env["tuqui.rpc.rule"].sudo().search_count([]), 3)

        # After preset, write/execute/private are blocked but reads work.
        resp = self._rpc("res.partner", "create", args=[{"name": "blocked"}], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "deny_rule_matched")

        resp = self._rpc("res.partner", "search_read", args=[[]], kwargs={"limit": 1}, expect_status=200)
        self.assertTrue(resp.json()["ok"])

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

    def test_internal_error_does_not_leak_details(self):
        """A 500 must surface a generic message — no SQL, no ValueError repr."""
        # Grouping by a non-stored field raises ValueError inside the ORM.
        resp = self._rpc(
            "res.partner",
            "read_group",
            args=[[], [], ["company_type"]],  # non-stored selection
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


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiRpcRuleConstraints(TransactionCase):
    """Python-level coverage of the policy rule model — no HTTP needed."""

    def test_allow_private_rejects_wildcard_in_model_pattern(self):
        with self.assertRaises(ValidationError):
            self.env["tuqui.rpc.rule"].sudo().create(
                {
                    "name": "bad",
                    "effect": "allow",
                    "model_pattern": "ai.*",  # wildcard
                    "method_pattern": "_run_transcription",
                    "operation_type": "private_execute",
                }
            )

    def test_allow_private_rejects_wildcard_in_method_pattern(self):
        with self.assertRaises(ValidationError):
            self.env["tuqui.rpc.rule"].sudo().create(
                {
                    "name": "bad",
                    "effect": "allow",
                    "model_pattern": "ai.video",
                    "method_pattern": "_run_*",  # wildcard
                    "operation_type": "private_execute",
                }
            )

    def test_allow_private_rejects_character_class(self):
        """[abc] is a glob too — must not slip past the exact check."""
        with self.assertRaises(ValidationError):
            self.env["tuqui.rpc.rule"].sudo().create(
                {
                    "name": "bad",
                    "effect": "allow",
                    "model_pattern": "ai.video",
                    "method_pattern": "_run_[abc]",
                    "operation_type": "private_execute",
                }
            )

    def test_allow_private_accepts_exact_patterns(self):
        rec = (
            self.env["tuqui.rpc.rule"]
            .sudo()
            .create(
                {
                    "name": "ok",
                    "effect": "allow",
                    "model_pattern": "ai.video",
                    "method_pattern": "_run_transcription",
                    "operation_type": "private_execute",
                }
            )
        )
        self.assertTrue(rec.id)

    def test_deny_with_wildcard_on_private_accepted(self):
        """Wildcards in deny rules are fine — the constraint only fires on allow."""
        rec = (
            self.env["tuqui.rpc.rule"]
            .sudo()
            .create(
                {
                    "name": "deny all private",
                    "effect": "deny",
                    "model_pattern": "*",
                    "method_pattern": "*",
                    "operation_type": "private_execute",
                }
            )
        )
        self.assertTrue(rec.id)

    def test_unique_constraint_blocks_duplicate(self):
        self.env["tuqui.rpc.rule"].sudo().create(
            {
                "name": "first",
                "effect": "deny",
                "model_pattern": "*",
                "method_pattern": "*",
                "operation_type": "write",
            }
        )
        with self.assertRaises(Exception):  # IntegrityError, surfaced via Odoo wrapper
            self.env["tuqui.rpc.rule"].sudo().create(
                {
                    "name": "duplicate",
                    "effect": "deny",
                    "model_pattern": "*",
                    "method_pattern": "*",
                    "operation_type": "write",
                }
            )
            self.env.cr.flush()


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiRpcPolicyConstraints(TransactionCase):
    """Policy-level constraints not covered by the HTTP suite."""

    def test_allow_private_only_in_advanced(self):
        """Default mode + allow_private_methods=True is a contradictory state."""
        policy = self.env["tuqui.rpc.policy"]._get_singleton()
        # Set up: advanced + flag on.
        policy.write({"policy_mode": "advanced"})
        policy.write({"allow_private_methods": True})
        # Now try to flip back without disabling the flag — constraint fires.
        with self.assertRaises(ValidationError):
            policy.write({"policy_mode": "default"})
        # The standard flow (disable flag, then switch mode) succeeds.
        policy.write({"allow_private_methods": False})
        policy.write({"policy_mode": "default"})
        self.assertEqual(policy.policy_mode, "default")
