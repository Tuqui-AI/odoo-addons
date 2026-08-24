/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { useService } from "@web/core/utils/hooks";
import { useEffect } from "@odoo/owl";

/**
 * Publica al asistente el contexto de una vista kanban como modo "list":
 * ubicación pura (modelo + nombre de menú). Kanban no tiene selección multi de
 * tarjetas, así que no hay caso "selection" acá — y sin selección no viajan
 * count ni dominio (pararse en una vista no es intención de operar); las
 * etiquetas legibles de los filtros sí, como parte de la ubicación.
 */
patch(KanbanController.prototype, {
    setup() {
        super.setup();
        this.tuquiAssistant = useService("tuquiAssistant");
        useEffect(
            () => {
                this.tuquiAssistant.setSearchContext(this, {
                    model: this.model.root.resModel,
                    modelLabel: this._tuquiModelLabel(),
                    filters: this._tuquiFacets(),
                });
                return () => this.tuquiAssistant.clearContext(this);
            },
            () => [this.model.root.resModel, this._tuquiFacets().join("\u0000")]
        );
    },

    /**
     * Etiquetas legibles de los filtros activos (best-effort, defensivo).
     * Viajan como UBICACIÓN, no como datos: son lo que la pantalla del usuario
     * dice ("Vencidas", "Cliente X"), para que el assistant pueda repreguntar
     * bien en vez de responder workspace-wide con confianza. El dominio máquina
     * NO viaja sin selección — para eso está "seleccionar todo".
     */
    _tuquiFacets() {
        try {
            return (this.env.searchModel?.facets || [])
                .map((f) => (f.values && f.values.length ? f.values.join(", ") : f.title))
                .filter(Boolean);
        } catch {
            return [];
        }
    },

    /**
     * Nombre humano del modelo: el breadcrumb de la acción ("Contactos"), que
     * ya viene traducido y pluralizado por Odoo. Best-effort: si la API del
     * breadcrumb cambia, el chip cae al nombre técnico y nada se rompe.
     */
    _tuquiModelLabel() {
        try {
            return this.env.config?.getDisplayName?.() || "";
        } catch {
            return "";
        }
    },

});
