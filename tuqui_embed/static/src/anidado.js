/** @odoo-module **/

/**
 * Cuando esta pantalla de Odoo está siendo MOSTRADA adentro de otra cosa, su
 * propio asistente no se abre.
 *
 * EL PROBLEMA, EN CRIOLLO. Este módulo le permite a Tuqui mostrar Odoo dentro de
 * su panel. Pero el Odoo que se muestra puede tener instalado el panel de Tuqui
 * — y ese panel se abre solo si quedó abierto antes. Entonces: Tuqui muestra
 * Odoo, ese Odoo abre su Tuqui, ese Tuqui restaura su panel con Odoo, y así.
 * Cada nivel carga un cliente web completo. **Tumba el navegador entero**, no
 * la pestaña: medido, con Chrome congelándose a los pocos segundos.
 *
 * POR QUÉ VIVE ACÁ Y NO EN `tuqui_assistant`. Este módulo es el que crea la
 * posibilidad de embeber: sin él, Odoo no se muestra adentro de nada y la
 * recursión no existe. El que abre la puerta trae el cerrojo. Y así el arreglo
 * no toca un módulo ajeno para protegerlo de algo que este habilitó.
 *
 * QUÉ HACE, Y POR QUÉ ESO ALCANZA. El panel decide abrirse leyendo dos lugares:
 * `sessionStorage` (quedó abierto en esta pestaña) y una señal en `localStorage`
 * (que se comparte entre TODAS las pestañas y iframes de este Odoo — por eso
 * alcanzaba con haber usado el panel en otra pestaña para disparar el bucle).
 * Estando anidados, los dos se limpian antes de que el panel los lea.
 *
 * Y ADEMÁS SE ESCONDE EL BOTÓN, porque limpiar el estado sólo evita la apertura
 * automática: el botón del systray seguiría abriéndolo a mano, con el mismo
 * resultado. Un Odoo mostrado adentro de Tuqui es una VISTA — la conversación ya
 * está del otro lado de la pantalla.
 *
 * NO SE TOCA NADA SI NO ESTAMOS ANIDADOS: en el uso normal de Odoo, este archivo
 * no hace absolutamente nada.
 */

/** Las dos llaves que mira el panel para decidir si se abre solo. */
const ESTADO_DEL_PANEL = "tuqui_panel_state";
const SENAL_DE_APERTURA = "tuqui_open_signal";

/** ¿Nos están mostrando adentro de otra página?
 *
 *  Un `window.top` de otro origen hace que leerlo tire SecurityError — y eso YA
 *  es la respuesta: si no podemos ni mirarlo, es de otro. Por eso el catch
 *  devuelve `true` en vez de tragarse el error.
 */
export function estamosAnidados(win = window) {
    try {
        return win.top !== win.self;
    } catch {
        return true;
    }
}

export function apagarElPanelAnidado(win = window) {
    if (!estamosAnidados(win)) {
        return false;
    }
    try {
        win.sessionStorage?.removeItem(ESTADO_DEL_PANEL);
    } catch {
        // Navegación privada o storage bloqueado: el panel tampoco va a poder
        // leer su estado, así que arranca cerrado igual.
    }
    try {
        win.localStorage?.removeItem(SENAL_DE_APERTURA);
    } catch {
        // Ídem.
    }
    win.document?.documentElement?.classList?.add("tuqui-anidado");
    return true;
}

apagarElPanelAnidado();
