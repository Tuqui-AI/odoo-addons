import json
import logging
import time
import uuid
from datetime import timedelta

import requests
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# How long each failed attempt waits before the next one. Five entries, so a row
# that exhausts them has been tried six times over roughly eight hours — long
# enough to ride out a deploy or a provider incident, short enough that a
# genuinely broken endpoint stops being retried the same working day.
#
# The ladder is what the native webhook cannot do at all: `_run_action_webhook`
# posts once with a one-second timeout and drops the event on any failure, which
# is the loss this queue exists to end.
_BACKOFF = [
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
]
MAX_ATTEMPTS = len(_BACKOFF) + 1

# Rows the cron takes per run. A mass action can enqueue thousands at once, and
# the cron holds a worker while it posts — this bounds one run's wall clock
# instead of letting a backlog starve every other scheduled job.
_BATCH_SIZE = 50

# Per-request ceiling. Generous next to Odoo's native one second, because here a
# timeout is not a lost event: it is one attempt of six.
#
# A pair and not a scalar: `requests` applies a scalar to connect AND to read,
# so `20` means one event can hold the worker for 40 seconds, and the cron
# budget below is checked between events, never inside one. Five seconds is
# plenty to open a socket to a host that is up.
_TIMEOUT_SECONDS = (5, 20)

# Wall clock one run may spend, checked between events. The batch bounds how
# many rows we take; this bounds how long taking them can last, and only the
# second one is what the server enforces: a cron thread is killed at
# ``--limit-time-real-cron``, which defaults to ``-1`` meaning "use
# ``--limit-time-real``" - 120 seconds. Fifty events that each time out is
# 1250, so the batch alone would get the worker killed every minute.
# Leaving early is free because ``_deliver`` commits per event, and the rows we
# did not reach are still due on the next run.
#
# 90 and not 120: the check happens between events, so a run can overshoot by
# one whole request. 90 + the 25 that `_TIMEOUT_SECONDS` now caps a request at
# still lands under the limit.
_CRON_BUDGET_SECONDS = 90

# Default retention for delivered events, overridable per database with the
# ``tuqui.event_retention_days`` config parameter. A sent row is evidence that
# something *was* processed, which stops being useful after a few weeks; the
# window is what keeps that evidence around long enough to answer "did this
# ever reach Tuqui?" without letting the table grow forever.
_DEFAULT_RETENTION_DAYS = 30

# Client-error statuses that WILL change on their own, and are therefore the
# only 4xx worth retrying. Everything else in that range is a refusal that
# waiting cannot fix: a feature switched off, a body Tuqui rejects. Burning the
# whole ladder on those costs eight hours before anybody reads the error, and
# the error is the useful part.
#
# 401 is in here because Tuqui answers it for a signature that is merely too
# old, not only for one that is wrong: it allows five minutes of clock skew and
# refuses beyond that. An on-premise whose clock drifts would otherwise send
# every event straight to `failed` reading "Tuqui refused", with nothing
# pointing at the clock. It also covers the minutes between this database
# minting its signing key and Tuqui fetching it.
_RETRYABLE_CLIENT_STATUSES = frozenset({401, 408, 429})

_STATE_SELECTION = [
    ("pending", "Pending"),
    ("sent", "Sent"),
    ("failed", "Failed"),
]


class TuquiEvent(models.Model):
    """One notification owed to Tuqui, and the record of trying to deliver it.

    **The queue is the whole point.** Odoo's native outgoing webhook is
    send-and-forget by design — `ir.actions.server._run_action_webhook` posts
    inside `cr.postcommit`, with `timeout=1`, and turns any failure into a log
    line. Nothing is written to the database, so when Tuqui is down the events
    are lost *and* invisible: the only way to find out is to notice, later, that
    something never happened.

    A row here is the opposite of that. It is created in the **same transaction**
    as the change that triggered it, so the two commit or roll back together —
    an event can never describe a write that did not land, and a write can never
    silently fail to notify. The HTTP call happens afterwards, from the cron,
    where it is free to fail and be retried without holding the user's request.

    That split is also what makes the identity trustworthy: `triggered_by_user_id`
    is read at enqueue time, from the transaction that made the change, rather
    than reconstructed later from something the caller asserts.
    """

    _name = "tuqui.event"
    _description = "Tuqui Outgoing Event Queue"
    _order = "create_date desc, id desc"
    _rec_name = "uuid"

    uuid = fields.Char(
        required=True,
        readonly=True,
        index=True,
        copy=False,
        default=lambda _self: str(uuid.uuid4()),
        help=(
            "Idempotency key sent to Tuqui. It names this delivery, not the record — "
            "which is what lets Tuqui recognise a retry as the same event a day later, "
            "where a model:id key could only ever absorb a burst."
        ),
    )

    # ---------- What moved ----------

    res_model = fields.Char(required=True, readonly=True, string="Model")
    res_id = fields.Integer(required=True, readonly=True, string="Record ID")
    agent_external_id = fields.Char(
        required=True,
        readonly=True,
        string="Tuqui Agent",
        help="Identifier of the Tuqui agent this event fires. Chosen on the server action.",
    )
    triggered_by_user_id = fields.Many2one(
        "res.users",
        readonly=True,
        string="Triggered by",
        help=(
            "The Odoo user whose write produced this event. Tuqui records it for "
            "attribution and delivery — who asked, who gets told, who the result is "
            "credited to. It never decides which credentials the agent runs with."
        ),
    )

    # ---------- Delivery ----------

    state = fields.Selection(_STATE_SELECTION, default="pending", required=True, readonly=True)
    attempt = fields.Integer(default=0, readonly=True, help="How many times delivery has been tried.")
    next_attempt_at = fields.Datetime(
        default=fields.Datetime.now,
        readonly=True,
        help="When the cron may try again. Set by the backoff ladder after each failure.",
    )
    sent_at = fields.Datetime(readonly=True)
    last_status_code = fields.Integer(readonly=True, help="HTTP status of the last attempt, 0 if it never answered.")
    last_error = fields.Text(readonly=True, help="Error of the last failed attempt.")

    _uuid_unique = models.Constraint(
        "unique(uuid)",
        "A Tuqui event UUID must be unique - it is the idempotency key Tuqui dedups on.",
    )
    # The index the dispatch cron actually needs, and the one its description
    # promises: `state = 'pending' AND next_attempt_at <= now()` ordered by
    # `next_attempt_at`, run every minute on every database that installs the
    # companion. Two per-field indexes each answered half of that, which is not
    # the same thing.
    #
    # And only this one. `state` alone is a prefix of it, so the retention sweep
    # and the list view's filters ride it too, and `next_attempt_at` is never
    # queried without a state - while every extra index is paid for on insert,
    # and a mass action inserts thousands at once. (A field that stops declaring
    # `index=True` keeps the index it already has: Odoo logs "Keep unexpected
    # index" rather than dropping one it did not create. Only databases that
    # installed an earlier commit of this branch carry the strays - the table
    # itself ships for the first time here.)
    _state_next_attempt_idx = models.Index("(state, next_attempt_at)")

    # ---------- Enqueue ----------

    @api.model
    def enqueue(self, agent_external_id, res_model, res_id):
        """Record one event owed to Tuqui. Does no HTTP.

        Called from the server action, so it runs inside the transaction of the
        write that triggered it. That is the entire reliability guarantee: if
        that transaction rolls back, this row goes with it and nothing was
        promised; if it commits, the row is there and the cron will keep trying
        until it lands.

        Args:
            agent_external_id: The Tuqui agent to fire.
            res_model: Technical model name of the record that moved.
            res_id: Database id of the record that moved.

        Returns:
            The created ``tuqui.event`` record.
        """
        return self.sudo().create(
            {
                "agent_external_id": agent_external_id,
                "res_model": res_model,
                "res_id": res_id,
                # Read here, from the transaction that made the change — not
                # later, when the cron runs as its own user.
                "triggered_by_user_id": self.env.user.id,
            }
        )

    # ---------- Manual retry ----------

    def action_retry(self):
        """Put failed rows back in the queue, from the list view.

        Deliberately available on a row that already exhausted its attempts:
        that is the whole recourse when Tuqui was down longer than the ladder,
        which is the incident this queue was built for.
        """
        for event in self:
            if event.state == "sent":
                continue
            event.write(
                {
                    "state": "pending",
                    "attempt": 0,
                    "next_attempt_at": fields.Datetime.now(),
                    "last_error": False,
                }
            )
        return True

    # ---------- Retention ----------

    @api.model
    def _gc_old_events(self):
        """Drop delivered events past the retention window.

        **Only delivered ones.** A failed row is the evidence that something was
        owed and never arrived, and it is the only thing a manual retry can act
        on — garbage-collecting it would quietly erase the very loss this queue
        exists to make visible. Failed rows leave when a human decides they can.
        """
        days = self.env["ir.config_parameter"].sudo().get_param("tuqui.event_retention_days", _DEFAULT_RETENTION_DAYS)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = _DEFAULT_RETENTION_DAYS
        if days <= 0:
            return
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.sudo().search([("state", "=", "sent"), ("create_date", "<", cutoff)])
        if stale:
            _logger.info("tuqui.event: garbage-collecting %s delivered event(s)", len(stale))
            stale.unlink()

    # ---------- Dispatch ----------

    @api.model
    def _cron_dispatch(self):
        """Deliver the events that are due. Entry point of the ir.cron.

        Cheap when there is nothing to do — one indexed SELECT that returns no
        rows — because it runs every minute on every database that installs the
        companion, most of which will never enqueue anything.
        """
        due = self.sudo().search(
            [
                ("state", "=", "pending"),
                ("next_attempt_at", "<=", fields.Datetime.now()),
            ],
            limit=_BATCH_SIZE,
            order="next_attempt_at asc, id asc",
        )
        if not due:
            return

        _logger.info("tuqui.event: dispatching %s event(s)", len(due))
        deadline = time.monotonic() + _CRON_BUDGET_SECONDS
        for position, event in enumerate(due, start=1):
            event._deliver()
            if time.monotonic() > deadline and position < len(due):
                _logger.info(
                    "tuqui.event: out of time after %s event(s), %s still due for the next run",
                    position,
                    len(due) - position,
                )
                break

    def _deliver(self, auto_commit=True):
        """Try once, and record what happened either way.

        Commits per event rather than per batch: without that, one slow event at
        the end of a batch would roll back the outcome of every event before it,
        and they would all be sent a second time on the next run.

        Args:
            auto_commit: Whether to make this event's outcome durable before the
                next one is attempted. True from the cron, which is the only
                caller that needs it. False from tests, where Odoo forbids
                committing the shared cursor — the same escape hatch
                ``mail.mail.send()`` exposes, for the same reason.
        """
        self.ensure_one()
        try:
            self._post()
        except Exception as exc:  # noqa: BLE001 — a timeout or a DNS failure belongs on the row, not in a traceback
            self._record_failure(str(exc)[:500], status_code=0)
        # OCA's rule against committing exists to stop a request handler from
        # breaking the caller's atomicity. This is a cron delivering side effects
        # that already left the database: without the commit, one failure at the
        # end of a batch rolls back the recorded outcome of every event before
        # it, and the next run sends them all a second time. Odoo core does the
        # same thing in `mail.mail.send()` for the same reason.
        if auto_commit:
            self.env.cr.commit()  # pylint: disable=invalid-commit

    def _payload(self):
        """The body Tuqui receives. A notification, never an instruction.

        It names the record, the agent, the delivery and who caused it — and
        nothing else. Tuqui goes and reads the record itself with the channel's
        own credentials, which is what keeps a compromised sender from being
        able to steer an agent.
        """
        self.ensure_one()
        return {
            "idempotency_key": self.uuid,
            "agent_id": self.agent_external_id,
            "model": self.res_model,
            "res_id": self.res_id,
            "triggered_by": {
                "odoo_user_id": self.triggered_by_user_id.id,
                "login": self.triggered_by_user_id.login,
                "name": self.triggered_by_user_id.name,
            },
        }

    def _post(self):
        """Deliver one event. Raises on anything that is not a 2xx."""
        self.ensure_one()
        client = self.env["tuqui.oauth.client"].sudo()._get_singleton()
        if not client:
            raise ValueError("This Odoo is not connected to Tuqui")
        if client.state != "active":
            # Checked before signing, not after being refused. Without this the
            # event goes out, comes back 401, and `_is_permanent` files it as
            # "Tuqui refused" - which sends whoever reads the queue looking for a
            # problem in Tuqui instead of at the connection in front of them.
            raise ValueError(f"The Tuqui connection is {client.state}, not active")

        body = json.dumps(self._payload(), separators=(",", ":"), sort_keys=True)
        timestamp = int(time.time())
        url = f"{client._get_tuqui_base_url()}/v1/companion/events"

        response = requests.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Tuqui-Client-Id": client.client_id,
                # Same shape Stripe uses, for the same reason: one header that
                # carries both halves, so a verifier cannot accidentally check
                # the signature without checking its age.
                "X-Tuqui-Signature": f"t={timestamp},v1={client._sign_outbound(body, timestamp)}",
            },
            timeout=_TIMEOUT_SECONDS,
        )
        if 200 <= response.status_code < 300:
            self._record_success(response.status_code)
            return
        self._record_failure(
            (response.text or "")[:500] or f"HTTP {response.status_code}",
            status_code=response.status_code,
        )

    @api.model
    def _is_permanent(self, status_code):
        """Whether this status means "never", as opposed to "not now".

        A transport failure arrives as status 0 and is always worth another
        try; a 5xx is the other side having a bad minute. A 4xx is Tuqui
        refusing on purpose, and it will refuse the next five times too.
        """
        return 400 <= status_code < 500 and status_code not in _RETRYABLE_CLIENT_STATUSES

    def _record_failure(self, message, status_code):
        """Move the row along the backoff ladder, or give up and wait for a human."""
        self.ensure_one()
        attempt = self.attempt + 1
        permanent = self._is_permanent(status_code)
        values = {
            "attempt": attempt,
            "last_error": message,
            "last_status_code": status_code,
        }
        if permanent or attempt >= MAX_ATTEMPTS:
            # Failed, not deleted: the row is the evidence that something was
            # owed and never delivered, and the only thing a manual retry can
            # act on.
            values["state"] = "failed"
            _logger.warning(
                "tuqui.event %s: %s (%s)",
                self.uuid,
                "refused by Tuqui, not retrying" if permanent else f"giving up after {attempt} attempts",
                message,
            )
        else:
            values["next_attempt_at"] = fields.Datetime.now() + _BACKOFF[attempt - 1]
        self.write(values)

    def _record_success(self, status_code):
        self.ensure_one()
        self.write(
            {
                "state": "sent",
                "attempt": self.attempt + 1,
                "sent_at": fields.Datetime.now(),
                "last_status_code": status_code,
                "last_error": False,
            }
        )
