/** @odoo-module **/

/**
 * La gota: el puntero de Odoo, en verde y sin el botón de parar el tour.
 *
 * Se hereda el componente en vez de escribir uno propio porque lo que importa
 * heredar no es el dibujo, es el COMPORTAMIENTO: cómo se ubica, cómo se voltea
 * cuando no entra, cómo sigue al elemento al scrollear, y cómo se abre el
 * tooltip. Todo eso ya está resuelto y probado.
 *
 * El template es una copia modificada del suyo (ver gota.xml): sin "Stop Tour",
 * que fuera de un tour desactivaría los tours del usuario y recargaría la página,
 * y con una clase propia para el salvia.
 *
 * POR QUÉ ES UNA FUNCIÓN Y NO UNA CLASE SUELTA. Heredar exige tener la clase
 * padre en el momento de definir la hija, así que un `export class Gota extends
 * TourPointer` obliga a importar `@web_tour/...` arriba de todo — y ese import
 * arrastra el bundle de tours de Odoo apenas se carga el archivo, aunque nadie
 * pida nunca una marca.
 *
 * Eso, con Odoo mostrado DENTRO del panel de Tuqui, **cuelga el hilo principal
 * de la pestaña entera** a los dos segundos de entrar al menú de aplicaciones.
 * Reproducido en 5 segundos y aislado quitando este archivo del bundle: sin él
 * no se cuelga, con él sí.
 *
 * Envolviéndolo en una función, el bundle de tours llega recién cuando hay una
 * marca que dibujar. Un Odoo embebido nunca pide una —el panel del asistente no
 * se abre ahí— así que nunca lo paga. Es, además, lo que el diseño ya prometía:
 * "una sesión que nunca ve una gota no paga nada".
 */

let _gota = null;

/** La clase de la gota, cargando el puntero de Odoo la primera vez. */
export async function obtenerGota() {
    if (!_gota) {
        const { TourPointer } = await import("@web_tour/js/tour_pointer/tour_pointer");
        _gota = class Gota extends TourPointer {
            static template = "tuqui_assistant.Gota";
        };
    }
    return _gota;
}
