/** @odoo-module **/
import { registry } from "@web/core/registry";
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import {
    deserializeDate,
    deserializeDateTime,
    serializeDate,
    serializeDateTime,
} from "@web/core/l10n/dates";
import { evaluateBooleanExpr } from "@web/core/py_js/py";
import { user } from "@web/core/user";
import { OPEN_SIGNAL_KEY, PANEL_STATE_KEY } from "@tuqui_assistant/storage_keys";
import { isNested } from "@tuqui_assistant/nested_guard";
import { whenTheScreenSettles } from "@tuqui_assistant/screen_settled";

import { makeSpotlight } from "./spotlight";

// Luxon es un global en Odoo, no un import ESM — igual que en
// web/static/src/core/l10n/dates.js.
const { DateTime } = luxon;

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

/** Tope de botones en el contexto: alcanza para cualquier formulario real. */
const TOPE_DE_BOTONES = 25;

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
 * Los campos que el ARCH deja fuera de la pantalla, leídos ocurrencia por
 * ocurrencia.
 *
 * POR QUÉ HACE FALTA, Y ES EL BORDE QUE MÁS ENGAÑA. `activeFields[campo].invisible`
 * trae SÓLO el modificador del propio nodo `<field>`. El `invisible` de un
 * `<group>`, una `<page>` o un `<div>` que lo contiene se compila a un `t-if` del
 * template y **nunca toca `activeFields`** — así que el evaluador de Odoo dice
 * que el campo se ve, el valor viaja, y el campo no está en el DOM. Como el
 * contrato de este mapa es "un campo ausente es normal", el modelo lee *visible*
 * sobre algo que la persona no tiene delante: exactamente la falla que este
 * contexto existe para evitar. Medido sobre 324 modelos: **20,19% de los campos
 * top-level** están en esa situación.
 *
 * EL ORDEN DE LA CUENTA ES EL QUE IMPORTA, y es la parte que se puede hacer mal
 * sin que se note: por CADA ocurrencia se pregunta si la esconde su propio
 * modificador **o** alguno de sus ancestros, y recién después se combinan las
 * ocurrencias entre sí. Calcular los dos ejes por separado —el AND de los
 * propios por un lado, el AND de los ancestros por el otro, y un OR al final—
 * deja pasar el patrón más común de Odoo: el mismo campo puesto dos veces, una
 * como `<field invisible="1"/>` técnico y otra adentro de un contenedor
 * condicional. Ahí ninguna de las dos se dibuja y ninguno de los dos ejes lo
 * marca. En los formularios del core de la 19 hay 20 campos así — `qr_code` en
 * los pagos, tres de gastos, tres de configuración del PdV, `pricelist_id` en
 * ventas — y en todos la mentira sale justo del lado peligroso.
 *
 * `field:not(field field)` es el mismo idioma que usa Odoo (`form_controller.js`,
 * el barrido de `footer`) para saltear lo que vive dentro de otro `<field>`: un
 * x2many embebido trae su propia vista, y sus campos no son de este formulario.
 *
 * Sólo `invisible`: un `readonly` o un `required` puestos en un ancestro **no se
 * propagan ni al render**, así que para esos dos el mapa ya estaba bien.
 *
 * @param {Object} xmlDoc  el arch del formulario (`archInfo.xmlDoc`)
 * @param {Object} record  el record OWL, para evaluar la condición
 * @returns {Set<string>}
 */
function camposOcultosSegunElArch(xmlDoc, record) {
    const ocultos = new Set();
    if (!xmlDoc?.querySelectorAll || !record) {
        return ocultos;
    }
    const ctx = record.evalContextWithVirtualIds || {};
    /** Por campo: cuántas veces aparece en el arch y cuántas están escondidas. */
    const cuenta = new Map();
    for (const nodo of xmlDoc.querySelectorAll("field:not(field field)")) {
        const nombre = nodo.getAttribute?.("name");
        if (!nombre) {
            continue;
        }
        // Arranca en el PROPIO nodo, no en su padre: para esta ocurrencia da lo
        // mismo que la esconda su modificador o el del `<group>` de arriba.
        let escondida = false;
        let el = nodo;
        while (el && !escondida) {
            const expr = el.getAttribute?.("invisible");
            try {
                if (expr && evaluateBooleanExpr(expr, ctx)) {
                    escondida = true;
                }
            } catch {
                // Una condición que no se puede evaluar no esconde nada: el
                // default es dejar pasar, como antes de este chequeo.
            }
            el = el.parentElement;
        }
        const previo = cuenta.get(nombre) || { veces: 0, escondidas: 0 };
        cuenta.set(nombre, {
            veces: previo.veces + 1,
            escondidas: previo.escondidas + (escondida ? 1 : 0),
        });
    }
    // UN CAMPO QUE APARECE DOS VECES ESTÁ OCULTO SÓLO SI LAS DOS ESTÁN OCULTAS,
    // y esto no es un detalle: el patrón de dos ramas mutuamente excluyentes
    // —el mismo campo en dos `<div>` con condiciones opuestas— es común en los
    // formularios de Odoo. Marcarlo con que UNA esté escondida daba el campo por
    // invisible cuando la persona lo tiene delante en la otra rama, y de paso
    // hacía descartar una propuesta legítima.
    //
    // Es la misma regla que usa Odoo al fusionar dos nodos del mismo campo:
    // `patchActiveFields` combina el `invisible` con AND (utils.js:174).
    for (const [nombre, { veces, escondidas }] of cuenta) {
        if (veces > 0 && escondidas === veces) {
            ocultos.add(nombre);
        }
    }
    return ocultos;
}

/**
 * El motivo REAL de un error del cliente web, desenvuelto.
 *
 * Odoo envuelve lo que falla dentro de un render de Owl en un error propio cuyo
 * `message` es *"An error occured in the owl lifecycle (see this Error's cause
 * property)"* — literalmente un cartel que le dice a la persona que mire una
 * propiedad de un objeto de JavaScript. Se vio en un Odoo real: el agente quiso
 * abrir la configuración del sitio web en una base sin ese módulo, y en vez de
 * "el modelo no existe" salió esa frase.
 *
 * El motivo está en la cadena de `cause`, así que se baja hasta el último que
 * tenga mensaje. Tope de saltos por si alguna vez viene circular.
 *
 * @param {unknown} e  lo que se atrapó
 * @returns {string} el mensaje más específico que haya
 */
export function motivoDeVerdad(e) {
    let actual = e;
    let mensaje = "";
    for (let i = 0; i < 5 && actual; i++) {
        const propio = typeof actual === "string" ? actual : actual?.message;
        if (propio && !/owl lifecycle/i.test(propio)) {
            mensaje = propio;
        }
        actual = actual?.cause;
    }
    if (mensaje) {
        return mensaje;
    }
    return typeof e === "string" ? e : e?.message || String(e);
}

/**
 * Por qué no se pudo abrir una vista, en algo que una persona pueda leer.
 *
 * Desenvolver el error de Owl deja de mandar a mirar una propiedad de
 * JavaScript, pero lo que queda abajo tampoco explica nada: para un modelo que
 * no existe, Odoo contesta `404: Not Found`. Así que en el camino de FALLA —y
 * sólo ahí, que es lo que hace que la consulta extra no cueste nada en el camino
 * bueno— se le pregunta a Odoo si ese modelo existe, y si no existe se dice eso,
 * que es la mitad de la respuesta que la persona necesita.
 *
 * Medido: le pidieron la configuración del sitio web en una base sin ese módulo.
 *
 * @param {unknown} e  lo que se atrapó
 * @param {string} model  el modelo que se quiso abrir
 * @param {object} orm  el servicio `orm`
 * @returns {Promise<string>}
 */
export async function porQueNoSePudoAbrir(e, model, orm) {
    const motivo = motivoDeVerdad(e);
    // `404` y `Unknown model` son las dos formas en que Odoo cuenta lo mismo, y
    // ninguna de las dos nombra el modelo.
    if (model && /(404|not found|unknown model)/i.test(motivo)) {
        try {
            const existe = await orm.searchCount("ir.model", [["model", "=", model]]);
            if (!existe) {
                return _t(
                    "the model '%s' does not exist in this database — that app is probably not installed",
                    model
                );
            }
        } catch {
            // Si ni eso se puede consultar, se devuelve lo que había: peor un
            // motivo pobre que ninguno.
        }
    }
    return motivo;
}

/**
 * Which sections the Settings screen is actually offering, by their technical key.
 *
 * WHY IT IS READ FROM THE SCREEN and not from the list of installed modules: they
 * are not the same list, and the difference is exactly where this goes wrong.
 * Measured on a real database — `sale` is installed, and there is no `sale`
 * section; the one that exists is `sale_management`. Asking for the wrong key is
 * not an error in Odoo: Settings opens with NO section selected, which reads as
 * "I took you there" on both sides while the person stares at a screen that
 * answers nothing.
 *
 * `data-key` is the settings tab's own attribute, the same one it uses to decide
 * which block to show, so this reads the authority instead of a copy of it.
 *
 * @returns {string[]} empty when this is not the Settings screen
 */
/**
 * Did asking for that settings section actually get the user anywhere?
 *
 * WHY THIS IS A DECISION AND NOT A CHECK. Odoo does not fail when you ask it for
 * a settings section that does not exist: it opens Settings with none selected.
 * So the dispatch succeeds, "I took you there" reads true on both sides, and the
 * person is looking at a screen that answers nothing. Measured on a real
 * certificate renewal, where the missing step was switching company.
 *
 * AN EMPTY LIST IS NOT A DENIAL. Not knowing which sections the screen offers —
 * it has not drawn yet, or this is not the settings screen — is not evidence
 * that the requested one is missing, and turning it into one would refuse
 * navigations that worked. Doubt stays optimistic here; what the answer owes is
 * the truth about what IS known.
 *
 * @param {string|undefined} pedida the section that was asked for
 * @param {string[]} hay the sections the screen actually offers
 * @returns {{ok: boolean, reason: string, detail: string}}
 */
export function didItLandOnTheSection(pedida, hay) {
    const nombre = pedida ? String(pedida) : "general_settings";
    if (pedida && hay.length && !hay.includes(nombre)) {
        return { ok: false, reason: "no_such_section", detail: hay.join(", ") };
    }
    return { ok: true, reason: "opened", detail: nombre };
}

export function settingsSectionsOnScreen(root = document) {
    const claves = [...(root.querySelectorAll?.(".settings_tab [data-key]") || [])]
        .map((e) => e.dataset?.key)
        .filter(Boolean);
    return [...new Set(claves)];
}

/**
 * Which settings section is OPEN right now, by its technical key.
 *
 * WHY THE OPEN ONE AND NOT JUST THE LIST. Settings is one screen with many
 * sections, and the page context identifies it by model — `res.config.settings`
 * — for every one of them. Someone standing on the general section and someone
 * standing on accounting look identical from here, so an assistant that needs
 * the accounting one reads "you are already on the settings screen" and does not
 * navigate. Measured over ten conversations with the same opening message: it
 * went to the right section 3 times, and the 7 that failed all started on the
 * settings screen already.
 *
 * @returns {string|null} null when this is not the Settings screen
 */
export function openSettingsSection(root = document) {
    const abierta = root.querySelector?.(".settings_tab [data-key].selected");
    return abierta?.dataset?.key || null;
}

/**
 * What the person READS where the mark landed, to contrast it with what was asked.
 *
 * WHY IT IS NOT JUST THE NODE'S TEXT. What gets marked is very often a control
 * with no text of its own — a checkbox, an input, an icon — and the words the
 * person reads live in its LABEL, which is a sibling and not an ancestor.
 * Measured live on the settings screen: marking the "Storage Locations" checkbox
 * answered `marked_on: null`, so the whole check for a mark that landed on the
 * wrong thing was inert exactly where it matters most.
 *
 * SO THE LABEL IS ASKED FOR THE WAY THE BROWSER ASKS: `aria-label`, then the
 * `<label for=...>` that points at this control, then `title`/`placeholder` —
 * the same links a screen reader follows, which is the same question ("what is
 * this control called?") and already solved. Only when none of those exist does
 * it climb, and then it climbs until it finds text instead of counting three
 * levels: the depth between a checkbox and its row is a detail of Odoo's markup
 * and changes between views, so counting it was guessing.
 */
export function textoDeLoMarcado(el) {
    if (!el) {
        return null;
    }
    const limpio = (t) => (t || "").trim().replace(/\s+/g, " ").slice(0, 80) || null;
    const propio = limpio(el.textContent);
    if (propio) {
        return propio;
    }
    const porAria = limpio(el.getAttribute?.("aria-label"));
    if (porAria) {
        return porAria;
    }
    if (el.id) {
        // `CSS.escape` porque los ids de Odoo llevan puntos y dos puntos
        // (`group_stock_multi_locations_0`, pero también nombres calificados), y
        // sin escapar el selector revienta y se pierde la etiqueta que SÍ estaba.
        const etiqueta = el.ownerDocument?.querySelector?.(`label[for="${CSS.escape(el.id)}"]`);
        const porLabel = limpio(etiqueta?.textContent);
        if (porLabel) {
            return porLabel;
        }
    }
    const porAtributo = limpio(el.getAttribute?.("title") || el.getAttribute?.("placeholder"));
    if (porAtributo) {
        return porAtributo;
    }
    // Recién acá se sube, y hasta encontrar texto: el contenedor con las palabras
    // puede estar a dos niveles en un formulario y a cinco en una casilla de
    // ajustes. Se frena en el formulario para no devolver la pantalla entera.
    let nodo = el.parentElement;
    while (nodo && !nodo.matches?.(".o_form_view, .o_list_view, body")) {
        const t = limpio(nodo.textContent);
        if (t) {
            return t;
        }
        nodo = nodo.parentElement;
    }
    return null;
}

/**
 * Which company the person is looking at, and which others they could switch to.
 *
 * WHY IT TRAVELS WITH EVERY SCREEN: half of Odoo's configuration is
 * company-dependent, so the SAME url shows different blocks depending on the
 * active company. Without this, the assistant describes a section that is not
 * on their screen and has no way of telling why. Measured on a certificate
 * renewal: the active company was a US one, the Argentinian localization only
 * renders for an Argentinian company, and the whole conversation went looking
 * for a button that could not be there. The person said it outright — "you are
 * sending me somewhere that does not exist" — and it still could not be used,
 * because nothing on this side said which company was active.
 *
 * THE OTHERS ARE THE HALF THAT DISCRIMINATES. Knowing only the active company
 * says the screen is incomplete; knowing another one is available says what to
 * do about it. Capped because this object is serialized and CUT at 8000
 * characters upstream: a base with fifty companies would eat the rest of the
 * context, and the point is to name the alternative, not to list a directory.
 */
export function companyInPlay() {
    const activa = user?.activeCompany;
    if (!activa?.name) {
        return null;
    }
    const info = { active: activa.name };
    const permitidas = Object.values(user?.allowedCompanies || {}).filter((c) => c?.name);
    const otras = permitidas.filter((c) => c.id !== activa.id).map((c) => c.name);
    if (otras.length) {
        info.canSwitchTo = otras.slice(0, 8);
        if (otras.length > 8) {
            info.canSwitchToTotal = otras.length;
        }
    }
    return info;
}

/**
 * Los botones que la persona puede clickear en la pantalla abierta.
 *
 * POR QUÉ VIAJAN. Sin esto el modelo tiene que ADIVINAR cómo se llama un botón, y
 * adivina mal de dos formas que se midieron contra un Odoo real: inventando uno
 * que no existe —pidió marcar "Action" en un proyecto que tiene "Share Project" a
 * la vista— y traduciendo el texto —"Log internal note" contra un botón que dice
 * "Log note"—. Las dos terminan igual: el addon no encuentra nada, y el agente le
 * dice a la persona "te lo resalté" sobre una pantalla donde no hay nada.
 *
 * EL ALCANCE ES EL MISMO CON EL QUE SE BUSCAN, y eso no es un detalle: mandar
 * botones de afuera ofrecería lo que después no se puede señalar. La marca busca
 * dentro de `.o_form_view` cuando hay un formulario y en todo el documento
 * cuando no (ver `pantalla` en `spotlight.js`), y esto sigue esa misma línea: el
 * formulario, y si no hay, la BARRA DE CONTROL. Por eso entran también los
 * botones del chatter, que viven adentro del formulario y son de los más
 * pedidos.
 *
 * Y POR QUÉ LA BARRA DE CONTROL, no la lista entera: una lista abierta tiene un
 * botón por fila, y ninguno de esos es algo que alguien pida por su nombre — son
 * de un registro, no de la pantalla. Los de la pantalla están arriba. Sin esto,
 * una lista o un kanban viajaban SIN botones, y el modelo volvía a lo que hacía
 * antes de que esta lista existiera: adivinar. Medido con la lista de pedidos
 * abierta en el panel — pidió marcar "Nuevo" contra un botón que dice "New".
 *
 * LA LISTA PUEDE SER PARCIAL —hay un tope, y se filtra lo que no tiene nombre
 * que una persona pueda decir (un contador, una fecha)—, así que sirve para
 * ELEGIR y no para negar: nada se rechaza por no estar acá.
 *
 * @returns {Array<{text: string, name?: string}>}
 */
function botonesEnPantalla() {
    const vista = document.querySelector(".o_form_view") || document.querySelector(".o_control_panel");
    if (!vista) {
        return [];
    }
    const salida = [];
    const vistos = new Set();
    for (const boton of vista.querySelectorAll("button, a.btn")) {
        const caja = boton.getBoundingClientRect();
        if (!caja.width || !caja.height) {
            continue;
        }
        const texto = (boton.innerText || "").replace(/\s+/g, " ").trim().slice(0, 40);
        // Un botón que sólo dice un número o una fecha —un contador, el selector
        // de un rango— no es algo que alguien pida por su nombre.
        if (!texto || !/[a-zá-úñ]/i.test(texto)) {
            continue;
        }
        const name = boton.getAttribute("name") || undefined;
        const clave = `${texto}|${name || ""}`;
        if (vistos.has(clave)) {
            continue;
        }
        vistos.add(clave);
        salida.push(name ? { text: texto, name } : { text: texto });
        if (salida.length >= TOPE_DE_BOTONES) {
            break;
        }
    }
    return salida;
}

/**
 * ¿Se puede confiar en el estado que reporta este record?
 *
 * Los tres evaluadores se llaman con `?.()`, y eso degrada en silencio HACIA EL
 * LADO PELIGROSO: si una versión de Odoo renombra sólo `_isInvisible`, el mapa no
 * queda vacío — queda sin ningún `invisible`, que es indistinguible de "todos los
 * campos se ven". Por eso se chequea una vez y se dice, en vez de inferirlo del
 * vacío del otro lado.
 */
function evaluadoresDisponibles(record) {
    return (
        typeof record?._isInvisible === "function" &&
        typeof record?._isReadonly === "function" &&
        typeof record?._isRequired === "function"
    );
}

/**
 * El ESTADO de los campos del formulario abierto: cuáles se ven, cuáles se
 * pueden escribir, cuáles son obligatorios.
 *
 * POR QUÉ HACE FALTA. Hasta acá el asistente recibía los VALORES de los campos y
 * nada de su estado, así que sabía qué dice cada campo pero no cuáles están en la
 * pantalla. De ahí salieron tres fallas distintas que son la misma: marcó un
 * campo que la configuración de ese registro tenía oculto; dijo "te lo señalé"
 * sobre una marca que no podía caer; y propuso escribir en campos de sólo
 * lectura, porque el único chequeo posible era la definición ESTÁTICA del campo y
 * la mitad de los readonly de Odoo son condicionales.
 *
 * Odoo ya resuelve las tres preguntas contra el registro concreto —con sus
 * condiciones evaluadas— y lo único que faltaba era transportarlo.
 *
 * SÓLO VIAJAN LAS EXCEPCIONES. Un formulario tiene decenas de campos y casi todos
 * son visibles, editables y opcionales; mandar tres booleanos por cada uno
 * engordaría cada mensaje para no decir nada. Un campo ausente de este mapa es un
 * campo normal.
 *
 * @param {Object} record  el record OWL del formulario abierto
 * @returns {Object} `{campo: {invisible?, readonly?, required?}}`, sólo con lo anómalo
 */
function serializeFieldState(record, ocultosSegunElArch = new Set()) {
    const activos = record?.activeFields || {};
    const out = {};
    for (const name of Object.keys(activos)) {
        try {
            const estado = {};
            // Métodos internos de Odoo (`_isInvisible` y compañía). Se usan igual
            // porque son los que EVALÚAN la condición contra este registro, que es
            // justamente el dato que no se puede reconstruir desde afuera. Cada
            // campo va en su propio try: si una versión los renombra, se pierde el
            // estado de los campos y no el contexto entero.
            // El OR con lo que dice el arch es lo que hace honesto a este mapa:
            // el evaluador de Odoo no ve los ancestros, y al fusionar dos nodos
            // del mismo campo pierde de vista que las dos ocurrencias podían
            // estar escondidas por motivos distintos.
            if (record._isInvisible?.(name) || ocultosSegunElArch.has(name)) {
                estado.invisible = true;
            }
            if (record._isReadonly?.(name)) {
                estado.readonly = true;
            }
            if (record._isRequired?.(name)) {
                estado.required = true;
            }
            if (Object.keys(estado).length) {
                out[name] = estado;
            }
        } catch {
            // Un campo que no se puede evaluar no tira abajo el resto.
        }
    }
    return out;
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
 * Parsea a Luxon el valor propuesto para un campo `date` / `datetime`.
 *
 * Por qué hace falta: el modelo OWL guarda las fechas como objetos Luxon
 * — `parseServerValue` (web/static/src/model/relational_model/utils.js) hace
 * literalmente `value ? deserializeDate(value) : false` — y el widget las
 * renderiza con `value.toFormat()`. Un string crudo metido por `record._update`
 * NO revienta ahí: revienta después, al renderizar, con `value.toFormat is not
 * a function`, un OwlError que se lleva puesto el formulario entero. Es el mismo
 * agujero que ya estaba tapado para los m2o (id pelado → `{id}`), en el campo de
 * al lado: coercionábamos las relaciones y no las fechas.
 *
 * Aceptamos las DOS formas que llegan en la práctica:
 *   - formato servidor de Odoo ("2026-09-30", "2026-09-30 14:30:00"), que es lo
 *     que manda un modelo que conoce Odoo y lo único que parsean los helpers;
 *   - ISO 8601 con "T" y offset ("2026-09-30T14:30:00+02:00"), que es lo que el
 *     modelo VE: nuestro propio serializador de salida emite `toISO()` para los
 *     datetime, así que lo natural es que lo devuelva en esa forma.
 *
 * No unificamos cambiando el formato de SALIDA: el SPA y el modelo están fuera
 * de nuestro control y pueden mandar cualquiera de las dos igual. Ser tolerante
 * acá cubre los dos casos; cambiar la salida no cubre ninguno.
 *
 * @param {*} value valor propuesto
 * @param {"date"|"datetime"} type tipo del campo
 * @returns {DateTime|false|null} el objeto Luxon; `false` para limpiar el campo;
 *   `null` si no se pudo parsear — el llamador descarta el campo y lo dice, en
 *   vez de meter un DateTime inválido que el form pinta como "Invalid DateTime".
 */
function coerceDateValue(value, type) {
    // Limpiar el campo es legítimo, y viaja como false/null/"".
    if (value === false || value === null || value === undefined) {
        return false;
    }
    // Ya es un DateTime: no llega así por postMessage, pero sí desde un test.
    if (typeof value?.toFormat === "function") {
        return value.isValid ? value : null;
    }
    if (typeof value !== "string") {
        return null;
    }
    const raw = value.trim();
    if (!raw) {
        return false;
    }
    // Exigir una fecha completa adelante. Luxon es MÁS permisivo de lo que sirve
    // acá: `fromSQL("14:30")` devuelve HOY a las 14:30, y `"2026"` se lee como la
    // hora 20:26 de hoy. Sin este guard, "poné la reunión a las 14:30" entra como
    // un valor plausible y equivocado — que es peor que rebotar, porque el aviso
    // de "no pude leer la fecha" nunca aparece y el usuario ve un dato creíble.
    if (!/^\d{4}-\d{2}-\d{2}/.test(raw)) {
        return null;
    }
    if (type === "date") {
        // Un campo date no tiene hora ni zona. Si viene un datetime nos quedamos
        // con la fecha TAL COMO ESTÁ ESCRITA, sin convertir: pasar por la zona
        // local puede correrla un día (las 23:00Z, en UTC-3, son el día anterior).
        const parsed = deserializeDate(raw.split(/[T ]/)[0]);
        return parsed.isValid ? parsed : null;
    }
    const fromServer = deserializeDateTime(raw);
    if (fromServer.isValid) {
        return fromServer;
    }
    // ISO con "T". Un string sin offset se interpreta en UTC, la misma convención
    // que `deserializeDateTime` (el servidor manda UTC); uno con offset ya trae
    // el instante y Luxon lo respeta.
    const fromIso = DateTime.fromISO(raw, { zone: "utc" });
    return fromIso.isValid ? fromIso.setZone("default") : null;
}

/**
 * Coerce los valores dentro de las `vals` de un comando CREATE/UPDATE de un
 * x2many a la forma que el record OWL del sub-modelo entiende. Dos tipos:
 *
 * **Relaciones.** `StaticList._applyCommands` (case CREATE) crea el datapoint de
 * la línea con `new Record(..., command[2])`, y `parseServerValue` para un
 * many2one acepta `[id, name]` o `{id, display_name}` pero un **entero pelado lo
 * devuelve tal cual** (no lo reconoce como m2o seteado). Un LLM manda
 * `{"product_id": 1}` (id pelado) → la línea queda SIN producto y el onchange ve
 * un producto vacío (no calcula name/price). Convertimos cada valor de un sub-
 * campo many2one/many2one_reference de `<int>` a `{id: <int>}` para que el m2o
 * quede seteado y el onchange del padre resuelva el resto (name, precio, totales).
 *
 * **Fechas.** Una línea nueva con fecha (`{"x_fecha_entrega": "2026-09-30"}`) se
 * aplica con `line._update(campo)` y cae en el MISMO agujero que los escalares
 * del record padre: string crudo → `value.toFormat is not a function` al
 * renderizar. Ver `coerceDateValue`.
 *
 * `subFields` son las defs de campo del SUB-modelo (las del StaticList del x2many).
 * Sin ellas (no resolubles) devolvemos las vals sin tocar (best-effort, no rompe).
 *
 * `badKeys` (opcional) recolecta los sub-campos fecha que no se pudieron parsear;
 * se descartan de las vals en vez de meter un DateTime inválido en la línea. Se
 * pasa por parámetro para que la función siga siendo pura (sin notificaciones ni
 * dependencias de OWL) y testeable en aislamiento.
 */
function coerceCommandVals(vals, subFields, badKeys) {
    if (!vals || typeof vals !== "object" || Array.isArray(vals) || !subFields) {
        return vals;
    }
    const out = {};
    for (const [k, v] of Object.entries(vals)) {
        const t = subFields[k]?.type;
        if ((t === "many2one" || t === "many2one_reference") && typeof v === "number") {
            out[k] = { id: v };
        } else if (t === "date" || t === "datetime") {
            // A FORMATO SERVIDOR, no a Luxon. Las vals de un comando las vuelve a
            // parsear Odoo: el case UPDATE de `_applyCommands` hace
            // `record._parseServerValues(changes)` y el CREATE por `_update` pasa
            // por `_setData` → lo mismo. `parseServerValue` llama a
            // `deserializeDate`, o sea `DateTime.fromSQL(...)`, y con un objeto
            // Luxon adentro eso da `Invalid DateTime` — sin tirar: la celda queda
            // en "Invalid DateTime" y al guardar se manda ese literal al server.
            // Normalizamos igual (parse + re-serialize) porque el modelo puede
            // mandar ISO con "T", que `deserializeDate` tampoco parsea.
            const parsed = coerceDateValue(v, t);
            if (parsed === null) {
                badKeys?.push(k);
            } else if (parsed === false) {
                out[k] = false;
            } else {
                out[k] = t === "date" ? serializeDate(parsed) : serializeDateTime(parsed);
            }
        } else {
            out[k] = v;
        }
    }
    return out;
}

/**
 * Aplica coerceCommandVals a cada comando CREATE(0)/UPDATE(1) de un valor x2many
 * ya normalizado a tuplas-comando. Deja intactos LINK/DELETE/UNLINK (no llevan
 * vals que coercer) y cualquier forma que no sea tupla.
 */
function coerceX2manyCommandVals(commands, subFields, badKeys) {
    if (!Array.isArray(commands) || !subFields) {
        return commands;
    }
    return commands.map((cmd) => {
        if (Array.isArray(cmd) && (cmd[0] === 0 || cmd[0] === 1) && cmd[2] && typeof cmd[2] === "object") {
            return [cmd[0], cmd[1], coerceCommandVals(cmd[2], subFields, badKeys)];
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

/**
 * ¿Nos están mostrando adentro de otra página?
 *
 * Leer `window.top` de otro origen tira SecurityError, y ese error ES la
 * respuesta: si no lo podemos ni mirar, es de otro. Por eso el catch devuelve
 * `true` en vez de tragarse el error.
 */
export const tuquiAssistantService = {
    // `action` is needed to open the standard composer (doAction) from proposeChatter.
    dependencies: ["notification", "orm", "action", "overlay"],
    start(env, { notification, orm, action, overlay }) {
        // Odoo mostrado adentro del panel de Tuqui NO abre su propia conversación
        // —ya está del otro lado de la pantalla, y abrirla colgaba el navegador
        // entero: Tuqui muestra Odoo, ese Odoo abre su Tuqui, y así. Pero lo que
        // no se monta es LA CONVERSACIÓN, no el servicio.
        //
        // Antes se devolvía un servicio inerte, y con él se apagaba también la
        // capa de guía: la marca, el contexto de la pantalla y las acciones que
        // el chat de afuera pide sobre ESTA pantalla — que es justo donde más
        // falta hacen. `nested_guard` ya corta el bucle donde corresponde (el
        // panel no se restaura solo, el botón del systray no se ve, y la señal
        // compartida no se consume acá: ver `isNested()` abajo), y lo hace con
        // sus propios tests. Dos remedios para lo mismo, y el viejo apagaba de
        // más; queda el fino.
        // Persist panel UI state across Ctrl+R, per-tab (sessionStorage is tab-scoped).
        // panelOpen / minimized / expanded survive reload; context and newChatRequest
        // are ephemeral (rebuilt from the current Odoo view on mount).
        const _SESSION_KEY = PANEL_STATE_KEY;
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
        const _OPEN_SIGNAL_KEY = OPEN_SIGNAL_KEY;
        const _OPEN_SIGNAL_TTL = 5000;
        let _openSignalFresh = false;
        try {
            // NOT consumed when nested. The signal lives in `localStorage`, so it
            // is shared with every other tab of this Odoo — and an embedded
            // frame loading inside the 5s window would eat the one a legitimate
            // top-level tab was about to use, leaving that tab without the panel
            // it was promised. Skipping the read here leaves the signal for its
            // real addressee; the guard no longer deletes it for the same reason.
            const raw = isNested() ? null : localStorage.getItem(_OPEN_SIGNAL_KEY);
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
                // La compañía va TAMBIÉN acá, y no es un detalle: la pantalla de
                // ajustes cae en esta rama, y es la pantalla donde la compañía
                // activa decide qué bloques existen.
                const sinPantalla = { kind: "none" };
                const cualEmpresa = companyInPlay();
                if (cualEmpresa) {
                    sinPantalla.company = cualEmpresa;
                }
                if (ultimaMarca) {
                    sinPantalla.lastMark = { ...ultimaMarca };
                }
                return sinPantalla;
            }
            const ctx = { ...state.context };
            // Los BOTONES que la persona tiene delante, en CUALQUIER pantalla y no
            // sólo en un formulario: una lista y un kanban también tienen botones
            // que alguien pide por su nombre, y sin ellos el modelo los adivina.
            //
            // VA PRIMERO por el mismo motivo que `fieldState` va antes de los
            // valores: el consumidor CORTA este objeto en 8000 caracteres, así que
            // la última clave es la primera en desaparecer — y sería justo lo que
            // no se puede reconstruir del otro lado. Pesa poco.
            // ANTES QUE TODO LO DEMÁS, por el mismo motivo de truncado que se
            // explica abajo: pesa dos renglones y es lo único de acá que del otro
            // lado no se puede reconstruir ni preguntando.
            const cualEmpresa = companyInPlay();
            if (cualEmpresa) {
                ctx.company = cualEmpresa;
            }
            ctx.buttons = botonesEnPantalla();
            // Y en la pantalla de Ajustes, QUÉ SECCIONES HAY. Es el mismo problema
            // que resolvían los botones: sin la lista, el nombre de la sección se
            // adivina, y se adivina mal de la forma más creíble posible (`sale`
            // por `sale_management`). Fuera de Ajustes queda vacío y no viaja.
            const secciones = settingsSectionsOnScreen();
            if (secciones.length) {
                // CUÁL ESTÁ ABIERTA va primero: sin eso, todas las secciones de
                // ajustes se ven iguales desde afuera —el modelo es el mismo— y
                // quien necesite otra cree que ya llegó.
                ctx.settingsSection = openSettingsSection();
                ctx.settingsSections = secciones;
            }
            // Y si hay una marca puesta, si la persona ya la usó. Viaja acá y no
            // por un canal propio porque no hace falta que llegue EN EL MOMENTO:
            // sólo importa cuando el chat va a hablar, y eso siempre pasa después
            // de un mensaje. Yendo con el contexto sirve a las dos direcciones
            // del embebido sin una línea extra en ninguna.
            if (ultimaMarca) {
                ctx.lastMark = { ...ultimaMarca };
            }
            if (ctx.kind === "record" && activeRecord) {
                ctx.dirty = Boolean(activeRecord.dirty);
                // Qué se ve, qué se puede escribir y qué es obligatorio. Sin esto
                // el asistente conoce los valores pero no la pantalla.
                //
                // VA ANTES DE `fields`, Y ES A PROPÓSITO. El consumidor serializa
                // este objeto entero y lo CORTA en 8000 caracteres (`prompts.py`,
                // `payload_truncated`). En un account.move o un sale.order los
                // valores solos se acercan a ese tope, así que la última clave es
                // la primera en desaparecer — y sería justo el estado, que es lo
                // único que no se puede reconstruir del otro lado. Puesto antes,
                // lo que se pierde es la cola de los VALORES, igual que antes de
                // que este mapa existiera. Ocupa poco: sólo viajan las
                // excepciones.
                const ocultos = camposOcultosSegunElArch(_owner?.archInfo?.xmlDoc, activeRecord);
                ctx.fieldState = serializeFieldState(activeRecord, ocultos);
                // Sólo viaja la excepción: si el mapa NO es confiable se dice, y
                // del otro lado el prompt deja de leer "ausente" como "normal".
                if (!evaluadoresDisponibles(activeRecord)) {
                    ctx.fieldStateUnavailable = true;
                }
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
                return { ok: false, reason: "no_form" };
            }
            if (!changes || typeof changes !== "object" || Array.isArray(changes)) {
                notification.add(_t("The proposal must be an object { field: value }."), {
                    type: "danger",
                });
                return { ok: false, reason: "bad_proposal" };
            }
            // Validar contra los campos del form: descartar inexistentes / readonly
            // antes de _update. Una propuesta con campos ajenos (p.ej. {email_from,
            // subject} sobre un res.partner) reventaba _update con un error JS sin
            // atrapar; ahora se ignoran y, si no queda nada válido, se avisa y corta.
            const fieldDefs = activeRecord.fields || {};
            const enPantalla = activeRecord.activeFields || {};
            // Un campo que el arch deja fuera de la pantalla está igual en
            // `activeFields`: escribirlo deja un cambio que la persona no puede
            // ver ni revisar antes de guardar, igual que un campo de otra vista.
            const ocultosSegunElArch = camposOcultosSegunElArch(
                _owner?.archInfo?.xmlDoc,
                activeRecord
            );
            const known = {};
            const dropped = [];
            for (const [name, value] of Object.entries(changes)) {
                // Primero que el campo EXISTA Y ESTÉ EN ESTE FORMULARIO. Son dos
                // conjuntos distintos: `fields` es el fields_get de TODAS las
                // vistas de la acción —lista y buscador incluidos— así que trae
                // campos que este form no muestra. Escribir uno de esos deja un
                // cambio que la persona no puede ver ni revisar antes de guardar,
                // que es exactamente lo que este chequeo existe para evitar.
                //
                // Y es lo mismo que hacía reventar al evaluador de abajo: lee
                // `activeFields[campo]` sin chequear que exista, tiraba TypeError,
                // el catch lo tapaba y el chequeo caía al readonly ESTÁTICO justo
                // en los campos que no están en pantalla.
                // El `invisible` PROPIO del campo cuenta igual que el del grupo
                // que lo contiene: en los dos casos la persona no lo tiene
                // delante, y escribirlo deja un cambio que no puede revisar. El
                // arch resuelve los dos; se le pregunta igual al evaluador de
                // Odoo, que es el único que ve lo que no salió del arch de este
                // formulario (el `invisible` que hereda un x2many, por ejemplo).
                let fueraDeLaVista = ocultosSegunElArch.has(name);
                try {
                    fueraDeLaVista = fueraDeLaVista || Boolean(activeRecord._isInvisible?.(name));
                } catch {
                    // Un campo que no se puede evaluar no se descarta por eso.
                }
                if (!fieldDefs[name] || !(name in enPantalla) || fueraDeLaVista) {
                    dropped.push(name);
                    continue;
                }
                // El readonly se pregunta EVALUADO contra este registro, no en la
                // definición del campo. La mitad de los readonly de Odoo son
                // condicionales —"sólo lectura si el pedido está confirmado"— y la
                // definición estática los da como editables: la propuesta pasaba el
                // filtro y `_update` la aplicaba sobre un campo que la persona no
                // puede tocar. Se cae al chequeo estático si el método no está.
                let soloLectura = fieldDefs[name].readonly === true;
                try {
                    if (activeRecord._isReadonly) {
                        soloLectura = activeRecord._isReadonly(name);
                    }
                } catch {
                    // Queda el chequeo estático.
                }
                if (soloLectura) {
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
                    return {
                        ok: false,
                        reason: "stale",
                        detail: "The user changed those fields after the proposal was reasoned about.",
                    };
                }
            }
            if (!Object.keys(known).length) {
                notification.add(
                    _t("No field from the proposal can be applied to the open form."),
                    { type: "warning" }
                );
                return {
                    ok: false,
                    reason: "no_applicable_field",
                    detail: "None of the proposed fields exist on the open form, or all are readonly.",
                };
            }
            // Normalizar x2many a tuplas-comando web: el LLM suele mandar la forma
            // amigable `[{product_id:1,…}]` (lista de objetos planos), pero
            // `record._update` → `StaticList._applyCommands` SOLO entiende
            // `[[0,false,{…}]]` (CREATE). Un objeto plano es un NO-OP silencioso
            // (no matchea ningún case, sin `default`) → la línea nunca se agrega y
            // `_update` igual resuelve "ok". Toleramos AMBAS formas. Ver helpers arriba.
            const normalized = normalizeProposalX2many(known, fieldDefs);
            // Coerce las vals de cada comando CREATE/UPDATE: los m2o pelados (id
            // entero) a `{id}` — si no, la línea nueva queda SIN el m2o (p.ej. sin
            // producto) y el onchange no calcula name/precio/totales — y las fechas
            // a Luxon, que si no revientan el render igual que en el record padre.
            // Las defs del sub-modelo salen del StaticList vivo del x2many (`.fields`).
            const unparsedLineDates = [];
            for (const [name, value] of Object.entries(normalized)) {
                const type = fieldDefs[name]?.type;
                if (type === "one2many" || type === "many2many") {
                    const subFields = activeRecord.data?.[name]?.fields;
                    normalized[name] = coerceX2manyCommandVals(value, subFields, unparsedLineDates);
                }
            }
            if (unparsedLineDates.length) {
                notification.add(
                    _t(
                        "Could not read the date for these line fields: %s. The lines go in without them.",
                        [...new Set(unparsedLineDates)].join(", ")
                    ),
                    { type: "warning" }
                );
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
            const unparsedDates = [];
            for (const [name, value] of Object.entries(normalized)) {
                const type = fieldDefs[name]?.type;
                if (type === "one2many" || type === "many2many") {
                    const { creates, rest } = splitX2manyCreates(value);
                    // Una línea cuyas vals quedaron VACÍAS tras descartar lo
                    // ilegible no es una línea: agregarla mete una fila en blanco
                    // en la grilla y encima el guard de crecimiento la da por
                    // buena. Si no quedó nada que poner, no hay línea que crear.
                    const usable = creates.filter((v) => v && Object.keys(v).length);
                    if (usable.length) {
                        createsByField[name] = usable;
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
                } else if (type === "date" || type === "datetime") {
                    // El modelo OWL guarda fechas como Luxon; un string crudo revienta
                    // el render del form entero. Ver `coerceDateValue`.
                    const parsed = coerceDateValue(value, type);
                    if (parsed === null) {
                        unparsedDates.push(name);
                    } else {
                        updatePayload[name] = parsed;
                    }
                } else {
                    updatePayload[name] = value;
                }
            }
            if (unparsedDates.length) {
                notification.add(
                    _t(
                        "Could not read the date for: %s. Ask again with a date like 2026-09-30.",
                        unparsedDates.join(", ")
                    ),
                    { type: "warning" }
                );
            }
            if (!Object.keys(updatePayload).length && !Object.keys(createsByField).length) {
                if (!unparsedDates.length && !unparsedLineDates.length) {
                    // Sin fechas ilegibles no dijimos NADA todavía, y salir mudo
                    // es peor que el verde falso que había antes: el usuario pidió
                    // algo y no pasa nada en pantalla.
                    notification.add(
                        _t("No field from the proposal can be applied to the open form."),
                        { type: "warning" }
                    );
                }
                // Todo lo que quedaba eran fechas ilegibles. Ya dijimos cuáles; cortamos
                // acá para no llegar a `_update` con un payload vacío y cantar un
                // "aplicado" verde. No hace falta re-publicar el contexto: a diferencia
                // del camino de `notApplied`, acá NO se escribió nada en el record.
                return {
                    ok: false,
                    reason: "nothing_applicable",
                    detail: "Every remaining value was a date Odoo could not read.",
                };
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
                            // Acá SÍ va Luxon: `_update` → `_applyChanges` asigna
                            // crudo, sin pasar por `parseServerValue`. Es el único
                            // camino de los tres que NO re-parsea, y por eso la
                            // conversión vive acá y no en `coerceCommandVals`.
                            const t = list.fields?.[k]?.type;
                            const value = t === "date" || t === "datetime" ? coerceDateValue(v, t) : v;
                            if (value === null) {
                                continue; // ya reportada al normalizar
                            }
                            await line._update({ [k]: value });
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
                return {
                    ok: false,
                    reason: "partial",
                    detail:
                        "A new line failed, but the scalar fields of the same proposal DID go in: " +
                        "the form is half-changed. Read it before saying anything.",
                };
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
                return {
                    ok: false,
                    reason: "partial",
                    detail:
                        "Some requested lines did not appear, while the rest of the proposal did: " +
                        "the form is half-changed. Read it before saying anything.",
                };
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
            return { ok: true, reason: "applied" };
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
                return { ok: false, reason: "no_record" };
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
                return { ok: false, reason: "error", detail: String(e && e.message ? e.message : e).slice(0, 200) };
            }
            // `composer_open` Y NO "posted": el compositor quedó abierto y escrito,
            // y nada se publica hasta que la persona le dé Enviar. Esa distinción
            // es estructural acá —que lo dispare un humano— así que el nombre del
            // resultado tiene que sostenerla y no dejar que se lea como publicado.
            return { ok: true, reason: "composer_open" };
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
        /**
         * Guardar el formulario abierto, y decir QUÉ PASÓ.
         *
         * POR QUÉ DEVUELVE UN MOTIVO Y NO UN BOOLEANO. Estas cuatro cosas son
         * respuestas distintas para quien está guiando a alguien, y las cuatro
         * colapsaban en `false`: no hay formulario, no hay nada para guardar,
         * Odoo rechazó por campos obligatorios, y algo falló. Sin el motivo, el
         * chat sólo podía decir "pedí que se guarde" — y lo que decía después era
         * una suposición.
         *
         * Medido cuatro veces, en cuatro implementaciones distintas: el asistente
         * afirmó que algo estaba guardado cuando no lo estaba. En la última la
         * pantalla mostraba "Missing required fields" con dos campos obligatorios
         * vacíos, y el chat decía "ya deberías ver el botón Save marcado". El dato
         * estaba acá, de este lado del vidrio, y se lo comunicábamos sólo a la
         * persona con un cartel.
         */
        async function saveRecord() {
            if (!activeRecord) {
                notification.add(_t("Open a form (1 record) to save it from here."), {
                    type: "warning",
                });
                return { ok: false, reason: "no_form" };
            }
            if (!activeRecord.dirty) {
                notification.add(_t("Nothing to save — the form has no pending changes."), {
                    type: "info",
                });
                return { ok: false, reason: "nothing_to_save" };
            }
            let saved = false;
            try {
                saved = await activeRecord.save();
            } catch (e) {
                const detail = String(e?.message || e || "").slice(0, 300);
                notification.add(_t("Could not save: %s", detail), { type: "danger" });
                return { ok: false, reason: "error", detail };
            }
            if (!saved) {
                // Odoo ya avisó a la persona: el único camino por el que `save()`
                // devuelve false es `_checkValidity({ displayNotification: true })`,
                // que muestra "Missing required fields" y marca los campos en rojo.
                // Un toast nuestro encima decía lo mismo, más vago y tapando el
                // panel. Lo que faltaba no era otro cartel: era que el motivo
                // volviera al chat.
                return {
                    ok: false,
                    reason: "rejected",
                    detail: "Odoo refused the save: required fields are missing or a value is invalid.",
                };
            }
            // Guardado: el registro dejó de estar sucio y los computados cambiaron.
            refreshRecordContext();
            notification.add(_t("Form saved."), { type: "success" });
            return { ok: true, reason: "saved" };
        }

        /**
         * La gota: señalar en la pantalla dónde hay que hacer algo, con el
         * puntero de `web_tour` (el que la gente ya conoce del onboarding).
         *
         * Devuelve si se pudo señalar. Ese dato NO vuelve al chat —el panel lo
         * usa para avisarle a la persona con un cartel (`_spotlight` en
         * `panel.js`)— y es una decisión, no una omisión: quien está mirando la
         * pantalla es el único que puede moverse a la vista correcta, y el agente
         * ya sabe de antemano dónde está parado, porque el contexto de la página
         * viaja en cada mensaje. Lo que hay que evitar es que la persona se quede
         * buscando una marca que nunca apareció.
         */
        /**
         * Lo último que se señaló, y si la persona ya lo hizo.
         *
         * ES UN DATO, NO UN DIARIO. Alcanza con la última marca porque es la
         * única pregunta que el chat necesita contestada cuando le vuelven a
         * hablar: ¿hizo lo que le pedí? Guardar una lista de todo lo que pasó
         * sería más caro, más difícil de recortar, y respondería preguntas que
         * nadie hace.
         *
         * Se limpia solo: cada marca nueva reemplaza a la anterior.
         */
        let ultimaMarca = null;

        const spotlightHandle = makeSpotlight(overlay, {
            onStepDone: () => {
                if (ultimaMarca) {
                    ultimaMarca.done = true;
                }
            },
            // Cuando la marca se apaga, el dato se va con ella: describir una
            // marca que no está en pantalla es peor que no decir nada, porque
            // quien lee —la persona o el chat— actúa sobre algo que no existe.
            onApagada: () => {
                ultimaMarca = null;
            },
            // La marca es de UN registro. Si la persona se va a otro, el campo se
            // llama igual y la gota lo señalaría con confianza en el lugar
            // equivocado; con esto se apaga sola.
            recordKey: () =>
                activeRecord ? `${activeRecord.resModel}:${activeRecord.resId}` : null,
        });
        const spotlight = async (payload) => {
            // A QUIÉN VA A REEMPLAZAR, leído ANTES de poner la nueva. Sólo puede
            // haber una marca a la vez —cada una apaga a la anterior— y desde el
            // chat eso no se ve: un turno puede pedir dos y las dos contestan que
            // cayeron, así que el asistente anuncia las dos con toda la razón
            // desde su lado. Medido: pidió la casilla "Storage Locations" y
            // después "Save", dijo «ahí te marqué la casilla y el botón Save», y
            // la persona contestó «me está señalando el botón Save, NO la
            // casilla». Tenía razón ella.
            const replacedMark = ultimaMarca && !ultimaMarca.done ? ultimaMarca.what : null;
            const puesta = await spotlightHandle.spotlight(payload);
            if (puesta) {
                // Con el texto que se pidió señalar, no con un identificador: es
                // lo que el chat va a poder nombrar al hablar de eso.
                const que = String(payload?.action || payload?.label || payload?.field || "").slice(0, 60);
                // Y SOBRE QUÉ QUEDÓ, que no siempre es lo que se pidió: el
                // resolver acierta por texto incluido. Medido en calibración:
                // pedir el campo `phone` dejó la marca sobre los botones
                // "Call/SMS" de al lado. Viaja en el contexto del turno siguiente,
                // así que la próxima vez que la persona hable el chat puede ver el
                // desvío en vez de sostener que marcó lo que pidió.
                const sobre = textoDeLoMarcado(puesta);
                ultimaMarca = que ? { what: que, on: sobre || null, done: false } : null;
                // Colgado del elemento y no devuelto aparte, por lo mismo que el
                // texto de lo marcado: el dato tiene que venir de ESTA operación y
                // no de una segunda lectura que ya ve otro DOM.
                puesta.tuquiReplacedMark = replacedMark;
            }
            return puesta;
        };

        /**
         * Poner la gota, y si no cae, decírselo a QUIEN PUEDE HACER ALGO.
         *
         * El aviso va a la persona y no de vuelta al agente, por lo que dice el
         * comentario de arriba: quien mira la pantalla es el único que puede
         * moverse a la vista correcta.
         *
         * Vive acá y no en quien recibe el pedido porque los que lo reciben ya
         * son DOS, y van a ser los mismos dos para cada cosa que se maneje desde
         * el chat: el panel, cuando Tuqui está adentro de Odoo, y el puente
         * anidado, cuando Odoo está adentro de Tuqui. La marca es la misma y el
         * aviso también; lo único que cambia es por dónde entró el pedido.
         */
        /**
         * Señalar, y decir SI CAYÓ.
         *
         * El resultado ya se conocía acá y no volvía al chat: se le avisaba a la
         * persona con un cartel y nada más. Por eso el asistente podía decir "ya
         * te marqué el botón" cuatro veces seguidas con la pantalla inmóvil, que
         * fue la queja textual de quien lo probó: «me estás diciendo que hiciste
         * algo que no hiciste». Y lo más filoso de su relato: sabe leer la
         * pantalla para desmentir a la persona cuando ella miente, pero no la
         * leía para desmentirse a sí mismo al anunciar una marca.
         */
        async function spotlightOrWarn(payload) {
            // El try NO es defensa por si acaso: un throw acá se volvía un
            // unhandled rejection y la persona se quedaba sin marca Y sin aviso —
            // que es exactamente lo que esto existe para evitar. Un error
            // señalando es indistinguible, para quien mira, de una marca que no
            // cayó.
            let marcado = null;
            let fallo = null;
            try {
                // Es `await` porque la marca puede tener que abrir una pestaña
                // del formulario para llegar al campo, y eso pasa por un render.
                marcado = await spotlight(payload);
            } catch (error) {
                fallo = String(error?.message || error || "").slice(0, 200);
                console.warn("tuqui_assistant: falló al señalar en la pantalla", error);
            }
            if (marcado) {
                // SOBRE QUÉ CAYÓ, y no sólo que cayó. El resolver busca por texto
                // incluido, así que puede acertar un elemento que NO es el que se
                // pidió: medido, se pidió "Certificates" y la marca terminó sobre
                // "Invite New Users" mientras el chat decía "ahí te marqué los
                // certificados". Quien lo vivió lo dijo así: «yo confío en la marca
                // verde, si me la pone, aprieto» — y apretar ahí invitaba usuarios
                // a la empresa. Devolver el texto de lo marcado es lo que permite
                // notar el desvío en vez de anunciarlo como un acierto.
                // El elemento se le pide al MISMO resolver que puso la marca, en vez
                // de cambiar lo que devuelve `spotlight`: así no se toca un
                // contrato que veinte tests ya fijan, y la respuesta sale de la
                // misma búsqueda, no de una reconstrucción parecida.
                return {
                    ok: true,
                    reason: "marked",
                    markedText: textoDeLoMarcado(marcado),
                    // A quién apagó al aparecer, cuando había otra viva. Sin esto,
                    // dos marcas en un turno se cuentan las dos como puestas.
                    replaced: marcado.tuquiReplacedMark || null,
                };
            }
            // `String()` porque el payload lo escribe el modelo: un objeto ahí
            // imprimía "[object Object]" en el cartel que lee la persona.
            const que = String(payload.label || payload.field || payload.action || "");
            notification.add(
                que
                    ? _t('Tuqui quiso señalarte "%s", pero no está en esta pantalla.', que)
                    : _t("Tuqui quiso señalarte algo, pero no está en esta pantalla."),
                { type: "warning" }
            );
            return {
                ok: false,
                reason: fallo ? "error" : "not_on_screen",
                detail: fallo || (que ? `"${que}" is not on the screen the user has open.` : null),
            };
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
        async function navigate({ model, mode, viewType, domain, defaults, title, resId, section } = {}) {
            if (mode === "settings") {
                // Los AJUSTES no se abren como los demás: `res.config.settings` es
                // un modelo transitorio, no tiene filas, y por eso el trío
                // record/new/browse no llega nunca — pedir "el registro" exige un
                // id que no existe y buscarlo devuelve cero para siempre. Se abre
                // por su acción, que es la única puerta que tiene.
                //
                // Y LA SECCIÓN VA EN EL CONTEXTO, que es el mecanismo propio de
                // Odoo: `SettingsFormController` lee `context.module` para elegir
                // qué app mostrar abierta. Con eso "llevame a los ajustes de
                // contabilidad" es un nombre de módulo y no una pantalla nueva por
                // cada caso.
                //
                // Sin esto, media implementación quedaba fuera de alcance: casi
                // toda configuración de Odoo vive en esta pantalla. Medido en una
                // renovación de certificado de AFIP, donde el asistente intentó
                // llegar, no pudo, y terminó mandando a la persona a otra parte.
                //
                // SE DESPACHA EL act_window A MANO y no por el xmlid de Ajustes
                // generales: esa acción trae `{'module': 'general_settings'}` en su
                // propio contexto, y el contexto de la acción PISA al que uno le
                // agrega — así que por ese camino todo terminaba en la sección
                // general, cualquiera fuese la pedida (medido: `account` y `sale`
                // daban las dos "General Settings"). Cada app de Odoo declara su
                // acción de ajustes con exactamente esta forma y lo único que
                // cambia entre ellas es ese `module`; acá se arma la misma con el
                // que se pidió, que es lo que la vuelve una sola puerta para todas.
                try {
                    await action.doAction({
                        type: "ir.actions.act_window",
                        name: title || _t("Settings"),
                        res_model: "res.config.settings",
                        views: [[false, "form"]],
                        target: "current",
                        context: { module: section ? String(section) : "general_settings", bin_size: false },
                    });
                } catch (e) {
                    notification.add(
                        _t("Could not open Odoo settings: %s", await porQueNoSePudoAbrir(e, "res.config.settings", orm)),
                        { type: "danger" }
                    );
                    return { ok: false, reason: "error", detail: String(e && e.message ? e.message : e).slice(0, 200) };
                }
                // Y SE COMPRUEBA QUE LA SECCIÓN EXISTA, porque pedir una que no
                // está no es un error para Odoo: abre Ajustes sin ninguna sección
                // elegida. Eso se lee como "te llevé" de los dos lados mientras la
                // persona mira una pantalla que no contesta nada. La lista real
                // sale de la pantalla y no de los módulos instalados: no son la
                // misma lista — `sale` está instalado y la sección se llama
                // `sale_management`.
                //
                // Y EL RESULTADO LO DECIDE ESA COMPROBACIÓN, no el despacho. Odoo
                // no falla al pedirle una sección que no existe: abre Ajustes sin
                // ninguna elegida, así que el `doAction` sale bien y decir "te
                // llevé" ahí es la mentira más fina de todas — la persona mira una
                // pantalla que no contesta nada. Por eso se espera a que la
                // pantalla deje de moverse antes de contestar.
                const hay = section
                    ? await new Promise((resolver) => {
                          whenTheScreenSettles(() => resolver(settingsSectionsOnScreen()));
                      })
                    : [];
                const llegada = didItLandOnTheSection(section, hay);
                if (!llegada.ok) {
                    notification.add(
                        _t(
                            "Opened Odoo settings, but there is no '%(pedida)s' section. Available: %(hay)s.",
                            { pedida: String(section), hay: hay.join(", ") }
                        ),
                        { type: "warning" }
                    );
                }
                return llegada;
            }
            if (typeof model !== "string" || !model.trim()) {
                notification.add(
                    _t("Cannot navigate: missing Odoo model to open."),
                    { type: "danger" }
                );
                return { ok: false, reason: "no_model" };
            }
            if (mode === "record") {
                // Abrir el formulario de UN registro que ya existe.
                //
                // Es el modo que le faltaba al trío, y sin él no se puede guiar a
                // nadie sobre un campo: los campos viven en el formulario, no en la
                // lista, así que "andá al diario y te marco el punto de venta"
                // terminaba mostrando una lista donde ese campo no existe y una
                // marca que no tenía dónde caer.
                const id = Number(resId);
                if (!Number.isInteger(id) || id <= 0) {
                    notification.add(
                        _t("Cannot open that record: missing or invalid id."),
                        { type: "danger" }
                    );
                    return { ok: false, reason: "bad_id" };
                }
                try {
                    await action.doAction({
                        type: "ir.actions.act_window",
                        name: title || model,
                        res_model: model,
                        res_id: id,
                        views: [[false, "form"]],
                        target: "current",
                        view_id: false,
                    });
                } catch (e) {
                    // Odoo rechaza por sus propios permisos o porque el registro no
                    // existe. Se lo decimos a la persona: un form que no se abre y
                    // no explica por qué es indistinguible de una pantalla colgada.
                    notification.add(
                        _t("Could not open that record in Odoo: %s", await porQueNoSePudoAbrir(e, model, orm)),
                        { type: "danger" }
                    );
                    return { ok: false, reason: "error", detail: String(e && e.message ? e.message : e).slice(0, 200) };
                }
                return { ok: true, reason: "opened", detail: model };
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
                        _t("Could not open the view in Odoo: %s", await porQueNoSePudoAbrir(e, model, orm)),
                        { type: "danger" }
                    );
                    return { ok: false, reason: "error", detail: String(e && e.message ? e.message : e).slice(0, 200) };
                }
                return { ok: true, reason: "opened", detail: model };
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
                    _t("Could not open the new form in Odoo: %s", await porQueNoSePudoAbrir(e, model, orm)),
                    { type: "danger" }
                );
                return { ok: false, reason: "error", detail: String(e && e.message ? e.message : e).slice(0, 200) };
            }
            return { ok: true, reason: "opened", detail: model };
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
            spotlight,
            spotlightOrWarn,
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
    coerceDateValue,
    coerceCommandVals,
    coerceX2manyCommandVals,
    splitX2manyCreates,
};
