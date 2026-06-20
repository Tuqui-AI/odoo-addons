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

/**
 * Serializa los valores en memoria de un record OWL a un objeto JSON-safe para
 * mandar como contexto al SPA. NO se puede `JSON.stringify(record.data)` directo:
 * los x2many son StaticList que referencian de vuelta al controller → ciclo →
 * `JSON.stringify` TIRA "Converting circular structure to JSON" y el catch de
 * abajo dejaba `fields={}` (se perdían TODOS los campos, hasta los escalares).
 * Serializamos por tipo, cada campo en su propio try (un campo no serializable
 * no tira abajo el resto):
 *   - many2one / many2one_reference → { id, display_name } | false
 *   - one2many / many2many → { count, ids } (guard de tamaño: ids capeados, sin filas)
 *   - date → ISO "YYYY-MM-DD"; datetime → ISO; binary → "<binary>"
 *   - resto (char/text/int/float/bool/selection/…) → tal cual
 */
function serializeRecordFields(record) {
    const fields = record.fields || {};
    const data = record.data || {};
    const out = {};
    for (const name of Object.keys(data)) {
        const type = fields[name]?.type;
        const val = data[name];
        try {
            if (val === false || val === null || val === undefined) {
                out[name] = val ?? false;
            } else if (type === "many2one" || type === "many2one_reference") {
                out[name] =
                    val.id !== undefined
                        ? { id: val.id, display_name: val.display_name ?? null }
                        : false;
            } else if (type === "one2many" || type === "many2many") {
                const ids = (val.currentIds || []).filter((x) => typeof x === "number");
                out[name] = { count: val.count ?? ids.length, ids: ids.slice(0, 50) };
            } else if (type === "date") {
                out[name] = typeof val.toISODate === "function" ? val.toISODate() : String(val);
            } else if (type === "datetime") {
                out[name] = typeof val.toISO === "function" ? val.toISO() : String(val);
            } else if (type === "binary") {
                out[name] = "<binary>";
            } else if (typeof val === "object") {
                out[name] = JSON.parse(JSON.stringify(val));
            } else {
                out[name] = val;
            }
        } catch {
            out[name] = null;
        }
    }
    return out;
}

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
         * valores en memoria (`fields`, escalares por tipo; con cambios sin guardar).
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
                ctx.fields = serializeRecordFields(activeRecord);
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
            // Validar contra los campos del form: descartar inexistentes / readonly
            // antes de _update. Una propuesta con campos ajenos (p.ej. {email_from,
            // subject} sobre un res.partner) reventaba _update con un error JS sin
            // atrapar; ahora se ignoran y, si no queda nada válido, se avisa y corta.
            const fieldDefs = activeRecord.fields || {};
            const known = {};
            const dropped = [];
            for (const [name, value] of Object.entries(changes)) {
                if (!fieldDefs[name] || fieldDefs[name].readonly === true) {
                    dropped.push(name);
                } else {
                    known[name] = value;
                }
            }
            if (dropped.length) {
                notification.add(
                    _t("Se ignoraron campos que no se pueden editar en este formulario: %s", dropped.join(", ")),
                    { type: "warning" }
                );
            }
            if (!Object.keys(known).length) {
                notification.add(
                    _t("Ningún campo de la propuesta se puede aplicar al formulario abierto."),
                    { type: "warning" }
                );
                return false;
            }
            try {
                await activeRecord.model.mutex.exec(() => activeRecord._update(known));
            } catch (e) {
                notification.add(_t("No se pudieron aplicar los cambios: %s", e.message || e), {
                    type: "danger",
                });
                return false;
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
