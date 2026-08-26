/** @odoo-module **/
import { registry } from "@web/core/registry";
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

// Guard de tamaño para ids de campos x2many enviados como contexto al SPA.
const MAX_X2MANY_IDS = 50;

// Guard de tamaño para los ids de una selección de lista/kanban.
//
// Espejaba `batch_service.MAX_RECORDS` (50), con este razonamiento: por encima
// de ese techo el assistant no corre el batch igual, así que mandar más ids es
// contexto pago sin uso. Ese razonamiento CADUCÓ. El trabajo sobre varios
// registros hoy va INLINE por default — una llamada a tool por registro, ahí
// mismo en la conversación — y `run_over_selection` (el batch) es la excepción
// cara. Lo inline no tiene techo de 50: lo limita el presupuesto del turno. Con
// el cap viejo, una selección de 80 llegaba truncada y el assistant se negaba a
// actuar sobre una lista parcial: correcto, pero por un límite que ya no
// gobernaba el camino que iba a tomar.
//
// El cap no desaparece, cambia de dueño. Lo que hacía caro mandar muchos ids
// era IMPRIMIRLOS: el contexto se pega al texto de cada turno, así que la lista
// se pagaba en todos. Eso se resolvió del lado de Tuqui — la nota deja de
// volcarlos y `get_selected_ids` los entrega a pedido. Acá queda sólo el guard
// de transporte: un tope que evita un postMessage disparatado, alineado con el
// tamaño por encima del cual enumerar deja de ser la respuesta (ahí la palanca
// es el dominio, o el "seleccionar todos" de Odoo, que manda el filtro).
//
// El `count` sigue siendo el real, y `truncated` sigue marcando el corte: por
// arriba de esto la lista ES parcial y nadie debe leerla como la selección.
const MAX_SELECTION_IDS = 200;

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
        // Skip Studio-generated display-only HTML banner fields (x_dynamic_message*):
        // readonly messages with no value for the AI context.
        if (name.startsWith("x_dynamic_message")) {
            continue;
        }
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
                out[name] = { count: val.count ?? ids.length, ids: ids.slice(0, MAX_X2MANY_IDS) };
            } else if (type === "date") {
                out[name] = typeof val.toISODate === "function" ? val.toISODate() : String(val);
            } else if (type === "datetime") {
                out[name] = typeof val.toISO === "function" ? val.toISO() : String(val);
            } else if (type === "binary") {
                out[name] = "<binary>";
            } else if (typeof val === "object") {
                out[name] = { ...val };
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
 * Dada la propuesta NORMALIZADA, devuelve los campos x2many con al menos un
 * comando CREATE (op 0), y cuántos — esos son los que TIENEN que hacer crecer
 * el `count` en exactamente esa cantidad. Sirve para el chequeo honesto
 * post-apply: pedimos N líneas nuevas, si no aparecieron N fue un no-op.
 *
 * LINK (op 4) queda AFUERA a propósito, por dos razones independientes:
 *
 *   1. Re-linkear una fila que ya está en la lista es un no-op LEGÍTIMO: lo
 *      que se pidió (que el registro quede vinculado) es verdad antes y
 *      después. Contarlo como "no se aplicó" es una falsa alarma, y el aviso
 *      le dice al usuario que revise algo que está bien.
 *   2. Para el caso que sí es un error —LINK a un id inexistente— el modelo
 *      falla visible por su propio camino, así que el guard no compra nada.
 *
 * Con CREATE no hay ambigüedad: "tiene que haber una fila nueva por cada
 * comando" es cierto siempre, y por eso el oráculo puede ser cuantitativo
 * (`after >= before + adds`) en vez del `after > before` que dejaba pasar
 * "pedí 3 líneas, entró 1".
 */
function x2manyFieldsExpectingGrowth(normalized, fieldDefs) {
    const defs = fieldDefs || {};
    const fields = [];
    for (const [name, value] of Object.entries(normalized)) {
        const type = defs[name]?.type;
        if ((type === "one2many" || type === "many2many") && Array.isArray(value)) {
            const adds = value.filter((c) => Array.isArray(c) && c[0] === 0).length;
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
    // `action` is needed to open the standard composer (doAction) from proposeChatter.
    dependencies: ["notification", "orm", "action"],
    start(env, { notification, orm, action }) {
        // Persist panel UI state across Ctrl+R, per-tab (sessionStorage is tab-scoped).
        // panelOpen / minimized / expanded survive reload; context and newChatRequest
        // are ephemeral (rebuilt from the current Odoo view on mount).
        const _SESSION_KEY = "tuqui_panel_state";
        let _savedState = {};
        try {
            _savedState = JSON.parse(sessionStorage.getItem(_SESSION_KEY) || "{}");
        } catch {
            _savedState = {};
        }

        // If another tab had the panel open and the user clicked an external link
        // from the chat (triggering "external-link-opening"), the panel wrote a
        // short-lived signal to localStorage. Consume it here: auto-open the panel
        // so the new tab resumes the conversation. The signal is cleared immediately
        // to avoid affecting unrelated tabs opened later.
        const _OPEN_SIGNAL_KEY = "tuqui_open_signal";
        const _OPEN_SIGNAL_TTL = 5000;
        let _openSignalFresh = false;
        try {
            const raw = localStorage.getItem(_OPEN_SIGNAL_KEY);
            if (raw) {
                localStorage.removeItem(_OPEN_SIGNAL_KEY);
                const signal = JSON.parse(raw);
                if (Date.now() - (signal.at || 0) < _OPEN_SIGNAL_TTL) {
                    _openSignalFresh = true;
                }
            }
        } catch {
            // Private browsing or malformed value — ignore.
        }

        // True only for the FIRST panel mount of this page load: when the session
        // was already active at reload (Ctrl+R with panel open) or when an external-
        // link signal triggered the open. Consumed on first call so a deliberate
        // close → reopen always starts a fresh chat instead of resuming.
        let _resumeOnFirstMount = Boolean(_savedState.panelOpen) || _openSignalFresh;
        function consumeResumeOnOpen() {
            const v = _resumeOnFirstMount;
            _resumeOnFirstMount = false;
            return v;
        }

        const state = reactive({
            panelOpen: Boolean(_savedState.panelOpen) || _openSignalFresh,
            minimized: Boolean(_savedState.minimized),
            expanded: Boolean(_savedState.expanded),
            context: null,
            // Nonce-counter that the systray increments to ask an already-open panel
            // to start a NEW chat without remounting the iframe (a remount would spend
            // a second SSO nonce → 401). The panel observes changes and posts
            // `new-chat` to the SPA. See openFreshChat / systray.
            newChatRequest: 0,
        });

        function _saveState() {
            try {
                sessionStorage.setItem(
                    _SESSION_KEY,
                    JSON.stringify({
                        panelOpen: state.panelOpen,
                        minimized: state.minimized,
                        expanded: state.expanded,
                    })
                );
            } catch {
                // Private browsing or quota exceeded — silently skip.
            }
        }

        // Active form's OWL record (record mode only). Intentionally non-reactive:
        // it is a model object, not UI state. `_owner` is whoever published the
        // current context (the controller), so clearContext doesn't stomp others.
        let activeRecord = null;
        let _owner = null;
        // Stack to restore parent context when a dialog closes. When a DIFFERENT
        // controller calls setRecordContext (e.g. mail.compose.message opened in a
        // dialog over a form), the current context is pushed here so clearContext
        // from the dialog restores it instead of clearing it.
        let _contextStack = [];

        // --- Contexto vivo: revisión + snapshots ---------------------------------
        // `_revision` es un contador monotónico del contexto de formulario. Sube
        // cuando cambia el record publicado y cuando los valores EN MEMORIA cambian
        // (on-blur, debounced desde el FormController). Viaja en el payload y vuelve
        // en la propuesta como `baseRevision`: con eso sabemos sobre qué valores
        // razonó el assistant y detectamos conflicto en vez de pisar en silencio.
        let _revision = 0;
        // Firma de los últimos valores publicados: evita re-publicar (y quemar una
        // revisión) cuando el usuario sale de un campo sin haber editado nada.
        let _lastFieldsSig = null;
        // Snapshots de lo publicado, por revisión. Ring chico: el SPA propone sobre
        // la última revisión o una muy cercana; guardar más es memoria al pedo.
        const _MAX_SNAPSHOTS = 10;
        const _snapshots = new Map();

        /** Firma JSON de los valores en memoria, o null si no se pudo serializar. */
        function _fieldsSignature(record) {
            if (!record) {
                return null;
            }
            try {
                return JSON.stringify(serializeRecordFields(record));
            } catch {
                return null;
            }
        }

        function _rememberSnapshot(revision, fields) {
            _snapshots.set(revision, fields);
            while (_snapshots.size > _MAX_SNAPSHOTS) {
                _snapshots.delete(_snapshots.keys().next().value);
            }
        }

        function _makeRecordContext(record) {
            return {
                kind: "record",
                model: record.resModel,
                resId: record.resId,
                displayName: record.data?.display_name || record.data?.name || "",
                dirty: Boolean(record.dirty),
                revision: _revision,
            };
        }

        function setRecordContext(owner, record) {
            // If there is already a DIFFERENT owner (e.g. a dialog opening over a
            // form), push the current context to the stack for later restoration.
            // During normal navigation the previous controller already called
            // clearContext before the new one mounted, so _owner is null here.
            if (_owner && _owner !== owner) {
                _contextStack.push({ owner: _owner, record: activeRecord, context: state.context });
            }
            _owner = owner;
            activeRecord = record || null;
            // Record nuevo → revisión nueva, y la firma arranca de sus valores
            // actuales (un focusout inmediato sin edición no debe re-publicar).
            _revision += 1;
            _lastFieldsSig = _fieldsSignature(activeRecord);
            state.context = record ? _makeRecordContext(record) : null;
        }

        /**
         * Re-publica el contexto del form si los valores EN MEMORIA cambiaron.
         * La llama el FormController al salir de un campo (`focusout`, debounced):
         * el assistant tiene que ver lo que el usuario está escribiendo sin guardar,
         * pero sin una revisión por tecla — eso infla el contexto de cada turno
         * (costo de LLM y ruido) sin agregar valor. Ver la spec
         * `assistant-en-odoo-confiabilidad` §Contexto vivo.
         *
         * No-op si no hay form abierto, si el llamador no es el dueño del contexto
         * (p.ej. el form de abajo mientras hay un diálogo arriba) o si nada cambió.
         *
         * @param {Object} [owner] controller que llama; si no es el dueño, no hace nada
         * @returns {boolean} true si publicó una revisión nueva
         */
        function refreshRecordContext(owner) {
            if (!activeRecord || (owner && _owner !== owner)) {
                return false;
            }
            const sig = _fieldsSignature(activeRecord);
            if (sig === null || sig === _lastFieldsSig) {
                return false;
            }
            _lastFieldsSig = sig;
            _revision += 1;
            state.context = _makeRecordContext(activeRecord);
            return true;
        }

        /**
         * Context for a list/kanban view. `payload`:
         *   { model, modelLabel, resIds, isDomainSelected, count, domain, filters }
         *
         * `modelLabel` es el nombre del modelo COMO LO VE el usuario (el
         * breadcrumb de la acción, ej. "Contactos") — el chip del SPA lo
         * muestra en lugar del nombre técnico `res.partner`. Display-only.
         *
         * La intención manda qué viaja: seleccionar (parcial o "todos los N")
         * es un gesto explícito y lleva el set (ids capeados, o domain cuando
         * allMatching); pararse en una lista sin seleccionar es solo ubicación
         * y lleva únicamente modelo + label.
         */
        function setSearchContext(owner, payload) {
            _owner = owner;
            activeRecord = null; // no form record in list/kanban context
            const resIds = payload.resIds || [];
            if (payload.isDomainSelected) {
                // "Select all N matching the domain": full filter, no explicit id list.
                // El dominio ES la selección acá: sin él, el assistant sabe
                // que "son todos los del filtro" pero no puede reproducir el set.
                state.context = {
                    kind: "selection",
                    model: payload.model,
                    modelLabel: payload.modelLabel || "",
                    count: payload.count,
                    resIds: [],
                    allMatching: true,
                    domain: payload.domain || [],
                };
            } else if (resIds.length) {
                // `count` es la selección REAL; `resIds` va capeado en
                // MAX_SELECTION_IDS. Mandar 500 ids infla el contexto de cada
                // turno para nada: por encima del límite el assistant no corre
                // el batch, deriva a automatización — y para decir eso le
                // alcanza con el count. Cuando la selección excede el cap, el
                // truncado es EXPLÍCITO (`truncated`) para que no crea que
                // tiene la lista entera.
                state.context = {
                    kind: "selection",
                    model: payload.model,
                    modelLabel: payload.modelLabel || "",
                    count: resIds.length,
                    resIds: resIds.slice(0, MAX_SELECTION_IDS),
                    truncated: resIds.length > MAX_SELECTION_IDS,
                };
            } else {
                // Sin selección NO hay intención de operar sobre el set: viaja
                // la ubicación ("estás parado en Facturas, filtrado por
                // Vencidas") — modelo, label y etiquetas legibles de los
                // filtros. Nada de count ni dominio máquina: para darle el set
                // al assistant está "seleccionar todo", gesto explícito.
                state.context = {
                    kind: "list",
                    model: payload.model,
                    modelLabel: payload.modelLabel || "",
                    filters: payload.filters || [],
                };
            }
        }

        function clearContext(owner) {
            if (!owner) {
                // No owner (legacy / manual call): clear everything.
                _owner = null;
                activeRecord = null;
                state.context = null;
                _contextStack = [];
                return;
            }
            // Remove from stack if this owner unmounted while a dialog was on top —
            // otherwise closing the dialog would restore a dead context.
            if (_contextStack.length) {
                _contextStack = _contextStack.filter((e) => e.owner !== owner);
            }
            if (_owner !== owner) {
                return; // not the current owner, nothing to do
            }
            // Restore parent context if any (e.g. the form below a dialog).
            const parent = _contextStack.pop();
            if (parent) {
                _owner = parent.owner;
                activeRecord = parent.record;
                // Volver al form de abajo es contexto nuevo para el SPA: revisión
                // nueva y firma re-sincronizada con lo que el record tiene AHORA
                // (el diálogo pudo haberlo cambiado).
                _revision += 1;
                _lastFieldsSig = _fieldsSignature(parent.record);
                // Re-build from the live record so dirty reflects current state.
                state.context = parent.record ? _makeRecordContext(parent.record) : parent.context;
            } else {
                _owner = null;
                activeRecord = null;
                state.context = null;
            }
        }

        function togglePanel() {
            state.panelOpen = !state.panelOpen;
            // On close/reopen from the systray, start with the card visible: minimized
            // state is per-open and must not survive a toggle.
            state.minimized = false;
            _saveState();
        }

        // Systray click (CTO item #4): ALWAYS opens the panel on a NEW chat
        // (closing is handled by the card's minimize/close buttons, no longer toggles).
        //   - Panel closed → opening mounts the iframe at `/embed/:slug`, which IS
        //     already a new chat: showing the card is enough (nothing posted; the
        //     iframe is not yet mounted/hydrated).
        //   - Panel already open (or minimized: iframe still mounted) → do NOT remount
        //     the iframe (would spend a second SSO nonce → 401): restore the card and
        //     increment newChatRequest so the panel posts `new-chat` to the SPA
        //     (internal navigation to a new chat).
        function openFreshChat() {
            const wasOpen = state.panelOpen;
            state.panelOpen = true;
            state.minimized = false;
            if (wasOpen) {
                state.newChatRequest += 1;
            }
            _saveState();
        }

        // Minimize to bubble / restore the card. Does NOT close the panel: the iframe
        // stays mounted (the card is hidden via CSS, not unmounted) — a remount would
        // spend a second single-use SSO nonce → 401. See panel.xml.
        function minimize() {
            state.minimized = true;
            _saveState();
        }
        function restore() {
            state.minimized = false;
            _saveState();
        }
        function expand() {
            state.expanded = true;
            _saveState();
        }
        function contract() {
            state.expanded = false;
            _saveState();
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
                const flat = JSON.parse(JSON.stringify(ctx));
                if (flat.kind === "record" && flat.fields) {
                    // Guardamos EXACTAMENTE lo que ve el SPA en esta revisión: es la
                    // base contra la que `applyProposal` mide el conflicto cuando la
                    // propuesta vuelve con su `baseRevision`.
                    _rememberSnapshot(flat.revision, flat.fields);
                }
                return flat;
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
        async function applyProposal(changes, options) {
            if (!activeRecord) {
                notification.add(
                    _t("Open a form (1 record) to apply changes from here."),
                    { type: "warning" }
                );
                return false;
            }
            if (!changes || typeof changes !== "object" || Array.isArray(changes)) {
                notification.add(_t("The proposal must be an object { field: value }."), {
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
                    _t("Skipped fields that cannot be edited in this form: %s", dropped.join(", ")),
                    { type: "warning" }
                );
            }
            // Conflicto de contexto vivo: la propuesta razonó sobre los valores de
            // `baseRevision`. Si el usuario cambió alguno de esos campos DESPUÉS, no
            // lo pisamos en silencio — lo sacamos de la propuesta y lo decimos, para
            // que vuelva a pedir sobre el valor actual. Sin `baseRevision` (SPA viejo)
            // o con la revisión ya fuera del ring, no hay contra qué comparar: se
            // aplica como antes.
            const baseRevision = options?.baseRevision;
            const stale = [];
            if (typeof baseRevision === "number" && _snapshots.has(baseRevision)) {
                const snapshot = _snapshots.get(baseRevision);
                const current = serializeRecordFields(activeRecord);
                for (const name of Object.keys(known)) {
                    if (!(name in snapshot)) {
                        continue; // no viajó en ese contexto: nada con qué comparar
                    }
                    if (JSON.stringify(current[name]) !== JSON.stringify(snapshot[name])) {
                        stale.push(name);
                    }
                }
            }
            for (const name of stale) {
                delete known[name];
            }
            if (stale.length) {
                notification.add(
                    _t(
                        "Not applied because you changed them after the proposal: %s. Ask again to work on the current value.",
                        stale.join(", ")
                    ),
                    { type: "warning" }
                );
                if (!Object.keys(known).length) {
                    // Todo lo propuesto quedó obsoleto. Ya dijimos por qué; el
                    // mensaje genérico de abajo ("ningún campo aplica al form")
                    // sería falso — los campos aplican, el valor cambió.
                    return false;
                }
            }
            if (!Object.keys(known).length) {
                notification.add(
                    _t("No field from the proposal can be applied to the open form."),
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
                } else if (type === "many2one" && typeof value === "number" && value > 0) {
                    // Wrap bare integer IDs for many2one fields so OWL resolves the
                    // display_name. Passing a raw integer leaves the field visually
                    // empty even though the ID is set.
                    updatePayload[name] = { id: value };
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
                notification.add(_t("Could not apply changes: %s", e.message || e), {
                    type: "danger",
                });
                // Re-publicar IGUAL, por la misma razón que abajo: los escalares se
                // aplican ANTES que las líneas nuevas, así que cuando revienta una
                // línea lo de arriba ya entró al formulario. Sin esto el contexto
                // queda en la revisión previa y la próxima propuesta sobre esa
                // misma baseRevision lee nuestro propio cambio como una edición del
                // usuario. Es el gemelo del camino de `notApplied` — y desde que el
                // guard mira sólo CREATE, el que de verdad se recorre.
                refreshRecordContext();
                return false;
            }
            // Chequeo honesto: ¿aparecieron TODAS las líneas nuevas que se pidieron?
            // Se compara contra `before + adds`, no contra `before`: con el `after >
            // before` de antes, pedir 3 líneas y que entre 1 pasaba como éxito.
            // Un faltante = la línea no se aplicó (forma inválida, vals que el
            // sub-modelo rechaza, etc.). Avisamos en vez del verde feliz.
            const notApplied = [];
            for (const { name, adds } of growthExpected) {
                const before = countsBefore[name];
                const after = x2manyCount(activeRecord, name);
                if (typeof before === "number" && typeof after === "number" && after < before + adds) {
                    notApplied.push(name);
                }
            }
            if (notApplied.length) {
                notification.add(
                    // Single string literal (no `+` concat) so Odoo's i18n
                    // extractor captures the full msgid. Same runtime output.
                    _t(
                        "Changes applied, but could not add line(s) for: %s. Check the form — a required field may be missing (e.g. the product). Do not assume the line was added.",
                        notApplied.join(", ")
                    ),
                    { type: "warning" }
                );
                // Re-publicar IGUAL antes de salir: el aviso dice "Changes
                // applied, but…", o sea que los escalares del mismo payload SÍ
                // entraron. Sin esto el contexto se queda en la revisión previa y
                // la próxima propuesta sobre esa misma baseRevision lee nuestro
                // propio cambio como una edición del usuario, y contesta que no
                // aplica por conflicto. Es exactamente lo que el comentario de
                // abajo dice que el re-publish evita — sólo que este camino se
                // iba antes de llegar ahí.
                refreshRecordContext();
                return false;
            }
            // Re-publicar: los valores que acabamos de aplicar son el nuevo estado
            // sobre el que el assistant tiene que razonar. Sin esto, una segunda
            // propuesta sobre la misma `baseRevision` leería su propio cambio como
            // conflicto del usuario.
            refreshRecordContext();
            notification.add(
                _t("Changes applied to the form (unsaved). Review and Save or Discard."),
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
        /**
         * Refresca lo que quedó viejo después de postear desde el composer.
         *
         * Hacen falta las DOS cosas, y con una sola no alcanza: `model.load()`
         * relee el registro pero NO el chatter, que es un componente aparte con
         * su propio store — verificado contra un Odoo real, el mensaje quedaba
         * en la base y el hilo seguía diciendo "The conversation is empty".
         * Odoo hace lo mismo en su composer nativo: refetch del thread y reload
         * de la vista de atrás (ver mail/static/src/chatter/web/chatter_patch.js,
         * onCloseFullComposerCallback).
         *
         * `mail.store` se toma acá y no como dependencia declarada porque este
         * addon no depende de `mail`: si no está instalado, no hay chatter que
         * refrescar y el reload de la vista alcanza.
         */
        async function refreshAfterChatterPost(model, resId) {
            try {
                const thread = env.services["mail.store"]?.Thread?.insert({ model, id: resId });
                await thread?.fetchNewMessages();
            } catch (e) {
                // El mensaje YA se posteó; no poder refrescar es cosmético.
                console.warn("[tuqui_assistant] Could not refresh the chatter:", e);
            }
            await reloadView();
        }

        async function proposeChatter({ mode, body, subject } = {}) {
            const ctx = state.context;
            if (!ctx || ctx.kind !== "record" || !ctx.model || !ctx.resId) {
                notification.add(
                    _t("Open a form (1 record) to post to the chatter."),
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
                // Without it, Odoo 19 marks `partner_ids` required while the
                // recipients block stays hidden in a note: posting fails with no
                // visible culprit. The native chatter passes it for the same reason.
                clicked_on_full_composer: true,
            };
            if (typeof subject === "string" && subject.trim()) {
                composerContext.default_subject = subject;
            }
            try {
                await action.doAction(
                    {
                        type: "ir.actions.act_window",
                        name: isMessage ? _t("Send message") : _t("Log note"),
                        res_model: "mail.compose.message",
                        view_mode: "form",
                        views: [[false, "form"]],
                        target: "new",
                        context: composerContext,
                    },
                    {
                        // El composer es un DIÁLOGO aparte: postea y se cierra, y el
                        // chatter de atrás no se entera — el mensaje sólo aparece si
                        // se recarga la página.
                        //
                        // Odoo avisa en `args` si el usuario NO posteó: {dismiss:true}
                        // al cerrar con la X o Escape, {special:true} con Discard.
                        // Mismo criterio que su composer nativo.
                        onClose: (args) => {
                            if (args?.dismiss || args?.special) {
                                return;
                            }
                            refreshAfterChatterPost(ctx.model, ctx.resId);
                        },
                    }
                );
            } catch (e) {
                notification.add(
                    _t("Could not open the chatter composer: %s", e.message || e),
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
         * Recarga los datos de la vista abierta (form o lista) sin recargar la
         * página. La pide el SPA después de un turno que escribió en Odoo por
         * atrás (`odoo_write`, `odoo_create`, `message_post`…): el dato ya cambió
         * en la base pero la vista sigue mostrando lo viejo, y el usuario lee al
         * assistant decir "listo" arriba de datos que no se movieron.
         *
         * REGLA DURA: si el registro está sucio NO recargamos. Recargar tira los
         * cambios sin guardar — perder lo que alguien estaba escribiendo es mucho
         * peor que mostrar un dato viejo un rato. Ahí avisamos y decide él.
         *
         * @returns {Promise<boolean>} true si efectivamente recargó
         */
        /**
         * Guarda el formulario abierto — el mismo Guardar que apretaría el usuario.
         * Lo pide el SPA cuando el usuario dice "guardá" (tool `save_odoo_form`).
         *
         * NO es un atajo para que el assistant confirme sus propias propuestas: el
         * propose-then-apply existe para que el usuario revise antes de que algo se
         * escriba, y guardar por él le saca justo eso. La tool del backend se lo
         * dice al modelo; acá guardamos igual porque quien decide es el usuario.
         *
         * La validación es de Odoo: campos requeridos, constraints y reglas de
         * acceso siguen aplicando, y un guardado rechazado se ve en Odoo.
         *
         * @returns {Promise<boolean>} true si guardó
         */
        async function saveRecord() {
            if (!activeRecord) {
                notification.add(_t("Open a form (1 record) to save it from here."), {
                    type: "warning",
                });
                return false;
            }
            if (!activeRecord.dirty) {
                notification.add(_t("Nothing to save — the form has no pending changes."), {
                    type: "info",
                });
                return false;
            }
            let saved = false;
            try {
                saved = await activeRecord.save();
            } catch (e) {
                notification.add(_t("Could not save: %s", e.message || e), { type: "danger" });
                return false;
            }
            if (!saved) {
                // Odoo ya avisó: el único camino por el que `save()` devuelve false
                // es `_checkValidity({ displayNotification: true })`, que muestra
                // "Missing required fields" y marca los campos en rojo. Un toast
                // nuestro encima decía lo mismo, más vago y tapando el panel.
                // Devolvemos false igual: es la señal interna, no un aviso.
                return false;
            }
            // Guardado: el registro dejó de estar sucio y los computados cambiaron.
            refreshRecordContext();
            notification.add(_t("Form saved."), { type: "success" });
            return true;
        }

        async function reloadView() {
            const viewModel = _owner?.model;
            if (!viewModel) {
                return false; // sin vista publicada (home, settings…): nada que recargar
            }
            if (activeRecord?.dirty) {
                notification.add(
                    _t(
                        "Data changed in Odoo, but the form has unsaved changes so it was not reloaded. Save or discard to see the new values."
                    ),
                    { type: "warning" }
                );
                return false;
            }
            try {
                // `model.load()` es el camino del propio Odoo para releer una vista;
                // en las que no lo exponen, el root del modelo sí sabe recargarse.
                if (typeof viewModel.load === "function") {
                    await viewModel.load();
                } else if (typeof viewModel.root?.load === "function") {
                    await viewModel.root.load();
                } else {
                    return false;
                }
            } catch (e) {
                // Un reload fallido no es un error del usuario: el dato está en la
                // base y se ve al refrescar a mano. Consola, sin toast.
                console.warn("[tuqui_assistant] Could not reload the view:", e);
                return false;
            }
            // `model.load()` REEMPLAZA el root del modelo: el record que teníamos
            // publicado queda DESCONECTADO, con los valores de antes del reload.
            // Si no lo re-apuntamos, el contexto sigue mostrando lo viejo y —peor—
            // la próxima propuesta se aplica sobre un datapoint que ya no está
            // renderizado: nada cambia en pantalla y `applyProposal` igual devuelve
            // true. Ese "ok silencioso" es justo lo que esta feature no puede
            // introducir. En modo lista no hay record publicado y no se toca.
            if (activeRecord && viewModel.root) {
                activeRecord = viewModel.root;
            }
            // Re-publicar: lo que se acaba de releer es el contexto nuevo sobre el
            // que el assistant tiene que razonar en el próximo turno.
            refreshRecordContext();
            return true;
        }

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
                    _t("Cannot navigate: missing Odoo model to open."),
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
                        _t("Could not open the view in Odoo: %s", e.message || e),
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
                    _t("Could not open the new form in Odoo: %s", e.message || e),
                    { type: "danger" }
                );
                return false;
            }
            return true;
        }

        return {
            state,
            setRecordContext,
            refreshRecordContext,
            setSearchContext,
            clearContext,
            togglePanel,
            openFreshChat,
            minimize,
            restore,
            expand,
            contract,
            applyProposal,
            proposeChatter,
            navigate,
            reloadView,
            saveRecord,
            getEmbedBootstrap,
            getSsoAuth,
            getContextPayload,
            getActiveRecord: () => activeRecord,
            consumeResumeOnOpen,
        };
    },
};

registry.category("services").add("tuquiAssistant", tuquiAssistantService);

// Helpers puros de normalización x2many, exportados para test en aislamiento
// (sin montar el servicio OWL). Ver static/tests/proposal_normalize.test.js.
export {
    normalizeX2manyValue,
    normalizeProposalX2many,
    x2manyFieldsExpectingGrowth,
    isX2manyCommandTuple,
    coerceRelationalCommandVals,
    coerceX2manyCommandRelations,
    splitX2manyCreates,
};
