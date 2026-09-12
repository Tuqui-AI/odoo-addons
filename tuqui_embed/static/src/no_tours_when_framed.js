/** @odoo-module **/

/**
 * No reanudar tours cuando esta pantalla se está mostrando dentro de otro sitio.
 *
 * EL PROBLEMA, MEDIDO. Con un tour de onboarding en curso, el webclient dentro
 * de un iframe de otro origen **le crashea la pestaña**: el puntero del tour
 * busca el documento del padre, eso tira `SecurityError` en bucle (59 contados
 * en pocos segundos) y se lleva la memoria del renderer.
 *
 * Y el tour no arranca en el panel: arranca en el Odoo de siempre. El usuario
 * entra, el tour empieza y deja su estado en `localStorage`; después abre el
 * panel —mismo origen, mismo `localStorage`— y el tour se REANUDA adentro del
 * iframe. Por eso apagarlo del lado del servidor no alcanza: la reanudación
 * lee `localStorage`, no el `session_info`.
 *
 * ES UN BUG DE ODOO, Y ESTÁ EN UNA LÍNEA. `tour_service.js` ya intenta evitar
 * esto: arranca y reanuda tours dentro de `if (!window.frameElement)`. Pero
 * `window.frameElement` devuelve `null` cuando el padre es de OTRO origen, así
 * que la guarda se cumple justo en el caso que quería prevenir. La condición
 * que sí funciona cross-origin es `window.top !== window.self`, la de acá.
 * Corresponde reportarlo arriba.
 *
 * POR QUÉ SE PARCHEA `tourState` Y NO SE SACA EL SERVICIO. Sacar
 * `tour_service` del registry rompería a quien lo pide: el widget de
 * onboarding y el POS hacen `useService("tour_service")` y reventarían al
 * renderizar. Devolviendo `null` acá, el servicio sigue vivo y `startTour`
 * queda disponible para quien lo llame a mano; sólo se corta la reanudación
 * automática.
 *
 * Y NO SE BORRA EL PROGRESO DEL USUARIO. Se devuelve `null` sólo dentro del
 * frame: el `localStorage` queda intacto, así que en su Odoo de siempre el
 * tour sigue donde lo dejó.
 *
 * TAMBIÉN SE DESCARTÓ falsificar `window.frameElement` para que la guarda de
 * Odoo funcionara sola. Es una línea y es tentador, pero hay código de
 * `website` y del editor que USA ese elemento (`dispatchEvent`,
 * `ownerDocument`): habría cambiado un crash de tours por roturas en otro
 * lado.
 */

import { patch } from "@web/core/utils/patch";
import { tourState } from "@web_tour/js/tour_state";

/**
 * Vive en un objeto —y no como función suelta— para que el test pueda
 * sustituirlo. El patch se aplica SIEMPRE y la decisión se toma en cada
 * llamada: así no depende del estado del frame al momento de importar, que es
 * justamente lo que no se puede simular en un test.
 */
export const framing = {
    /** ¿Esta página está dentro de un frame? Vale también cross-origin. */
    isFramed() {
        try {
            return window.top !== window.self;
        } catch {
            // Leer `window.top` cross-origin no tira, pero si algún día lo
            // hiciera, estar enmarcado es la respuesta conservadora.
            return true;
        }
    },
};

patch(tourState, {
    getCurrentTour() {
        if (framing.isFramed()) {
            return null;
        }
        return super.getCurrentTour();
    },
});
