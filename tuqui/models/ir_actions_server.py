import logging
import time

import requests
from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# How long the agent list is reused before asking Tuqui again. A dynamic
# Selection is evaluated on every form load and on every `fields_get`, so
# without this, opening the automation list would fire one HTTP call per row.
#
# Minutes and not hours: the list changes when somebody creates an agent in
# Tuqui and comes straight back here to point an automation at it, and waiting
# an hour to see it would read as broken.
_AGENT_CACHE_SECONDS = 300

# Short on purpose. This runs while a person waits for a form to open, so a slow
# Tuqui has to degrade into an empty selector quickly rather than hang the UI.
_AGENT_FETCH_TIMEOUT = 5

# Per-process, per-database. Not an ir.config_parameter: this is a cache, and
# writing it to the database would turn "ask again in five minutes" into a row
# somebody has to maintain, back up and wonder about.
#
# Each entry is ``(fetched_at, choices, problem)``. The problem travels with the
# choices because an empty list has four different causes and each one asks the
# admin for something different; recomputing it in the onchange would mean a
# second round trip to a Tuqui we just failed to reach.
_agent_cache: dict[str, tuple[float, list, str | None]] = {}

# Why the list came back empty. None means it did not.
_NOT_CONNECTED = "not_connected"
_NO_SIGNING_KEY = "no_signing_key"
_UNREACHABLE = "unreachable"
_NO_AGENTS = "no_agents"


class IrActionsServer(models.Model):
    """Adds the "Notify Tuqui" server action type.

    **Why the configuration lives here and not in Tuqui.** The trigger condition
    belongs to Odoo: the real automations filter by tags or by an empty field,
    and Tuqui will never have a domain editor competitive with the native one.
    The person building the automation is already in this form; the only thing
    missing was the list of agents to point it at.

    The action itself does no HTTP. It writes a `tuqui.event` row inside the same
    transaction as the change that triggered it, and the queue's cron delivers it
    with retries — which is the entire difference from the native webhook action,
    whose one-second send-and-forget POST is how events went missing in the first
    place.
    """

    _inherit = "ir.actions.server"

    state = fields.Selection(
        selection_add=[("tuqui_notify", "Notify Tuqui")],
        ondelete={"tuqui_notify": "cascade"},
    )
    tuqui_agent_id = fields.Selection(
        selection="_tuqui_agent_choices",
        string="Tuqui Agent",
        help="The Tuqui agent this automation notifies.",
    )

    # ---------- The selector ----------

    @api.model
    def _tuqui_agent_choices(self):
        """The list for the selector. Empty when it could not be built.

        No mirror and no sync: nothing is copied into this database, so nothing
        can go stale. The saved ``tuqui_agent_id`` survives on its own — if Tuqui
        is unreachable the selector comes back empty and the automation that is
        already configured keeps firing, which is the whole point of storing the
        id rather than a foreign key.

        Returns:
            A list of ``(value, label)`` pairs, empty when there is nothing to
            offer. ``_tuqui_agent_problem`` says why.
        """
        return self._tuqui_agent_state()[0]

    @api.model
    def _tuqui_agent_problem(self):
        """Why the list is empty, or None when it is not.

        One of the four module-level constants. Kept apart from the choices
        because a ``Selection`` can only return pairs, and "empty" is the one
        answer an admin cannot act on without being told which of the four
        situations they are in.
        """
        return self._tuqui_agent_state()[1]

    @api.model
    def _tuqui_agent_state(self):
        """Resolve ``(choices, problem)``, from cache when it is still warm."""
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        if not client or client.state != "active":
            # Nothing to ask and nobody to ask it of. Not cached: connecting is
            # a deliberate act and the admin should see the result of it at once.
            return [], _NOT_CONNECTED

        cache_key = self.env.cr.dbname
        cached = _agent_cache.get(cache_key)
        if cached and time.time() - cached[0] < _AGENT_CACHE_SECONDS:
            return cached[1], cached[2]

        if not client.sudo().event_signing_key:
            # Checked here rather than caught below: a missing key raises the
            # same ValueError family as a non-JSON response, and telling an
            # admin to renew credentials when Tuqui merely answered HTML would
            # send them down the wrong path.
            _agent_cache[cache_key] = (time.time(), [], _NO_SIGNING_KEY)
            return [], _NO_SIGNING_KEY

        try:
            choices = self._fetch_tuqui_agents(client)
        except Exception as exc:  # noqa: BLE001 — an unreachable Tuqui is an empty list, not a traceback
            _logger.warning("tuqui.agents: could not fetch the agent list (%s)", str(exc)[:200])
            # Cached as empty too, briefly: otherwise every field render retries a
            # Tuqui that is down and each one costs the timeout above.
            _agent_cache[cache_key] = (time.time(), [], _UNREACHABLE)
            return [], _UNREACHABLE

        problem = None if choices else _NO_AGENTS
        _agent_cache[cache_key] = (time.time(), choices, problem)
        return choices, problem

    @api.model
    def _fetch_tuqui_agents(self, client):
        """One signed GET. Raises on anything that is not a 200."""
        timestamp = int(time.time())
        # A GET has no body, so the empty string is the signed material — the
        # same scheme the event queue uses, so there is one way to prove who is
        # calling and not two.
        signature = client._sign_outbound("", timestamp)
        response = requests.get(
            f"{client._get_tuqui_base_url()}/v1/companion/agents",
            headers={
                "X-Tuqui-Client-Id": client.client_id,
                "X-Tuqui-Signature": f"t={timestamp},v1={signature}",
            },
            timeout=_AGENT_FETCH_TIMEOUT,
        )
        response.raise_for_status()
        agents = response.json().get("agents") or []
        return [
            (
                agent["id"],
                # An agent Tuqui says cannot run is offered with the reason
                # rather than hidden: hiding it answers "where is my agent?"
                # with silence, and declaring a service identity is not
                # something an admin would guess.
                agent["name"]
                if agent.get("ready")
                else f"{agent['name']} ({agent.get('blocked_reason') or 'not ready'})",
            )
            for agent in agents
            if agent.get("id") and agent.get("name")
        ]

    @api.onchange("state")
    def _onchange_state_tuqui_notify(self):
        """Offer the current agent list as soon as the type is chosen.

        When there is nothing to offer, say which of the four situations this is.
        One message for all of them read as "something broke" in every case,
        including the two where nothing broke at all — and each one is fixed
        somewhere else, by somebody else.
        """
        if self.state != "tuqui_notify":
            return {}
        choices, problem = self._tuqui_agent_state()
        if choices:
            return {"domain": {}}
        return {
            "warning": {"title": self._tuqui_problem_title(problem), "message": self._tuqui_problem_message(problem)}
        }

    @api.model
    def _tuqui_problem_title(self, problem):
        return {
            _NOT_CONNECTED: _("Tuqui is not connected"),
            _NO_SIGNING_KEY: _("Tuqui credentials need renewing"),
            _UNREACHABLE: _("Tuqui unavailable"),
            _NO_AGENTS: _("No agents in Tuqui yet"),
        }.get(problem, _("Tuqui unavailable"))

    @api.model
    def _tuqui_problem_message(self, problem):
        if problem == _NOT_CONNECTED:
            return _(
                "This database is not connected to Tuqui yet. Connect it from "
                "Settings > Tuqui and the agents will appear here."
            )
        if problem == _NO_SIGNING_KEY:
            return _(
                "This database connected to Tuqui before signed events existed, so it has no "
                "signing key yet. Tuqui asks for one on its own within the hour; to do it now, "
                "disconnect and activate again from Settings > Tuqui."
            )
        if problem == _NO_AGENTS:
            return _(
                "The connection works, but your Tuqui workspace has no agents yet. Create one "
                "in Tuqui and it will show up here within a few minutes."
            )
        return _(
            "Tuqui did not answer, so the list could not be loaded. The action can still be "
            "saved if you already know the agent id, and an automation that is already "
            "configured keeps working."
        )

    # ---------- Running ----------

    def _run_action_tuqui_notify_multi(self, eval_context=None):
        """Queue one event per record. Does no HTTP.

        Runs inside the transaction of the write that triggered it, which is the
        reliability guarantee: roll that back and the events go with it.
        """
        self.ensure_one()
        if not self.tuqui_agent_id:
            raise UserError(_("This action has no Tuqui agent selected."))

        records = eval_context.get("records") if eval_context else None
        if records is None:
            records = self.env[self.model_id.model].browse(self.env.context.get("active_ids", []))

        events = self.env["tuqui.event"]
        for record in records:
            events |= events.enqueue(self.tuqui_agent_id, record._name, record.id)
        if events:
            _logger.info("tuqui.notify: queued %s event(s) for agent %s", len(events), self.tuqui_agent_id)
        return False
