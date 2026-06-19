/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { useService } from "@web/core/utils/hooks";
import { useEffect } from "@odoo/owl";

/**
 * Publica al asistente el contexto de una vista kanban como modo "list"
 * (dominio + filtros + count). Kanban no tiene selección multi de tarjetas, así
 * que no hay caso "selection" acá. Solo lectura. Espeja el patch de la lista.
 */
patch(KanbanController.prototype, {
    setup() {
        super.setup();
        this.tuquiAssistant = useService("tuquiAssistant");
        useEffect(
            () => {
                const root = this.model.root;
                this.tuquiAssistant.setSearchContext(this, {
                    model: root.resModel,
                    resIds: [],
                    count: root.count,
                    domain: root.domain,
                    filters: this._tuquiFacets(),
                });
                return () => this.tuquiAssistant.clearContext(this);
            },
            () => [
                this.model.root.resModel,
                this.model.root.count,
                JSON.stringify(this.model.root.domain),
            ]
        );
    },

    _tuquiFacets() {
        try {
            return (this.env.searchModel?.facets || [])
                .map((f) => (f.values && f.values.length ? f.values.join(", ") : f.title))
                .filter(Boolean);
        } catch {
            return [];
        }
    },
});
