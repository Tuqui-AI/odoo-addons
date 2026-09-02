/** @odoo-module **/
import { MacroMutationObserver } from "@web/core/macro";
import { debounce } from "@web/core/utils/timing";
import { createPointerState } from "@web_tour/js/tour_pointer/tour_pointer_state";
import { getScrollParent } from "@web_tour/js/utils/tour_utils";
import { Gota } from "@tuqui_assistant/spotlight/gota";

/**
 * La gota: señalar en la pantalla de Odoo el lugar donde hay que hacer algo.
 *
 * POR QUÉ EXISTE. Un asistente que sabe la respuesta y la escribe en el chat deja
 * al usuario traduciendo prosa a pantalla: "el punto de venta va en el diario de
 * ventas" obliga a buscar dónde está el diario, cuál de todos, y qué campo. La
 * gota cierra esa distancia — marca el lugar exacto y la persona hace el resto.
 * El reparto es deliberado: **la gota dice DÓNDE, la conversación dice QUÉ**.
 *
 * NO HAY MOTOR DE SECUENCIA, y ahí está la decisión de fondo. Un tour espera que
 * completes cada paso para avanzar, y el caso real es el contrario: alguien carga
 * el dato en el diario equivocado y sigue de largo. Ahí un tour se queda esperando
 * un trigger que nunca llega. La secuencia la lleva la conversación, una gota por
 * vez, y que el paso salió mal lo detecta el agente verificando por RPC.
 *
 * PERO SÍ HAY UN LOOP MIENTRAS LA MARCA VIVE, y esto se aprendió a los golpes. El
 * puntero de Odoo está diseñado para vivir dentro de un observador de mutaciones
 * que en cada cambio del DOM re-busca el anchor y re-apunta; todo su auto-arreglo
 * cuelga de ahí. Apuntar UNA vez y soltarlo dejaba cuatro fallas que son la misma:
 * al scrollear la gota quedaba clavada (el puntero se auto-bloquea cuando está
 * cerrado y el anchor quieto, y sólo se libera si alguien vuelve a disparar el
 * cálculo), tras un re-render quedaba flotando sobre la pantalla nueva, un campo
 * abajo del fold se marcaba fuera de pantalla **declarando éxito**, y de la
 * segunda marca en adelante la clasificación era la anterior. Se reusa el
 * observador de Odoo —vive en `@web/core/macro`, no en el módulo de tours, así
 * que no arrastra el motor— más un oído en el contenedor scrolleable, que es
 * exactamente lo que hace `tour_interactive` por cada paso.
 *
 * LA MARCA ES LA DE ODOO, EN VERDE. La forma, el posicionamiento y la animación
 * son las del puntero de `web_tour` — la gente ya las conoce del onboarding y no
 * hay nada ahí que mejorar. El color sí es nuestro: cuando alguien te interviene
 * la pantalla, es lo que dice QUIÉN fue. Ver `spotlight/gota.xml` para los dos
 * cambios sobre el original y por qué.
 *
 * SE VE COMO UNA GOTA, NO COMO UN CARTEL. Queda cerrada: un punto que dice "acá".
 * El texto se despliega al acercarse al campo o a la marca — un globo abierto
 * permanente taparía los campos vecinos justo cuando la persona necesita verlos
 * para ubicarse.
 *
 * QUÉ ES PORTABLE Y QUÉ NO. El contrato —a qué se apunta— es nuestro y no menciona
 * Odoo. El dibujo es del dueño de la pantalla, inevitablemente: la gota la pinta
 * código que corre DENTRO de la página del host, y en Odoo podemos porque el
 * cliente instala este módulo. Si algún día hay otro host donde podamos ejecutar,
 * se escribe otro renderer contra el mismo contrato.
 *
 * CÓMO SE APUNTA, y por qué no con un selector CSS: un selector sobre el DOM de
 * Odoo se rompe en la próxima versión, y quien escribe el procedimiento —un
 * implementador, no un dev— no tiene por qué saber uno. Se apunta por nombre
 * técnico del campo, por la etiqueta que la persona LEE, o por un botón. Lo que
 * NO se acepta es un selector libre: eso sería un puntero teledirigido sobre la
 * pantalla de otra persona.
 */

/** Las cuatro que entiende `computePosition` de Owl. Cualquier otra la hace tirar. */
const POSICIONES = new Set(["top", "right", "bottom", "left"]);

/**
 * Los botones de la interfaz de Odoo que tienen nombre propio y no técnico.
 *
 * Son tres porque son los tres que la persona nombra como acción ("guardá",
 * "descartá", "creá uno nuevo") y cuyo selector es de la interfaz, no de un
 * módulo — así que no envejecen con la contabilidad. Cualquier OTRO botón se
 * busca por su texto visible o su nombre técnico, ver `botonPorTextoONombre`.
 */
const ACCIONES_DE_LA_INTERFAZ = {
    save: ".o_form_button_save",
    discard: ".o_form_button_cancel",
    new: ".o_list_button_add, .o_form_button_create",
};

/** Cuánto queda la marca antes de apagarse sola. Una marca que no se va deja de
 *  leerse como "acá, ahora" y pasa a ser decoración de la pantalla. */
export const SPOTLIGHT_MS = 15000;

/** Cada cuánto se vuelve a mirar mientras la persona scrollea. El propio
 *  `tour_interactive` usa 50 ms para lo mismo. */
const SCROLL_DEBOUNCE_MS = 50;

/**
 * Desde qué ancho un campo deja de ser "un lugar" y pasa a ser "una zona".
 *
 * En un formulario de Odoo el control de un campo ocupa toda la columna: medido,
 * 505 px el teléfono y 514 px el CUIT en una ventana de 1280. La gota se centra
 * sobre lo que se le apunte, así que caía **238 a 249 px a la derecha del lugar
 * donde la persona hace clic** — señalando el renglón, no el campo. Un botón, en
 * cambio, mide 51 px: centrarse ahí es exactamente lo correcto.
 */
const ANCHO_QUE_YA_ES_UNA_ZONA = 120;

/** Cuánto del principio del campo se toma como "el lugar del clic". */
const ANCHO_DEL_PUNTO_DE_CLIC = 24;

/**
 * El texto de un elemento, comparable.
 *
 * MISMA SEMÁNTICA QUE EL `:contains` DE ODOO, y no es un detalle de estilo: los
 * tours apuntan por texto con `:contains(Confirm)`, que colapsa los espacios,
 * ignora mayúsculas y compara por SUBCADENA. Nuestra comparación era `===` sobre
 * el `textContent` crudo, y por eso fallaba en la mayoría de los formularios
 * reales: Odoo le agrega un `<sup>?</sup>` a la etiqueta de todo campo que tenga
 * `help` —y de todos, en modo debug— así que el texto era `"Punto de venta?"` y
 * la igualdad no daba nunca. Con subcadena, da.
 */
function normalizar(texto) {
    return String(texto || "")
        .replace(/[^\S\n\r]+/g, " ")
        .replace(/[\n\r]+/g, " ")
        .trim()
        .toLowerCase();
}

/**
 * Dónde se busca: la pantalla del formulario, no la página entera.
 *
 * En Odoo el atributo `name` no está calificado — `field.xml` lo pone en un
 * `<div>` y `list_renderer.xml` en un `<td>` — así que `[name="ref"]` con una
 * vista lista abierta marcaba **la celda de la primera fila**. Acotar al
 * formulario y saltear las celdas es lo que hace que la marca signifique algo.
 */
function pantalla(root) {
    if (root !== document) {
        return root;
    }
    return document.querySelector(".o_form_view") || document;
}

/** Un botón por lo que dice o por su nombre técnico, dentro de la pantalla.
 *
 *  No es un selector libre: la forma es cerrada (botones y anclas con pinta de
 *  botón) y lo único que viaja es cómo identificarlo. Es lo que hacen los tours
 *  —`.modal button:contains(Confirm)`— y lo que permite que un procedimiento diga
 *  "apretá Confirmar" sin que nadie escriba CSS. */
function botonPorTextoONombre(root, quien) {
    const porNombre = root.querySelector?.(`button[name="${String(quien).replace(/"/g, '\\"')}"]`);
    if (porNombre) {
        return porNombre;
    }
    const buscado = normalizar(quien);
    if (!buscado) {
        return null;
    }
    for (const boton of root.querySelectorAll?.("button, a.btn") || []) {
        if (normalizar(boton.textContent).includes(buscado)) {
            return boton;
        }
    }
    return null;
}

/**
 * Encontrar en la pantalla el elemento a señalar.
 *
 * El orden importa: primero lo exacto (el nombre técnico), después lo humano (la
 * etiqueta visible). Al revés, "Nombre" —que aparece en media pantalla—
 * señalaría el primer parecido en vez del campo pedido.
 *
 * @returns {HTMLElement|null}
 */
export function findSpotlightTarget(payload, root = document) {
    const { field, label, action } = payload || {};
    const donde = pantalla(root);

    if (action) {
        // `Object.hasOwn` y no un acceso directo: el mapa hereda de Object, así
        // que `action: "toString"` devolvía una FUNCIÓN y `querySelector` moría
        // con SyntaxError.
        const clave = String(action);
        const selector = Object.hasOwn(ACCIONES_DE_LA_INTERFAZ, clave)
            ? ACCIONES_DE_LA_INTERFAZ[clave]
            : null;
        if (selector) {
            return donde.querySelector?.(selector) || document.querySelector(selector);
        }
        return botonPorTextoONombre(donde, clave);
    }

    if (field) {
        const safe = String(field).replace(/"/g, '\\"');
        // `:not(td)` saca las celdas de una lista: ahí el mismo `name` está en
        // cada fila y la marca caería sobre la primera. Y `:not(.nav-link)`
        // porque la solapa de un notebook TAMBIÉN lleva `name` (el de la página),
        // así que un nombre que coincida marcaría la pestaña en vez del campo.
        const el = donde.querySelector?.(`[name="${safe}"]:not(td):not(.nav-link)`);
        if (el) {
            return afinar(el);
        }
    }

    if (label) {
        const buscado = normalizar(label);
        // La etiqueta apunta a su input por `for`: es la forma fiable de saltar
        // del texto que la persona lee al control que tiene que tocar.
        for (const el of donde.querySelectorAll?.("label") || []) {
            if (!buscado || !normalizar(el.textContent).includes(buscado)) {
                continue;
            }
            const forAttr = el.getAttribute("for");
            // Se busca DENTRO de la pantalla, no en `document`: el id de un
            // formulario de Odoo puede repetirse entre una vista y un diálogo
            // abierto encima, y ahí la marca caería en la pantalla de atrás.
            const target = forAttr
                ? donde.querySelector?.(`[id="${forAttr.replace(/"/g, '\\"')}"]`)
                : null;
            const elegido = target || el.closest(".o_cell, .o_field_widget, .o_inner_group");
            // Se SIGUE buscando si esta etiqueta no lleva a ningún lado: con el
            // `return` adentro del loop, una etiqueta duplicada sin `for` se
            // comía el intento de la que sí servía.
            if (elegido || target) {
                return afinar(elegido || target);
            }
        }
    }

    return null;
}

/**
 * Bajar de la caja del campo al control que la persona toca.
 *
 * En Odoo `[name="login"]` es el CONTENEDOR del campo, y en muchos formularios
 * ocupa toda la columna. La gota se centra sobre lo que se le apunte, así que
 * apuntarle al contenedor la deja flotando lejos del dato — señalando una zona en
 * vez de un lugar, que es justo lo que la gota vino a evitar.
 *
 * En un campo de sólo lectura no hay control: ahí queda la caja, que sigue siendo
 * lo correcto porque es todo lo que hay.
 */
function afinar(el) {
    const control = el?.querySelector?.("input, textarea, select");
    if (control) {
        return control;
    }
    // Sin control no significa "apuntá al contenedor". Un campo en modo lectura
    // —un teléfono ya cargado, un m2o, un link— muestra su valor en un hijo, y el
    // contenedor ocupa media columna: la gota terminaba flotando a la derecha del
    // dato, señalando una zona. Se baja al primer hijo que tenga texto visible.
    for (const hijo of el?.children || []) {
        const r = hijo.getBoundingClientRect?.();
        if (r && r.width > 0 && r.height > 0 && (hijo.textContent || "").trim()) {
            return hijo;
        }
    }
    return el;
}

/**
 * Buscar el campo en las pestañas CERRADAS del formulario, abriéndolas.
 *
 * En un formulario de Odoo, media configuración vive en una pestaña que no es la
 * que está abierta, y Odoo no renderiza el contenido de una pestaña cerrada: el
 * campo no está en el DOM, así que la gota no tiene dónde caer. Medido con el
 * caso real —"ARCA POS Number" vive en "Advanced Settings"— donde el agente hacía
 * todo bien y la marca igual no aparecía.
 *
 * SE ACOTA A UN NOTEBOOK, y esto era un bug: juntando las solapas de *todos* los
 * notebooks de la página —el del formulario, el de un x2many embebido, el de un
 * diálogo— y guardando UNA sola como original, podía dejar un x2many en la
 * pestaña equivocada. Se toma el notebook propio del formulario y se restaura su
 * propia solapa.
 *
 * @returns {Promise<HTMLElement|null>}
 */
async function buscarEnPestanas(payload) {
    const form = document.querySelector(".o_form_view");
    // El notebook del formulario, no el de una vista embebida dentro de un campo.
    const notebook = [...(form?.querySelectorAll(".o_notebook") || [])].find(
        (n) => !n.closest(".o_field_widget")
    );
    // La estructura real de Odoo: `.o_notebook > .o_notebook_headers > ul.nav >
    // li > a.nav-link` (ver `web.Notebook`).
    const solapas = [...(notebook?.querySelectorAll(":scope > .o_notebook_headers .nav-link") || [])];
    if (!solapas.length) {
        return null;
    }
    const original = solapas.find((s) => s.classList.contains("active"));
    for (const solapa of solapas) {
        if (solapa === original) {
            continue;
        }
        solapa.click();
        const el = await esperarA(() => findSpotlightTarget(payload));
        if (el) {
            return el;
        }
    }
    original?.click();
    // La vuelta también necesita su render: si no, la pestaña queda marcada como
    // activa con el contenido de la otra a medio dibujar.
    await esperarA(() => null);
    return null;
}

/**
 * Reintentar hasta que Owl termine de dibujar, o hasta rendirse.
 *
 * Un solo `requestAnimationFrame` no alcanza: Owl encola el render en microtasks
 * y el DOM de la pestaña recién abierta puede llegar uno o dos frames después.
 *
 * Tres intentos y no doce: doce se pagaban SIEMPRE, por pestaña y otra vez al
 * final, y con seis pestañas eran ~1,4 s de formulario parpadeando para terminar
 * diciendo "no está". Con tres alcanza para el render de Owl — medido — y el
 * costo del intento fallido baja a la décima parte.
 */
async function esperarA(buscar, intentos = 3) {
    for (let i = 0; i < intentos; i++) {
        await new Promise((r) => requestAnimationFrame(() => r()));
        const encontrado = buscar();
        if (encontrado) {
            return encontrado;
        }
    }
    return null;
}

/**
 * Un ancla angosta al principio del campo: donde se hace clic para escribir.
 *
 * Es la misma técnica que usa el propio `tour_pointer_state` de Odoo cuando el
 * objetivo está fuera de la pantalla — se crea un div `position: fixed` y se
 * apunta a él— y acá resuelve lo contrario: un objetivo demasiado ANCHO. No
 * reemplaza al campo para nada más: el hover sigue atado al campo de verdad, así
 * que acercarse a cualquier parte del renglón despliega el texto.
 *
 * @param {HTMLElement} proxy  el div reutilizable
 * @param {HTMLElement} campo  el control real
 * @returns {HTMLElement} a qué apuntar
 */
function alPuntoDeClic(anclas, campo) {
    const r = campo?.getBoundingClientRect?.();
    if (!r || r.width <= ANCHO_QUE_YA_ES_UNA_ZONA) {
        soltarAnclas(anclas);
        return campo;
    }
    // UN ELEMENTO NUEVO EN CADA APUNTADO, y esto no es derroche: el puntero de
    // Odoo se AUTO-BLOQUEA cuando el ancla es la misma y no se movió más de 1px
    // (`tour_pointer.js`), y un ancla fija nunca se mueve. Con un ancla
    // reutilizada, el bloqueo congelaba la posición calculada en la primera
    // pasada — cuando el globo todavía estaba expandido midiendo su texto— y la
    // gota quedaba 120 px a la izquierda del campo. Medido: `left: 85.9px` para
    // un ancla en 208. Un elemento distinto hace que el puntero lo trate como
    // otro ancla, libere el bloqueo y recalcule con el tamaño ya colapsado.
    const ancla = document.createElement("div");
    ancla.dataset.tuquiAncla = "1";
    ancla.style.cssText =
        "position:fixed;pointer-events:none;z-index:-1;" +
        `left:${Math.round(r.left)}px;top:${Math.round(r.top)}px;` +
        `width:${ANCHO_DEL_PUNTO_DE_CLIC}px;height:${Math.round(r.height)}px;`;
    document.body.appendChild(ancla);
    soltarAnclas(anclas);
    anclas.add(ancla);
    return ancla;
}

/** Sacar del DOM las anclas anteriores. */
function soltarAnclas(anclas) {
    for (const vieja of anclas) {
        vieja.remove();
    }
    anclas.clear();
}

/** ¿Esta mutación la provocamos nosotros acomodando el ancla?
 *
 *  Sin este filtro el ancla se alimenta a sí misma: acomodarla es una mutación
 *  del DOM, el loop escucha mutaciones, y el loop vuelve a acomodarla. Giraba
 *  mientras la marca viviera — colgó la suite tres corridas seguidas, que es la
 *  forma barata de que aparezca; en la pantalla de una persona habría sido la
 *  pestaña al palo 15 segundos. Nuestra propia contabilidad no es un cambio de
 *  pantalla. */
function esDeUnAncla(m) {
    const nuestro = (n) => n?.dataset?.tuquiAncla === "1";
    if (nuestro(m.target)) {
        return true;
    }
    const tocados = [...(m.addedNodes || []), ...(m.removedNodes || [])];
    return tocados.length > 0 && tocados.every(nuestro);
}

/**
 * La gota, atada al servicio `overlay` de Odoo.
 *
 * Se monta la primera vez que se usa: una sesión que nunca ve una gota no paga
 * nada.
 *
 * @param {object} overlay servicio `overlay` de Odoo
 * @param {object} [deps] sólo para tests: reemplaza el componente real
 */
export function makeSpotlight(overlay, deps = {}) {
    const makePointer = deps.createPointerState || createPointerState;
    const Pointer = deps.Gota || Gota;
    /**
     * Qué registro está abierto, para saber si sigue siendo el mismo.
     *
     * Un turno largo entrega la marca sobre un formulario que la persona ya dejó:
     * el campo se llama igual en el registro nuevo, así que el loop la volvería a
     * apuntar ahí — señalando con confianza el lugar correcto del registro
     * equivocado, que es el modo de falla más caro de todos. Con la clave, la
     * marca es de ESE registro y de ningún otro.
     */
    const claveDelRegistro = deps.recordKey || (() => null);
    let pointer = null;
    let removeOverlay = null;
    let timer = null;
    /** Para despegar los listeners de la gota anterior al mover la marca. */
    let desatar = null;
    /**
     * Token de la marca vigente.
     *
     * El panel llama `spotlight()` sin esperar el resultado, así que dos pedidos
     * seguidos se interleavan dentro de `buscarEnPestanas` —que hace `await` por
     * cada solapa— y el primero terminaba apagando la gota del segundo, o
     * revirtiendo la pestaña que el segundo necesitaba. Cada llamada se queda con
     * su número y abandona en silencio si dejó de ser la última.
     */
    let generacion = 0;
    /** El loop de la marca vigente, o null si no hay marca. */
    let vigilancia = null;
    /**
     * Si el globo está desplegado ahora.
     *
     * Lo lleva la marca y no cada apuntado, y el motivo salió de mirar un video:
     * el loop re-apunta en cada cambio del DOM, y si cada re-apuntado forzaba
     * "cerrado", el texto se abría al acercarse el mouse y **se cerraba solo** un
     * frame después — el hover no llegaba a servir para nada.
     */
    let abierto = false;
    /** Las anclas angostas vivas. Ver `alPuntoDeClic`. */
    const anclas = new Set();

    function apagarVigilancia() {
        vigilancia?.();
        vigilancia = null;
    }

    /**
     * Mientras la marca vive: re-resolver el objetivo y re-apuntar.
     *
     * Las tres razones para volver a mirar, y cada una arregla una falla medida:
     * el DOM cambió (el formulario se re-renderizó, o el campo desapareció al
     * cambiar de registro), la persona scrolleó (el puntero se auto-bloquea y
     * sólo se libera si alguien vuelve a disparar el cálculo), o la ventana
     * cambió de tamaño.
     *
     * Y hay una cuarta, que es la más silenciosa: la clasificación de "está
     * dentro o fuera de la pantalla" la hace un IntersectionObserver, que es
     * asincrónico. En la PRIMERA llamada todavía no observó nada, y el getter de
     * Odoo devuelve "in" por defecto — así que un campo abajo del fold se
     * marcaba fuera de pantalla y se declaraba éxito. Con el loop, el
     * re-apuntado siguiente ya tiene la observación y toma el camino bueno de
     * Odoo: el puntero va al borde del área scrolleable, dice que hay que bajar,
     * y al clickearlo lleva.
     *
     * @returns {() => void} para apagarlo
     */
    function vigilar(payload, marcar, claveInicial) {
        const revisar = () => {
            if (claveDelRegistro() !== claveInicial) {
                // Otro registro: la marca era para el anterior.
                apagar();
                return;
            }
            const el = findSpotlightTarget(payload || {});
            if (!el) {
                // El campo se fue de la pantalla: esconderse es lo honesto. Una
                // gota flotando sobre la pantalla nueva señala cualquier cosa.
                pointer?.hide();
                return;
            }
            marcar(el);
        };
        const observer = new MacroMutationObserver((mutaciones) => {
            // Lo que provocamos nosotros acomodando el ancla no cuenta como un
            // cambio de pantalla. Ver `esDeUnAncla`.
            if (Array.isArray(mutaciones) && mutaciones.length && mutaciones.every(esDeUnAncla)) {
                return;
            }
            revisar();
        });
        observer.observe(document.body);
        const alScrollear = debounce(revisar, SCROLL_DEBOUNCE_MS);
        const scrollEl = getScrollParent(findSpotlightTarget(payload || {}));
        scrollEl?.addEventListener("scroll", alScrollear);
        window.addEventListener("resize", alScrollear);
        return () => {
            observer.disconnect();
            scrollEl?.removeEventListener("scroll", alScrollear);
            window.removeEventListener("resize", alScrollear);
        };
    }

    /**
     * Poner la gota sobre lo que pida el payload.
     *
     * @returns {Promise<boolean>} si se encontró dónde ponerla. Ese dato decide si
     *   hay que avisarle a la persona que está parada en otra pantalla.
     */
    async function spotlight(payload) {
        const mia = ++generacion;
        const claveInicial = claveDelRegistro();
        let el = findSpotlightTarget(payload || {});
        if (!el) {
            // Puede estar en una pestaña cerrada del formulario, que Odoo no
            // renderiza hasta que se abre.
            el = await buscarEnPestanas(payload || {});
        }
        if (mia !== generacion) {
            // Llegó otra marca mientras buscábamos: esta ya no manda.
            return false;
        }
        if (claveDelRegistro() !== claveInicial) {
            // La persona cambió de registro mientras se buscaba el campo.
            return false;
        }
        if (!el) {
            return false;
        }
        if (!pointer) {
            pointer = makePointer();
            // Mismo `sequence` que usa el propio tour_service: la capa por encima
            // de los z-index de bootstrap, para que la marca no quede debajo de un
            // modal o de la barra de acciones.
            removeOverlay = overlay.add(
                Pointer,
                { pointerState: pointer.state, bounce: true },
                { sequence: 1100 }
            );
        }
        clearTimeout(timer);
        apagarVigilancia();
        desatar?.();
        const hint = payload?.hint || "";
        // La posición se valida como todo lo demás del payload: `computePosition`
        // de Owl revienta con "directions is not iterable" ante cualquier valor
        // fuera de estas cuatro, y ese throw sube por el efecto de posicionamiento
        // —fuera de nuestro try— así que no habría marca ni aviso.
        const donde = POSICIONES.has(String(payload?.position)) ? String(payload.position) : "bottom";
        // Una gota nueva reemplaza a la anterior: dos marcas prendidas convierten
        // "acá" en "en algún lado de estos dos".
        const marcar = (target) => {
            pointer.pointTo(alPuntoDeClic(anclas, target), { content: hint, tooltipPosition: donde });
            // CERRADA por defecto: la gota es un punto que dice "acá", no un
            // cartel. El texto se despliega al acercarse —al campo o a la marca—,
            // que es exactamente cuando la persona lo necesita y no antes. Un
            // globo abierto permanente taparía los campos vecinos.
            //
            // Se respeta el estado en curso: si la persona tiene el mouse encima,
            // un re-apuntado del loop no le cierra el texto en la cara.
            pointer.showContent(abierto);
        };
        abierto = false;
        marcar(el);
        if (hint) {
            desatar = atarHover(pointer, el, (v) => (abierto = v));
        } else {
            desatar = null;
        }
        vigilancia = vigilar(payload, marcar, claveInicial);
        // El segundo apuntado, un frame después, es el que ve la clasificación
        // del IntersectionObserver — ver `vigilar`. Sin esto, un campo abajo del
        // fold se marca fuera de pantalla y se declara éxito.
        requestAnimationFrame(() => {
            if (mia === generacion && pointer && claveDelRegistro() === claveInicial) {
                const actual = findSpotlightTarget(payload || {});
                if (actual) {
                    marcar(actual);
                }
            }
        });
        timer = setTimeout(apagar, SPOTLIGHT_MS);
        return true;
    }

    /**
     * Apagar la marca y no dejar nada colgado.
     *
     * `hide()` del puntero de Odoo sólo apaga el estado: deja vivo su
     * IntersectionObserver y su `floatingAnchor` en el `body` — el div que usa
     * para señalar el borde cuando el campo está fuera de pantalla. Eso lo limpia
     * `destroy()`, así que al apagarse por tiempo se desmonta todo. La próxima
     * marca lo vuelve a montar: es lo mismo que pasa la primera vez.
     */
    function apagar() {
        clearTimeout(timer);
        apagarVigilancia();
        desatar?.();
        desatar = null;
        pointer?.hide?.();
        soltarAnclas(anclas);
        removeOverlay?.();
        pointer?.destroy?.();
        pointer = null;
        removeOverlay = null;
    }

    return { spotlight, destroy: apagar };
}

/**
 * Desplegar el texto cuando la persona se acerca — al campo marcado o a la gota.
 *
 * Los dos, y no sólo la gota, porque el gesto natural es ir al campo: quien ve la
 * marca mueve el mouse al lugar que le están señalando, no al puntito. Es lo
 * mismo que hace `tour_interactive` con los pasos de un tour.
 *
 * @returns {() => void} para despegar los listeners cuando la gota se mueve o se
 *   apaga. Sin esto, el campo de la marca anterior seguiría abriendo un texto que
 *   ya no le corresponde.
 */
function atarHover(pointer, anchor, anotar = () => {}) {
    const abrir = () => {
        anotar(true);
        pointer.showContent(true);
    };
    const cerrar = () => {
        anotar(false);
        pointer.showContent(false);
    };
    pointer.setState({ onMouseEnter: abrir, onMouseLeave: cerrar });
    anchor.addEventListener("mouseenter", abrir);
    anchor.addEventListener("mouseleave", cerrar);
    return () => {
        anchor.removeEventListener("mouseenter", abrir);
        anchor.removeEventListener("mouseleave", cerrar);
        pointer.setState({ onMouseEnter: null, onMouseLeave: null });
    };
}
