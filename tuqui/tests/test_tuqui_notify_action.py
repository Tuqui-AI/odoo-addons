"""Tests for the "Notify Tuqui" server action type (task #72545).

Coverage mirrors the spec's §Configuración acceptance criteria:

* the action asks for an agent and nothing else — no URL, no token;
* the agent list is fetched from Tuqui, never mirrored, and cached for minutes
  rather than re-fetched on every field render;
* if Tuqui does not answer, only the selector goes empty: an action that was
  already configured keeps firing;
* running the action queues events instead of posting them.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models import ir_actions_server as action_mod


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


_AGENTS = {
    "agents": [
        {"id": "11111111-1111-1111-1111-111111111111", "name": "Minutes", "ready": True, "blocked_reason": None},
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Leads",
            "ready": False,
            "blocked_reason": "This agent declares no service identity to run as",
        },
    ]
}


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiNotifyAction(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Action = cls.env["ir.actions.server"].sudo()
        cls.client = cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()[0]
        cls.client.write({"event_signing_key": "test-signing-key", "state": "active"})
        cls.partner_model = cls.env["ir.model"]._get("res.partner")

    def setUp(self):
        super().setUp()
        # The cache is per-process and would leak between tests.
        action_mod._agent_cache.clear()

    def _action(self, **overrides):
        values = {
            "name": "Notify Tuqui",
            "model_id": self.partner_model.id,
            "state": "tuqui_notify",
            "tuqui_agent_id": "11111111-1111-1111-1111-111111111111",
        }
        values.update(overrides)
        return self.Action.create(values)

    # ─── The selector ────────────────────────────────────────────────────────

    def test_the_list_comes_from_tuqui(self):
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            return_value=_Response(_AGENTS),
        ):
            choices = self.Action._tuqui_agent_choices()
        assert dict(choices)["11111111-1111-1111-1111-111111111111"] == "Minutes"

    def test_an_agent_that_cannot_run_is_offered_with_the_reason(self):
        """Hiding it would answer "where is my agent?" with silence."""
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            return_value=_Response(_AGENTS),
        ):
            label = dict(self.Action._tuqui_agent_choices())["22222222-2222-2222-2222-222222222222"]
        assert "no service identity" in label

    def test_the_list_is_cached_instead_of_fetched_per_render(self):
        """A dynamic Selection is evaluated on every form load; without the cache
        opening a list of automations would be one HTTP call per row."""
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            return_value=_Response(_AGENTS),
        ) as get:
            self.Action._tuqui_agent_choices()
            self.Action._tuqui_agent_choices()
            self.Action._tuqui_agent_choices()
        assert get.call_count == 1

    def test_the_request_is_signed(self):
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            return_value=_Response(_AGENTS),
        ) as get:
            self.Action._tuqui_agent_choices()
        headers = get.call_args.kwargs["headers"]
        assert headers["X-Tuqui-Client-Id"] == self.client.client_id
        assert headers["X-Tuqui-Signature"].startswith("t=")
        assert ",v1=" in headers["X-Tuqui-Signature"]

    @mute_logger("odoo.addons.tuqui.models.ir_actions_server")
    def test_a_tuqui_that_is_down_yields_an_empty_list_and_no_traceback(self):
        """@mute_logger: an unreachable Tuqui deserves a warning in production,
        but this test causes it on purpose and runbot counts any WARNING in the
        log as a build failure."""
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            side_effect=OSError("connection refused"),
        ):
            assert self.Action._tuqui_agent_choices() == []

    @mute_logger("odoo.addons.tuqui.models.ir_actions_server")
    def test_an_action_already_configured_keeps_its_agent_when_tuqui_is_down(self):
        """The point of storing the id rather than a foreign key: only the
        selector degrades, never the automation."""
        action = self._action()
        action_mod._agent_cache.clear()
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            side_effect=OSError("connection refused"),
        ):
            assert self.Action._tuqui_agent_choices() == []
        assert action.tuqui_agent_id == "11111111-1111-1111-1111-111111111111"

    def test_a_database_not_connected_to_tuqui_asks_nothing(self):
        self.client.write({"state": "pending"})
        try:
            with patch("odoo.addons.tuqui.models.ir_actions_server.requests.get") as get:
                assert self.Action._tuqui_agent_choices() == []
            get.assert_not_called()
        finally:
            self.client.write({"state": "active"})

    # ─── Por qué la lista vino vacía ─────────────────────────────────────────

    def test_a_database_not_connected_says_so(self):
        """The message an admin actually needs: connect the database. Saying
        "could not be loaded" here sends them to look for a network problem
        that does not exist."""
        self.client.write({"state": "pending"})
        try:
            assert self.Action._tuqui_agent_problem() == action_mod._NOT_CONNECTED
        finally:
            self.client.write({"state": "active"})

    def test_a_database_without_a_signing_key_says_so(self):
        """Every database that connected before signed events existed is here."""
        self.client.write({"event_signing_key": False})
        try:
            with patch("odoo.addons.tuqui.models.ir_actions_server.requests.get") as get:
                assert self.Action._tuqui_agent_problem() == action_mod._NO_SIGNING_KEY
            get.assert_not_called()
        finally:
            self.client.write({"event_signing_key": "test-signing-key"})

    @mute_logger("odoo.addons.tuqui.models.ir_actions_server")
    def test_a_tuqui_that_does_not_answer_says_so(self):
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            side_effect=OSError("connection refused"),
        ):
            assert self.Action._tuqui_agent_problem() == action_mod._UNREACHABLE

    def test_a_workspace_without_agents_says_so(self):
        """Nothing is broken here — the workspace is simply empty, which is how
        every new one starts. It was the most misleading of the four."""
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            return_value=_Response({"agents": []}),
        ):
            assert self.Action._tuqui_agent_problem() == action_mod._NO_AGENTS

    def test_a_working_list_has_no_problem(self):
        with patch(
            "odoo.addons.tuqui.models.ir_actions_server.requests.get",
            return_value=_Response(_AGENTS),
        ):
            assert self.Action._tuqui_agent_problem() is None

    def test_each_problem_has_its_own_message(self):
        """Four causes, four things to do about them. One message for all of
        them read as "something broke" even in the two cases where nothing did."""
        problems = [
            action_mod._NOT_CONNECTED,
            action_mod._NO_SIGNING_KEY,
            action_mod._UNREACHABLE,
            action_mod._NO_AGENTS,
        ]
        messages = {self.Action._tuqui_problem_message(p) for p in problems}
        titles = {self.Action._tuqui_problem_title(p) for p in problems}
        assert len(messages) == 4
        assert len(titles) == 4

    # ─── Running ─────────────────────────────────────────────────────────────

    def test_running_queues_one_event_per_record_without_http(self):
        action = self._action()
        partners = self.env["res.partner"].create([{"name": "One"}, {"name": "Two"}])

        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post") as post:
            action.with_context(active_ids=partners.ids)._run_action_tuqui_notify_multi({"records": partners})

        post.assert_not_called()
        events = self.env["tuqui.event"].search([("res_model", "=", "res.partner"), ("res_id", "in", partners.ids)])
        assert len(events) == 2
        assert set(events.mapped("state")) == {"pending"}
        assert set(events.mapped("agent_external_id")) == {"11111111-1111-1111-1111-111111111111"}

    def test_an_action_without_an_agent_says_so(self):
        action = self._action(tuqui_agent_id=False)
        partner = self.env["res.partner"].create({"name": "One"})
        with self.assertRaises(UserError):
            action._run_action_tuqui_notify_multi({"records": partner})
