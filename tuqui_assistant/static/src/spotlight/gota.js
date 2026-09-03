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
/*
 * POR QUÉ EL IMPORT VA ARRIBA Y NO DIFERIDO (se probó al revés y no funciona).
 *
 * Hubo una versión que cargaba el puntero con `await import("@web_tour/...")`
 * adentro de una función, para "no arrastrar el bundle de tours". Está revertida
 * por tres razones medidas:
 *
 *   1. Odoo NO transpila el `import()` dinámico: corriendo
 *      `js_transpiler.transpile_javascript` sobre el archivo, la llamada sale
 *      literal. En el browser queda un import nativo de un especificador pelado
 *      dentro de un script clásico — no resuelve, y la gota nunca se dibuja.
 *      El core de Odoo no usa `await import("@...")` en ningún addon; su forma
 *      de acceder a un módulo sin importarlo es `odoo.loader.modules.get(...)`.
 *   2. No había nada que ahorrar: `web_tour/static/src/js/tour_pointer/**` está
 *      en `web.assets_backend`, el mismo bundle que este addon, y `web_tour` es
 *      `auto_install: True`. El navegador ya lo bajó, importemos o no.
 *   3. Tampoco se ahorra la evaluación: el loader de Odoo encola y evalúa TODO
 *      módulo definido sin `lazy` al arrancar (`module_loader.js`, `define()`),
 *      así que `tour_pointer` se evalúa igual sin que nadie lo importe.
 *
 * El cuelgue del iframe que motivó aquel cambio tiene otra causa, ya aislada: un
 * tour de onboarding pendiente cuyo puntero lee `parent.document` cross-origin.
 * El propio commit que introdujo la carga diferida dice que el cuelgue persiste
 * con el asistente apagado.
 */
export class Gota extends TourPointer {
    static template = "tuqui_assistant.Gota";
}
