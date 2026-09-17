"""The Tuqui side of the relevance search: the RPC contract, and nothing else.

The engine and everything it guarantees (permissions, ranking, coverage) are
tested in ``base_search_relevant``. What is pinned here is that
``tuqui.search.search_relevant`` still answers with the name, the signature and
the shape the Tuqui backend calls (``CONTRATO-busqueda.md`` §1), and that the
call reaches the engine with the caller's own environment. The end-to-end path
through ``/tuqui/rpc`` is in ``test_rpc``.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "tuqui")
class TestTuquiSearchFacade(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "Quenapox azulino industrial"})
        fields_ = cls.env["ir.model.fields"].search([("model", "=", "res.partner"), ("name", "=", "name")])
        cls.config = cls.env["search.relevant.config"].create(
            {"model_id": cls.env["ir.model"]._get_id("res.partner"), "field_ids": [(6, 0, fields_.ids)]}
        )
        while cls.config.backfill_cursor:
            cls.config._backfill_batch(1000)

    def test_the_contract_answers_what_the_engine_answers(self):
        answer = self.env["tuqui.search"].search_relevant("res.partner", "quenapox azulino", limit=5)
        self.assertEqual(answer["engine"], "companion_fts")
        self.assertEqual(answer["terms"], ["quenapox", "azulino"])
        self.assertEqual(answer["coverage"]["state"], "complete")
        self.assertEqual([record["id"] for record in answer["records"]], [self.partner.id])
        self.assertEqual(
            answer, self.env["search.relevant"].search_relevant("res.partner", "quenapox azulino", limit=5)
        )

    def test_the_call_reaches_the_engine_with_the_callers_environment(self):
        """Same environment: the engine runs as the calling user and with the
        caller's context — the language among it, which is what decides the
        words that only name the kind of record."""
        engine = type(self.env["search.relevant"])
        original = engine.search_relevant
        seen = {}

        def spy(records, model, query, domain=None, limit=None):
            seen.update(uid=records.env.uid, lang=records.env.context.get("lang"), args=(model, query, domain, limit))
            return original(records, model, query, domain=domain, limit=limit)

        with patch.object(engine, "search_relevant", spy):
            env = self.env(context={"lang": "en_US"})
            env["tuqui.search"].search_relevant("res.partner", "quenapox", domain=[("active", "=", True)], limit=3)
        self.assertEqual(seen["args"], ("res.partner", "quenapox", [("active", "=", True)], 3))
        self.assertEqual(seen["uid"], self.env.uid)
        self.assertEqual(seen["lang"], "en_US")
