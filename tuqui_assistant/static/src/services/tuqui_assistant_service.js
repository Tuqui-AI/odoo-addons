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
 * Detecta si un elemento de un valor x2many YA es una tupla-comando web de Odoo
 * (`[op, id, vals]`, op numérico 0..6) en vez de un objeto plano de valores.
 * El frontend de Odoo (`StaticList._applyCommands`) SOLO entiende tuplas-comando:
 * `[[0, false, {vals}]]` para CREATE, `[[1, id, {vals}]]` UPDATE, etc. Un objeto
 * plano `{product_id: 1}` como "comando" tiene `command[0] === undefined`, no
 * matchea ningún case y `_applyCommands` no tiene `default` → NO-OP SILENCIOSO
 * (la línea nunca se crea y `_update` igual resuelve "ok"). Ver record.js
 * `_preprocessX2manyChanges` + static_list.js `_applyCommands`.
 */
function isX2manyCommandTuple(el) {
    return Array.isArray(el) && typeof el[0] === "number";
}

/**
 * Normaliza el valor de UN campo x2many a tuplas-comando web de Odoo, tolerando
 * la forma "amigable" (lista de objetos planos) que un LLM tiende a mandar:
 *   - `[{product_id: 1, product_uom_qty: 2}]`  → `[[0, false, {product_id: 1, …}]]`
 *     (cada objeto plano se vuelve un CREATE, op 0).
 *   - `[[0, false, {…}]]` (ya tuplas)          → se deja igual.
 *   - listas mixtas                            → se normaliza elemento por elemento.
 *   - ids sueltos `[1, 2]` (atajo LINK m2m)    → `[[4, 1], [4, 2]]` (op 4 = LINK).
 * Devuelve el valor tal cual si no es un array (no se entromete con otras formas).
 * CREATE=0, LINK=4 son los códigos del web command set de Odoo (orm_service.js).
 */
function normalizeX2manyValue(value) {
    if (!Array.isArray(value)) {
        return value;
    }
    return value.map((el) => {
        if (isX2manyCommandTuple(el)) {
            return el; // ya es [op, id, vals]
        }
        if (el && typeof el === "object" && !Array.isArray(el)) {
            // Objeto plano de valores → CREATE (agregar línea nueva).
            return [0, false, el];
        }
        if (typeof el === "number") {
            // Id suelto → LINK (vincular registro existente, típico m2m).
            return [4, el];
        }
        return el; // forma desconocida: no la tocamos (que falle visible, no en silencio)
    });
}

/**
 * Normaliza una propuesta `{campo: valor}` ya validada contra los campos del
 * form: para cada campo one2many/many2many, convierte la forma amigable (lista
 * de objetos planos) a tuplas-comando que `record._update` sí aplica. Pura (sin
 * dependencias de OWL) → testeable en aislamiento. No muta la entrada.
 *
 * @param {Object} known   propuesta filtrada {campo: valor}
 * @param {Object} fieldDefs  `record.fields` (defs con `.type`)
 * @returns {Object} nueva propuesta con los x2many normalizados a tuplas-comando
 */
function normalizeProposalX2many(known, fieldDefs) {
    const defs = fieldDefs || {};
    const out = {};
    for (const [name, value] of Object.entries(known)) {
        const type = defs[name]?.type;
        if (type === "one2many" || type === "many2many") {
            out[name] = normalizeX2manyValue(value);
        } else {
            out[name] = value;
        }
    }
    return out;
}

/**
 * Coerce los valores RELACIONALES dentro de las `vals` de un comando CREATE/UPDATE
 * de un x2many a la forma que el record OWL del sub-modelo entiende.
 *
 * Por qué hace falta: `StaticList._applyCommands` (case CREATE) crea el datapoint
 * de la línea con `new Record(..., command[2])`, y `parseServerValue` para un
 * many2one acepta `[id, name]` o `{id, display_name}` pero un **entero pelado lo
 * devuelve tal cual** (no lo reconoce como m2o seteado). Un LLM manda
 * `{"product_id": 1}` (id pelado) → la línea queda SIN producto y el onchange ve
 * un producto vacío (no calcula name/price). Convertimos cada valor de un sub-
 * campo many2one/many2one_reference de `<int>` a `{id: <int>}` para que el m2o
 * quede seteado y el onchange del padre resuelva el resto (name, precio, totales).
 *
 * `subFields` son las defs de campo del SUB-modelo (las del StaticList del x2many).
 * Sin ellas (no resolubles) devolvemos las vals sin tocar (best-effort, no rompe).
 */
function coerceRelationalCommandVals(vals, subFields) {
    if (!vals || typeof vals !== "object" || Array.isArray(vals) || !subFields) {
        return vals;
    }
    const out = {};
    for (const [k, v] of Object.entries(vals)) {
        const t = subFields[k]?.type;
        if ((t === "many2one" || t === "many2one_reference") && typeof v === "number") {
            out[k] = { id: v };
        } else {
            out[k] = v;
        }
    }
    return out;
}

/**
 * Aplica coerceRelationalCommandVals a cada comando CREATE(0)/UPDATE(1) de un
 * valor x2many ya normalizado a tuplas-comando. Deja intactos LINK/DELETE/UNLINK
 * (no llevan vals que coercer) y cualquier forma que no sea tupla.
 */
function coerceX2manyCommandRelations(commands, subFields) {
    if (!Array.isArray(commands) || !subFields) {
        return commands;
    }
    return commands.map((cmd) => {
        if (Array.isArray(cmd) && (cmd[0] === 0 || cmd[0] === 1) && cmd[2] && typeof cmd[2] === "object") {
            return [cmd[0], cmd[1], coerceRelationalCommandVals(cmd[2], subFields)];
        }
        return cmd;
    });
}

/**
 * Separa los comandos x2many de un valor (ya normalizado a tuplas) en:
 *   - `creates`: comandos CREATE (op 0) → se aplican vía `list.addNewRecord` +
 *     `line._update(campo)` para que dispare el ONCHANGE de la línea (sin eso, el
 *     producto queda sin name/precio: el onchange del padre NO computa la línea
 *     nueva — verificado en runtime). `vals` = el objeto de valores del comando.
 *   - `rest`: el resto (UPDATE/DELETE/UNLINK/LINK/SET) → se mandan tal cual a
 *     `record._update` (esos sí los maneja bien `_applyCommands`).
 * Si el valor no es un array de tuplas, va entero a `rest` (no nos arriesgamos).
 */
function splitX2manyCreates(value) {
    const creates = [];
    const rest = [];
    if (!Array.isArray(value)) {
        return { creates, rest: value };
    }
    for (const cmd of value) {
        if (Array.isArray(cmd) && cmd[0] === 0 && cmd[2] && typeof cmd[2] === "object") {
            creates.push(cmd[2]); // las vals del CREATE
        } else {
            rest.push(cmd);
        }
    }
    return { creates, rest };
}

/**
 * Cuenta los registros vivos de un campo x2many en un record OWL (para el
 * chequeo "honesto": ¿la propuesta realmente agregó la línea?). El StaticList
 * expone `.count` y, defensivamente, `.records`/`.currentIds`. Devuelve null si
 * no se puede leer (no es x2many, o no hay valor) → el caller no infiere nada.
 */
function x2manyCount(record, name) {
    try {
        const val = record?.data?.[name];
        if (!val) {
            return null;
        }
        if (typeof val.count === "number") {
            return val.count;
        }
        if (Array.isArray(val.records)) {
            return val.records.length;
        }
        if (Array.isArray(val.currentIds)) {
            return val.currentIds.length;
        }
    } catch {
        // sin lectura confiable: no afirmamos nada sobre si tomó o no.
    }
    return null;
}

/**
 * Dada la propuesta NORMALIZADA, devuelve los campos x2many que incluyen al
 * menos un comando que AGREGA filas (CREATE=0 o LINK=4) — esos son los que
 * deberían hacer crecer el `count`. Sirve para el chequeo honesto post-apply:
 * si pedimos agregar una línea y el count no subió, fue un no-op → warning.
 */
function x2manyFieldsExpectingGrowth(normalized, fieldDefs) {
    const defs = fieldDefs || {};
    const fields = [];
    for (const [name, value] of Object.entries(normalized)) {
        const type = defs[name]?.type;
        if ((type === "one2many" || type === "many2many") && Array.isArray(value)) {
            const adds = value.filter((c) => Array.isArray(c) && (c[0] === 0 || c[0] === 4)).length;
            if (adds > 0) {
                fields.push({ name, adds });
            }
        }
    }
    return fields;
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
            // Normalizar x2many a tuplas-comando web: el LLM suele mandar la forma
            // amigable `[{product_id:1,…}]` (lista de objetos planos), pero
            // `record._update` → `StaticList._applyCommands` SOLO entiende
            // `[[0,false,{…}]]` (CREATE). Un objeto plano es un NO-OP silencioso
            // (no matchea ningún case, sin `default`) → la línea nunca se agrega y
            // `_update` igual resuelve "ok". Toleramos AMBAS formas. Ver helpers arriba.
            const normalized = normalizeProposalX2many(known, fieldDefs);
            // Coerce los m2o pelados (id entero) dentro de las vals de cada comando
            // CREATE/UPDATE a `{id}`: si no, la línea nueva queda SIN el m2o (p.ej.
            // sin producto) y el onchange no calcula name/precio/totales. Las defs
            // del sub-modelo salen del StaticList vivo del x2many (`.fields`).
            for (const [name, value] of Object.entries(normalized)) {
                const type = fieldDefs[name]?.type;
                if (type === "one2many" || type === "many2many") {
                    const subFields = activeRecord.data?.[name]?.fields;
                    normalized[name] = coerceX2manyCommandRelations(value, subFields);
                }
            }
            // Las líneas NUEVAS (comando CREATE) NO se aplican con
            // `_update({campo:[[0,false,vals]]})`: por ese camino la línea se crea
            // pero su ONCHANGE no dispara → queda sin name/precio (p.ej. producto en
            // blanco) — verificado en runtime. Se aplican aparte con
            // `list.addNewRecord()` + `line._update(campo)` (lo que hace la grilla al
            // elegir un producto: dispara el onchange de la línea y resuelve
            // name/precio/totales). Separamos CREATE del resto (UPDATE/DELETE/LINK),
            // que sí van bien por `_update`.
            const updatePayload = {};
            const createsByField = {}; // { campo: [vals, …] }
            for (const [name, value] of Object.entries(normalized)) {
                const type = fieldDefs[name]?.type;
                if (type === "one2many" || type === "many2many") {
                    const { creates, rest } = splitX2manyCreates(value);
                    if (creates.length) {
                        createsByField[name] = creates;
                    }
                    // Solo mandamos por _update el resto si quedó algún comando.
                    if (Array.isArray(rest) ? rest.length : rest != null) {
                        updatePayload[name] = rest;
                    }
                } else {
                    updatePayload[name] = value;
                }
            }
            // Snapshot del count de los x2many que esperamos que CREZCAN (CREATE/LINK),
            // para el chequeo honesto post-apply: si pedimos agregar una línea y el
            // count no sube, fue un no-op y NO mostramos el "aplicado" verde a secas.
            const growthExpected = x2manyFieldsExpectingGrowth(normalized, fieldDefs);
            const countsBefore = {};
            for (const { name } of growthExpected) {
                countsBefore[name] = x2manyCount(activeRecord, name);
            }
            try {
                // 1) Escalares + m2o + comandos x2many que NO son CREATE → _update
                //    (lo serializamos por el mutex del modelo, como antes).
                if (Object.keys(updatePayload).length) {
                    await activeRecord.model.mutex.exec(() => activeRecord._update(updatePayload));
                }
                // 2) Líneas nuevas: una por una vía `list.addNewRecord` + `line._update`
                //    (dispara el onchange de la línea → resuelve name/precio/totales).
                //    OJO: `addNewRecord` YA toma el `model.mutex` por dentro. NO lo
                //    envolvemos en otro `mutex.exec` (sería deadlock: el exec interno
                //    esperaría al externo que espera al interno). El `line._update`
                //    posterior se llama directo (await secuencial), igual que la grilla.
                for (const [fieldName, valsList] of Object.entries(createsByField)) {
                    const list = activeRecord.data?.[fieldName];
                    if (!list || typeof list.addNewRecord !== "function") {
                        // Fallback defensivo: sin la API de StaticList, mandamos el
                        // CREATE crudo por _update (al menos agrega la fila, aunque el
                        // onchange no encadene). Mejor eso que perder la línea.
                        await activeRecord.model.mutex.exec(() =>
                            activeRecord._update({ [fieldName]: valsList.map((v) => [0, false, v]) })
                        );
                        continue;
                    }
                    for (const vals of valsList) {
                        const line = await list.addNewRecord({
                            activeFields: list.activeFields,
                            context: {},
                            mode: "edit",
                        });
                        // Aplicar cada campo de la línea (dispara su onchange). `_update`
                        // toma el mutex por dentro, así que lo llamamos directo.
                        for (const [k, v] of Object.entries(vals)) {
                            await line._update({ [k]: v });
                        }
                    }
                }
            } catch (e) {
                notification.add(_t("No se pudieron aplicar los cambios: %s", e.message || e), {
                    type: "danger",
                });
                return false;
            }
            // Chequeo honesto: ¿los x2many que debían crecer realmente crecieron?
            // Un count que no cambió (de un número conocido) = la línea no se aplicó
            // (forma inválida, id inexistente, etc.). Avisamos en vez del verde feliz.
            const notApplied = [];
            for (const { name } of growthExpected) {
                const before = countsBefore[name];
                const after = x2manyCount(activeRecord, name);
                if (typeof before === "number" && typeof after === "number" && after <= before) {
                    notApplied.push(name);
                }
            }
            if (notApplied.length) {
                notification.add(
                    // Single string literal (no `+` concat) so Odoo's i18n
                    // extractor captures the full msgid. Same runtime output.
                    _t(
                        "Se aplicaron los cambios, pero no se pudo agregar la(s) línea(s) en: %s. Revisá el formulario; puede que falte resolver un dato (p.ej. el producto). No des por hecho que la línea quedó agregada.",
                        notApplied.join(", ")
                    ),
                    { type: "warning" }
                );
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

// Helpers puros de normalización x2many, exportados para test en aislamiento
// (sin montar el servicio OWL). Ver embed_apply_x2many_normalize_test.js.
export {
    normalizeX2manyValue,
    normalizeProposalX2many,
    x2manyFieldsExpectingGrowth,
    isX2manyCommandTuple,
    coerceRelationalCommandVals,
    coerceX2manyCommandRelations,
    splitX2manyCreates,
};
