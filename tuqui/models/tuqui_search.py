import math
from collections import deque, namedtuple
from datetime import timedelta
from itertools import islice

from lxml import etree
from odoo import api, models, tools
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools import SQL

from . import tuqui_search_text as text
from .tuqui_search_config import NORMALIZATION_VERSION, STALE_AFTER

ENGINE = "companion_fts"

DEFAULT_LIMIT = 10
MAX_LIMIT = 50

# Longest query we look at. An agent sends a sentence, not a document.
MAX_QUERY_CHARS = 1000

# Ranked candidates walked, at most, when the final permission check removes
# records. The domain and the record rules are already inside the ranking query
# (``_search`` as the user), so this only bounds models that also filter in
# Python; when it is hit before ``limit`` records survive, the answer is
# ``partial``.
MAX_CANDIDATES = 1000

# The first page of candidates, as a multiple of ``limit``; each following page
# doubles, capped at MAX_WINDOW.
WINDOW_FACTOR = 3
MIN_WINDOW = 30
MAX_WINDOW = 400

# How many index rows the candidate query should touch, at most, when the query
# mixes rare and very common terms (see ``_indexed_candidates``).
CANDIDATE_BUDGET = 20_000

# Document frequencies are counted exactly, inside the user's scope, up to this
# many records per term. Past it the term is common anyway: its weight comes from
# a small block sample of the model's index rows instead, which is where the
# difference between 3.000 and 30.000 still matters to the order. Measured at
# 100k tasks: counting up to 2.000 cost ~40 ms for an 11-term query, up to 500 a
# quarter of that.
DF_CAP = 500
SAMPLE_ROWS = 3_000
MIN_SAMPLE_ROWS = 300

# Weight of a word that names the kind of record rather than its content
# ("tarea", "proyecto" when searching tasks): it still counts in matched_terms
# and still breaks ties, but never outweighs a word about the content.
TYPE_TERM_WEIGHT = 0.01

# Longest raw value read per field to build the fragment and the matched terms:
# a ticket that carries a whole mail thread in HTML is otherwise the dominant
# cost of the answer.
DESCRIBE_MAX_CHARS = 50_000

# Records written after the last freshness pass are read and matched at search
# time. The margin covers transactions that started before the pass and
# committed after it; past LIVE_MAX such records the cron is far behind and the
# answer is ``partial``.
LIVE_MARGIN = timedelta(minutes=2)
LIVE_MAX = 200

# Equal weight for every field in ts_rank_cd: the weight labels exist to know
# which field a word came from (see ``_readable_weights``), not to rank a field
# above another — that was never measured.
RANK_WEIGHTS = "{1,1,1,1}"


# One searched term: its tsquery operand (a quoted lexeme, or a prefix with
# ``:*``), the prefix if the term fell back to one, and its weight in the order.
Item = namedtuple("Item", "term operand prefix weight")


def _sort_key(candidate):
    res_id, score, rank = candidate
    return (-score, -rank, -res_id)


class TuquiSearch(models.AbstractModel):
    """Relevance search over the models an administrator enabled.

    No table, no data of its own: the public entry point is
    :meth:`search_relevant`. Its name starts with ``search`` on purpose — the
    ``/tuqui/rpc`` gate classifies it as a read without a new module version —
    and it must therefore never write anything.
    """

    _name = "tuqui.search"
    _description = "Tuqui Relevance Search"

    @api.model
    @api.readonly
    def search_relevant(self, model, query, domain=None, limit=DEFAULT_LIMIT):
        """Records of ``model`` that best match ``query``, as the current user sees them.

        **Order.** The candidates are the records with any of the terms (an OR:
        ``websearch_to_tsquery`` is an AND and came back empty for two thirds of
        the proof-of-concept queries). They are ordered by how much of the query
        they cover, weighting each term by its rarity inside the user's scope
        (``idf = ln(1 + N / (df + 1))``), then ``ts_rank_cd``, then the newest.
        Words that only name the kind of record weigh almost nothing. ``score``
        is the covered share of the query's total weight.

        **Permissions.** The candidates are restricted *inside* the ranking query
        to ``env[model]._search(domain)`` run as the calling user — access
        rights, record rules, multi-company and the caller's domain — so no
        internal cap can starve a narrow domain. On top of that, no id leaves
        without passing through ``env[model].search([('id', 'in', ids)] +
        domain)`` as the user, for models that also filter in Python. Words in
        fields the user cannot read do not match (each field carries its own
        weight label), and ``snippet`` and ``matched_terms`` come from the
        user's own ``read``. Called as the superuser (the ``/tuqui/rpc``
        connection path), it sees what the superuser sees.

        **Coverage.** ``complete`` means: every record that existed when the
        model was enabled is indexed, the cron ran recently, and what was
        written after its last pass was matched directly. Anything short of
        that is ``partial``. ``indexed_until`` is the last freshness pass;
        ``indexed_from`` (only while indexing) is how far back the backfill got.

        :param str model: technical name of the model to search
        :param str query: natural-language text, as the user would say it
        :param list domain: extra domain, applied as the user
        :param int limit: records to return, at most 50
        :returns: ``{"engine", "terms", "coverage": {"state", "indexed_until",
            "fields"}, "records": [{"id", "score", "matched_terms", "snippet"}]}``;
            ``coverage.state`` is ``not_enabled`` (and ``records`` empty) when
            no administrator enabled ``model`` — a signal to use another engine,
            not an error. ``terms`` are the normalized terms searched; empty
            means nothing was searched.
        """
        _ = self.env._
        if not isinstance(model, str) or not model:
            raise ValidationError(_("search_relevant: 'model' must be a model name."))
        if not isinstance(query, str):
            raise ValidationError(_("search_relevant: 'query' must be a string."))
        if domain is None:
            domain = []
        if not isinstance(domain, (list, tuple)):
            raise ValidationError(_("search_relevant: 'domain' must be a list."))
        if isinstance(limit, float) and limit.is_integer():
            limit = int(limit)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError(_("search_relevant: 'limit' must be an integer."))
        limit = min(max(limit, 1), MAX_LIMIT)

        terms, _ignored, fallbacks = text.normalize_query(query[:MAX_QUERY_CHARS])
        not_enabled = {
            "engine": ENGINE,
            "terms": terms,
            "coverage": {"state": "not_enabled", "indexed_until": None, "fields": []},
            "term_stats": {},
            "term_stats_df_cap": DF_CAP,
            "records": [],
        }
        if model not in self.env:
            return not_enabled
        records_model = self.env[model]
        # Before looking at the configuration: whether a model is enabled is not
        # something to tell a user who cannot read it.
        records_model.check_access("read")
        config = self.env["tuqui.search.config"].sudo().search([("model", "=", model)], limit=1)
        if not config:
            return not_enabled

        readable = [field for field in config._indexed_fields() if records_model._has_field_access(field, "read")]
        coverage = self._coverage(config, readable)
        result = {
            "engine": ENGINE,
            "terms": terms,
            "coverage": coverage,
            "term_stats": {},
            "term_stats_df_cap": DF_CAP,
            "records": [],
        }
        if not terms or not readable:
            return result

        domain = Domain(list(domain))
        allowed = self._allowed_subquery(records_model, domain)
        live_entries, examined, live_overflow = self._live_records(config, records_model, allowed, readable)
        weights = self._readable_weights(config, readable)
        if fallbacks:
            terms = self._resolve_codes(config, terms, fallbacks, weights, allowed, live_entries, examined)
            result["terms"] = terms
        items, frequencies, term_stats = self._search_items(
            config, records_model, terms, weights, allowed, live_entries, examined
        )
        result["term_stats"] = term_stats
        live = self._score_live(live_entries, items)
        progress = {"truncated": False}
        window = max(limit * WINDOW_FACTOR, MIN_WINDOW)
        indexed = self._indexed_candidates(config, items, frequencies, weights, allowed, examined, window, progress)
        visible = self._filter_visible(records_model, self._merge(indexed, live), domain, limit, window)
        if live_overflow or (progress["truncated"] and len(visible) < limit):
            coverage["state"] = "partial"

        described = self._describe(
            config, records_model.browse([candidate[0] for candidate in visible]), items, readable, examined
        )
        total_weight = sum(item.weight for item in items) or 1.0
        result["records"] = [
            {
                "id": res_id,
                "score": round(min(score / total_weight, 1.0), 4),
                "matched_terms": described[res_id][0],
                "snippet": described[res_id][1],
            }
            for res_id, score, _rank in visible
        ]
        return result

    # ─── Coverage & fields ───────────────────────────────────────────────

    @api.model
    def _coverage(self, config, readable):
        now = self.env.cr.now()
        watermark = config.sync_watermark
        partial = (
            bool(config.backfill_cursor)
            or config.normalization != NORMALIZATION_VERSION
            or not config._layout_intact()
            or not watermark
            or now - watermark > STALE_AFTER
        )
        coverage = {
            "state": "partial" if partial else "complete",
            "indexed_until": watermark.strftime("%Y-%m-%dT%H:%M:%SZ") if watermark else None,
            "fields": [field.name for field in readable],
        }
        if config.backfill_cursor and config.backfill_reached:
            coverage["indexed_from"] = config.backfill_reached.strftime("%Y-%m-%dT%H:%M:%SZ")
        return coverage

    @api.model
    def _readable_weights(self, config, readable):
        """Weight labels to search: ``""`` (all of them) when every field of the
        layout is there and readable, else only the labels of readable fields."""
        weights = config._weight_map()
        names = {field.name for field in readable}
        if set(weights) == names:
            return ""
        return "".join(weight for name, weight in weights.items() if name in names)

    # ─── Candidates ──────────────────────────────────────────────────────

    @api.model
    def _allowed_subquery(self, records_model, domain):
        """The records the user may get, as a subquery: the caller's domain and
        every access rule, from the user's own ``_search``."""
        return records_model._search(domain).subselect()

    @api.model
    def _search_items(self, config, records_model, terms, weights, allowed, live_entries, examined=()):
        """``(items, frequencies, term_stats)``: each term with its operand and weight, its df, and what
        the answer reports about it.

        ``term_stats`` is ``{term: {df, type, prefix, prefix_df}}``, for Tuqui's
        confidence signal: ``df`` and ``prefix_df`` are counts of records **the
        user may see** (index rows inside ``allowed`` plus the records read live),
        capped at ``DF_CAP`` (declared in the answer as ``term_stats_df_cap``);
        never the sampled estimate, which is not scoped. ``prefix`` is the prefix
        the term was searched with, or ``None``. No extra query: the counts are
        the ones the weights are built from.

        **Rarity inside the user's scope.** The document frequency of each term
        is counted on the index rows the user may see (``allowed``), up to
        ``DF_CAP``: a portal user who sees 40 tasks gets the order calibrated for
        those 40, not for the whole database. Terms past the cap get their
        frequency from a block sample of the model's rows.

        **Prefix for a term nothing has.** A word of 5 letters or more with no
        match in scope — in the index or among the records read live — is
        searched by its prefix (``prefix_for``).

        **Words that name the kind of record** (``_type_terms``) weigh
        ``TYPE_TERM_WEIGHT``.
        """
        suffix = f":{weights}" if weights else ""
        operands = {term: text.quote_lexeme(term) + suffix for term in terms}
        prefix_suffix = f":*{weights}"
        live_texts = ["\n\n".join(part for _weight, part in parts) for _res_id, parts in live_entries]
        counts = self._scoped_frequencies(config, list(operands.values()), allowed, examined)
        frequencies = {
            term: min(count + sum(1 for body in live_texts if text.matched_terms(body, [term])), DF_CAP)
            for term, count in zip(terms, counts, strict=True)
        }
        prefixes, prefix_counts = {}, {}
        for term in terms:
            prefix = text.prefix_for(term)
            if frequencies[term] or not prefix:
                continue
            operand = text.quote_lexeme(prefix) + prefix_suffix
            (count,) = self._scoped_frequencies(config, [operand], allowed, examined)
            count = min(
                count + sum(1 for body in live_texts if text.matched_terms(body, [term], {term: prefix})), DF_CAP
            )
            if count:
                operands[term], prefixes[term], prefix_counts[term] = operand, prefix, count
        scoped = dict(frequencies)
        frequencies.update(prefix_counts)
        common = [term for term in terms if frequencies[term] >= DF_CAP]
        cr = self.env.cr
        cr.execute(
            SQL("SELECT GREATEST(reltuples, 0)::bigint FROM pg_class WHERE oid = %s::regclass", config._source_table())
        )
        population = max(cr.fetchone()[0], config.backfill_total, 1)
        if common:
            estimated = self._sampled_frequencies(config, [operands[term] for term in common], population)
            for term, value in zip(common, estimated, strict=True):
                frequencies[term] = max(frequencies[term], value)
        population = max(population, *frequencies.values())
        type_terms = self._type_terms(records_model._name, self.env.lang or "en_US")
        term_stats = {
            term: {
                "df": scoped[term],
                "type": term in type_terms,
                "prefix": prefixes.get(term),
                "prefix_df": prefix_counts.get(term),
            }
            for term in terms
        }
        items = [
            Item(
                term,
                operands[term],
                prefixes.get(term),
                TYPE_TERM_WEIGHT
                if term in type_terms
                else round(math.log(1 + population / (frequencies[term] + 1)), 6),
            )
            for term in terms
        ]
        return items, frequencies, term_stats

    @api.model
    def _scoped_frequencies(self, config, operands, allowed, examined=()):
        """Index rows the user may see that match each operand, counted up to ``DF_CAP``.

        Rows of records read live (``examined``) are left out: they are out of
        date, and the caller counts those records from what it read."""
        if not operands:
            return []
        marker = text.quote_lexeme(config._marker())
        rows = self.env.execute_query(
            SQL(
                """
                SELECT o.n, (SELECT count(*) FROM (
                           SELECT 1 FROM tuqui_search_document d
                            WHERE d.tsv @@ (%(marker)s::tsquery && o.operand::tsquery)
                              AND d.config_id = %(config)s
                              AND d.res_id IN %(allowed)s
                              AND d.res_id <> ALL (%(examined)s::int[])
                            LIMIT %(cap)s) AS hits)
                  FROM unnest(%(operands)s::text[]) WITH ORDINALITY AS o(operand, n)
                 ORDER BY o.n
                """,
                marker=marker,
                config=config.id,
                allowed=allowed,
                cap=DF_CAP,
                examined=list(examined),
                operands=operands,
            )
        )
        return [count for _n, count in rows]

    @api.model
    def _sampled_frequencies(self, config, operands, population):
        """Estimated matching rows of the model for each operand, from a block sample.

        ``TABLESAMPLE SYSTEM ... REPEATABLE``: the same answer for the same data,
        so two searches of the same query rank the same way.
        """
        fraction = min(1.0, SAMPLE_ROWS / population)
        self.env.cr.execute(
            SQL(
                """
                SELECT count(*), %s
                  FROM tuqui_search_document d TABLESAMPLE SYSTEM (%s) REPEATABLE (0)
                 WHERE d.config_id = %s
                """,
                SQL(", ").join(SQL("count(*) FILTER (WHERE d.tsv @@ %s::tsquery)", operand) for operand in operands),
                100.0 * fraction,
                config.id,
            )
        )
        sampled, *counts = self.env.cr.fetchone()
        if sampled < MIN_SAMPLE_ROWS:
            return [0] * len(operands)
        return [count / fraction for count in counts]

    @api.model
    @tools.ormcache("model_name", "lang")
    def _type_terms(self, model_name, lang):
        """Terms of the words that name the kind of record, not its content.

        Derived, not listed: the model's description and the descriptions of the
        many2one fields of its search view (``project_id`` → "Project"), in the
        user's language and in English. Searching tasks, "tarea" and "proyecto"
        say what the person is looking for, not what the task says.
        """
        records_model = self.env[model_name].sudo()
        model_names = [model_name]
        try:
            arch = records_model.get_view(view_type="search")["arch"]
            for node in etree.fromstring(arch).iter("field"):
                field = records_model._fields.get(node.get("name") or "")
                if field and field.type == "many2one":
                    model_names.append(field.comodel_name)
        except (ValueError, etree.XMLSyntaxError):
            pass
        found = set()
        ir_model = self.env["ir.model"].sudo()
        for name in dict.fromkeys(model_names):
            record = ir_model._get(name)
            if not record:
                continue
            for language in {lang, "en_US"}:
                found.update(text.normalize_query(record.with_context(lang=language).name or "")[0])
        return frozenset(found)

    @api.model
    def _indexed_candidates(self, config, items, frequencies, weights, allowed, exclude, first_page, progress):
        """Ranked ``(res_id, score, rank)`` from the index, fetched lazily in pages.

        **Rare terms first, but never at the expense of the answer.** In a
        database where most records say "odoo", scanning for it to rank half the
        index is wasted work whenever the page fills with records that also carry
        a rarer term. So candidates first come only from the rarest terms whose
        matches fit ``CANDIDATE_BUDGET``. That is exact as long as the last record
        of the page scores more than the terms left out can add up to: a record
        without any of the rare terms scores at most the sum of their weights.
        When a page cannot vouch for that — it is short, or its last record
        scores too little — the page is computed again with every term, and so
        is the rest. The weights are fixed before the first page, so every page
        belongs to the same order.
        """
        anchor = self._anchor_items(config, items, frequencies)
        offset, size = 0, first_page
        while offset < MAX_CANDIDATES:
            size = min(size, MAX_CANDIDATES - offset)
            page = self._rank_page(config, items, anchor, allowed, exclude, offset, size)
            if len(anchor) < len(items):
                anchored = {item.term for item in anchor}
                left_out = sum(item.weight for item in items if item.term not in anchored)
                if len(page) < size or page[-1][1] <= left_out + 1e-9:
                    anchor = items
                    page = self._rank_page(config, items, anchor, allowed, exclude, offset, size)
            yield from page
            if len(page) < size:
                return
            offset += size
            size = min(size * 2, MAX_WINDOW)
        progress["truncated"] = True

    @api.model
    def _anchor_items(self, config, items, frequencies):
        """The rarest items whose matches fit ``CANDIDATE_BUDGET``; every item on small models."""
        if len(items) < 2 or max(config.backfill_total, sum(frequencies.values())) <= CANDIDATE_BUDGET:
            return items
        anchor, total = [], 0.0
        for item in sorted(items, key=lambda item: (frequencies[item.term], items.index(item))):
            if anchor and total + frequencies[item.term] > CANDIDATE_BUDGET:
                break
            anchor.append(item)
            total += frequencies[item.term]
        return anchor

    @api.model
    def _rank_page(self, config, items, anchor, allowed, exclude, offset, count):
        """``[(res_id, score, rank)]`` at positions ``[offset, offset + count)``.

        Only records ``allowed`` (the user's ``_search`` subquery) that carry at
        least one ``anchor`` term are candidates. ``ts_rank_cd`` is by far the
        expensive part and only breaks ties between records with the same score,
        so it is computed only for the records that score at least as much as
        the last one of the page — the same page, for a fraction of the work.
        """
        score = SQL(" + ").join(
            SQL("%s::float8 * (d.tsv @@ %s::tsquery)::int", item.weight, item.operand) for item in items
        )
        any_term = " | ".join(item.operand for item in items)
        any_anchor = " | ".join(item.operand for item in anchor)
        # execute_query, not cr.execute: the subquery carries fields to flush
        # (an ``active`` written earlier in this transaction must count).
        return self.env.execute_query(
            SQL(
                """
                WITH candidates AS MATERIALIZED (
                    SELECT d.id, %(score)s AS score
                      FROM tuqui_search_document d
                     WHERE d.tsv @@ %(query)s::tsquery
                       AND d.config_id = %(config)s
                       AND d.res_id IN %(allowed)s
                       AND d.res_id <> ALL (%(exclude)s::int[])
                ), tier AS (
                    SELECT COALESCE(
                        (SELECT score FROM candidates ORDER BY score DESC OFFSET %(last)s LIMIT 1), 0
                    ) AS floor
                )
                SELECT d.res_id, %(score)s AS score,
                       ts_rank_cd(%(rank_weights)s::float4[], d.tsv, %(any_term)s::tsquery, 33) AS rank
                  FROM tuqui_search_document d
                 WHERE d.id = ANY (ARRAY(
                           SELECT c.id FROM candidates c WHERE c.score >= (SELECT floor FROM tier)
                       ))
                 ORDER BY score DESC, rank DESC, d.res_id DESC
                OFFSET %(offset)s LIMIT %(count)s
                """,
                score=score,
                query=f"{text.quote_lexeme(config._marker())} & ({any_anchor})",
                config=config.id,
                allowed=allowed,
                exclude=list(exclude),
                last=offset + count - 1,
                rank_weights=RANK_WEIGHTS,
                any_term=any_term,
                offset=offset,
                count=count,
            )
        )

    @api.model
    def _live_records(self, config, records_model, allowed, readable):
        """``(entries, examined, overflow)`` for records the index has not seen.

        The records the user may see (``allowed``) written since shortly before
        the last freshness pass, whose index row is missing or older, are read
        as the user: the task someone created a minute ago is exactly what they
        look for next. ``entries`` are ``(res_id, parts)`` from that read;
        ``examined`` are their ids — their index rows are out of date, so the
        index must not answer for them.
        """
        since = (config.sync_watermark or self.env.cr.now()) - LIVE_MARGIN
        records_model.flush_model(["write_date"])
        # One query, and a sequential scan of the model's table: there is no
        # index on write_date, and creating one means touching a table this
        # module does not own. Splitting the write_date filter from the access
        # rules was measured (it can parallelize in psql) and bought nothing
        # from Odoo: with the timestamp as a literal the planner keeps a single
        # worker. ~60 ms at 100k tasks, linear with the table.
        rows = self.env.execute_query(
            SQL(
                """
                SELECT t.id FROM %(table)s t
                 WHERE t.id IN %(allowed)s
                   AND t.write_date >= %(since)s
                   AND NOT EXISTS (
                       SELECT 1 FROM tuqui_search_document d
                        WHERE d.config_id = %(config)s AND d.res_id = t.id
                          AND d.source_write_date = t.write_date AND d.layout = %(layout)s
                   )
                 ORDER BY t.write_date DESC, t.id DESC
                 LIMIT %(limit)s
                """,
                table=SQL.identifier(records_model._table),
                allowed=allowed,
                since=since,
                config=config.id,
                layout=config.layout,
                limit=LIVE_MAX + 1,
            )
        )
        ids = [row[0] for row in rows]
        examined = ids[:LIVE_MAX]
        entries = []
        if examined:
            for row in records_model.browse(examined).read([field.name for field in readable]):
                entries.append((row["id"], config._text_parts(row, readable)))
        return entries, examined, len(ids) > LIVE_MAX

    @api.model
    def _score_live(self, entries, items):
        """``[(res_id, score, rank)]`` for the live records that carry a term."""
        terms = [item.term for item in items]
        prefixes = {item.term: item.prefix for item in items if item.prefix}
        vectors = {}
        for res_id, parts in entries:
            if text.matched_terms("\n\n".join(part for _weight, part in parts), terms, prefixes):
                vectors[res_id] = text.build_tsvector(parts)
        if not vectors:
            return []
        # The live vectors only hold readable fields: no weight labels needed.
        plain = [self._plain_operand(item) for item in items]
        rows = self.env.execute_query(
            SQL(
                """
                SELECT v.res_id, %(score)s, ts_rank_cd(%(rank_weights)s::float4[], v.tsv, %(any_term)s::tsquery, 33)
                  FROM unnest(%(ids)s::int[], %(vectors)s::tsvector[]) AS v(res_id, tsv)
                """,
                score=SQL(" + ").join(
                    SQL("%s::float8 * (v.tsv @@ %s::tsquery)::int", item.weight, operand)
                    for item, operand in zip(items, plain, strict=True)
                ),
                rank_weights=RANK_WEIGHTS,
                any_term=" | ".join(plain),
                ids=list(vectors),
                vectors=list(vectors.values()),
            )
        )
        return sorted(rows, key=_sort_key)

    @staticmethod
    def _plain_operand(item):
        operand = text.quote_lexeme(item.prefix) + ":*" if item.prefix else text.quote_lexeme(item.term)
        return operand

    @api.model
    def _resolve_codes(self, config, terms, fallbacks, weights, allowed, live_entries, examined):
        """Replace each whole code no visible record has by its parts with digits.

        "PR-979" is searched whole first; if no record the user may see carries
        it — in the index or among the records read live — its digit part
        ("979") is searched instead.
        """
        resolved = []
        suffix = f":{weights}" if weights else ""
        for term in terms:
            parts = fallbacks.get(term)
            if not parts:
                resolved.append(term)
                continue
            found = any(
                term in text.matched_terms("\n\n".join(part for _weight, part in entry_parts), [term])
                for _res_id, entry_parts in live_entries
            ) or self.env.execute_query(
                SQL(
                    """
                    SELECT 1 FROM tuqui_search_document d
                     WHERE d.tsv @@ %s::tsquery AND d.config_id = %s
                       AND d.res_id IN %s AND d.res_id <> ALL (%s::int[])
                     LIMIT 1
                    """,
                    f"{text.quote_lexeme(config._marker())} & {text.quote_lexeme(term)}{suffix}",
                    config.id,
                    allowed,
                    list(examined),
                )
            )
            resolved.extend([term] if found else parts)
        return list(dict.fromkeys(resolved))

    @staticmethod
    def _merge(indexed, live):
        """Both candidate streams in one order, pulling the index lazily."""
        live = deque(live)
        if not live:
            yield from indexed
            return
        for candidate in indexed:
            while live and _sort_key(live[0]) < _sort_key(candidate):
                yield live.popleft()
            yield candidate
        yield from live

    @api.model
    def _filter_visible(self, records_model, candidates, domain, limit, window):
        """The first ``limit`` candidates the current user can see, in rank order.

        The ranking already ran inside the user's ``_search``; this is the
        invariant's second guard, for what a model filters in Python.
        """
        visible = []
        while len(visible) < limit:
            chunk = list(islice(candidates, window))
            if not chunk:
                break
            allowed = set(records_model.search(Domain("id", "in", [c[0] for c in chunk]) & domain).ids)
            visible.extend(candidate for candidate in chunk if candidate[0] in allowed)
            window = min(window * 2, MAX_WINDOW)
        return visible[:limit]

    # ─── What the user reads ─────────────────────────────────────────────

    @api.model
    def _describe(self, config, records, items, readable, examined):
        """``{id: (matched_terms, snippet)}`` from the user's own read.

        Only the fields that can hold a match are read: the record name, plus
        the fields whose weight label the index says matched (all readable
        fields for records read live). Each raw value is cut at
        ``DESCRIBE_MAX_CHARS`` before its HTML is converted.
        """
        if not records:
            return {}
        terms = [item.term for item in items]
        prefixes = {item.term: item.prefix for item in items if item.prefix}
        weight_map = config._weight_map()
        name_field = records._rec_name
        by_label = {weight_map[field.name]: field for field in readable}
        to_read = {res_id: {field.name for field in readable} for res_id in records.ids if res_id in set(examined)}
        indexed_ids = [res_id for res_id in records.ids if res_id not in to_read]
        if indexed_ids and by_label:
            any_plain = " | ".join(self._plain_operand(item) for item in items)
            rows = self.env.execute_query(
                SQL(
                    "SELECT d.res_id, %s FROM tuqui_search_document d WHERE d.config_id = %s AND d.res_id = ANY(%s)",
                    SQL(", ").join(
                        SQL('ts_filter(d.tsv, %s::"char"[]) @@ %s::tsquery', "{%s}" % label.lower(), any_plain)
                        for label in by_label
                    ),
                    config.id,
                    indexed_ids,
                )
            )
            labels = list(by_label)
            for res_id, *hits in rows:
                to_read[res_id] = {by_label[label].name for label, hit in zip(labels, hits, strict=True) if hit}
        readable_names = {field.name for field in readable}
        groups = {}
        for res_id in records.ids:
            names = set(to_read.get(res_id) or readable_names)
            if name_field in readable_names:
                names.add(name_field)
            groups.setdefault(frozenset(names), []).append(res_id)
        described = {}
        for names, ids in groups.items():
            fields_ = [field for field in readable if field.name in names]
            for row in records.browse(ids).read([field.name for field in fields_]):
                row = {
                    key: value[:DESCRIBE_MAX_CHARS] if isinstance(value, str) else value for key, value in row.items()
                }
                body = "\n\n".join(part for _weight, part in config._text_parts(row, fields_))
                described[row["id"]] = (
                    text.matched_terms(body, terms, prefixes),
                    text.snippet(body, terms, prefixes),
                )
        return described
