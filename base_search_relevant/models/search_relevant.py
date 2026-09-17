import math
from collections import deque, namedtuple
from datetime import timedelta
from itertools import islice

from lxml import etree
from odoo import api, models, tools
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools import SQL

from . import search_relevant_text as text
from .search_relevant_config import NORMALIZATION_VERSION

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

# Document frequencies are counted inside the user's scope, up to this many
# records per term: past it the term is common and nothing else about it changes
# its weight. It is also the cap of ``term_stats.df`` (documented, not sent in
# every answer), and the line between a term that brings in candidates and one
# that only ranks them. Measured at 100k tasks: counting up to 2.000 cost ~40 ms
# for an 11-term query, up to 500 a quarter of that.
DF_CAP = 500

# **One weight function for both sides of the contract** (X3, decision 3, agreed
# with #975): the ranking here and Tuqui's confidence signal weigh a term the
# same way, so they never disagree about which word carries the query. A word
# that names the kind of record weighs nothing — it still brings in candidates,
# still shows in ``matched_terms`` and still breaks ties through ``ts_rank_cd``.
# Everything else weighs ``ln(1 + population / (df + 1))`` with ``df`` counted
# inside the user's scope and capped at ``DF_CAP``; the answer carries
# ``population`` and ``term_stats`` so Tuqui computes the same number.

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

# ts_rank_cd weights, from label D to label A. The first indexed field is the
# record name (``_indexed_fields``), so the same word in the title breaks a tie
# against one in the body. PostgreSQL's own defaults.
RANK_WEIGHTS = "{0.1,0.2,0.4,1.0}"


def term_weight(df, population, type_word):
    """The weight of one term, shared with Tuqui (see the note above)."""
    if type_word:
        return 0.0
    return round(math.log(1 + population / (min(df, DF_CAP) + 1)), 6)


# One searched term: its tsquery operand (a quoted lexeme, or a prefix with
# ``:*``), the prefix if the term fell back to one, and its weight in the order.
Item = namedtuple("Item", "term operand prefix weight")


def _sort_key(candidate):
    res_id, score, rank = candidate
    return (-score, -rank, -res_id)


class SearchRelevant(models.AbstractModel):
    """Relevance search over the models an administrator enabled.

    No table, no data of its own: the public entry point is
    :meth:`search_relevant`, and it never writes anything. A consumer that
    reaches it over RPC calls it through its own facade (Tuqui does, on
    ``tuqui.search``); nothing here knows about any consumer.
    """

    _name = "search.relevant"
    _description = "Relevance Search"

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
        user's own ``read``. Called as the superuser, it sees what the
        superuser sees.

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
            "population": 0,
            "records": [],
        }
        if model not in self.env:
            return not_enabled
        records_model = self.env[model]
        # Before looking at the configuration: whether a model is enabled is not
        # something to tell a user who cannot read it.
        records_model.check_access("read")
        config = self.env["search.relevant.config"].sudo().search([("model", "=", model)], limit=1)
        if not config:
            return not_enabled

        readable = [field for field in config._indexed_fields() if records_model._has_field_access(field, "read")]
        coverage = self._coverage(config, readable)
        result = {
            "engine": ENGINE,
            "terms": terms,
            "coverage": coverage,
            "term_stats": {},
            "population": 0,
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
        items, frequencies, term_stats, population = self._search_items(
            config, records_model, terms, weights, allowed, live_entries, examined
        )
        result["term_stats"] = term_stats
        result["population"] = population
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
        watermark = config.sync_watermark
        partial = (
            bool(config.backfill_cursor)
            or config.normalization != NORMALIZATION_VERSION
            or not config._layout_intact()
            or config._is_stale()
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
        """``(items, frequencies, term_stats, population)``: each term with its operand and weight,
        its df, and what the answer reports about it.

        ``term_stats`` is ``{term: {df, type, prefix, prefix_df}}``, for Tuqui's
        confidence signal: ``df`` and ``prefix_df`` are counts of records **the
        user may see** (index rows inside ``allowed`` plus the records read live),
        capped at ``DF_CAP`` = 500 (documented, not sent in every answer).
        ``prefix`` is the prefix the term was searched with, or ``None``: on
        Tuqui's side a term found only by its prefix counts as absent. No extra
        query: the counts are the ones the weights are built from.

        **Rarity inside the user's scope.** The document frequency of each term
        is counted on the index rows the user may see (``allowed``), up to
        ``DF_CAP``: a portal user who sees 40 tasks gets the order calibrated for
        those 40, not for the whole database. Past the cap the term is common,
        and the weight cannot tell 500 from 50.000 apart anyway.

        **Prefix for a term nothing has.** A word of 5 letters or more with no
        match in scope — in the index or among the records read live — is
        searched by its prefix (``prefix_for``).

        **Words that name the kind of record** (``_type_terms``) weigh nothing.
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
        population = self._population(config)
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
                term_weight(frequencies[term], population, term in type_terms),
            )
            for term in terms
        ]
        return items, frequencies, term_stats, population

    @api.model
    def _population(self, config):
        """Records of the model, for the weight: what both sides divide by.

        The planner's estimate, or what the backfill counted — never a query of
        its own. It is the size of what is indexed, not the caller's scope: the
        scope is already in ``df``. With a ``domain``, the table's size is not
        what is indexed, and what the backfill counted is the only cheap number
        that is.
        """
        if config.domain:
            return max(config.backfill_total, 1)
        self.env.cr.execute(
            SQL("SELECT GREATEST(reltuples, 0)::bigint FROM pg_class WHERE oid = %s::regclass", config._source_table())
        )
        return max(self.env.cr.fetchone()[0], config.backfill_total, 1)

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
                           SELECT 1 FROM search_relevant_document d
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
        a rarer term. So candidates first come only from the terms that
        are rare in the user's scope. That is exact as long as the last record
        of the page scores more than the terms left out can add up to: a record
        without any of the rare terms scores at most the sum of their weights.
        When a page cannot vouch for that — it is short, or its last record
        scores too little — the page is computed again with every term, and so
        is the rest. The weights are fixed before the first page, so every page
        belongs to the same order.
        """
        anchor = self._anchor_items(items, frequencies)
        left_out = sum(item.weight for item in items if item not in anchor)
        if anchor != items and min(item.weight for item in anchor) <= left_out:
            # No record found through an anchor could outscore what the common
            # terms alone add up to, so the shortcut would be recomputed anyway:
            # one query with every term instead of two.
            anchor = items
        offset, size = 0, first_page
        while offset < MAX_CANDIDATES:
            size = min(size, MAX_CANDIDATES - offset)
            page = self._rank_page(config, items, anchor, allowed, exclude, offset, size)
            if len(anchor) < len(items):
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
    def _anchor_items(self, items, frequencies):
        """The items rare enough to bring in candidates, or all of them.

        A term the user's scope holds in fewer than ``DF_CAP`` records is cheap
        to scan for; one past that is a word half the database says. When every
        term is common there is nothing to prune.
        """
        anchor = [item for item in items if frequencies[item.term] < DF_CAP]
        return anchor or items

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
                      FROM search_relevant_document d
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
                  FROM search_relevant_document d
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
        index must not answer for them. The configuration's ``domain`` applies
        here too: what is not indexed is not searched, however recent it is.
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
                   AND %(indexed)s
                   AND NOT EXISTS (
                       SELECT 1 FROM search_relevant_document d
                        WHERE d.config_id = %(config)s AND d.res_id = t.id
                          AND d.source_write_date = t.write_date AND d.layout = %(layout)s
                   )
                 ORDER BY t.write_date DESC, t.id DESC
                 LIMIT %(limit)s
                """,
                table=SQL.identifier(records_model._table),
                allowed=allowed,
                since=since,
                indexed=config._in_domain(SQL.identifier("t", "id")),
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
                    SELECT 1 FROM search_relevant_document d
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
                    "SELECT d.res_id, %s FROM search_relevant_document d WHERE d.config_id = %s AND d.res_id = ANY(%s)",
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
