import json
import secrets
import time
from unittest.mock import patch

from odoo.addons.tuqui.controllers.rpc import (
    _DEFAULT_STATEMENT_TIMEOUT_MS,
    _classify,
    _statement_timeout_ms,
)
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
        cls.admin_uid = cls.env.ref("base.user_admin").id
        cls.basic_user = cls.env["res.users"].create(
            {
                "name": "Tuqui Basic Test",
                "login": "tuqui_basic_test",
                "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
            }
        )
        # A share (portal) user — the per-member path must refuse impersonating it.
        cls.portal_user = cls.env["res.users"].create(
            {
                "name": "Tuqui Portal Test",
                "login": "tuqui_portal_test",
                "group_ids": [(6, 0, [cls.env.ref("base.group_portal").id])],
            }
        )

    # ─── Test bookkeeping ────────────────────────────────────────────

    def setUp(self):
        super().setUp()
        self.client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        # Tests start with read_only=False (pre-existing behaviour). The
        # test_member_read_only_* cases flip it explicitly inside each test.
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
        acting_uid="__admin__",
        connection=False,
        token=None,
        body_override=None,
        expect_status=None,
    ):
        """Invoke ``/tuqui/rpc`` and return the response.

        Two paths, matching the protocol:

        * MEMBER PATH (default) — sends ``X-Tuqui-Acting-Uid``. ``acting_uid``
          defaults to the admin user's id so the privileged read/write tests
          behave like before the per-member rework. Pass an explicit id to
          impersonate another member.
        * CONNECTION PATH — pass ``connection=True`` to omit the acting header
          entirely; the gateway then runs the call as superuser, locked to
          reads.

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
        headers = {
            **self._db_headers(),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if not connection:
            uid = self.admin_uid if acting_uid == "__admin__" else acting_uid
            headers["X-Tuqui-Acting-Uid"] = str(uid)
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

    def test_classify_covers_companion_transport_surface(self):
        """Contract guard: every method Tuqui posts to /tuqui/rpc must classify
        as intended. Mirror of
        tuqui_core/integrations/odoo/transports/companion.py — when its method
        surface changes, update this list and _READ_METHODS/_WRITE_METHODS
        together.

        The ``untyped`` group is the one that matters: those calls go through
        the transport's generic ``execute()`` / ``execute_method()`` instead of
        a method of their own, so nothing about them is greppable from the
        transport's signatures. This guard only mirrored the typed surface, and
        that is precisely how ``get_view`` and ``check_access_rights`` reached
        production classified as ``execute`` — refused on every read-only
        companion. Add the method here when a new call site appears."""
        reads = (
            "search_read",
            "read",
            "read_group",
            "formatted_read_group",
            "search_count",
            "fields_get",
            "get_view",
        )
        writes = ("create", "write", "unlink", "copy")
        # Reached through the generic execute()/execute_method(), not through a
        # typed method — see the docstring.
        untyped_reads = (
            "get_view",  # integrations/odoo/search_view.py
            "check_access_rights",  # integrations/odoo/adapter.py ← open_odoo_view
            "has_group",  # workspaces/service.py
        )
        for method in reads + untyped_reads:
            self.assertEqual(_classify(method), "read", f"{method} must classify as a read")
        for method in writes:
            self.assertEqual(_classify(method), "write", f"{method} must classify as a write")

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

    def test_unknown_acting_uid(self):
        resp = self._rpc("res.partner", "search_read", args=[[]], acting_uid=99999999, expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "forbidden_acting_user")

    # ─── Acting-user vetting (member path) ────────────────────────────

    def test_acting_uid_superuser_is_forbidden(self):
        """uid == SUPERUSER_ID (1) must never be impersonable — it would bypass
        every record rule."""
        resp = self._rpc("res.partner", "search_read", args=[[]], acting_uid=1, expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "forbidden_acting_user")

    def test_acting_uid_share_user_is_forbidden(self):
        """A share (portal/public) user is not an internal member → refused."""
        resp = self._rpc("res.partner", "search_read", args=[[]], acting_uid=self.portal_user.id, expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "forbidden_acting_user")

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

    def test_api_private_method_is_refused_on_member_path(self):
        """A public-named @api.private ORM method (init, mapped, …) must be
        refused on the member path — parity with Odoo's native get_public_method,
        which the gateway delegates to. Without the guard these reach the ORM
        (init runs raw DDL outside ACL)."""
        for method in ("init", "mapped", "filtered", "new"):  # @api.private in 18 AND 19
            resp = self._rpc("res.partner", method, args=[[1]], expect_status=403)
            self.assertEqual(
                resp.json()["error"]["code"],
                "access_denied",
                f"{method} is @api.private and must be refused",
            )

    # ─── Connection path (no acting uid → superuser, read-only) ──────

    def test_connection_path_read_runs_as_superuser(self):
        """No acting uid → run as superuser. A read that even admin's own ACL
        allows returns data; the audit row has no acting user."""
        resp = self._rpc(
            "ir.config_parameter",
            "search_read",
            args=[[]],
            kwargs={"fields": ["key"], "limit": 1},
            connection=True,
            expect_status=200,
        )
        self.assertTrue(resp.json()["ok"])
        log = self._latest_log(method="search_read", model_name="ir.config_parameter")
        self.assertFalse(log.acting_user_id, "connection-path calls have no acting member")

    def test_connection_path_blocks_writes_unconditionally(self):
        """The connection path is read-only UNCONDITIONALLY: a write/execute is
        refused with connection_read_only regardless of any external flag."""
        # write
        resp = self._rpc("res.partner", "create", args=[{"name": "conn_blocked"}], connection=True, expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "connection_read_only")
        # execute
        resp = self._rpc("res.partner", "action_archive", args=[[1]], connection=True, expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "connection_read_only")
        # the denial is logged on the policy axis
        log = self._latest_log(method="action_archive")
        self.assertFalse(log.policy_allowed)
        self.assertEqual(log.policy_denied_reason, "connection_read_only")

    # ─── Token expiry ────────────────────────────────────────────────

    def test_expired_access_token_is_rejected(self):
        """A token whose exp is in the past is refused by /tuqui/rpc with 401,
        even though its signature still verifies.

        Minted with the real ``_issue_access_token`` helper but with the clock
        wound back past the TTL so the resulting ``exp`` is already in the past.
        """
        from ..controllers import oauth as oauth_mod

        backdated = int(time.time()) - oauth_mod._ACCESS_TOKEN_TTL_SECONDS - 3600
        with patch.object(oauth_mod.time, "time", return_value=backdated):
            expired_token = oauth_mod._issue_access_token(self.env, self.client_id)

        resp = self._rpc(
            "res.partner",
            "search_read",
            args=[[]],
            token=expired_token,
            expect_status=401,
        )
        self.assertEqual(resp.json()["error"]["code"], "unauthorized")

    # ─── Error mapping ───────────────────────────────────────────────

    def test_access_error_maps_to_403(self):
        """Basic user without system rights can't read ir.config_parameter — AccessError → 403."""
        resp = self._rpc(
            "ir.config_parameter",
            "search_read",
            args=[[]],
            kwargs={"fields": ["key"]},
            acting_uid=self.basic_user.id,
            expect_status=403,
        )
        self.assertEqual(resp.json()["error"]["code"], "access_denied")

    @mute_logger("odoo.addons.tuqui.controllers.rpc")
    def test_schema_error_is_recoverable_validation_error(self):
        """Ordering/grouping/filtering by a non-stored field is a recoverable
        caller mistake, not a server fault: the gateway returns
        validation_error/400 (not a fatal 500) with the offending field name
        and a pivot hint, so the client self-corrects instead of giving up.
        The relayed text is only Odoo's schema message (field/model name) — no
        traceback or repr.
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
            expect_status=400,
        )
        body = resp.json()
        self.assertEqual(body["error"]["code"], "validation_error")
        msg = body["error"]["message"]
        self.assertIn("company_type", msg, f"Expected the offending field in message, got: {msg!r}")
        self.assertIn("read_group", msg, "Expected an actionable pivot hint in the message")
        for leak in ("Traceback", "ValueError"):
            self.assertNotIn(leak, msg, f"Message leaked {leak!r}: {msg!r}")

    def test_failed_write_rolls_back_instead_of_half_persisting(self):
        """A write refused by a constraint must leave NOTHING behind. Pre-fix
        the gateway swallowed the exception and returned a well-formed error,
        but never rolled back — Odoo then COMMITTED the request, silently
        persisting the very change whose validation failed (here: the
        recursive parent_id survives its own ValidationError). Every error
        branch must roll back before answering, and the audit row — written
        after the rollback — must survive to record the failure.
        """
        partner = self.env["res.partner"].create({"name": "Tuqui Flush Probe"})
        resp = self._rpc(
            "res.partner",
            "write",
            args=[[partner.id], {"parent_id": partner.id}],  # recursion → ValidationError
            expect_status=400,
        )
        body = resp.json()
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertIn("recursi", body["error"]["message"])
        # The failed write must not half-persist once the request commits.
        partner.invalidate_recordset()
        self.assertFalse(partner.parent_id)
        # The audit row survives the rollback and records the failure.
        log = self._latest_log(method="write", model_name="res.partner")
        self.assertTrue(log)
        self.assertFalse(log.success)
        self.assertEqual(log.error_code, "validation_error")

    @mute_logger("odoo.sql_db", "odoo.addons.tuqui.controllers.rpc")
    def test_deferred_sql_constraint_error_does_not_escape_as_html(self):
        """The exact prod failure shape of #70932 (account.move + AR
        localization): the ORM buffers UPDATEs until flush, so a write that
        violates a SQL constraint only explodes at commit — after the handler
        already built an ok response. Pre-fix, Odoo rolled the request back
        (audit row included) and replied with its opaque HTML error page: the
        caller saw a non-JSON 4xx/5xx with no business message and could not
        self-correct. The in-handler flush surfaces it as a recoverable
        validation_error/400 JSON instead. @mute_logger: the expected
        constraint violation logs an ERROR through odoo.sql_db that runbot
        would count as a build failure.
        """
        partner = self.env["res.partner"].create({"name": "Tuqui SQL Probe", "type": "contact"})
        resp = self._rpc(
            "res.partner",
            "write",
            args=[[partner.id], {"name": False}],  # violates CHECK "Contacts require a name"
            expect_status=400,
        )
        body = resp.json()  # pre-fix: HTML error page, not JSON
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertTrue(body["error"]["message"])
        partner.invalidate_recordset()
        self.assertEqual(partner.name, "Tuqui SQL Probe")

    def test_deferred_computes_run_with_acting_user_env(self):
        """Deferred computes run under the transaction's ``default_env``,
        which ir.http pins to ``request.env`` on EVERY web request
        (ir_http.py: ``transaction.default_env = request.env``). This route
        is ``auth="none"``, so that env has NO user — and any compute that
        touches ``self.env.user`` blows up with "Expected singleton:
        res.users()" at flush time (#70932, third leg: enterprise's
        ``_compute_signing_user`` calls ``env.user.has_group`` on every
        account.move create, so companion could not create invoices at all).
        The gateway must adopt the acting user REQUEST-wide
        (``request.update_env``), not just on the dispatched recordset.
        Simulated here with a user-touching stored compute on res.partner —
        no enterprise dependency.
        """
        seen = []
        Partner = self.env.registry["res.partner"]
        orig = Partner._compute_commercial_partner

        def compute_touching_env_user(records):
            # env.uid is a plain int — no ORM query, no flush cascade.
            # Pre-fix this was 0 (no user on auth="none" request.env);
            # post-fix it's the acting member's uid.
            seen.append(records.env.uid)
            return orig(records)

        with patch.object(Partner, "_compute_commercial_partner", compute_touching_env_user):
            resp = self._rpc(
                "res.partner",
                "create",
                args=[{"name": "Flush Env Probe"}],
                expect_status=200,
            )
        self.assertTrue(resp.json()["ok"], resp.text)
        self.assertTrue(seen, "the patched compute never ran — pick a field that recomputes on create")
        self.assertTrue(all(seen), "the compute ran with uid=0 (no user in env)")

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
            acting_uid=self.basic_user.id,
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

    # ─── Member read_only ─────────────────────────────────────────────────

    def test_member_read_only_blocks_writes_and_executes(self):
        """read_only=True: write and execute ops on the member path return 403 read_only_mode."""
        self.client.write({"read_only": True})
        resp = self._rpc("res.partner", "create", args=[{"name": "ro_blocked"}], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "read_only_mode")
        resp = self._rpc("res.partner", "action_archive", args=[[1]], expect_status=403)
        self.assertEqual(resp.json()["error"]["code"], "read_only_mode")

    def test_member_read_only_allows_reads(self):
        """read_only=True: read ops on the member path still work."""
        self.client.write({"read_only": True})
        resp = self._rpc(
            "res.partner",
            "search_read",
            args=[[]],
            kwargs={"fields": ["name"], "limit": 2},
            expect_status=200,
        )
        self.assertTrue(resp.json()["ok"])

    def test_read_only_paths_allow_the_access_check(self):
        """``check_access_rights`` answers "may this user do X" and reads no
        records, so both restricted paths must let it through.

        Classified as ``execute`` it was refused, and this one fails loudly:
        its only caller is ``open_odoo_view`` in ``mode='new'``, an embed-only
        tool — so it runs exclusively over the companion. On a read-only one
        (the default right after activation) asking the embedded chat to create
        a record answered "Could not verify Odoo access rights", blaming the
        user's permissions for a refusal of ours.
        """
        self.client.write({"read_only": True})
        resp = self._rpc("res.partner", "check_access_rights", args=["read"], expect_status=200)
        self.assertTrue(resp.json()["ok"])
        resp = self._rpc("res.partner", "check_access_rights", args=["read"], connection=True, expect_status=200)
        self.assertTrue(resp.json()["ok"])

    def test_read_only_paths_allow_the_view_metadata_read(self):
        """``get_view`` is a read, and both restricted paths must let it through.

        It reads no records: Odoo checks read access on the model and hands back
        the arch already pruned to the acting user's groups. Classified as
        ``execute`` it was refused on the two paths that only allow reads — a
        read-only companion (the default right after activation) and the
        connection path — which silently degraded the search-view field ranking
        Tuqui derives from that arch.
        """
        self.client.write({"read_only": True})
        resp = self._rpc("res.partner", "get_view", args=[False, "search"], expect_status=200)
        self.assertIn("arch", resp.json()["data"])
        resp = self._rpc("res.partner", "get_view", args=[False, "search"], connection=True, expect_status=200)
        self.assertIn("arch", resp.json()["data"])

    def test_member_read_only_false_allows_writes(self):
        """read_only=False: write ops work normally on the member path."""
        self.client.write({"read_only": False})
        resp = self._rpc("res.partner", "create", args=[{"name": "ro_allowed"}], expect_status=200)
        self.assertIsInstance(resp.json()["data"], int)

    def test_connection_path_always_read_only_regardless_of_member_flag(self):
        """connection_read_only is unconditional: connection path refuses writes
        even when read_only=False on the client."""
        self.client.write({"read_only": False})
        resp = self._rpc(
            "res.partner",
            "create",
            args=[{"name": "conn_still_blocked"}],
            connection=True,
            expect_status=403,
        )
        self.assertEqual(resp.json()["error"]["code"], "connection_read_only")

    # ─── Cost guard: statement_timeout (#70305) ──────────────────────────

    def test_statement_timeout_ms_uses_client_value(self):
        """The client's own declared budget is used as-is — no ceiling to clamp
        against. CompanionTransport always sends one; a single query is already
        bounded by the client's own hardcoded ceiling (odoo_execute.py's 600s)."""
        self.assertEqual(_statement_timeout_ms(30_000), 30_000)
        self.assertEqual(_statement_timeout_ms(600_000), 600_000)

    def test_statement_timeout_ms_falls_back_to_default_when_missing_or_invalid(self):
        """A missing/garbage/zero/negative client_timeout_ms falls back to
        _DEFAULT_STATEMENT_TIMEOUT_MS instead of leaving the query unbounded."""
        self.assertEqual(_statement_timeout_ms(None), _DEFAULT_STATEMENT_TIMEOUT_MS)
        self.assertEqual(_statement_timeout_ms("not-a-number"), _DEFAULT_STATEMENT_TIMEOUT_MS)
        self.assertEqual(_statement_timeout_ms(0), _DEFAULT_STATEMENT_TIMEOUT_MS)
        self.assertEqual(_statement_timeout_ms(-5), _DEFAULT_STATEMENT_TIMEOUT_MS)

    def test_normal_read_succeeds_under_default_cap(self):
        """A normal, fast query must succeed under the fallback default (no
        client_timeout_ms sent — the request-building helper doesn't add one
        unless asked to via body_override)."""
        resp = self._rpc(model="res.partner", method="search_count", args=[[]], expect_status=200)
        self.assertTrue(resp.json()["ok"], resp.text)

    @mute_logger("odoo.sql_db")
    def test_query_over_client_budget_returns_query_timeout(self):
        """client_timeout_ms=1 over a large table forces the cutoff → 400
        query_timeout, worker freed. @mute_logger: without it, runbot counts
        the expected psycopg2 ERROR log as a build failure."""
        resp = self._rpc(
            body_override={
                "model": "ir.model.fields",
                "method": "search_read",
                "args": [[]],
                "kwargs": {"fields": ["name", "model", "field_description", "help"]},
                "client_timeout_ms": 1,
            }
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "query_timeout")
