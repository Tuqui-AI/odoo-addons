/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { ListController } from "@web/views/list/list_controller";
import { useService } from "@web/core/utils/hooks";
import { useEffect } from "@odoo/owl";

/**
 * Publica al asistente el contexto de la vista lista mientras está montada:
 *   - con N seleccionados → modo "selection" (ids; o "todo el filtro" si el
 *     usuario eligió seleccionar todo lo que matchea el dominio),
 *   - sin selección → modo "list" (dominio + count + filtros).
 *
 * Solo lectura: no habilita propose-then-apply (eso es solo para el form de 1
 * registro). Mandamos ids/dominio/count, NO las filas (guard de tamaño).
 *
 * Reusa el patrón del propio ListController, que ya reacciona a
 * `[selection.length, isDomainSelected]` (list_controller.js).
 */
patch(ListController.prototype, {
    setup() {
        super.setup();
        this.tuquiAssistant = useService("tuquiAssistant");
        useEffect(
            () => {
                const root = this.model.root;
                this.tuquiAssistant.setSearchContext(this, {
                    model: root.resModel,
                    resIds: root.selection.map((r) => r.resId),
                    isDomainSelected: root.isDomainSelected,
                    count: root.count,
                    domain: root.domain,
                    filters: this._tuquiFacets(),
                });
                return () => this.tuquiAssistant.clearContext(this);
            },
            () => [
                this.model.root.resModel,
                this.model.root.selection.length,
                this.model.root.isDomainSelected,
                this.model.root.count,
                JSON.stringify(this.model.root.domain),
            ]
        );
    },

    /** Etiquetas legibles de los filtros activos (best-effort, defensivo). */
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
