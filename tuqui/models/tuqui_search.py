from odoo import api, models
from odoo.addons.base_search_relevant.models.search_relevant import DEFAULT_LIMIT


class TuquiSearch(models.AbstractModel):
    """The RPC contract Tuqui calls, over the engine of ``base_search_relevant``.

    Everything that searches and ranks lives in that module and knows nothing
    about Tuqui; what is Tuqui's own is this name and this signature. The Tuqui
    backend calls ``tuqui.search.search_relevant`` by ``execute_kw``
    (``CONTRATO-busqueda.md`` §1), so neither the model name nor the shape of
    the answer can change here without changing the other side.

    The method name starts with ``search`` on purpose — the ``/tuqui/rpc`` gate
    classifies it as a read without a new module version — and it must
    therefore never write anything.
    """

    _name = "tuqui.search"
    _description = "Tuqui Relevance Search"

    @api.model
    @api.readonly
    def search_relevant(self, model, query, domain=None, limit=DEFAULT_LIMIT):
        """Records of ``model`` that best match ``query``, as the current user sees them.

        Order, permissions and coverage: see
        :meth:`~odoo.addons.base_search_relevant.models.search_relevant.SearchRelevant.search_relevant`.

        :returns: ``{"engine", "terms", "term_stats", "population", "coverage":
            {"state", "indexed_until", "fields"}, "records": [{"id", "score",
            "matched_terms", "snippet"}]}``; ``coverage.state`` is
            ``not_enabled`` (and ``records`` empty) when no administrator
            enabled ``model`` — a signal to use another engine, not an error.
        """
        # Same environment, so the engine runs as the calling user and with the
        # caller's context — the language among it, which is what decides the
        # words that only name the kind of record.
        return self.env["search.relevant"].search_relevant(model, query, domain=domain, limit=limit)
