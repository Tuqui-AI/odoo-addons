import ast
import logging
from datetime import timedelta

import psycopg2
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain
from odoo.tools import SQL, html2plaintext

from . import search_relevant_text as text

_logger = logging.getLogger(__name__)

TEXT_FIELD_TYPES = ("char", "text", "html")

# Fingerprint of the rule that turns text into index entries, derived from the
# rule itself (see ``search_relevant_text``).
NORMALIZATION_VERSION = text.NORMALIZATION_VERSION

# Records read and indexed per step of the cron. Overridable per database with
# the ``base_search_relevant.batch_size`` config parameter.
DEFAULT_BATCH_SIZE = 500

# Past a couple hundred thousand characters a document is an attachment pasted
# into a field, and its tail adds nothing a search would miss. Also keeps the
# tsvector far below its 1 MB limit.
MAX_TEXT_CHARS = 200_000

# How far back each freshness pass looks before the previous watermark.
# ``write_date`` is the start of the writing transaction, not its commit: a
# transaction that started before the last pass and committed after it has a
# write_date older than the watermark. Rows re-read inside the margin that did
# not change are skipped by comparing write_date, so the margin costs a scan,
# not a reindex.
SYNC_MARGIN = timedelta(hours=1)

# The cron runs every five minutes. A watermark older than this means it is not
# running (failing, deactivated by Odoo after repeated failures, or starved),
# and searches stop claiming a complete answer.
STALE_AFTER = timedelta(minutes=30)

# Rows deleted per step when an archived model's index is dropped.
CLEANUP_BATCH_SIZE = 20_000

# Deleted records never leak — the user's own search drops ids that no longer
# exist — so purging their rows is housekeeping, done once a day.
PURGE_INTERVAL = timedelta(days=1)


class SearchRelevantConfig(models.Model):
    """A model an administrator enabled for relevance search, and its indexing state.

    Activation is the administrator's decision: a consumer may suggest the model
    and the fields, it never enables them. Enabling a model does not touch its
    table: it records where the backfill starts and wakes the cron, which fills
    ``search.relevant.document`` in batches from the newest record to the oldest.
    An optional ``domain`` narrows what is indexed to part of the model.

    The same cron keeps the index honest:

    * **Backfill** — ids below ``backfill_cursor`` are still pending.
    * **Freshness** — records whose ``write_date`` is newer than the last pass
      (minus ``SYNC_MARGIN``) and differs from the indexed one are re-indexed.
      What was written after the last pass is covered at search time
      (``search.relevant._live_records``). A record written out of the
      ``domain`` loses its row in the same pass.
    * **Deletions** — once a day, rows whose record no longer exists, or no
      longer matches the ``domain``, are dropped.
    * **Layout** — each row records the field layout and normalization it was
      built with (``layout``). Rows of an older layout are not searched: when
      the fields change, a field's text must stop matching at once, not when the
      backfill gets to it.
    """

    _name = "search.relevant.config"
    _description = "Relevance Search: Enabled Model"
    _order = "model"
    _rec_name = "model"

    active = fields.Boolean(
        default=True,
        help="Archiving a model stops its search at once; the cron then deletes its index rows in the background.",
    )
    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
        domain=[("transient", "=", False), ("abstract", "=", False)],
    )
    model = fields.Char(related="model_id.model", string="Model Name", store=True, index=True, readonly=True)
    field_ids = fields.Many2many(
        "ir.model.fields",
        string="Indexed fields",
        domain="[('model_id', '=', model_id), ('ttype', 'in', ('char', 'text', 'html')), ('store', '=', True)]",
        help="Up to four text fields whose content is searched. Changing them re-indexes the model in the background.",
    )
    domain = fields.Char(
        help="Optional Odoo domain that limits which records are indexed. Empty indexes every record of the model. "
        "Example, to index only the chatter messages of tasks: "
        "[('message_type', '=', 'comment'), ('model', '=', 'project.task')]",
    )
    state = fields.Selection(
        [("indexing", "Indexing"), ("ready", "Ready")],
        compute="_compute_state",
    )
    backfill_cursor = fields.Integer(
        readonly=True,
        copy=False,
        help="Records with an id below this one are not indexed yet. Zero means the backfill is done.",
    )
    backfill_total = fields.Integer(readonly=True, copy=False)
    backfill_done = fields.Integer(readonly=True, copy=False)
    backfill_progress = fields.Float(compute="_compute_state", string="Progress")
    backfill_reached = fields.Datetime(
        readonly=True,
        copy=False,
        help="While indexing: records created on or after this date are already indexed.",
    )
    sync_watermark = fields.Datetime(
        string="Last freshness pass",
        readonly=True,
        copy=False,
        help="Everything written before this moment is in the index.",
    )
    layout = fields.Integer(default=1, readonly=True, copy=False)
    layout_fields = fields.Char(
        readonly=True,
        copy=False,
        help="Indexed fields in weight-label order (A, B, …) for the current layout.",
    )
    # No default on purpose: rows that exist before this column (indexed by an
    # earlier rule) must read as outdated, and the cron re-indexes them.
    normalization = fields.Char(readonly=True, copy=False)
    cleanup_pending = fields.Boolean(
        readonly=True,
        copy=False,
        help="Archived, and its index rows are still being deleted by the cron.",
    )
    last_purge_at = fields.Datetime(readonly=True, copy=False)
    document_count = fields.Integer(compute="_compute_document_count", string="Indexed records")

    _model_unique = models.Constraint(
        "UNIQUE (model_id)",
        "This model is already enabled for search (it may be archived).",
    )

    # ─── Computes & constraints ─────────────────────────────────────────

    @api.depends("backfill_cursor", "backfill_total", "backfill_done")
    def _compute_state(self):
        for config in self:
            config.state = "indexing" if config.backfill_cursor else "ready"
            if not config.backfill_cursor:
                config.backfill_progress = 100.0
            elif config.backfill_total:
                config.backfill_progress = min(100.0 * config.backfill_done / config.backfill_total, 99.9)
            else:
                config.backfill_progress = 0.0

    def _compute_document_count(self):
        counts = {}
        ids = tuple(config_id for config_id in self.ids if isinstance(config_id, int))
        if ids:
            self.env.cr.execute(
                SQL(
                    "SELECT config_id, count(*) FROM search_relevant_document WHERE config_id IN %s GROUP BY config_id",
                    ids,
                )
            )
            counts = dict(self.env.cr.fetchall())
        for config in self:
            config.document_count = counts.get(config.id, 0)

    @api.onchange("model_id")
    def _onchange_model_id(self):
        """Preselect the record name: it is what people remember a record by."""
        if not self.model_id or self.model_id.model not in self.env:
            return
        model = self.env[self.model_id.model]
        field = model._fields.get(model._rec_name or "")
        if field and field.type in TEXT_FIELD_TYPES and field.store:
            self.field_ids = self.env["ir.model.fields"]._get(model._name, field.name)

    @api.constrains("model_id", "field_ids", "domain")
    def _check_model_and_fields(self):
        _ = self.env._
        for config in self:
            model_name = config.model_id.model
            if model_name not in self.env:
                raise ValidationError(_("Model %s is not loaded in this database.", model_name))
            model = self.env[model_name]
            if (
                model._abstract
                or model._transient
                or not model._auto
                or model._table_query
                or model._name == "search.relevant.document"
            ):
                raise ValidationError(_("%s is not a regular database model and cannot be searched.", model_name))
            if not model._log_access:
                # Without write_date the index could never learn about changes.
                raise ValidationError(
                    _("%s does not track modification dates, so its index could not stay current.", model_name)
                )
            if not config.field_ids:
                raise ValidationError(_("Choose at least one field to index for %s.", model_name))
            if len(config.field_ids) > len(text.WEIGHTS):
                # One weight label per field is what lets a search skip the
                # fields a user cannot read, and PostgreSQL has four.
                raise ValidationError(_("At most %s fields can be indexed per model.", len(text.WEIGHTS)))
            for field_rec in config.field_ids:
                field = model._fields.get(field_rec.name)
                if field_rec.model != model_name or field is None:
                    raise ValidationError(_("Field %s does not belong to %s.", field_rec.name, model_name))
                if (
                    field.type not in TEXT_FIELD_TYPES
                    or not field.store
                    or not field.column_type
                    or field.inherited
                    or field.company_dependent
                ):
                    raise ValidationError(
                        _(
                            "Field %(field)s cannot be indexed: only text, char and HTML fields stored in the %(model)s table can.",
                            field=field.name,
                            model=model_name,
                        )
                    )
            config._check_domain()

    def _check_domain(self):
        """The domain must read as a list and the model must accept it.

        Checked when it is written, not when the cron uses it: a domain with a
        misspelled field would make every run fail, and Odoo deactivates a cron
        that keeps failing — the index would then age with nobody looking.
        """
        self.ensure_one()
        _ = self.env._
        if not self.domain:
            return
        model_name = self.model_id.model
        try:
            parsed = ast.literal_eval(self.domain)
        except (SyntaxError, ValueError) as error:
            raise ValidationError(_("The domain is not valid: %s", error)) from error
        if not isinstance(parsed, list):
            raise ValidationError(_("The domain must be a list, like [('field', '=', value)]."))
        try:
            Domain(parsed).validate(self.env[model_name])
        except (KeyError, TypeError, ValueError, NotImplementedError) as error:
            raise ValidationError(
                _("%(model)s does not accept this domain: %(error)s", model=model_name, error=error)
            ) from error

    # ─── CRUD ─────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        configs = super().create(vals_list)
        configs.filtered("active")._start_indexing()
        return configs

    def write(self, vals):
        if "model_id" in vals and any(config.model_id.id != vals["model_id"] for config in self):
            raise UserError(self.env._("The model of an enabled search cannot be changed. Create a new one instead."))
        res = super().write(vals)
        if "field_ids" in vals:
            for config in self:
                config.write({"layout": config.layout + 1, "layout_fields": config._expected_layout_fields()})
        if "domain" in vals:
            # The rows of the records the new domain leaves out are garbage from
            # now on, and nothing writes those records: asking for the purge
            # makes the next cron run drop them, instead of the next day. The
            # layout is not bumped — the text of the records that stay is still
            # the right text, and dropping the whole index would be worse than
            # a few minutes of extra rows.
            self.write({"last_purge_at": False})
        if "active" in vals:
            if vals["active"]:
                self._start_indexing()
            else:
                self._stop_indexing()
        elif "field_ids" in vals or "domain" in vals:
            self.filtered("active")._start_indexing()
        return res

    def action_reindex(self):
        self.filtered("active")._start_indexing()

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def _start_indexing(self):
        """Point the backfill at the newest record and wake the cron.

        Existing rows are kept: rows of the current layout stay searchable while
        the backfill overwrites them; rows of an older layout are ignored by
        searches until then, and rows of records deleted meanwhile go with the
        daily purge.
        """
        if not self:
            return
        cr = self.env.cr
        now = cr.now()
        for config in self:
            ((max_id, total),) = self.env.execute_query(
                SQL(
                    "SELECT COALESCE(MAX(t.id), 0), count(*) FROM %s t WHERE %s",
                    SQL.identifier(config._source_table()),
                    config._in_domain(SQL.identifier("t", "id")),
                )
            )
            config.write(
                {
                    # Everything that exists now is the backfill's job; anything
                    # written from now on is the freshness pass's.
                    "backfill_cursor": max_id + 1 if total else 0,
                    "backfill_total": total,
                    "backfill_done": 0,
                    "backfill_reached": now if total else False,
                    "sync_watermark": config.sync_watermark or now,
                    "normalization": NORMALIZATION_VERSION,
                    "layout_fields": config.layout_fields or config._expected_layout_fields(),
                    "cleanup_pending": False,
                }
            )
        self._wake_cron()

    def _stop_indexing(self):
        """Stop searching now; delete the rows later, in batches.

        Search ignores archived models from this very transaction. Deleting a
        few million rows is not something to do inside an admin's click, so the
        cron does it (``_cleanup_batch``).
        """
        self.write(
            {
                "backfill_cursor": 0,
                "backfill_done": 0,
                "backfill_reached": False,
                "sync_watermark": False,
                "cleanup_pending": True,
            }
        )
        self._wake_cron()

    def _cleanup_batch(self, batch_size):
        """Delete up to ``batch_size`` rows of an archived model. Returns ``(count, more)``."""
        self.ensure_one()
        self.env.cr.execute(
            SQL(
                """
                DELETE FROM search_relevant_document
                 WHERE id IN (SELECT id FROM search_relevant_document WHERE config_id = %s LIMIT %s)
                """,
                self.id,
                batch_size,
            )
        )
        deleted = self.env.cr.rowcount
        more = deleted >= batch_size
        if not more:
            self.cleanup_pending = False
        return deleted, more

    def _wake_cron(self):
        cron = self.env.ref("base_search_relevant.ir_cron_search_relevant_index", raise_if_not_found=False)
        if not cron:
            return
        cron = cron.sudo()
        # Only ever switch it on here. The cron switches itself off when no
        # model is enabled; writing active=False from a user request would wait
        # on the row lock of a run in progress.
        if not cron.active:
            cron.active = True
        cron._trigger()

    # ─── Health ───────────────────────────────────────────────────────────

    def _is_stale(self):
        """Whether the last freshness pass is too old to call an answer complete.

        The one place that decides it: the coverage of every search
        (``search.relevant._coverage``) and the warning a health probe shows
        (``_stale_report``) have to agree on what "behind" means.
        """
        self.ensure_one()
        return not self.sync_watermark or self.env.cr.now() - self.sync_watermark > STALE_AFTER

    @api.model
    def _stale_report(self):
        """Short warnings about an index that fell behind. Empty when all is well.

        The cron is the only thing that keeps the index current, and when it
        stops nothing breaks in the open: the watermark just ages and every
        answer quietly turns ``partial``. This is what lets a probe say it out
        loud. One search over the enabled models and one read of the cron; no
        model's table is touched, so it is cheap enough for a health endpoint.

        No model name and no count on purpose: whoever shows this may be
        answering a public request.
        """
        env = self.env(su=True)
        configs = env["search.relevant.config"].search([])
        if not configs:
            return []
        cron = env.ref("base_search_relevant.ir_cron_search_relevant_index", raise_if_not_found=False)
        behind = "The relevance search index is behind"
        still = "Searches still answer, but they report partial coverage."
        where = "Settings > Technical > Scheduled Actions"
        if not cron or not cron.active:
            return [
                f"{behind}: the scheduled action that updates it is off. {still} "
                f"Turn 'Maintain the relevance search index' back on in {where}."
            ]
        if any(config._is_stale() for config in configs):
            minutes = int(STALE_AFTER.total_seconds() // 60)
            return [
                f"{behind}: it was not updated in the last {minutes} minutes. {still} "
                f"Look at 'Maintain the relevance search index' in {where} to see why it failed, "
                "and check that this server runs cron workers."
            ]
        return []

    # ─── What is indexed ─────────────────────────────────────────────────

    def _source_table(self):
        self.ensure_one()
        return self.env[self.model]._table

    def _domain_query(self, parsed=None):
        """The records the configured domain allows, as a ``Query``, or ``None``.

        As the superuser and with archived records included: the domain says
        *which* records are indexed, and nothing else. The index holds archived
        records with or without a domain, and the reading of each record still
        happens with the permissions of whoever searches.
        """
        self.ensure_one()
        if parsed is None:
            parsed = ast.literal_eval(self.domain) if self.domain else None
        if parsed is None:
            return None
        return self.env[self.model].sudo().with_context(active_test=False)._search(Domain(parsed))

    def _in_domain(self, column):
        """SQL asking whether ``column`` (an id of the model) is indexed at all."""
        query = self._domain_query()
        return SQL("TRUE") if query is None else SQL("%s IN %s", column, query.subselect())

    def _out_of_domain(self, column):
        """SQL asking the opposite of :meth:`_in_domain`."""
        query = self._domain_query()
        return SQL("FALSE") if query is None else SQL("%s NOT IN %s", column, query.subselect())

    # ─── Text extraction ─────────────────────────────────────────────────

    def _expected_layout_fields(self):
        """The configured fields in weight-label order: record name first, then by name."""
        self.ensure_one()
        model = self.env[self.model]
        names = sorted(self.field_ids.mapped("name"), key=lambda name: (name != model._rec_name, name))
        return ",".join(name for name in names if name in model._fields)

    def _weight_map(self):
        """``{field name: weight label}`` of the layout the index rows were built with.

        Stored, not recomputed: when a field disappears (a manual field deleted,
        a module update that drops one) its row in ``ir.model.fields`` goes away
        by cascade, without passing through ``write``. Recomputing positions
        would shift the labels of the fields after it; the stored map keeps
        each surviving field on its own label until the model is re-indexed.
        """
        self.ensure_one()
        names = (self.layout_fields or self._expected_layout_fields()).split(",")
        return {name: weight for weight, name in zip(text.WEIGHTS, names, strict=False) if name}

    def _layout_intact(self):
        """Whether the indexed rows still describe exactly the configured fields."""
        self.ensure_one()
        return bool(self.layout_fields) and self.layout_fields == self._expected_layout_fields()

    def _indexed_fields(self):
        """Field objects of the layout that still exist, in weight-label order."""
        self.ensure_one()
        model = self.env[self.model]
        configured = set(self.field_ids.mapped("name"))
        return [model._fields[name] for name in self._weight_map() if name in configured and name in model._fields]

    def _field_text(self, field, value):
        """Plain text for one field value. **Extension point.**

        ``value`` is what the database holds when indexing (a ``dict`` of
        translations for a translatable field) and what the ORM returns when the
        user reads the record; both go through here, so what is matched at search
        time is the same text that was indexed. Override to clean text of a
        specific model — e.g. quoted replies in ``mail.message.body`` (task
        75091) — and return the cleaned string.
        """
        if not value:
            return ""
        if isinstance(value, dict):
            # Translations: every language is searchable.
            value = "\n".join(dict.fromkeys(v for v in value.values() if v))
        if field.type == "html":
            return html2plaintext(value, include_references=False)
        return str(value)

    def _text_parts(self, values, fields_=None):
        """``[(weight, text)]`` for one record (``{field_name: value}``).

        ``fields_`` restricts the parts to some of the indexed fields (the ones a
        user can read) without changing their weight labels.
        """
        self.ensure_one()
        indexed = self._indexed_fields()
        weights = self._weight_map()
        wanted = {field.name for field in (fields_ if fields_ is not None else indexed)}
        parts, budget = [], MAX_TEXT_CHARS
        for field in indexed:
            weight = weights[field.name]
            if field.name not in wanted or budget <= 0:
                continue
            content = self._field_text(field, values.get(field.name))[:budget]
            if content:
                parts.append((weight, content))
                budget -= len(content)
        return parts

    def _marker(self):
        """A lexeme only this model's current-layout rows carry.

        Every model shares one GIN index. Filtered by ``config_id`` alone, the
        GIN still hands over every model's postings for a term, and Postgres
        intersects them with the ``config_id`` index afterwards. Measured with
        tasks and partners sharing the table, a partner search for "odoo" went
        from 137k TIDs to 14k (16 ms → 10 ms) with the lexeme. It carries the
        layout too, so rows built with other fields are never candidates. The
        colon keeps it out of reach of any query term, which is letters and
        digits only.
        """
        self.ensure_one()
        return f"search:config:{self.id}:{self.layout}"

    def _upsert_documents(self, rows):
        """Index ``rows``: ``(res_id, write_date, {field: raw value})`` tuples.

        A record without any searchable text gets no row (and loses the one it
        had): an empty row would only weigh.
        """
        self.ensure_one()
        if not rows:
            return
        # Ascending ids: a stable lock order if two runs ever overlap.
        rows = sorted(rows, key=lambda row: row[0])
        marker = self._marker()
        entries, empty = [], []
        for res_id, write_date, values in rows:
            vector = text.build_tsvector(self._text_parts(values), extra_lexemes=[marker])
            if vector is None:
                empty.append(res_id)
            else:
                entries.append((res_id, write_date, vector))
        cr = self.env.cr
        if empty:
            cr.execute(
                SQL("DELETE FROM search_relevant_document WHERE config_id = %s AND res_id = ANY(%s)", self.id, empty)
            )
        if not entries:
            return
        try:
            with cr.savepoint(flush=False):
                self._insert_entries(entries)
        except psycopg2.Error:
            # One bad record must not stop the backfill at this batch forever
            # (Odoo deactivates a cron that keeps failing). Retry one by one and
            # skip what still fails.
            for entry in entries:
                try:
                    with cr.savepoint(flush=False):
                        self._insert_entries([entry])
                except psycopg2.Error as error:
                    _logger.warning(
                        "search.relevant: could not index %s,%s: %s", self.model, entry[0], error.pgerror or error
                    )

    def _insert_entries(self, entries):
        self.env.cr.execute(
            SQL(
                """
                INSERT INTO search_relevant_document (config_id, res_id, layout, source_write_date, indexed_at, tsv)
                SELECT %(config)s, v.res_id, %(layout)s, v.write_date, %(now)s, v.tsv
                  FROM unnest(%(ids)s::int[], %(dates)s::timestamp[], %(vectors)s::tsvector[]) AS v(res_id, write_date, tsv)
                ON CONFLICT (config_id, res_id) DO UPDATE
                   SET layout = EXCLUDED.layout,
                       source_write_date = EXCLUDED.source_write_date,
                       indexed_at = EXCLUDED.indexed_at,
                       tsv = EXCLUDED.tsv
                """,
                config=self.id,
                layout=self.layout,
                now=self.env.cr.now(),
                ids=[entry[0] for entry in entries],
                dates=[entry[1] for entry in entries],
                vectors=[entry[2] for entry in entries],
            )
        )

    def _select_source(self, where, order, limit):
        """Read id, dates and the indexed columns straight from the table.

        SQL and not the ORM on purpose: the indexer runs as superuser over the
        whole table, and must neither trigger computes nor load every column.
        The configured ``domain`` is part of every read: what it leaves out is
        never indexed, whichever step asks.
        """
        self.ensure_one()
        indexed_fields = self._indexed_fields()
        # Pending ORM writes of this transaction would be invisible to the SQL below.
        self.env[self.model].flush_model(["write_date", *(field.name for field in indexed_fields)])
        columns = [SQL.identifier("t", field.name) for field in indexed_fields]
        rows = self.env.execute_query(
            SQL(
                "SELECT t.id, t.write_date, t.create_date, %s FROM %s t WHERE (%s) AND (%s) ORDER BY %s LIMIT %s",
                SQL(", ").join(columns),
                SQL.identifier(self._source_table()),
                where,
                self._in_domain(SQL.identifier("t", "id")),
                order,
                limit,
            )
        )
        names = [field.name for field in indexed_fields]
        return [(row[0], row[1], row[2], dict(zip(names, row[3:], strict=True))) for row in rows]

    # ─── Indexing steps ──────────────────────────────────────────────────

    def _backfill_remaining(self):
        self.ensure_one()
        if not self.backfill_cursor:
            return 0
        ((count,),) = self.env.execute_query(
            SQL(
                "SELECT count(*) FROM %s t WHERE t.id < %s AND %s",
                SQL.identifier(self._source_table()),
                self.backfill_cursor,
                self._in_domain(SQL.identifier("t", "id")),
            )
        )
        return count

    def _backfill_batch(self, batch_size):
        """Index the next ``batch_size`` records below the cursor, newest first."""
        self.ensure_one()
        if not self.backfill_cursor:
            return 0
        rows = self._select_source(
            SQL("t.id < %s", self.backfill_cursor),
            SQL("t.id DESC"),
            batch_size,
        )
        self._upsert_documents([(res_id, write_date, values) for res_id, write_date, _create, values in rows])
        done = len(rows) < batch_size
        if done:
            # Fresh statistics for the planner once the backfill has filled the table.
            self.env.cr.execute("ANALYZE search_relevant_document")
        oldest = min((create for _id, _write, create, _values in rows if create), default=None)
        self.write(
            {
                "backfill_cursor": 0 if done else rows[-1][0],
                "backfill_done": self.backfill_done + len(rows),
                "backfill_reached": False if done else (oldest or self.backfill_reached),
            }
        )
        return len(rows)

    def _sync_batch(self, batch_size, after=None):
        """Re-index records written since the last pass.

        Returns ``(count, more, last)``; pass ``last`` back as ``after`` to go on
        with the same pass. Walking by ``(write_date, id)`` instead of relying on
        the NOT EXISTS alone matters for records without searchable text: they
        keep no row, so they would be selected again forever.
        """
        self.ensure_one()
        now = self.env.cr.now()
        since = (self.sync_watermark or now) - SYNC_MARGIN
        if after is None:
            self._drop_left_domain(since)
        rows = self._select_source(
            SQL(
                """
                t.write_date >= %s AND (%s OR (t.write_date, t.id) > (%s, %s)) AND NOT EXISTS (
                    SELECT 1 FROM search_relevant_document d
                     WHERE d.config_id = %s AND d.res_id = t.id
                       AND d.source_write_date = t.write_date AND d.layout = %s
                )
                """,
                since,
                after is None,
                after[0] if after else since,
                after[1] if after else 0,
                self.id,
                self.layout,
            ),
            SQL("t.write_date, t.id"),
            batch_size,
        )
        self._upsert_documents([(res_id, write_date, values) for res_id, write_date, _create, values in rows])
        more = len(rows) >= batch_size
        if not more:
            self.sync_watermark = now
        last = (rows[-1][1], rows[-1][0]) if rows else after
        return len(rows), more, last

    def _drop_left_domain(self, since):
        """Drop the rows of records written out of the ``domain``. Returns how many.

        A record leaves the domain by being written, so the window the freshness
        pass already walks is where it shows up — and it stops being searchable
        in the same pass that would have re-indexed it. What leaves the domain
        without a write (the domain itself changing) goes with ``_purge_deleted``.
        """
        self.ensure_one()
        if not self.domain:
            return 0
        self.env[self.model].flush_model(["write_date"])
        self.env.execute_query(
            SQL(
                """
                DELETE FROM search_relevant_document d
                 USING %(table)s t
                 WHERE d.config_id = %(config)s AND t.id = d.res_id
                   AND t.write_date >= %(since)s AND %(outside)s
                """,
                table=SQL.identifier(self._source_table()),
                config=self.id,
                since=since,
                outside=self._out_of_domain(SQL.identifier("t", "id")),
            )
        )
        return self.env.cr.rowcount

    def _purge_deleted(self):
        """Drop rows whose record no longer exists, or no longer belongs in the
        index. Returns how many.

        The daily catch-all: a deleted record and one the ``domain`` stopped
        matching are the same kind of garbage, a row nothing should ever answer
        with. Changing the domain asks for this pass on the next cron run.
        """
        self.ensure_one()
        self.env.execute_query(
            SQL(
                """
                DELETE FROM search_relevant_document d
                 WHERE d.config_id = %s
                   AND (NOT EXISTS (SELECT 1 FROM %s t WHERE t.id = d.res_id) OR %s)
                """,
                self.id,
                SQL.identifier(self._source_table()),
                self._out_of_domain(SQL.identifier("d", "res_id")),
            )
        )
        purged = self.env.cr.rowcount
        self.last_purge_at = self.env.cr.now()
        return purged

    def _rebuild_outdated(self):
        """Re-index, under a new layout, the models whose rows no longer match:
        built by an earlier rule, or with a field that has since disappeared."""
        outdated = self.filtered(
            lambda config: config.normalization != NORMALIZATION_VERSION or not config._layout_intact()
        )
        for config in outdated:
            config.write({"layout": config.layout + 1, "layout_fields": config._expected_layout_fields()})
        outdated._start_indexing()

    @api.model
    def _batch_size(self):
        param = self.env["ir.config_parameter"].sudo().get_param("base_search_relevant.batch_size")
        try:
            return max(int(param), 1) if param else DEFAULT_BATCH_SIZE
        except ValueError:
            return DEFAULT_BATCH_SIZE

    @api.model
    def _cron_index(self):
        """Keep every enabled model's index complete and current.

        Reports progress through ``ir.cron._commit_progress``: while a backfill
        has records left, ``remaining`` is positive and Odoo runs the job again
        as soon as it can; once everything is indexed it drops back to its
        regular interval, where each run is a freshness pass. With no model
        enabled it deactivates itself — enabling one wakes it again.
        """
        IrCron = self.env["ir.cron"]
        # A model whose module is being uninstalled is still configured but no
        # longer loaded; its configuration goes away with the module.
        configs = self.search([]).filtered(lambda config: config.model in self.env)
        archived = self.with_context(active_test=False).search([("active", "=", False), ("cleanup_pending", "=", True)])
        if not configs and not archived:
            IrCron._commit_progress(deactivate=True)
            return

        configs._rebuild_outdated()

        batch_size = self._batch_size()
        pending = {config.id: config._backfill_remaining() for config in configs}
        more = set()  # (step, config id) that stopped with work left

        def remaining():
            return sum(pending.values()) + len(more)

        time_left = IrCron._commit_progress(remaining=remaining())

        # Freshness first: a record someone just wrote is what they are most
        # likely to look for, and a long backfill must not delay it.
        for config in configs:
            last = None
            while time_left:
                processed, has_more, last = config._sync_batch(batch_size, last)
                (more.add if has_more else more.discard)(("sync", config.id))
                time_left = IrCron._commit_progress(processed, remaining=remaining())
                if not has_more:
                    break

        now = self.env.cr.now()
        for config in configs:
            if time_left and (not config.last_purge_at or config.last_purge_at <= now - PURGE_INTERVAL):
                purged = config._purge_deleted()
                if purged:
                    _logger.info("search.relevant: purged %s deleted %s records from the index", purged, config.model)
                time_left = IrCron._commit_progress(purged, remaining=remaining())

        for config in archived:
            while time_left:
                deleted, has_more = config._cleanup_batch(CLEANUP_BATCH_SIZE)
                (more.add if has_more else more.discard)(("cleanup", config.id))
                time_left = IrCron._commit_progress(deleted, remaining=remaining())
                if not has_more:
                    break

        for config in configs:
            while time_left and config.backfill_cursor:
                processed = config._backfill_batch(batch_size)
                pending[config.id] = 0 if not config.backfill_cursor else max(pending[config.id] - processed, 0)
                time_left = IrCron._commit_progress(processed, remaining=remaining())

        self._flush_pending_index()
        if not configs and not any(archived.mapped("cleanup_pending")):
            IrCron._commit_progress(deactivate=True)

    @api.model
    def _flush_pending_index(self):
        """Move the GIN pending list into the index, at the end of each cron run.

        With ``fastupdate`` (the default) the upserts of a run land in a pending
        list that every search scans linearly until a vacuum flushes it. Turning
        it off instead was measured: writes got 60% slower (244 → 389 ms per
        batch of 500) for no latency difference we could see at 100k tasks
        with 2.000 pending rows. Flushing once per run keeps cheap writes and a
        clean index for the searches between runs.
        """
        self.env.cr.execute("SELECT gin_clean_pending_list('search_relevant_document_tsv_gin'::regclass)")
