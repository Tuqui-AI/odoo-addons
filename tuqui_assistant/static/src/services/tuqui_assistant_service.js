/** @odoo-module **/
import { registry } from "@web/core/registry";
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * Estado compartido + puente (bridge) entre el panel del asistente y lo que el
 * usuario está mirando en Odoo.
 *
 * Contexto etiquetado (`state.context.kind`):
 *   - null         → sin contexto (dashboard, settings…): chat general.
 *   - "record"     → form abierto, 1 registro. Habilita propose-then-apply.
 *   - "selection"  → lista/kanban con N registros seleccionados (solo lectura).
 *   - "list"       → lista/kanban sin selección → dominio + filtros (solo lectura).
 *
 * Apply in-memory (`record._update`) **solo** existe para "record" (el único con
 * un record de form en memoria). Escribir sobre varios es un bulk write por el
 * gateway del companion (gateado por chat-permission-system) — vía distinta, no
 * se mezcla acá. Ver docs/embed-protocol.md y la spec tuqui-embebido-en-odoo.
 */
export const tuquiAssistantService = {
    dependencies: ["notification", "orm"],
    start(env, { notification, orm }) {
        const state = reactive({
            panelOpen: false,
            context: null,
        });

        // Record OWL del form activo (solo en modo "record"). No reactivo a
        // propósito: es un objeto del modelo, no UI state. `_owner` es quien
        // publicó el contexto actual (el controller), para limpiar sin pisarse.
        let activeRecord = null;
        let _owner = null;

        function setRecordContext(owner, record) {
            _owner = owner;
            activeRecord = record || null;
            state.context = record
                ? {
                      kind: "record",
                      model: record.resModel,
                      resId: record.resId,
                      displayName: record.data?.display_name || record.data?.name || "",
                  }
                : null;
        }

        /**
         * Contexto de una vista lista/kanban. `payload`:
         *   { model, resIds, isDomainSelected, count, domain, filters }
         */
        function setSearchContext(owner, payload) {
            _owner = owner;
            activeRecord = null; // no hay form record donde aplicar
            const resIds = payload.resIds || [];
            if (payload.isDomainSelected) {
                // "Seleccionar los N que matchean el dominio": todo el filtro.
                state.context = {
                    kind: "selection",
                    model: payload.model,
                    count: payload.count,
                    resIds: [],
                    allMatching: true,
                };
            } else if (resIds.length) {
                state.context = {
                    kind: "selection",
                    model: payload.model,
                    count: resIds.length,
                    resIds,
                };
            } else {
                state.context = {
                    kind: "list",
                    model: payload.model,
                    count: payload.count,
                    domain: payload.domain || [],
                    filters: payload.filters || [],
                };
            }
        }

        function clearContext(owner) {
            if (!owner || _owner === owner) {
                _owner = null;
                activeRecord = null;
                state.context = null;
            }
        }

        function togglePanel() {
            state.panelOpen = !state.panelOpen;
        }

        /**
         * SSO embebido (ADR 0001 / spec §2.2): mintea un nonce single-use atado
         * al usuario Odoo logueado y devuelve { nonce, client_id } para pasárselo
         * al iframe. El SPA lo canjea contra Tuqui por un token de sesión corto,
         * así no hay login dentro del iframe. Devuelve null si el companion no
         * está activado (sin client_id) → el SPA muestra "usuario no vinculado".
         */
        async function getSsoAuth() {
            try {
                return await orm.call("tuqui.assistant.sso.nonce", "issue_for_current_user", []);
            } catch {
                return null;
            }
        }

        /**
         * Bootstrap del embed resuelto desde companion (ADR 0001): devuelve
         * `{ connected, base_url, slug }`. La base sale de `tuqui.base_url`
         * (default tuqui.com) y el slug del workspace activado en el oauth client
         * — ya no hay `tuqui_assistant.spa_url`. Lee vía un método sudo porque
         * `tuqui.oauth.client` es admin-only.
         */
        async function getEmbedBootstrap() {
            try {
                return await orm.call("tuqui.assistant.sso.nonce", "embed_bootstrap", []);
            } catch {
                return { connected: false, base_url: null, slug: null };
            }
        }

        /**
         * Contexto a mandarle al SPA (PageContext). Para "record" incluye los
         * valores en memoria (`fields`, JSON-clonable; con cambios sin guardar).
         * Para "selection"/"list" manda ids/dominio/count — NO las filas (guard
         * de tamaño): el detalle lo pide Tuqui por el gateway si lo necesita.
         */
        function getContextPayload() {
            if (!state.context) {
                return null;
            }
            const ctx = { ...state.context };
            if (ctx.kind === "record" && activeRecord) {
                ctx.dirty = Boolean(activeRecord.dirty);
                try {
                    ctx.fields = JSON.parse(JSON.stringify(activeRecord.data ?? {}));
                } catch {
                    ctx.fields = {};
                }
            }
            return ctx;
        }

        /**
         * Aplica una propuesta { campo: valor, ... } al form abierto, en memoria.
         * Requiere modo "record" (hay activeRecord). Origen: panel (fallback) o
         * iframe del SPA (tool propose_odoo_form_changes vía postMessage).
         */
        async function applyProposal(changes) {
            if (!activeRecord) {
                notification.add(
                    _t("Abrí un formulario (1 registro) para aplicar cambios desde acá."),
                    { type: "warning" }
                );
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
            setRecordContext,
            setSearchContext,
            clearContext,
            togglePanel,
            applyProposal,
            getEmbedBootstrap,
            getSsoAuth,
            getContextPayload,
            getActiveRecord: () => activeRecord,
        };
    },
};

registry.category("services").add("tuquiAssistant", tuquiAssistantService);
