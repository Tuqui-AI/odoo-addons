"""Relevance search over enabled models (task 75090).

Everything runs on ``res.partner`` so the suite depends on ``base`` only; the
``project.task`` case is skipped when ``project`` is not installed.

What is pinned here, in the order of the contract and its amendments after the
adversarial review of PR #102 (the reviewer's examples, reduced to base models):

* a model nobody enabled answers ``not_enabled``, not an error;
* enabling a model does not index it in the request; the cron fills the index
  in batches, reports progress and switches itself off;
* **no internal cap can starve a narrow domain or a user who sees little**: the
  domain and the record rules are inside the ranking query (B1);
* **the rare-terms shortcut never changes the answer** (B2);
* **a word in a field the user cannot read does not match** (I1);
* **freshness is honest**: what the cron has not seen yet is matched at search
  time, and a cron that stopped makes the answer ``partial`` (I2);
* codes with hyphens, dots and slashes split like on Tuqui's side, and the
  normalization rule is the same one (I3, I5);
* nothing is installed in the database besides the table (I4);
* records the user cannot see never come back, neither as ids nor in fragments;
  archived records neither; a model's index never answers for another one;
* ``snippet`` and ``matched_terms`` come from the user's own read, never from
  the index.
"""

import math
from datetime import timedelta
from unittest.mock import patch

import psycopg2
from odoo.exceptions import AccessError, ValidationError
from odoo.modules.db import has_unaccent
from odoo.tests import BaseCase, TransactionCase, tagged
from odoo.tools import SQL, mute_logger

from ..models import (
    search_relevant as search_module,
    search_relevant_text as text,
)
from ..models.search_relevant_config import SearchRelevantConfig

_RULE_SAMPLES = [
    "Motor, TRIFÁSICO; (Siemens)! de la estado",
    "PR-979 SO-1234 FURN_7800 inv/2024/0001 v2.0 10A ana.perez@acme.com wiki.adhoc.inc",
    "la tarea donde charlamos de conciliación bancaria con Ñandú",
    "x" * 70 + " ok " + "tambien también",
]


def _rule_behaviour():
    """A digest of what the rule does to a fixed set of texts."""
    import hashlib

    digest = hashlib.sha256()
    for sample in _RULE_SAMPLES:
        digest.update(repr(text.normalize_query(sample)).encode())
        digest.update(repr(text.build_tsvector([("A", sample), ("B", sample)])).encode())
    return digest.hexdigest()[:16]


PINNED_RULE = ("c95c331bf112cba9", "eb9cf59ab2bdbe9c")


@tagged("post_install", "-at_install", "base_search_relevant")
class TestSearchRelevantText(BaseCase):
    """This module's normalization rule. Compatible with Tuqui's (same shape of
    ``terms``), not a copy of it: the cases started from Tuqui's tests."""

    def test_normalization_rule(self):
        cases = [
            ("Motor, TRIFÁSICO; (Siemens)!", ["motor", "trifasico", "siemens"], []),
            (
                "la tarea donde charlamos de conciliación bancaria",
                ["tarea", "conciliacion", "bancaria"],
                ["la", "donde", "charlamos", "de"],
            ),
            ("tambien también ticket", ["ticket"], ["tambien"]),
            ("OT 10 v2 ab 10A", ["10", "v2", "10a"], ["ot", "ab"]),
            # Codes with digits are one term; emails too; the rest splits.
            ("PR-979", ["pr-979"], []),
            ("pedido SO-1234", ["pedido", "so-1234"], []),
            ("versión v2.0 del 10/2025", ["version", "v2.0", "10/2025"], ["del"]),
            ("FURN_7800", ["furn_7800"], []),
            ("ana.perez@acme.com", ["ana.perez@acme.com"], []),
            ("motor/bomba wiki.adhoc.inc", ["motor", "bomba", "wiki", "adhoc", "inc"], []),
            ("siemens Siemens SIEMENS camión camion", ["siemens", "camion"], []),
            ("lo de la que", [], ["lo", "de", "la", "que"]),
            ("Revisar PR 979 estado", ["revisar", "979", "estado"], ["pr"]),
        ]
        for query, terms, ignored in cases:
            got_terms, got_ignored, _fallbacks = text.normalize_query(query)
            self.assertEqual(got_terms, terms, query)
            self.assertEqual(set(got_ignored), set(ignored), query)
        # The parts with digits are what a code falls back to.
        self.assertEqual(
            text.normalize_query("PR-979 inv/2024/0001")[2], {"pr-979": ["979"], "inv/2024/0001": ["2024", "0001"]}
        )
        self.assertEqual(text.normalize_query("ana.perez@acme.com")[2], {})

    def test_the_cap_keeps_codes_and_long_words_whatever_the_order(self):
        words = [
            "uno1",
            "motores",
            "trifasicos",
            "proveedor",
            "siemens",
            "pepito",
            "bobinado",
            "carcasa",
            "eje",
            "rotor",
            "tornillo",
            "arandela",
            "junta",
            "cable",
            "fusible",
        ]
        self.assertGreater(len(words), text.MAX_TERMS)
        kept = {frozenset(text.normalize_query(" ".join(words[i:] + words[:i]))[0]) for i in range(len(words))}
        self.assertEqual(len(kept), 1)
        (terms,) = kept
        self.assertEqual(len(terms), text.MAX_TERMS)
        self.assertIn("uno1", terms)
        self.assertNotIn("eje", terms)

    def test_index_entries_hold_the_query_terms_with_their_field_and_position(self):
        vector = text.build_tsvector([("A", "PR-979 de Siemens"), ("B", "motor de siemens")], extra_lexemes=["x:y"])
        self.assertEqual(vector, "'979':2A 'pr-979':1A 'siemens':4A,15B 'motor':13B 'x:y'")
        self.assertIsNone(text.build_tsvector([("A", "de la y o"), ("B", "")]))

    def test_the_rule_version_follows_the_rule(self):
        """N3: an index built by another rule must be re-indexed, so the version
        cannot depend on someone remembering to bump it.

        Word lists, patterns and limits are inside the fingerprint. What the
        code does with them is pinned here by its output: if this fails because
        the output changed, bump ``RULE_REVISION``; then update both values."""
        self.assertEqual((text.NORMALIZATION_VERSION, _rule_behaviour()), PINNED_RULE)

    def test_changing_the_word_list_changes_the_version(self):
        with patch.object(text, "STOPWORDS", text.STOPWORDS | {"estado"}):
            self.assertNotEqual(text._rule_fingerprint(), text.NORMALIZATION_VERSION)
        with patch.object(text, "MAX_LEXEME_CHARS", 32):
            self.assertNotEqual(text._rule_fingerprint(), text.NORMALIZATION_VERSION)


@tagged("post_install", "-at_install", "base_search_relevant")
class TestSearchRelevant(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Results are asserted on the records these tests create. A database with
        # hundreds of modules has demo partners that legitimately contain a word a
        # test searches ("Odoo Inc" for "odoo", on runbot): they may rank in, and
        # that is correct. Ids only grow, so "created here" is "above this line".
        cls.baseline = {}
        for model in ("res.partner", "res.partner.category", "project.task"):
            if model in cls.env:
                cls.env.cr.execute(SQL("SELECT COALESCE(MAX(id), 0) FROM %s", SQL.identifier(cls.env[model]._table)))
                cls.baseline[model] = cls.env.cr.fetchone()[0]
        cls.Config = cls.env["search.relevant.config"]
        cls.main_company = cls.env.company
        cls.other_company = cls.env["res.company"].create({"name": "Relevance Search Other Co"})
        cls.restricted_group = cls.env["res.groups"].create({"name": "Relevance search restricted"})
        cls.user = cls.env["res.users"].create(
            {
                "name": "Relevance Search User",
                "login": "search_relevant_user",
                "company_id": cls.main_company.id,
                "company_ids": [(6, 0, [cls.main_company.id])],
                "group_ids": [(6, 0, [cls.env.ref("base.group_user").id, cls.restricted_group.id])],
            }
        )
        # A record rule only this user's group carries: partners flagged with
        # this ref are invisible to them.
        cls.env["ir.rule"].create(
            {
                "name": "Relevance search: hide flagged partners",
                "model_id": cls.env.ref("base.model_res_partner").id,
                "groups": [(6, 0, [cls.restricted_group.id])],
                "domain_force": "[('ref', '!=', 'search-hidden')]",
            }
        )
        Partner = cls.env["res.partner"]
        cls.both_terms = Partner.create(
            {
                "name": "Bomba industrial",
                "comment": "<p>El motor trifásico de <b>Siemens</b> quedó instalado en planta.</p>",
            }
        )
        cls.one_term = Partner.create(
            {
                "name": "Motor genérico",
                "comment": "<p>motor motor motor, repuesto de motor para el motor viejo</p>",
            }
        )
        cls.other_company_partner = Partner.create(
            {
                "name": "Proveedor externo",
                "company_id": cls.other_company.id,
                "comment": "<p>Contrato confidencial del motor Siemens de la otra compañía</p>",
            }
        )
        cls.hidden_partner = Partner.create(
            {
                "name": "Cliente reservado",
                "ref": "search-hidden",
                "comment": "<p>Oferta secreta por el motor Siemens del cliente reservado</p>",
            }
        )

    # ─── Helpers ──────────────────────────────────────────────────────────

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _enable(self, model="res.partner", field_names=("name", "comment")):
        fields_ = self.env["ir.model.fields"].search([("model", "=", model), ("name", "in", list(field_names))])
        existing = self.Config.with_context(active_test=False).search([("model", "=", model)])
        if existing:  # a database where an administrator already enabled it
            existing.write({"field_ids": [(6, 0, fields_.ids)], "active": True})
            return existing
        return self.Config.create({"model_id": self.env["ir.model"]._get_id(model), "field_ids": [(6, 0, fields_.ids)]})

    def _index(self, config):
        while config.backfill_cursor:
            config._backfill_batch(1000)
        config._sync_batch(1000)

    def _search(self, query, user=None, model="res.partner", **kwargs):
        env = self.env(user=user or self.user)
        # Room for the records created here even if demo data of a big database
        # also matches; the default limit is tested on its own.
        kwargs.setdefault("limit", 50)
        response = env["search.relevant"].search_relevant(model, query, **kwargs)
        response["_model"] = model
        return response

    def _later_write_date(self, record, seconds=5):
        """Give ``record`` the write_date a later transaction would give it.

        Inside a test everything happens in one transaction, so a write keeps
        the same ``now()`` the indexing saw and looks unchanged. In production
        the next write always comes from a newer transaction.
        """
        record.flush_recordset()
        self.env.cr.execute(
            SQL(
                "UPDATE %s SET write_date = %s WHERE id = %s",
                SQL.identifier(record._table),
                self.env.cr.now() + timedelta(seconds=seconds),
                record.id,
            )
        )

    def _indexed_ids(self, config):
        self.env.cr.execute("SELECT res_id FROM search_relevant_document WHERE config_id = %s", [config.id])
        return {row[0] for row in self.env.cr.fetchall()}

    def _age_all_partners(self):
        """Move every partner's write_date a day back, so nothing is matched live
        and only the index answers."""
        self.env.flush_all()
        self.env.cr.execute("UPDATE res_partner SET write_date = write_date - interval '1 day'")
        self.env["res.partner"].invalidate_model(["write_date"])

    def _mine(self, response):
        """The records of ``response`` created by these tests, in rank order."""
        baseline = self.baseline.get(response.get("_model", "res.partner"), 0)
        return [record for record in response["records"] if record["id"] > baseline]

    def _ids(self, response):
        return [record["id"] for record in self._mine(response)]

    def _cannot_read(self, user, model, field_name):
        """Patch ``_has_field_access`` so ``user`` cannot read ``model.field_name``
        — how ``project.task`` hides fields from portal users."""
        model_class = type(self.env[model])
        original = model_class._has_field_access

        def restricted(records, field, operation):
            if records.env.uid == user.id and field.name == field_name:
                return False
            return original(records, field, operation)

        return patch.object(model_class, "_has_field_access", restricted)

    # ─── Enablement & coverage ───────────────────────────────────────────

    def test_model_not_enabled_is_a_state_not_an_error(self):
        response = self._search("motor siemens")
        self.assertEqual(response["engine"], "companion_fts")
        self.assertEqual(response["coverage"], {"state": "not_enabled", "indexed_until": None, "fields": []})
        self.assertEqual(self._mine(response), [])
        self.assertEqual(response["terms"], ["motor", "siemens"])
        response = self._search("motor", model="no.such.model")
        self.assertEqual(response["coverage"]["state"], "not_enabled")

    def test_enabling_indexes_nothing_until_the_cron_runs(self):
        config = self._enable()
        self.assertTrue(config.backfill_cursor, "enabling must leave the work to the cron")
        self.assertEqual(config.document_count, 0)
        response = self._search("motor siemens")
        self.assertEqual(response["coverage"]["state"], "partial")
        self.assertTrue(response["coverage"]["indexed_from"])
        self.assertEqual(response["coverage"]["fields"], ["name", "comment"])

        self._index(config)
        response = self._search("motor siemens")
        self.assertEqual(response["coverage"]["state"], "complete")
        self.assertEqual(response["coverage"]["indexed_until"], config.sync_watermark.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertNotIn("indexed_from", response["coverage"])
        self.assertIn(self.both_terms.id, self._ids(response))

    def test_backfill_goes_from_newest_to_oldest_in_batches(self):
        config = self._enable()
        total = config.backfill_total
        self.assertGreater(total, 4)

        self.assertEqual(config._backfill_batch(2), 2)
        self.assertEqual(config.backfill_done, 2)
        self.assertEqual(config.state, "indexing")
        indexed = self._indexed_ids(config)
        self.assertIn(self.hidden_partner.id, indexed)
        self.assertIn(self.other_company_partner.id, indexed)
        self.assertNotIn(self.both_terms.id, indexed)

        while config.backfill_cursor:
            config._backfill_batch(2)
        self.assertEqual(config.state, "ready")
        self.assertEqual(config.backfill_done, total)
        # Partners without any searchable text get no row.
        self.assertLessEqual(config.document_count, total)
        self.assertIn(self.both_terms.id, self._indexed_ids(config))

    def test_cron_reports_progress_by_batches_and_switches_itself_off(self):
        config = self._enable()
        cron = self.env.ref("base_search_relevant.ir_cron_search_relevant_index")
        self.assertTrue(cron.active, "enabling a model must wake the cron")
        self.env["ir.config_parameter"].sudo().set_param("base_search_relevant.batch_size", "3")
        total = config.backfill_total

        def run_job():
            self.env.flush_all()
            with self.enter_registry_test_mode(), self.registry.cursor() as cr:
                self.registry["ir.cron"]._process_job(
                    cr, {**cron.read(load=None)[0], "done": 0, "remaining": 0, "timed_out_counter": 0}
                )
            self.env.invalidate_all()

        original = SearchRelevantConfig._backfill_batch
        flush = SearchRelevantConfig._flush_pending_index
        with (
            patch.object(SearchRelevantConfig, "_backfill_batch", autospec=True, side_effect=original) as batches,
            patch.object(SearchRelevantConfig, "_flush_pending_index", autospec=True, side_effect=flush) as flushes,
        ):
            run_job()
        # S9: the GIN pending list is flushed at the end of each run.
        self.assertTrue(flushes.called)
        self.assertGreaterEqual(batches.call_count, total // 3, "the backfill must run in batches")
        self.assertEqual(config.state, "ready")
        progress = self.env["ir.cron.progress"].sudo().search([("cron_id", "=", cron.id)])
        self.assertGreaterEqual(sum(progress.mapped("done")), total, "progress must count the indexed records")
        self.assertEqual(progress.sorted("id")[-1:].remaining, 0)
        self.assertTrue(cron.active, "while a model is enabled the cron keeps the index fresh")

        self.Config.search([]).active = False
        run_job()
        self.assertFalse(cron.active, "with no model enabled the cron switches itself off")

    def test_archiving_disables_search_and_drops_the_index(self):
        config = self._enable()
        self._index(config)
        rows = config.document_count
        self.assertTrue(rows)
        config.active = False
        self.assertEqual(self._search("motor")["coverage"]["state"], "not_enabled", "search stops at once")
        self.assertTrue(config.cleanup_pending)
        self.assertEqual(config._cleanup_batch(rows - 1), (rows - 1, True), "rows are deleted by the cron, in batches")
        self.assertEqual(config._cleanup_batch(rows), (1, False))
        self.assertFalse(config.cleanup_pending)
        config.invalidate_recordset(["document_count"])
        self.assertEqual(config.document_count, 0)

    # ─── Freshness (I2) ───────────────────────────────────────────────────

    def test_a_record_created_after_the_last_pass_is_found_before_the_cron(self):
        """Reviewer: "fresh record, before cron: {'state': 'complete', ...} []"."""
        config = self._enable()
        self._index(config)
        fresh = self.env["res.partner"].create({"name": "Recién creado ornitorrinco"})
        self.assertNotIn(fresh.id, self._indexed_ids(config))
        response = self._search("ornitorrinco")
        self.assertEqual(self._ids(response), [fresh.id])
        self.assertEqual(response["coverage"]["state"], "complete")
        self.assertEqual(self._mine(response)[0]["matched_terms"], ["ornitorrinco"])

    def test_a_record_written_after_indexing_is_matched_on_its_new_text(self):
        config = self._enable()
        self._index(config)
        self.assertIn(self.one_term.id, self._ids(self._search("genérico")))

        self.one_term.name = "Compresor rotativo"
        self._later_write_date(self.one_term)
        # Before the cron: matched on what the record says now, not on the index.
        self.assertIn(self.one_term.id, self._ids(self._search("compresor rotativo")))
        self.assertNotIn(self.one_term.id, self._ids(self._search("genérico")))

        processed, more, _last = config._sync_batch(100)
        self.assertEqual((processed, more), (1, False), "only the written record is re-indexed")
        self.assertIn(self.one_term.id, self._ids(self._search("compresor rotativo")))
        self.assertNotIn(self.one_term.id, self._ids(self._search("genérico")))

    def test_a_cron_that_stopped_makes_the_answer_partial(self):
        config = self._enable()
        self._index(config)
        self.assertEqual(self._search("motor")["coverage"]["state"], "complete")
        config.sync_watermark = self.env.cr.now() - timedelta(hours=2)
        self.assertEqual(self._search("motor")["coverage"]["state"], "partial")

    def test_an_index_that_fell_behind_is_reported_to_whoever_asks(self):
        """I2 seen from outside: the same "behind" that makes an answer partial
        is what a health probe shows, so a cron that stopped is not invisible."""
        Config = self.Config
        self.assertEqual(Config._stale_report(), [], "nothing enabled, nothing to warn about")
        config = self._enable()
        self._index(config)
        self.assertEqual(Config._stale_report(), [])
        config.sync_watermark = self.env.cr.now() - timedelta(hours=2)
        (warning,) = Config._stale_report()
        self.assertIn("Scheduled Actions", warning)
        config.sync_watermark = self.env.cr.now()
        self.env.ref("base_search_relevant.ir_cron_search_relevant_index").active = False
        (warning,) = Config._stale_report()
        self.assertIn("is off", warning)

    def test_too_many_unindexed_writes_make_the_answer_partial(self):
        config = self._enable()
        self._index(config)
        self.env["res.partner"].create([{"name": f"Pendiente kiwi {n}"} for n in range(3)])
        with patch.object(search_module, "LIVE_MAX", 2):
            response = self._search("kiwi", limit=50)
        self.assertEqual(response["coverage"]["state"], "partial")
        self.assertEqual(len(self._mine(response)), 2)

    def test_a_deleted_record_is_never_returned_and_is_purged(self):
        config = self._enable()
        self._index(config)
        gone = self.env["res.partner"].create({"name": "Registro efímero zanahoria"})
        config._sync_batch(100)
        self.assertEqual(self._ids(self._search("zanahoria")), [gone.id])

        gone.unlink()
        self.assertEqual(self._mine(self._search("zanahoria")), [], "the user's own search drops deleted ids")
        self.assertEqual(config._purge_deleted(), 1)
        self.assertNotIn(gone.id, self._indexed_ids(config))

    def test_a_record_that_fails_to_index_does_not_stop_the_batch(self):
        config = self._enable()
        original = SearchRelevantConfig._insert_entries
        broken = self.one_term.id

        def insert(records, entries):
            if any(entry[0] == broken for entry in entries):
                raise psycopg2.errors.ProgramLimitExceeded("simulated")
            return original(records, entries)

        with patch.object(SearchRelevantConfig, "_insert_entries", insert), mute_logger(
            "odoo.addons.base_search_relevant.models.search_relevant_config"
        ):
            self._index(config)
        indexed = self._indexed_ids(config)
        self.assertNotIn(broken, indexed)
        self.assertIn(self.both_terms.id, indexed)
        self.assertFalse(config.backfill_cursor)

    # ─── Permissions ─────────────────────────────────────────────────────

    def test_records_the_user_cannot_see_never_come_back(self):
        config = self._enable()
        self._index(config)
        forbidden = {self.other_company_partner.id, self.hidden_partner.id}

        # They are in the index and rank for this query...
        items = [search_module.Item(term, f"'{term}'", None, 1.0) for term in ("motor", "siemens")]
        everything = self.env["res.partner"].sudo()._search([]).subselect()
        ranked = {
            row[0] for row in self.env["search.relevant"]._rank_page(config, items, items, everything, [], 0, 1000)
        }
        self.assertTrue(forbidden <= ranked, "precondition: the forbidden records match the query")

        # ...but the user gets neither their ids nor their text.
        response = self._search("motor siemens", limit=50)
        self.assertFalse(forbidden & set(self._ids(response)))
        self.assertIn(self.both_terms.id, self._ids(response))
        fragments = " ".join(record["snippet"] for record in self._mine(response)).lower()
        for secret in ("confidencial", "secreta", "reservado", "otra compañía"):
            self.assertNotIn(secret, fragments)

        # An administrator of both companies sees the other company's record.
        admin = self.env.ref("base.user_admin")
        admin.company_ids = [(4, self.other_company.id)]
        admin_env = self.env(user=admin, context={"allowed_company_ids": [self.main_company.id, self.other_company.id]})
        self.assertIn(
            self.other_company_partner.id,
            self._ids(admin_env["search.relevant"].search_relevant("res.partner", "motor siemens", limit=50)),
        )

    def test_a_narrow_domain_is_not_starved_by_the_candidate_cap(self):
        """B1, reviewer: 1.200 "Factura N" in project A, one in project C →
        ``domain=[('project_id','=',C)]`` came back empty and complete."""
        config = self._enable()
        self.env["res.partner"].create([{"name": f"Facturex {n}", "ref": "lote-a"} for n in range(20)])
        target = self.env["res.partner"].create(
            {"name": "Consulta cliente", "ref": "lote-c", "comment": "<p>facturex electrónica rechazada</p>"}
        )
        self._index(config)
        with patch.object(search_module, "MAX_CANDIDATES", 5), patch.object(search_module, "MAX_WINDOW", 5):
            response = self._search("facturex", domain=[("ref", "=", "lote-c")])
        self.assertEqual(self._ids(response), [target.id])
        self.assertEqual(response["coverage"]["state"], "complete")

    def test_a_user_who_sees_little_is_not_starved_by_the_candidate_cap(self):
        """B1, reviewer: a portal user who sees one task — 'zanahoria' → [7],
        'zanahoria factura' → []."""
        config = self._enable()
        self.env["res.partner"].create(
            [{"name": f"Zqfacturex zqciruelax {n}", "company_id": self.other_company.id} for n in range(20)]
        )
        # Made-up words, and without a real word's first letters: a word the user
        # sees nowhere is retried by prefix ("facturex" → "factu" would find facturas).
        visible = self.env["res.partner"].create({"name": "Zqzanahorix del portal"})
        self._index(config)
        with patch.object(search_module, "MAX_CANDIDATES", 5), patch.object(search_module, "MAX_WINDOW", 5):
            for query in ("zqzanahorix", "zqzanahorix zqfacturex", "zqfacturex zqciruelax zqzanahorix"):
                response = self._search(query, limit=50)
                self.assertEqual([record["id"] for record in response["records"]], [visible.id], query)

    def test_when_the_final_check_removes_candidates_it_asks_for_more(self):
        """The second guard (``search`` on the ids), for models that filter in
        Python: simulated with a ``_search`` that does not see the record rule."""
        config = self._enable()
        self.env["res.partner"].create(
            [{"name": f"Zinquex pernito cromax {n}", "company_id": self.other_company.id} for n in range(5)]
        )
        visible = self.env["res.partner"].create({"name": "Zinquex común"})
        self._index(config)
        search_class = type(self.env["search.relevant"])

        def blind(search, records_model, domain):
            return records_model.sudo()._search(domain).subselect()

        with (
            patch.object(search_class, "_allowed_subquery", blind),
            patch.object(search_module, "MIN_WINDOW", 1),
            patch.object(search_module, "WINDOW_FACTOR", 1),
            patch.object(search_module, "MAX_WINDOW", 2),
        ):
            response = self._search("zinquex pernito cromax", limit=1)
        # Made-up words: the whole answer is asserted, not only the records created here.
        self.assertEqual([record["id"] for record in response["records"]], [visible.id])
        self.assertEqual(response["records"][0]["matched_terms"], ["zinquex"])

    def test_archived_records_do_not_come_back(self):
        config = self._enable()
        archived = self.env["res.partner"].create({"name": "Archivado mandarinox"})
        active = self.env["res.partner"].create({"name": "Mandarinox activa"})
        more_archived = self.env["res.partner"].create(
            [{"name": "Mandarinox"} for _n in range(10)]
        )  # shorter: rank first
        self._index(config)
        (archived | more_archived).active = False
        self._index(config)
        # The ranking itself leaves them out: archived records ranking first must
        # not eat the candidates the active one needs.
        with patch.object(search_module, "MAX_CANDIDATES", 5), patch.object(search_module, "MAX_WINDOW", 5):
            self.assertEqual(self._ids(self._search("mandarinox", limit=50)), [active.id])
        # Each guard on its own: the final check still drops it when the ranking
        # subquery lets archived records through.
        search_class = type(self.env["search.relevant"])

        def with_archived(search, records_model, domain):
            return records_model.with_context(active_test=False)._search(domain).subselect()

        with patch.object(search_class, "_allowed_subquery", with_archived):
            self.assertEqual(self._ids(self._search("mandarinox", limit=50)), [active.id])

    def test_a_model_index_never_answers_for_another_model(self):
        """Ids repeat across models: a category and a partner can share one."""
        partners = self._enable()
        categories = self._enable("res.partner.category", ("name",))
        # Give the category the id of a partner the user sees.
        partner = self.both_terms
        self.assertFalse(self.env["res.partner.category"].browse(partner.id).exists())
        self.env.cr.execute("SELECT setval('res_partner_category_id_seq', %s, false)", [partner.id])
        category = self.env["res.partner.category"].create({"name": "Cuarzocat"})
        self.assertEqual(category.id, partner.id, "precondition: same id in both models")
        self._index(partners)
        self._index(categories)
        self.assertEqual(self._mine(self._search("cuarzocat")), [])
        self.assertEqual(self._ids(self._search("cuarzocat", model="res.partner.category")), [category.id])

    def test_words_in_a_field_the_user_cannot_read_do_not_match(self):
        """I1, reviewer: a portal user confirmed the content of ``partner_phone``
        through a result with ``matched_terms: []``."""
        config = self._enable()
        self._index(config)
        with self._cannot_read(self.user, "res.partner", "comment"):
            # "trifásico" is only in the comment of both_terms.
            self.assertEqual(self._mine(self._search("trifásico")), [])
            response = self._search("bomba siemens")
            self.assertEqual(response["coverage"]["fields"], ["name"])
            record = next(r for r in response["records"] if r["id"] == self.both_terms.id)
            self.assertEqual(record["matched_terms"], ["bomba"])
            self.assertNotIn("Siemens", record["snippet"], "the fragment must not show the unreadable field")
        # Everyone else still finds it by the comment.
        self.assertIn(self.both_terms.id, self._ids(self._search("trifásico", user=self.env.ref("base.user_admin"))))

    def test_records_and_snippets_are_read_as_the_user(self):
        """N6: the fragment is built from the user's own read, never a sudo one."""
        config = self._enable()
        self._index(config)
        fresh = self.env["res.partner"].create({"name": "Recién leído pitahaya"})
        partner_class = type(self.env["res.partner"])
        original = partner_class.read
        reads = []

        def spy(records, fields=None, load="_classic_read"):
            reads.append((records.env.su, records.env.uid, set(records.ids)))
            return original(records, fields, load)

        with patch.object(partner_class, "read", spy), self._cannot_read(self.user, "res.partner", "comment"):
            response = self._search("pitahaya bomba")
        self.assertEqual(set(self._ids(response)), {fresh.id, self.both_terms.id})
        self.assertTrue(reads)
        self.assertEqual({(su, uid) for su, uid, _ids in reads}, {(False, self.user.id)})
        snippet = next(r["snippet"] for r in response["records"] if r["id"] == self.both_terms.id)
        self.assertNotIn("Siemens", snippet, "no text of a field the user cannot read")

    def test_freshness_never_returns_what_the_user_cannot_see(self):
        """N6: a record written after the last pass that the user cannot see is
        neither returned nor even examined — asserted, not just an error."""
        config = self._enable()
        self._index(config)
        visible = self.env["res.partner"].create({"name": "Visible frambuesa"})
        hidden = self.env["res.partner"].create({"name": "Oculto frambuesa", "company_id": self.other_company.id})

        search = self.env(user=self.user)["search.relevant"]
        partners = self.env(user=self.user)["res.partner"]
        allowed = search._allowed_subquery(partners, search_module.Domain([]))
        partner_class = type(self.env["res.partner"])

        def unchecked_read(records, fields=None, load="_classic_read"):
            return [{"id": res_id, **{name: False for name in fields or []}} for res_id in records.ids]

        with patch.object(partner_class, "read", unchecked_read):
            _entries, examined, _overflow = search._live_records(config, partners, allowed, config._indexed_fields())
        self.assertIn(visible.id, examined)
        self.assertNotIn(hidden.id, examined)
        # And end to end, the hidden one is not in the answer.
        self.assertEqual(self._ids(self._search("frambuesa", limit=50)), [visible.id])

    def test_the_index_table_is_not_readable_through_the_orm(self):
        config = self._enable()
        self._index(config)
        for user in (self.user, self.env.ref("base.user_admin")):
            documents = self.env(user=user)["search.relevant.document"]
            with self.assertRaises(AccessError), mute_logger("odoo.addons.base.models.ir_model"):
                documents.search([])
            with self.assertRaises(AccessError), mute_logger("odoo.addons.base.models.ir_model"):
                documents.search_read([], ["res_id"])
        with self.assertRaises(AccessError), mute_logger("odoo.addons.base.models.ir_model"):
            self.env(user=self.user)["search.relevant.config"].search([])

    def test_a_user_without_read_access_to_the_model_is_refused(self):
        portal = self.env["res.users"].create(
            {
                "name": "Relevance Search Portal",
                "login": "search_relevant_portal",
                "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
            }
        )
        # Same refusal whether the model is enabled or not: that is not his to know.
        for enable in (False, True):
            if enable:
                self._enable("res.partner.category", ("name",))
            with self.assertRaises(AccessError), mute_logger("odoo.addons.base.models.ir_model"):
                self._search("anything", user=portal, model="res.partner.category")

    # ─── Ranking ─────────────────────────────────────────────────────────

    def test_more_distinct_terms_rank_first(self):
        config = self._enable()
        self._index(config)
        response = self._search("motor siemens")
        ids = self._ids(response)
        self.assertLess(ids.index(self.both_terms.id), ids.index(self.one_term.id))
        scores = [record["score"] for record in response["records"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(0 <= score <= 1 for score in scores))
        self.assertEqual(self._mine(response)[0]["id"], self.both_terms.id)
        self.assertEqual(self._mine(response)[0]["matched_terms"], ["motor", "siemens"])

    def test_ranking_pages_are_exactly_the_full_ordering(self):
        """``_rank_page`` computes ``ts_rank_cd`` only for the top tier of each
        page; the pages must still be the full weighted ordering, ties included."""
        config = self._enable()
        self.env["res.partner"].create(
            [{"name": f"Válvula {'esférica ' * (n % 3)}bronce {n}", "comment": "válvula " * (n % 2)} for n in range(40)]
        )
        self._index(config)
        search = self.env["search.relevant"]
        partners = self.env["res.partner"].sudo()
        everything = partners._search([]).subselect()
        items, _frequencies, _stats, _population = search._search_items(
            config, partners, ["valvula", "esferica", "bronce"], "", everything, []
        )
        self.assertGreater(len({item.weight for item in items}), 1, "precondition: different weights")
        any_term = " | ".join(item.operand for item in items)
        self.env.cr.execute(
            SQL(
                """
                SELECT res_id, %s, ts_rank_cd(%s::float4[], tsv, %s::tsquery, 33) FROM search_relevant_document d
                 WHERE config_id = %s AND tsv @@ %s::tsquery
                """,
                SQL(", ").join(SQL("(tsv @@ %s::tsquery)::int", item.operand) for item in items),
                search_module.RANK_WEIGHTS,
                any_term,
                config.id,
                any_term,
            )
        )
        rows = self.env.cr.fetchall()
        scored = [
            (res_id, sum(item.weight * hit for item, hit in zip(items, hits, strict=True)), rank)
            for res_id, *hits, rank in rows
        ]
        expected = [res_id for res_id, score, rank in sorted(scored, key=lambda r: (-r[1], -r[2], -r[0]))]
        self.assertGreater(len(expected), 30)
        paged = []
        for offset, count in ((0, 7), (7, 1), (8, 13), (21, 100)):
            paged += [row[0] for row in search._rank_page(config, items, items, everything, [], offset, count)]
        self.assertEqual(paged, expected)

    def test_common_terms_never_hide_records_that_match_them(self):
        """B2, reviewer: 25.000 records with "odoo" and "tuqui" →
        'odoo tuqui zzzinexistente' came back empty. The candidates of the
        rare-terms shortcut must never change the answer.

        Made-up words, so no record of the database can match them and the
        whole answer — not only the records created here — is asserted."""
        config = self._enable()
        common = self.env["res.partner"].create([{"name": f"Quenapo tatabro soporte {n}"} for n in range(20)])
        rare = self.env["res.partner"].create({"name": "Zorzalito solo"})
        self._index(config)
        queries = ("quenapo tatabro zorzalito", "quenapo zorzalito", "tatabro soporte zorzalito")

        def answer(query):
            return [(r["id"], r["score"]) for r in self._search(query, limit=50)["records"]]

        search_class = type(self.env["search.relevant"])
        original = search_class._rank_page
        shortened = []

        def spy(search, config, items, anchor, *args, **kwargs):
            shortened.append(len(anchor) < len(items))
            return original(search, config, items, anchor, *args, **kwargs)

        # A low cap makes "quenapo" and "tatabro" common, so they stop bringing in
        # candidates. The weights are the same in both runs: only the shortcut changes.
        with patch.object(search_module, "DF_CAP", 5):
            with patch.object(search_class, "_rank_page", spy):
                response = self._search("quenapo tatabro zzzinexistente", limit=50)
                self.assertEqual({record["id"] for record in response["records"]}, set(common.ids))
                self.assertEqual({tuple(r["matched_terms"]) for r in response["records"]}, {("quenapo", "tatabro")})
                shortcut = [answer(query) for query in queries]
            self.assertTrue(any(shortened), "precondition: the shortcut kicks in")
            with patch.object(search_class, "_anchor_items", lambda search, items, frequencies: items):
                full = [answer(query) for query in queries]
        self.assertEqual(shortcut, full, "the shortcut must not change any answer")
        self.assertEqual({res_id for res_id, _score in full[0]}, set(common.ids) | {rare.id})

    def test_the_weight_of_a_term_is_the_one_the_caller_uses(self):
        """X3, decision 3: one weight function on both sides, so the ranking and
        the confidence signal never disagree about which word carries the query."""
        self.assertEqual(search_module.term_weight(3, 1000, False), round(math.log(1 + 1000 / 4), 6))
        self.assertEqual(search_module.term_weight(0, 1000, True), 0.0, "a word naming the record type")
        common = search_module.term_weight(search_module.DF_CAP * 20, 1000, False)
        self.assertEqual(common, search_module.term_weight(search_module.DF_CAP, 1000, False), "df caps")
        self.assertLess(common, search_module.term_weight(1, 1000, False))

    def test_a_word_in_the_title_breaks_the_tie(self):
        """Same word, same weight: the record with it in the name comes first."""
        config = self._enable()
        body = self.env["res.partner"].create({"name": "Registro sin marca", "comment": "<p>zqtitulox</p>"})
        title = self.env["res.partner"].create({"name": "Zqtitulox en el nombre", "comment": "<p>otro texto</p>"})
        self._index(config)
        self.assertEqual(self._ids(self._search("zqtitulox")), [title.id, body.id])

    def test_rare_terms_outweigh_common_ones(self):
        """Real case (bench on the Adhoc copy, 2026-09-15): "la tarea de gap
        analysis del proyecto Mercofrío" lost "Gap Analisis - Mercofrio" to tasks
        with "tarea" + "proyecto" + "gap", because every term weighed the same."""
        config = self._enable()
        self.env["res.partner"].create([{"name": f"Tarea del proyecto gap {n}"} for n in range(60)])
        target = self.env["res.partner"].create({"name": "Gap Analisis - Mercofrio"})
        self._index(config)
        response = self._search("la tarea de gap analysis del proyecto Mercofrío")
        self.assertEqual(self._ids(response)[0], target.id)
        self.assertEqual(self._mine(response)[0]["matched_terms"], ["gap", "mercofrio"])
        scores = [record["score"] for record in response["records"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(0 <= score <= 1 for score in scores))

    def test_rarity_is_counted_inside_the_users_scope(self):
        """Risk 3 of the cross review: a word common in the database and rare for
        this user must weigh as rare for this user."""
        config = self._enable()
        self.env["res.partner"].create(
            [{"name": f"Pomeloz hidden {n}", "company_id": self.other_company.id} for n in range(30)]
        )
        self.env["res.partner"].create({"name": "Pomeloz visible"})
        self._index(config)
        search = self.env["search.relevant"]
        admin_partners = self.env["res.partner"].sudo()
        user_partners = self.env(user=self.user)["res.partner"]
        _items, admin_df, _stats, _population = search._search_items(
            config, admin_partners, ["pomeloz"], "", admin_partners._search([]).subselect(), []
        )
        _items, user_df, _stats, _population = search._search_items(
            config, user_partners, ["pomeloz"], "", user_partners._search([]).subselect(), []
        )
        self.assertEqual((admin_df["pomeloz"], user_df["pomeloz"]), (31, 1))

    def test_term_stats_count_only_what_the_user_may_see(self):
        """Confidence signal: df per term inside the user's scope, as a count and
        nothing else — hidden records change no number the user gets."""
        config = self._enable()
        self.env["res.partner"].create(
            [{"name": f"Zqpomeloz zqquinquejas ocultas {n}", "company_id": self.other_company.id} for n in range(7)]
        )
        self.env["res.partner"].create({"name": "Zqpomeloz zqquinqueja visible"})
        self._index(config)
        query = "zqpomeloz zqquinquejaz contact"
        admin = self.env.ref("base.user_admin")
        admin.company_ids = [(4, self.other_company.id)]
        admin_env = self.env(user=admin, context={"allowed_company_ids": [self.main_company.id, self.other_company.id]})
        admin_stats = admin_env["search.relevant"].search_relevant("res.partner", query)["term_stats"]
        response = self._search(query)
        user_stats = response["term_stats"]
        self.assertNotIn("term_stats_df_cap", response, "the cap is documented, not sent every time")
        self.assertGreaterEqual(response["population"], 1)
        self.assertEqual(admin_stats["zqpomeloz"]["df"], 8)
        self.assertEqual(user_stats["zqpomeloz"], {"df": 1, "type": False, "prefix": None, "prefix_df": None})
        # Resolved by prefix: the exact word is nowhere, "zqquinque" is in the one it sees.
        self.assertEqual(user_stats["zqquinquejaz"], {"df": 0, "type": False, "prefix": "zqquinque", "prefix_df": 1})
        self.assertEqual(admin_stats["zqquinquejaz"]["prefix_df"], 8)
        self.assertTrue(user_stats["contact"]["type"])
        # A portal user sees none of those partners.
        portal = self.env["res.users"].create(
            {
                "name": "Relevance Search Portal Stats",
                "login": "search_relevant_portal_stats",
                "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
            }
        )
        portal_stats = self._search(query, user=portal)["term_stats"]
        self.assertEqual(portal_stats["zqpomeloz"]["df"], 0)
        self.assertIsNone(portal_stats["zqquinquejaz"]["prefix"])
        # Only these keys, only these kinds of values, for every term.
        for stats in user_stats.values():
            self.assertEqual(set(stats), {"df", "type", "prefix", "prefix_df"})
            self.assertIsInstance(stats["df"], int)
        # The user's df is exactly what the user's own search finds.
        visible = self.env(user=self.user)["res.partner"].search_count([("name", "ilike", "zqpomeloz")])
        self.assertEqual(user_stats["zqpomeloz"]["df"], visible)
        # Records read live count too, and still only the visible ones.
        self.env["res.partner"].create({"name": "Zqpomeloz recien creado"})
        self.env["res.partner"].create({"name": "Zqpomeloz oculto recien", "company_id": self.other_company.id})
        self.assertEqual(self._search(query)["term_stats"]["zqpomeloz"]["df"], 2)

    def test_words_naming_the_record_type_weigh_least(self):
        """S3: the model's description and the many2one comodels of its search
        view name what is searched, not what the record says."""
        config = self._enable()
        self.assertIn("contact", self.env["search.relevant"]._type_terms("res.partner", "en_US"))
        self.env["res.partner"].create([{"name": "Mercofrix uno"}, {"name": "Mercofrix dos"}])
        loner = self.env["res.partner"].create({"name": "Contact solitario zzq"})
        self._index(config)
        response = self._search("contact mercofrix")
        self.assertNotEqual(self._ids(response)[0], loner.id, "a rare type word does not outrank content")
        self.assertIn(loner.id, self._ids(response), "it still counts")

    def test_a_term_nothing_has_is_retried_by_prefix(self):
        """S4: plural and inflection, not translation."""
        config = self._enable()
        record = self.env["res.partner"].create({"name": "Quinqueja mensual"})
        spanish = self.env["res.partner"].create({"name": "Informe Analisix"})
        self._index(config)
        response = self._search("quinquejas")
        self.assertEqual(self._ids(response), [record.id])
        self.assertEqual(self._mine(response)[0]["matched_terms"], ["quinquejas"])
        self.assertNotIn(spanish.id, self._ids(self._search("analysix")), "analysix is not analisix")

    # ─── Normalization (I3, I5) ──────────────────────────────────────────

    def test_accents_and_case_do_not_matter(self):
        config = self._enable()
        record = self.env["res.partner"].create({"name": "Conciliación Bancaria de Ñandú"})
        self._index(config)
        for query in ("CONCILIACION bancária", "conciliación BANCARIA", "nandu"):
            self.assertEqual(self._ids(self._search(query))[:1], [record.id], query)
        response = self._search("Conciliacion Bancaria")
        self.assertEqual(self._mine(response)[0]["matched_terms"], ["conciliacion", "bancaria"])
        self.assertIn("Conciliación Bancaria", self._mine(response)[0]["snippet"])

    def test_codes_split_on_hyphens_dots_and_slashes(self):
        """I3, reviewer: 'PR-979' was indexed as '-979' and "979" did not find it.
        Contract adjustment 3: a code is searched whole first, then by its parts
        with digits.

        Made-up numbers: whether a code falls back depends on whether any record
        of the database has it, so they must not exist outside this test."""
        config = self._enable()
        spaced = self.env["res.partner"].create({"name": "Revisión PR 97913 aprobada"})
        hyphen = self.env["res.partner"].create(
            {"name": "PR-98024 listo", "comment": "<p>ver wiki.zorzalhost.inc y inv/2024/00731</p>"}
        )
        furn = self.env["res.partner"].create({"name": "Caja FURN_78156"})
        furnx = self.env["res.partner"].create({"name": "Caja FURNX78156"})
        self._index(config)
        # Whole code found: searched whole.
        response = self._search("PR-98024")
        self.assertEqual((response["terms"], self._ids(response)), (["pr-98024"], [hyphen.id]))
        # No record has "PR-97913" whole: falls back to its part with digits.
        response = self._search("PR-97913")
        self.assertEqual((response["terms"], self._ids(response)), (["97913"], [spaced.id]))
        self.assertEqual(self._ids(self._search("98024")), [hyphen.id])
        self.assertEqual(self._ids(self._search("zorzalhost")), [hyphen.id])
        self.assertEqual(self._ids(self._search("00731")), [hyphen.id])
        self.assertEqual(self._ids(self._search("inv/2024/00731")), [hyphen.id])
        self.assertEqual(self._ids(self._search("FURN_78156")), [furn.id], "FURN_78156 does not match FURNX78156")
        self.assertEqual(self._ids(self._search("furnx78156")), [furnx.id])
        # A hidden record with the code must not stop the fallback, nor show.
        self.env["res.partner"].create({"name": "PR-97913 secreto", "company_id": self.other_company.id})
        self._index(config)
        response = self._search("PR-97913")
        self.assertEqual((response["terms"], self._ids(response)), (["97913"], [spaced.id]))

    def test_the_answer_says_which_terms_were_searched(self):
        """I5, reviewer: 'de la' answered complete and empty, and the agent was told
        it was a real "not found"."""
        config = self._enable()
        self._index(config)
        for query in ("", "de la que", "   ", "a y o"):
            response = self._search(query)
            self.assertEqual(response["terms"], [], query)
            self.assertEqual(self._mine(response), [], query)
        self.assertEqual(self._search("Revisar PR 979 estado")["terms"], ["revisar", "979", "estado"])

    def test_html_is_indexed_as_text(self):
        config = self._enable()
        record = self.env["res.partner"].create(
            {
                "name": "Nota",
                "comment": '<p>Revisar <span style="color:red">caldera</span><img src="data:image/png;base64,QUJD"/></p>',
            }
        )
        self._index(config)
        self.assertEqual(self._ids(self._search("caldera")), [record.id])
        self.assertNotIn(record.id, self._ids(self._search("span style color")))

    # ─── Fragment & matched terms come from the record, as the user reads it ──

    def test_snippet_and_terms_come_from_the_current_record_not_the_index(self):
        config = self._enable()
        self._index(config)
        # Edited by SQL, the way nothing updates write_date: the index still
        # says "Siemens", the record does not.
        self.env.cr.execute(
            "UPDATE res_partner SET comment = '<p>Se reemplazó por un equipo nuevo.</p>' WHERE id = %s",
            [self.both_terms.id],
        )
        self.both_terms.invalidate_recordset(["comment"])
        response = self._search("siemens trifásico", limit=50)
        record = next(r for r in response["records"] if r["id"] == self.both_terms.id)
        self.assertEqual(record["matched_terms"], [], "nothing the user can read contains the terms any more")
        self.assertNotIn("Siemens", record["snippet"])

    def test_domain_and_limit_are_applied(self):
        config = self._enable()
        self._index(config)
        response = self._search("motor", domain=[("id", "!=", self.one_term.id)], limit=50)
        self.assertNotIn(self.one_term.id, self._ids(response))
        self.assertIn(self.both_terms.id, self._ids(response))
        self.env["res.partner"].create([{"name": f"Motor lote {n}"} for n in range(60)])
        self._index(config)
        self.assertEqual(len(self._search("motor", limit=500)["records"]), search_module.MAX_LIMIT)
        self.assertEqual(
            len(self.env(user=self.user)["search.relevant"].search_relevant("res.partner", "motor")["records"]),
            search_module.DEFAULT_LIMIT,
        )
        self.assertEqual(len(self._search("motor", limit=3.0)["records"]), 3)

    def test_invalid_arguments_are_rejected(self):
        for args in (
            (None, "x"),
            ("res.partner", None),
            ("res.partner", "x", "not-a-domain"),
            ("res.partner", "x", [], "10"),
            ("res.partner", "x", [], 2.5),
        ):
            with self.assertRaises(ValidationError, msg=str(args)):
                self.env(user=self.user)["search.relevant"].search_relevant(*args)

    # ─── The indexed subset ──────────────────────────────────────────────

    def test_a_domain_indexes_only_the_records_that_match(self):
        """The case this was built for, on partners: part of a model, not all of it.

        The records here are written in this transaction, so they are also what
        the search reads live: the domain has to hold on both paths.
        """
        inside = self.env["res.partner"].create({"name": "Repuesto berenjena", "ref": "search-indexed"})
        outside = self.env["res.partner"].create({"name": "Otra berenjena", "ref": "search-ignored"})
        config = self._enable("res.partner", ("name",))
        config.domain = "[('ref', '=', 'search-indexed')]"
        self.assertEqual(config.backfill_total, 1, "only what the domain allows is counted")
        self._index(config)
        self.assertEqual(self._indexed_ids(config), {inside.id})
        self.assertEqual(self._ids(self._search("berenjena")), [inside.id])

        # Leaving the domain is a write, and the freshness pass is where the
        # write shows up: the row goes with it.
        outside.ref = "search-indexed"
        inside.ref = "search-ignored"
        self._later_write_date(inside)
        self._later_write_date(outside)
        config._sync_batch(100)
        self.assertEqual(self._indexed_ids(config), {outside.id})
        self.assertEqual(self._ids(self._search("berenjena")), [outside.id])

    def test_a_domain_that_changes_drops_what_it_leaves_out(self):
        """Nothing writes the records a new domain leaves out, so the next cron
        run purges their rows instead of waiting for the daily pass."""
        config = self._enable("res.partner", ("name",))
        self._index(config)
        self.assertIn(self.both_terms.id, self._indexed_ids(config))
        config.domain = f"[('id', '=', {self.one_term.id})]"
        self.assertFalse(config.last_purge_at, "the purge is due now, not tomorrow")
        self.assertTrue(config._purge_deleted() >= 1)
        self.assertNotIn(self.both_terms.id, self._indexed_ids(config))

    def test_a_domain_that_does_not_parse_or_does_not_apply_is_refused(self):
        config = self._enable("res.partner", ("name",))
        for domain in ("[('ref', '=', ", "{'ref': 'x'}", "[('no_such_field', '=', 1)]"):
            with self.assertRaises(ValidationError, msg=domain):
                config.domain = domain

    # ─── Configuration & database footprint (I4) ─────────────────────────

    def test_only_plain_stored_text_fields_can_be_enabled(self):
        with self.assertRaises(ValidationError):
            self._enable("res.partner", ("color",))  # an integer
        with self.assertRaises(ValidationError):
            self._enable("res.users", ("name",))  # delegated to res.partner through _inherits
        with self.assertRaises(ValidationError):
            self._enable("res.partner", ())
        with self.assertRaises(ValidationError):
            self._enable("base.language.export", ("name",))  # transient
        with self.assertRaises(ValidationError):
            self._enable("res.partner", ("name", "comment", "ref", "email", "website"))  # a weight per field, four

    def test_changing_the_fields_stops_the_old_ones_from_matching_at_once(self):
        config = self._enable()
        self._age_all_partners()
        self._index(config)
        self.assertIn(self.both_terms.id, self._ids(self._search("trifásico")))
        config.field_ids = self.env["ir.model.fields"].search([("model", "=", "res.partner"), ("name", "=", "name")])
        response = self._search("trifásico")
        self.assertEqual(self._mine(response), [])
        self.assertEqual(response["coverage"]["state"], "partial")
        self._index(config)
        self.assertEqual(self._mine(self._search("trifásico")), [])
        self.assertIn(self.both_terms.id, self._ids(self._search("bomba")))

    def test_a_field_that_disappears_does_not_shift_the_others(self):
        """N5: a field removed by cascade (a deleted manual field, a module update)
        never passes through ``write``; the labels of the other fields must not
        move, its words must stop matching, and the model is re-indexed."""
        partner = self.env["res.partner"].create(
            {"name": "Cliente ciruela", "comment": "<p>kiwano</p>", "ref": "kumquat"}
        )
        config = self._enable(field_names=("name", "comment", "ref"))
        self._age_all_partners()
        self._index(config)
        self.assertEqual(config.layout_fields, "name,comment,ref")
        admin = self.env.ref("base.user_admin")
        self.assertEqual(self._ids(self._search("kiwano", user=admin)), [partner.id])

        field_ids = config._fields["field_ids"]
        comment = self.env["ir.model.fields"]._get("res.partner", "comment")
        self.env.cr.execute(
            SQL(
                "DELETE FROM %s WHERE %s = %s AND %s = %s",
                SQL.identifier(field_ids.relation),
                SQL.identifier(field_ids.column1),
                config.id,
                SQL.identifier(field_ids.column2),
                comment.id,
            )
        )
        config.invalidate_recordset(["field_ids"])
        self.assertFalse(config._layout_intact())
        response = self._search("kiwano", user=admin)
        self.assertEqual(self._mine(response), [], "the disappeared field's words no longer match")
        self.assertEqual(response["coverage"]["state"], "partial")
        self.assertEqual(self._ids(self._search("kumquat", user=admin)), [partner.id], "ref keeps its own label")

        config._rebuild_outdated()
        self._index(config)
        self.assertTrue(config._layout_intact())
        self.assertEqual(self._mine(self._search("kiwano", user=admin)), [])
        response = self._search("kumquat", user=admin)
        self.assertEqual((response["coverage"]["state"], self._ids(response)), ("complete", [partner.id]))

    def test_an_index_built_by_another_rule_is_rebuilt_and_partial_meanwhile(self):
        config = self._enable()
        self._index(config)
        config.normalization = False  # as left by an earlier version of the module
        self.assertEqual(self._search("motor")["coverage"]["state"], "partial")
        old_layout = config.layout
        config._rebuild_outdated()  # what each cron run starts with
        self.assertEqual(config.normalization, search_module.NORMALIZATION_VERSION)
        self.assertEqual(config.layout, old_layout + 1)
        self.assertEqual(self._search("motor")["coverage"]["state"], "partial", "until the backfill is done")
        self._index(config)
        self.assertEqual(self._search("motor")["coverage"]["state"], "complete")

    def test_the_fragment_reads_only_the_fields_that_matched(self):
        """Risk 4: a long HTML field is not read for a record that matched by name."""
        record = self.env["res.partner"].create(
            {"name": "Lectura corta zapallox", "comment": "<p>" + "texto " * 5000 + "</p>"}
        )
        config = self._enable()
        self._age_all_partners()
        self._index(config)
        partner_class = type(self.env["res.partner"])
        original = partner_class.read
        read_fields = []

        def spy(records, fields=None, load="_classic_read"):
            if record.id in records.ids:
                read_fields.append(set(fields or []))
            return original(records, fields, load)

        with patch.object(partner_class, "read", spy):
            response = self._search("zapallox")
        self.assertEqual(self._ids(response), [record.id])
        self.assertTrue(read_fields)
        self.assertTrue(all("comment" not in fields for fields in read_fields), read_fields)

    def test_nothing_is_installed_in_the_database_besides_the_table(self):
        """I4: an extension created by the module broke ``pg_restore`` onto a
        database that has ``unaccent`` in ``public``. No extension, no text
        search configuration, no function — and ``ilike`` is untouched."""

        def footprint():
            self.env.cr.execute(
                """
                SELECT (SELECT array_agg(extname ORDER BY extname) FROM pg_extension),
                       (SELECT count(*) FROM pg_ts_config WHERE cfgname LIKE 'search_relevant%%'),
                       (SELECT count(*) FROM pg_proc WHERE proname LIKE 'search_relevant%%' OR proname = 'unaccent')
                """
            )
            return self.env.cr.fetchone(), has_unaccent(self.env.cr)

        before = footprint()
        config = self._enable()
        self._index(config)
        self._search("conciliación")
        self.assertEqual(footprint(), before)

    # ─── A business model, when installed ───────────────────────────────

    def test_project_task_description(self):
        if "project.task" not in self.env:
            self.skipTest("project is not installed")
        config = self._enable("project.task", ("name", "description"))
        project = self.env["project.project"].create({"name": "Relevance search project"})
        task = self.env["project.task"].create(
            {
                "name": "Ticket de soporte",
                "project_id": project.id,
                "description": "<p>El <b>variador</b> no arranca</p>",
            }
        )
        private = self.env["project.project"].create({"name": "Private", "privacy_visibility": "followers"})
        hidden = self.env["project.task"].create(
            {"name": "Tarea privada", "project_id": private.id, "description": "<p>variador secreto</p>"}
        )
        self._index(config)
        self.user.group_ids = [(4, self.env.ref("project.group_project_user").id)]
        ids = self._ids(self._search("variador", model="project.task"))
        self.assertIn(task.id, ids)
        self.assertNotIn(hidden.id, ids)

    def test_project_task_mercofrio(self):
        """The real case, on tasks: "task" and "project" name the kind of record
        (the model and the project_id of the search view), so they weigh least."""
        if "project.task" not in self.env:
            self.skipTest("project is not installed")
        self.assertTrue({"task", "project"} <= self.env["search.relevant"]._type_terms("project.task", "en_US"))
        project = self.env["project.project"].create({"name": "Relevance search project"})
        target = self.env["project.task"].create({"name": "Gap Analisis - Mercofrio", "project_id": project.id})
        self.env["project.task"].create(
            [{"name": f"Task for the project gap {n}", "project_id": project.id} for n in range(3)]
        )
        config = self._enable("project.task", ("name", "description"))
        self._index(config)
        self.user.group_ids = [(4, self.env.ref("project.group_project_user").id)]
        response = self._search("the task of gap analysis of the project Mercofrio", model="project.task")
        self.assertEqual(self._ids(response)[0], target.id)
