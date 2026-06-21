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

/**
 * Sanitiza defensivamente un fragmento HTML (body de chatter) ANTES de pasarlo
 * al compositor. El campo `body` de mail.compose.message es html con sanitize=True,
 * así que el server limpia al guardar; esto es defensa en profundidad del lado
 * cliente: parsea en un documento desconectado (NO se ejecuta nada — DOMParser
 * no corre scripts ni dispara <img>/<iframe>) y elimina vectores activos:
 *   - <script> / <style> / <iframe> / <object> / <embed> / <link> / <meta>
 *   - atributos de evento on* (onclick, onerror, …)
 *   - URLs javascript: en href/src
 * Si el parseo falla por cualquier motivo, devuelve "" (no se arriesga a inyectar).
 */
function sanitizeChatterBody(html) {
    if (typeof html !== "string" || !html.trim()) {
        return "";
    }
    try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const KILL = ["script", "style", "iframe", "object", "embed", "link", "meta", "base"];
        doc.querySelectorAll(KILL.join(",")).forEach((el) => el.remove());
        doc.querySelectorAll("*").forEach((el) => {
            for (const attr of [...el.attributes]) {
                const name = attr.name.toLowerCase();
                const value = (attr.value || "").replace(/\s+/g, "").toLowerCase();
                if (name.startsWith("on") || ((name === "href" || name === "src") && value.startsWith("javascript:"))) {
                    el.removeAttribute(attr.name);
                }
            }
        });
        return doc.body ? doc.body.innerHTML : "";
    } catch {
        return "";
    }
}

export const tuquiAssistantService = {
    // `action` es para abrir el compositor estándar (doAction) desde proposeChatter.
    dependencies: ["notification", "orm", "action"],
    start(env, { notification, orm, action }) {
        // Preferencias persistidas (sobreviven recargas): card expandida y si el
        // panel sigue el contexto de Odoo. Lectura tolerante a fallos (localStorage
        // puede tirar en modo privado / cuota). `followContext` default true.
        const LS_EXPANDED = "tuqui_assistant.expanded";
        const LS_FOLLOW = "tuqui_assistant.followContext";
        function readBool(key, fallback) {
            try {
                const raw = window.localStorage.getItem(key);
                return raw === null ? fallback : raw === "1";
            } catch {
                return fallback;
            }
        }
        function writeBool(key, value) {
            try {
                window.localStorage.setItem(key, value ? "1" : "0");
            } catch {
                // sin persistencia (modo privado / cuota): el estado vive en memoria.
            }
        }

        const state = reactive({
            panelOpen: false,
            minimized: false,
            expanded: readBool(LS_EXPANDED, false),
            followContext: readBool(LS_FOLLOW, true),
            context: null,
            // Contador-nonce que el systray incrementa para pedirle al panel
            // (ya abierto) que arranque un chat NUEVO sin remontar el iframe
            // (un remount gastaría un 2º nonce SSO → 401). El panel observa los
            // cambios y postea `new-chat` al SPA. Ver openFreshChat / systray.
            newChatRequest: 0,
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
            // Al cerrar/reabrir desde el systray, arrancá con la card visible: el
            // estado minimizado es per-apertura y no debe sobrevivir un toggle.
            state.minimized = false;
        }

        // Click del systray (item CTO #4): SIEMPRE abre el panel en un chat NUEVO
        // (cerrar queda en los botones minimizar/cerrar de la card, ya no togglea).
        //   - Panel cerrado → abrirlo monta el iframe en `/embed/:slug`, que YA es
        //     un chat nuevo: alcanza con mostrar la card (no se postea nada; el
        //     iframe aún no está montado/hidratado).
        //   - Panel ya abierto (o minimizado: el iframe sigue montado) → NO se
        //     remonta el iframe (gastaría un 2º nonce SSO → 401): se restaura la
        //     card y se incrementa newChatRequest para que el panel le postee
        //     `new-chat` al SPA (navegación interna a un chat nuevo).
        function openFreshChat() {
            const wasOpen = state.panelOpen;
            state.panelOpen = true;
            state.minimized = false;
            if (wasOpen) {
                state.newChatRequest += 1;
            }
        }

        // Minimizar a burbuja / restaurar la card. NO cierra el panel: el iframe
        // sigue montado (la card se oculta por CSS, no se desmonta) — un remount
        // gastaría un 2º nonce SSO single-use → 401. Ver panel.xml.
        function minimize() {
            state.minimized = true;
        }
        function restore() {
            state.minimized = false;
        }

        // Expandir / colapsar la card (Fase 2): toggle de tamaño, sin resize libre.
        // Se persiste para que la preferencia sobreviva recargas.
        function toggleExpand() {
            state.expanded = !state.expanded;
            writeBool(LS_EXPANDED, state.expanded);
        }

        // Seguir contexto (Fase 3a): cuando está OFF, navegar a otro registro de
        // Odoo NO empuja contexto nuevo al iframe (la conversación congela el suyo).
        // El gate vive en el panel (useEffect del _contextKey). Se persiste.
        function toggleFollowContext() {
            state.followContext = !state.followContext;
            writeBool(LS_FOLLOW, state.followContext);
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
         *
         * CRÍTICO: el resultado se manda por `postMessage`, que serializa con el
         * structured clone algorithm — y ese algoritmo NO puede clonar Proxies.
         * `state.context` es `reactive(...)`, así que sus valores ANIDADOS
         * (`domain`, `filters`, `resIds`) son arrays Proxy: un `{...state.context}`
         * los copia tal cual (siguen siendo Proxy) y `postMessage` tira
         * `DataCloneError`. El form no lo sufría porque su `fields` se reconstruye
         * como objeto plano (serializeRecordFields) y el resto son escalares — pero
         * list/selection mandaban arrays reactivos y el `postMessage` reventaba en
         * silencio (el catch de `_postContext`), así que NUNCA llegaba el contexto
         * de lista/kanban al SPA. Devolvemos un objeto PLANO (deep copy) para que
         * cualquier estructura reactiva quede aplanada, sin importar el `kind`.
         */
        function getContextPayload() {
            if (!state.context) {
                // Sin registro/lista abiertos (home, dashboard, settings…): NO
                // devolvemos null. Un sentinel {kind:"none"} hace que el SPA mande
                // SIEMPRE un odoo_context, así el backend trata el turno como embed
                // y expone open_odoo_view (navegar desde el home). Ver _build_odoo_
                // context_note (rama kind=="none") y el panel (_postContext on open).
                return { kind: "none" };
            }
            const ctx = { ...state.context };
            if (ctx.kind === "record" && activeRecord) {
                ctx.dirty = Boolean(activeRecord.dirty);
                ctx.fields = serializeRecordFields(activeRecord);
            }
            // Aplanar a JSON puro: arranca los Proxies reactivos (domain/filters/
            // resIds) que romperían el structured clone de postMessage. Los datos
            // que llevan son JSON-safe (arrays/strings/números/booleanos), así que
            // un round-trip por JSON es seguro y suficiente.
            try {
                return JSON.parse(JSON.stringify(ctx));
            } catch {
                // Defensa: si algo no fuese serializable, devolvé al menos la
                // identidad mínima en vez de nada (mejor contexto parcial que cero).
                return { kind: ctx.kind, model: ctx.model };
            }
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

        /**
         * Propone contenido para el chatter del registro abierto: abre el
         * compositor ESTÁNDAR de Odoo (mail.compose.message) pre-cargado en un
         * diálogo (target:"new"). El usuario revisa y hace clic en Enviar — NUNCA
         * se publica en silencio (el dispatch humano es estructural). Mirror del
         * `openFullComposer` del AI de Odoo Enterprise.
         *
         * GUARDRAILS (no se confía en el SPA):
         *   - Requiere contexto "record" (model + resId). Si no hay form abierto,
         *     avisa y corta (no se puede mandar al chatter de "nada").
         *   - Default a NOTA INTERNA (mail.mt_note). Solo mode==="message" usa
         *     mail.mt_comment (mensaje a seguidores).
         *   - body se trata como HTML y se sanitiza defensivamente.
         *
         * @param {{mode?: string, body?: string, subject?: string}} payload
         */
        async function proposeChatter({ mode, body, subject } = {}) {
            const ctx = state.context;
            if (!ctx || ctx.kind !== "record" || !ctx.model || !ctx.resId) {
                notification.add(
                    _t("Abrí un formulario (1 registro) para mandar al chatter."),
                    { type: "warning" }
                );
                return false;
            }
            // Default a nota interna; solo "message" notifica a seguidores.
            const isMessage = mode === "message";
            const safeBody = sanitizeChatterBody(body);
            const composerContext = {
                default_model: ctx.model,
                default_res_ids: [ctx.resId],
                default_body: safeBody,
                default_subtype_xmlid: isMessage ? "mail.mt_comment" : "mail.mt_note",
                default_composition_mode: "comment",
            };
            if (typeof subject === "string" && subject.trim()) {
                composerContext.default_subject = subject;
            }
            try {
                await action.doAction({
                    type: "ir.actions.act_window",
                    name: isMessage ? _t("Enviar mensaje") : _t("Registrar nota"),
                    res_model: "mail.compose.message",
                    view_mode: "form",
                    views: [[false, "form"]],
                    target: "new",
                    context: composerContext,
                });
            } catch (e) {
                notification.add(
                    _t("No se pudo abrir el compositor del chatter: %s", e.message || e),
                    { type: "danger" }
                );
                return false;
            }
            return true;
        }

        // View types que el frontend puede abrir en modo browse. En sync con la
        // allow-list del backend (open_odoo_view); cualquier otra cosa cae a "list".
        const BROWSE_VIEW_TYPES = ["list", "kanban", "pivot", "graph", "calendar", "form"];

        /**
         * Navega la UI de Odoo desde el chat embebido (tool open_odoo_view del
         * SPA vía postMessage). NO escribe nada: abre una pantalla con el
         * act_window estándar de Odoo (que chequea permisos como cualquier acción).
         * A diferencia de proposeChatter, navega la ventana completa
         * (target:"current"), NO un diálogo.
         *
         *   - mode==="browse": abre una lista/pivot/gráfico filtrado por `domain`.
         *     `viewType` se valida contra BROWSE_VIEW_TYPES (default "list"); se
         *     incluyen también form y list para que el usuario pueda alternar vista.
         *   - mode==="new" (default): abre un formulario VACÍO para crear un
         *     registro, con `defaults` pre-cargados como default_<campo>.
         *
         * @param {{model?: string, mode?: string, viewType?: string, domain?: any[], defaults?: object, title?: string}} payload
         */
        async function navigate({ model, mode, viewType, domain, defaults, title } = {}) {
            if (typeof model !== "string" || !model.trim()) {
                notification.add(
                    _t("No se pudo navegar: falta el modelo de Odoo a abrir."),
                    { type: "danger" }
                );
                return false;
            }
            if (mode === "browse") {
                let vt = BROWSE_VIEW_TYPES.includes(viewType) ? viewType : "list";
                // "browse" es ver un CONJUNTO filtrado: un form como vista de
                // apertura no tiene res_id → Odoo abre un form de CREACIÓN vacío y
                // descarta el dominio. Para crear un registro está mode="new"; acá
                // degradamos form→list (la lista filtrada, con form para drill-in).
                if (vt === "form") {
                    vt = "list";
                }
                // La vista pedida primero (la que abre), más form y list para que
                // el usuario pueda navegar a un registro o volver a la lista.
                const views = [[false, vt]];
                if (vt !== "form") {
                    views.push([false, "form"]);
                }
                if (vt !== "list") {
                    views.push([false, "list"]);
                }
                try {
                    await action.doAction(
                        {
                            type: "ir.actions.act_window",
                            name: title || model,
                            res_model: model,
                            views,
                            domain: Array.isArray(domain) ? domain : [],
                            target: "current",
                            context: {},
                        },
                        { viewType: vt }
                    );
                } catch (e) {
                    notification.add(
                        _t("No se pudo abrir la vista en Odoo: %s", e.message || e),
                        { type: "danger" }
                    );
                    return false;
                }
                return true;
            }
            // mode "new" (default): formulario vacío para crear un registro.
            const ctx = {};
            if (defaults && typeof defaults === "object" && !Array.isArray(defaults)) {
                for (const [name, value] of Object.entries(defaults)) {
                    ctx[`default_${name}`] = value;
                }
            }
            try {
                await action.doAction({
                    type: "ir.actions.act_window",
                    name: title || model,
                    res_model: model,
                    views: [[false, "form"]],
                    target: "current",
                    view_id: false,
                    context: ctx,
                });
            } catch (e) {
                notification.add(
                    _t("No se pudo abrir el formulario nuevo en Odoo: %s", e.message || e),
                    { type: "danger" }
                );
                return false;
            }
            return true;
        }

        return {
            state,
            setRecordContext,
            setSearchContext,
            clearContext,
            togglePanel,
            openFreshChat,
            minimize,
            restore,
            toggleExpand,
            toggleFollowContext,
            applyProposal,
            proposeChatter,
            navigate,
            getEmbedBootstrap,
            getSsoAuth,
            getContextPayload,
            getActiveRecord: () => activeRecord,
        };
    },
};

registry.category("services").add("tuquiAssistant", tuquiAssistantService);
