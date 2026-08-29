/** @odoo-module **/
import { TourPointer } from "@web_tour/js/tour_pointer/tour_pointer";

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
 */
export class Gota extends TourPointer {
    static template = "tuqui_assistant.Gota";
}
