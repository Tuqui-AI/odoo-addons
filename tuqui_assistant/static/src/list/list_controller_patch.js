/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { ListController } from "@web/views/list/list_controller";
import { useService } from "@web/core/utils/hooks";
import { useEffect } from "@odoo/owl";

/**
 * Publica al asistente el contexto de la vista lista mientras está montada:
 *   - con N seleccionados → modo "selection" (ids; o "todo el filtro" si el
 *     usuario eligió seleccionar todo lo que matchea el dominio),
 *   - sin selección → modo "list": ubicación (modelo + nombre de menú +
 *     etiquetas legibles de los filtros activos; sin count ni dominio).
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
                    modelLabel: this._tuquiModelLabel(),
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
                // Los IDS, no su cantidad. Con `selection.length` acá, destildar
                // una fila y tildar otra deja el largo igual: el efecto no vuelve
                // a correr, no se re-publica el contexto, y el assistant sigue
                // creyendo que está seleccionada la que sacaste.
                //
                // Con el cap en 50 eso terminaba en una negativa: la selección
                // llegaba marcada como cortada y el assistant no actuaba. Ahora
                // que viaja entera, el prompt manda hacer el trabajo INLINE — así
                // que el mismo desfasaje termina en una escritura sobre el
                // registro equivocado, en silencio. Es el modo de falla que el
                // flag `truncated` existe para evitar, entrando por otra puerta.
                this.model.root.selection.map((r) => r.resId).join(","),
                this.model.root.isDomainSelected,
                this.model.root.count,
                JSON.stringify(this.model.root.domain),
                this._tuquiFacets().join("\u0000"),
            ]
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
