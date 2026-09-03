"""Tests for the outgoing event queue (task #72545).

Coverage mirrors the acceptance criteria of the spec
`trigger-odoo-tuqui-entrega-confiable`:

* enqueueing does no HTTP and records who caused the event;
* a failed delivery walks the backoff ladder and ends `failed`, never lost;
* every attempt leaves its number, its HTTP status and its error on the row;
* a failed event can be retried by hand, which is the recourse when Tuqui was
  down longer than the ladder;
* the signature covers the timestamp, so a captured request cannot be replayed
  forever;
* retention drops delivered events and never touches failed ones.
"""

from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models.tuqui_event import MAX_ATTEMPTS


class _Response:
    """Minimal stand-in for requests' response — the code reads two attributes."""

    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiEvent(TransactionCase):
    """The durable half of the trigger: a queue that survives Tuqui being down."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Event = cls.env["tuqui.event"].sudo()
        cls.client = cls.env["tuqui.oauth.client"].sudo()._get_or_create_singleton()[0]
        # Active, with a signing key: exactly what activation leaves behind, and
        # the only state in which a database has any business queueing events.
        cls.client.write({"state": "active", "event_signing_key": "test-signing-key"})

    def _event(self, **overrides):
        values = {
            "agent_external_id": "agent-1",
            "res_model": "res.partner",
            "res_id": 1,
        }
        values.update(overrides)
        return self.Event.create(values)

    # ─── Enqueue ──────────────────────────────────────────────────────────────

    def test_enqueue_does_no_http_and_leaves_the_event_pending(self):
        """The reliability guarantee: the server action only writes a row. If it
        posted, a rollback of the triggering write would leave an event already
        delivered for a change that never happened."""
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post") as post:
            event = self.Event.enqueue("agent-1", "res.partner", 1)
        post.assert_not_called()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempt, 0)

    def test_enqueue_records_who_caused_it(self):
        """Read from the transaction that made the change, not reconstructed later
        by the cron running as its own user."""
        event = self.Event.enqueue("agent-1", "res.partner", 1)
        self.assertEqual(event.triggered_by_user_id, self.env.user)

    def test_every_event_gets_its_own_key(self):
        """The uuid names the delivery, not the record — that is what lets Tuqui
        dedup a retry a day later."""
        first = self.Event.enqueue("agent-1", "res.partner", 1)
        second = self.Event.enqueue("agent-1", "res.partner", 1)
        self.assertTrue(first.uuid)
        self.assertNotEqual(first.uuid, second.uuid)

    # ─── Delivery ─────────────────────────────────────────────────────────────

    def test_a_delivered_event_is_marked_sent(self):
        event = self._event()
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post", return_value=_Response(202)):
            event._post()
        self.assertEqual(event.state, "sent")
        self.assertEqual(event.last_status_code, 202)
        self.assertTrue(event.sent_at)

    def test_a_failure_walks_the_ladder_instead_of_being_lost(self):
        event = self._event()
        before = fields.Datetime.now()
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post", return_value=_Response(503, "down")):
            event._post()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempt, 1)
        self.assertGreater(event.next_attempt_at, before)

    def test_each_attempt_records_status_and_error(self):
        event = self._event()
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post", return_value=_Response(500, "boom")):
            event._post()
        self.assertEqual(event.last_status_code, 500)
        self.assertIn("boom", event.last_error)

    def test_a_timeout_lands_on_the_row_and_not_in_a_traceback(self):
        """A network error is one attempt of six, not a crashed cron."""
        event = self._event()
        with patch(
            "odoo.addons.tuqui.models.tuqui_event.requests.post",
            side_effect=OSError("connection reset"),
        ):
            event._deliver(auto_commit=False)
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempt, 1)
        self.assertEqual(event.last_status_code, 0)
        self.assertIn("connection reset", event.last_error)

    def test_a_disconnected_odoo_does_not_post_at_all(self):
        """Checked before signing, not after being refused.

        Posting anyway harvests a 401, which `_is_permanent` files as "Tuqui
        refused" — sending whoever reads the queue looking for a problem in Tuqui
        instead of at the connection in front of them.
        """
        self.client.write({"state": "disconnected"})
        self.addCleanup(self.client.write, {"state": "active"})
        event = self._event()
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post") as post:
            event._deliver(auto_commit=False)
        post.assert_not_called()
        self.assertEqual(event.state, "pending")
        self.assertIn("disconnected", event.last_error)

    @mute_logger("odoo.addons.tuqui.models.tuqui_event")
    def test_it_gives_up_after_the_ladder_and_keeps_the_evidence(self):
        """@mute_logger: giving up is worth a warning in production, but this
        test drives it on purpose and runbot counts any WARNING in the log as a
        build failure."""
        event = self._event()
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post", return_value=_Response(503)):
            for _ in range(MAX_ATTEMPTS):
                event._post()
        self.assertEqual(event.state, "failed")
        self.assertEqual(event.attempt, MAX_ATTEMPTS)
        # Failed, not deleted: the row is the only trace that something was owed.
        self.assertTrue(event.exists())

    @mute_logger("odoo.addons.tuqui.models.tuqui_event")
    def test_a_refusal_does_not_burn_the_ladder(self):
        """A 4xx is Tuqui refusing on purpose — a feature switched off, a key it
        does not know — and it will refuse the next five times too. Retrying for
        eight hours only delays the moment somebody reads the error."""
        event = self._event()
        with patch(
            "odoo.addons.tuqui.models.tuqui_event.requests.post",
            return_value=_Response(404, '{"detail":"Unknown webhook"}'),
        ):
            event._post()
        self.assertEqual(event.state, "failed")
        self.assertEqual(event.attempt, 1)
        self.assertIn("Unknown webhook", event.last_error)
        # And the cron leaves it alone from here: the recourse is the manual
        # retry, once whoever owns the refusal has fixed it.
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post") as post:
            self.Event._cron_dispatch()
        post.assert_not_called()

    def test_a_rate_limit_is_not_a_refusal(self):
        """429 is the server asking for later, not for never."""
        event = self._event()
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post", return_value=_Response(429)):
            event._post()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempt, 1)

    def test_a_timeout_status_is_not_a_refusal(self):
        event = self._event()
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post", return_value=_Response(408)):
            event._post()
        self.assertEqual(event.state, "pending")

    def test_a_server_error_still_walks_the_ladder(self):
        event = self._event()
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post", return_value=_Response(503)):
            event._post()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempt, 1)

    # ─── The cron picks what is due ───────────────────────────────────────────

    def test_the_cron_leaves_an_event_that_is_not_due_yet(self):
        event = self._event()
        event.write({"next_attempt_at": fields.Datetime.add(fields.Datetime.now(), hours=1)})
        with patch("odoo.addons.tuqui.models.tuqui_event.requests.post") as post:
            self.Event._cron_dispatch()
        post.assert_not_called()
        self.assertEqual(event.state, "pending")

    # ─── Manual retry ─────────────────────────────────────────────────────────

    def test_a_failed_event_can_be_retried_by_hand(self):
        """The recourse when Tuqui was down longer than the ladder — which is the
        incident that motivated the whole spec."""
        event = self._event(state="failed", attempt=MAX_ATTEMPTS, last_error="down")
        event.action_retry()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempt, 0)
        self.assertFalse(event.last_error)

    def test_retry_never_resends_something_already_delivered(self):
        event = self._event(state="sent", attempt=1)
        event.action_retry()
        self.assertEqual(event.state, "sent")

    # ─── Signature ────────────────────────────────────────────────────────────

    def test_the_signature_covers_the_timestamp(self):
        """Signing only the body would make every captured request replayable
        forever; Tuqui refuses anything older than five minutes."""
        body = '{"a":1}'
        first = self.client._sign_outbound(body, 1000)
        second = self.client._sign_outbound(body, 1001)
        self.assertNotEqual(first, second)

    def test_the_signature_depends_on_the_key(self):
        body = '{"a":1}'
        with_first = self.client._sign_outbound(body, 1000)
        self.client.write({"event_signing_key": "a-different-key"})
        try:
            self.assertNotEqual(with_first, self.client._sign_outbound(body, 1000))
        finally:
            self.client.write({"event_signing_key": "test-signing-key"})

    def test_an_odoo_without_a_key_says_so_instead_of_signing_with_nothing(self):
        self.client.write({"event_signing_key": False})
        try:
            with self.assertRaises(ValueError):
                self.client._outbound_signing_key()
        finally:
            self.client.write({"event_signing_key": "test-signing-key"})

    def test_the_payload_names_the_record_and_the_person_and_nothing_else(self):
        """A notification, never an instruction: Tuqui reads the record itself."""
        event = self.Event.enqueue("agent-1", "res.partner", 7)
        payload = event._payload()
        self.assertEqual(
            set(payload),
            {"idempotency_key", "agent_id", "model", "res_id", "triggered_by"},
        )
        self.assertEqual(payload["model"], "res.partner")
        self.assertEqual(payload["res_id"], 7)
        self.assertEqual(payload["triggered_by"]["odoo_user_id"], self.env.user.id)

    # ─── Retention ────────────────────────────────────────────────────────────

    def _backdate(self, event, days):
        """create_date is a magic field: the ORM drops writes to it."""
        old = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        self.env.cr.execute("UPDATE tuqui_event SET create_date = %s WHERE id = %s", (old, event.id))
        event.invalidate_recordset(["create_date"])

    def test_retention_drops_delivered_events(self):
        stale = self._event(state="sent")
        self._backdate(stale, 40)
        fresh = self._event(state="sent")
        self.Event._gc_old_events()
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_retention_never_touches_a_failed_event(self):
        """Collecting it would erase the very loss the queue exists to make
        visible, and the only row a manual retry can act on."""
        failed = self._event(state="failed", attempt=MAX_ATTEMPTS)
        self._backdate(failed, 400)
        self.Event._gc_old_events()
        self.assertTrue(failed.exists())

    def test_retention_can_be_switched_off(self):
        self.env["ir.config_parameter"].sudo().set_param("tuqui.event_retention_days", "0")
        stale = self._event(state="sent")
        self._backdate(stale, 400)
        try:
            self.Event._gc_old_events()
            self.assertTrue(stale.exists())
        finally:
            self.env["ir.config_parameter"].sudo().set_param("tuqui.event_retention_days", "30")
