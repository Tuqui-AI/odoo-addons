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
_agent_cache: dict[str, tuple[float, list]] = {}


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
        """Ask Tuqui which agents this database may fire.

        No mirror and no sync: nothing is copied into this database, so nothing
        can go stale. The saved ``tuqui_agent_id`` survives on its own — if Tuqui
        is unreachable the selector comes back empty and the automation that is
        already configured keeps firing, which is the whole point of storing the
        id rather than a foreign key.

        Returns:
            A list of ``(value, label)`` pairs, empty when Tuqui cannot be
            reached.
        """
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        if not client or client.state != "active":
            return []

        cache_key = self.env.cr.dbname
        cached = _agent_cache.get(cache_key)
        if cached and time.time() - cached[0] < _AGENT_CACHE_SECONDS:
            return cached[1]

        try:
            choices = self._fetch_tuqui_agents(client)
        except Exception as exc:  # noqa: BLE001 — an unreachable Tuqui is an empty list, not a traceback
            _logger.warning("tuqui.agents: could not fetch the agent list (%s)", str(exc)[:200])
            # Cached as empty too, briefly: otherwise every field render retries a
            # Tuqui that is down and each one costs the timeout above.
            _agent_cache[cache_key] = (time.time(), [])
            return []

        _agent_cache[cache_key] = (time.time(), choices)
        return choices

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
        """Offer the current agent list as soon as the type is chosen."""
        if self.state != "tuqui_notify":
            return {}
        choices = self._tuqui_agent_choices()
        if not choices:
            return {
                "warning": {
                    "title": _("Tuqui unavailable"),
                    "message": _(
                        "The list of agents could not be loaded. The action can still be saved "
                        "if you already know the agent id, and an automation that is already "
                        "configured keeps working."
                    ),
                }
            }
        return {"domain": {}}

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
