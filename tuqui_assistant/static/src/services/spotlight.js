/** @odoo-module **/
import { createPointerState } from "@web_tour/js/tour_pointer/tour_pointer_state";
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
 * técnico del campo, por la etiqueta que la persona LEE, o por un botón de una
 * lista cerrada.
 */

/** Botones señalables y su selector. Lista CERRADA: "señalá lo que yo te diga"
 *  con selector libre es un puntero teledirigido sobre la pantalla de otra
 *  persona. Eso no es una función, es una superficie. */
const ACTION_TARGETS = {
    save: ".o_form_button_save",
    discard: ".o_form_button_cancel",
    confirm: "button[name='action_post'], button[name='action_confirm']",
    new: ".o_list_button_add, .o_form_button_create",
};

/** Cuánto queda la marca antes de apagarse sola. Una marca que no se va deja de
 *  leerse como "acá, ahora" y pasa a ser decoración de la pantalla. */
export const SPOTLIGHT_MS = 15000;

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

    if (action) {
        const selector = ACTION_TARGETS[String(action)];
        return selector ? root.querySelector(selector) : null;
    }

    if (field) {
        const safe = String(field).replace(/"/g, '\\"');
        const el = root.querySelector(`[name="${safe}"]`);
        if (el) {
            return afinar(el);
        }
    }

    if (label) {
        const wanted = String(label).trim().toLowerCase();
        // La etiqueta apunta a su input por `for`: es la forma fiable de saltar
        // del texto que la persona lee al control que tiene que tocar.
        for (const el of root.querySelectorAll("label")) {
            if ((el.textContent || "").trim().toLowerCase() !== wanted) {
                continue;
            }
            const forAttr = el.getAttribute("for");
            // Se busca DENTRO del root, no en `document`: el id de un formulario
            // de Odoo puede repetirse entre una vista y un diálogo abierto encima,
            // y ahí la marca caería en la pantalla de atrás.
            const target = forAttr
                ? root.querySelector(`[id="${forAttr.replace(/"/g, '\\"')}"]`)
                : null;
            return afinar(target || el.closest(".o_cell, .o_field_widget, .o_inner_group") || el);
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
    return el?.querySelector?.("input, textarea, select") || el;
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
    let pointer = null;
    let removeOverlay = null;
    let timer = null;
    /** Para despegar los listeners de la gota anterior al mover la marca. */
    let desatar = null;

    /**
     * Poner la gota sobre lo que pida el payload.
     *
     * @returns {boolean} si se encontró dónde ponerla. Ese dato decide si hay que
     *   avisarle a la persona que está parada en otra pantalla.
     */
    function spotlight(payload) {
        const el = findSpotlightTarget(payload || {});
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
        desatar?.();
        const hint = payload?.hint || "";
        // Una gota nueva reemplaza a la anterior: dos marcas prendidas convierten
        // "acá" en "en algún lado de estos dos".
        pointer.pointTo(el, { content: hint, tooltipPosition: payload?.position || "bottom" });
        // CERRADA por defecto: la gota es un punto que dice "acá", no un cartel.
        // El texto se despliega al acercarse —al campo o a la marca—, que es
        // exactamente cuando la persona lo necesita y no antes. Un globo abierto
        // permanente taparía los campos vecinos del formulario.
        pointer.showContent(false);
        if (hint) {
            desatar = atarHover(pointer, el);
        } else {
            desatar = null;
        }
        timer = setTimeout(() => {
            desatar?.();
            desatar = null;
            pointer.hide();
        }, SPOTLIGHT_MS);
        return true;
    }

    function destroy() {
        clearTimeout(timer);
        desatar?.();
        desatar = null;
        removeOverlay?.();
        pointer?.destroy?.();
        pointer = null;
        removeOverlay = null;
    }

    return { spotlight, destroy };
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
function atarHover(pointer, anchor) {
    const abrir = () => pointer.showContent(true);
    const cerrar = () => pointer.showContent(false);
    pointer.setState({ onMouseEnter: abrir, onMouseLeave: cerrar });
    anchor.addEventListener("mouseenter", abrir);
    anchor.addEventListener("mouseleave", cerrar);
    return () => {
        anchor.removeEventListener("mouseenter", abrir);
        anchor.removeEventListener("mouseleave", cerrar);
        pointer.setState({ onMouseEnter: null, onMouseLeave: null });
    };
}
