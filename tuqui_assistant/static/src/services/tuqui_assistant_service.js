/** @odoo-module **/
import { registry } from "@web/core/registry";
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * Estado compartido + puente (bridge) entre el panel del asistente y el
 * formulario abierto.
 *
 * El corazón del spike: aplicar una propuesta de cambios al record activo EN
 * MEMORIA (record._update), dejando el formulario "dirty" para que el usuario
 * Guarde o Descarte con los controles nativos de Odoo. No persiste nada a la
 * base de datos. Espeja el mecanismo de `ai_fields` (ver
 * enterprise/ai_fields/static/src/model/relational_model/record.js).
 */
export const tuquiAssistantService = {
    dependencies: ["notification"],
    start(env, { notification }) {
        const state = reactive({
            panelOpen: false,
            context: null, // PageContext-lite: { model, resId, displayName }
        });

        // Record OWL activo del FormController montado. No es reactivo a
        // propósito: es un objeto del modelo, no UI state.
        let activeRecord = null;

        function setActiveRecord(record) {
            activeRecord = record || null;
            state.context = record
                ? {
                      model: record.resModel,
                      resId: record.resId,
                      displayName: record.data?.display_name || record.data?.name || "",
                  }
                : null;
        }

        function clearActiveRecord(record) {
            // Limpiar solo si el que se desmonta es el que teníamos registrado.
            if (!record || activeRecord === record) {
                activeRecord = null;
                state.context = null;
            }
        }

        function togglePanel() {
            state.panelOpen = !state.panelOpen;
        }

        /**
         * Aplica una propuesta { campo: valor, ... } al formulario abierto, en
         * memoria. En el spike la arma el panel a mano; al integrar vendrá de
         * la tool `propose_odoo_form_changes` de Tuqui (por postMessage).
         */
        async function applyProposal(changes) {
            if (!activeRecord) {
                notification.add(_t("No hay un formulario abierto donde aplicar cambios."), {
                    type: "warning",
                });
                return false;
            }
            if (!changes || typeof changes !== "object" || Array.isArray(changes)) {
                notification.add(_t("La propuesta debe ser un objeto { campo: valor }."), {
                    type: "danger",
                });
                return false;
            }
            try {
                await activeRecord.model.mutex.exec(() => activeRecord._update(changes));
            } catch (e) {
                notification.add(_t("No se pudieron aplicar los cambios: %s", e.message || e), {
                    type: "danger",
                });
                throw e;
            }
            notification.add(
                _t("Cambios aplicados al formulario (sin guardar). Revisá y Guardá o Descartá."),
                { type: "success" }
            );
            return true;
        }

        return {
            state,
            setActiveRecord,
            clearActiveRecord,
            togglePanel,
            applyProposal,
            getActiveRecord: () => activeRecord,
        };
    },
};

registry.category("services").add("tuquiAssistant", tuquiAssistantService);
