from odoo import fields, models
from odoo.tools.sql import column_exists, create_column, create_index


class TuquiSearchDocument(models.Model):
    """One row of the relevance index: the searchable text of one record.

    **Why a table of our own, and not a column on the indexed model.** The
    obvious design — a generated ``tsvector`` column on ``project_task`` with a
    GIN index — has two costs we are not willing to pay on a customer database:

    * ``ALTER TABLE ... ADD COLUMN ... GENERATED ... STORED`` rewrites the whole
      table under an ``ACCESS EXCLUSIVE`` lock. On ``mail_message`` that is a
      maintenance window, not something an admin clicks in Settings.
    * Nothing an admin enables should leave marks on tables other modules own:
      disabling a model has to be a ``DELETE`` here, and uninstalling the module
      has to be dropping this table.

    The price, measured and written down in the PR: the text is stored twice
    (the source column and this ``tsvector``), and freshness is no longer free —
    a generated column updates itself, this table is kept current by the cron
    (see ``tuqui.search.config._cron_index``) and by the search itself for what
    the cron has not seen yet. An expression index
    (``CREATE INDEX ... USING gin (to_tsvector(...))``) would avoid the copy but
    still locks writes on the source table while it builds, cannot hold cleaned
    HTML, and makes ``ts_rank_cd`` re-parse every candidate's text.

    **Nobody reads this model through the ORM.** Its only ACL row grants nothing,
    so an internal user — and even an administrator without ``sudo`` — gets an
    ``AccessError``. The text in here was extracted without anybody's record
    rules; the only way it reaches a user is ``tuqui.search.search_relevant``,
    which returns ids that went through the user's own ``search`` and fragments
    built from the user's own ``read``.
    """

    _name = "tuqui.search.document"
    _description = "Tuqui Search Index Entry"
    # Millions of rows on a big model: the four audit columns would be pure
    # weight. What matters (when the source row was written, when we indexed it)
    # is kept explicitly below.
    _log_access = False
    _rec_name = "res_id"

    config_id = fields.Many2one(
        "tuqui.search.config",
        required=True,
        readonly=True,
        # Deleting a configuration drops its rows in the same statement.
        ondelete="cascade",
    )
    res_id = fields.Integer(required=True, readonly=True)
    layout = fields.Integer(
        readonly=True,
        help="Field layout and normalization this row was built with (tuqui.search.config.layout).",
    )
    source_write_date = fields.Datetime(
        readonly=True,
        help="write_date of the source record when it was indexed. The cron compares it to skip unchanged records.",
    )
    indexed_at = fields.Datetime(readonly=True)

    # Also the arbiter of the upserts (ON CONFLICT (config_id, res_id)) and the
    # index every lookup by record uses.
    _config_res_unique = models.Constraint(
        "UNIQUE (config_id, res_id)",
        "A record can only be indexed once per enabled model.",
    )

    def init(self):
        """Add the ``tsvector`` column and its GIN index.

        The ORM has no field type for ``tsvector``, so both live outside the
        field declarations. They are created on install, when the table is
        empty; on later updates the catalog checks make this a no-op that takes
        no lock.
        """
        cr = self.env.cr
        if not column_exists(cr, self._table, "tsv"):
            create_column(cr, self._table, "tsv", "tsvector")
        create_index(cr, f"{self._table}_tsv_gin", self._table, ['"tsv"'], method="gin")
