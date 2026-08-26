"""Retention of tuqui.access.log: the row cap and the autovacuum that enforces it.

The audit log is bounded by row count, not by age: ``_max_rows`` resolves the
cap from ``tuqui.access_log.max_rows`` and ``_gc_old_logs`` — an
``@api.autovacuum``, so it runs unattended in production — deletes the excess.
Both were shipped without tests, and every failure mode here is silent: a cap
that resolves too low or a delete that picks the wrong end of the table
destroys audit history without raising anything.

The GC operates on the whole table (``search_count([])``, no domain), so these
tests own the table instead of filtering: they clear it first and assert on
the rows they created. The TransactionCase rolls back.
"""

from odoo.addons.tuqui.models.tuqui_access_log import _DEFAULT_MAX_ROWS
from odoo.tests import TransactionCase, tagged

_MAX_ROWS_PARAM = "tuqui.access_log.max_rows"
# _max_rows() clamps to this floor, so it is the smallest cap a GC test can use.
_FLOOR = 100


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiAccessLogRetention(TransactionCase):
    """Row cap resolution and the autovacuum that trims the table to it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.AccessLog = cls.env["tuqui.access.log"].sudo()
        cls.ICP = cls.env["ir.config_parameter"].sudo()
        cls.AccessLog.search([]).unlink()

    def _make_logs(self, count, prefix="gc"):
        """Create ``count`` rows, oldest first. Returns the method names in
        creation order — ids ascend with it, which is the order the GC uses."""
        names = [f"{prefix}_{i:04d}" for i in range(count)]
        self.AccessLog.create([{"method": name, "operation_type": "read"} for name in names])
        return names

    def _surviving_methods(self):
        return self.AccessLog.search([], order="id asc").mapped("method")

    # ─── Cap resolution ───────────────────────────────────────────────────

    def test_max_rows_resolution(self):
        """The cap honours a sane value, floors a small one and falls back on junk.

        The floor is what keeps a fat-fingered parameter from truncating the
        table to a useless size, and the fallback is what keeps a non-numeric
        one from raising inside an autovacuum.
        """
        cases = [
            ("a value above the floor is used as-is", "250", 250),
            ("a value below the floor is raised to it", "5", _FLOOR),
            ("zero is raised to the floor", "0", _FLOOR),
            ("a negative value is raised to the floor", "-1", _FLOOR),
            ("a non-numeric value falls back to the default", "abc", _DEFAULT_MAX_ROWS),
            ("an empty value is treated as unset", "", _DEFAULT_MAX_ROWS),
        ]
        for label, raw, expected in cases:
            with self.subTest(label):
                self.ICP.set_param(_MAX_ROWS_PARAM, raw)
                self.assertEqual(self.AccessLog._max_rows(), expected)

    def test_max_rows_without_parameter_is_the_default(self):
        """Unconfigured — the common case — resolves to the documented default."""
        self.ICP.search([("key", "=", _MAX_ROWS_PARAM)]).unlink()
        self.assertEqual(self.AccessLog._max_rows(), _DEFAULT_MAX_ROWS)

    # ─── Garbage collection ───────────────────────────────────────────────

    def test_gc_is_registered_as_autovacuum(self):
        """Nothing calls ``_gc_old_logs`` in production — the decorator does.

        The tests below invoke it by hand; the only thing that runs it on a real
        database is ``@api.autovacuum``, which registers it with the vacuum cron.
        Drop the decorator and the log grows without bound while this whole suite
        stays green, so the registration itself has to be asserted.
        """
        self.assertTrue(
            getattr(type(self.AccessLog)._gc_old_logs, "_autovacuum", False),
            "_gc_old_logs must keep @api.autovacuum or it never runs unattended",
        )

    def test_gc_trims_to_the_cap_keeping_the_newest(self):
        """Over the cap: the excess goes, and it is the OLDEST rows that go.

        Both halves matter. Trimming the right *number* of rows while picking
        them from the wrong end leaves a table of the correct size holding
        nothing but stale history — a green count over a destroyed audit
        trail, which is exactly the failure this test exists to catch.
        """
        self.ICP.set_param(_MAX_ROWS_PARAM, str(_FLOOR))
        created = self._make_logs(_FLOOR + 5)

        self.AccessLog._gc_old_logs()

        surviving = self._surviving_methods()
        self.assertEqual(len(surviving), _FLOOR, "the table must be trimmed down to the cap")
        self.assertEqual(surviving, created[5:], "the 5 oldest rows are the ones that must go")

    def test_gc_is_a_noop_at_or_below_the_cap(self):
        """At or below the cap nothing is deleted — not even the oldest row."""
        self.ICP.set_param(_MAX_ROWS_PARAM, str(_FLOOR))

        for label, count in (("below the cap", _FLOOR - 1), ("exactly at the cap", _FLOOR)):
            with self.subTest(label):
                self.AccessLog.search([]).unlink()
                created = self._make_logs(count, prefix=f"noop{count}")

                self.AccessLog._gc_old_logs()

                self.assertEqual(self._surviving_methods(), created)
