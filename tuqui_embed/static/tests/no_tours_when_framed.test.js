/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { patchWithCleanup } from "@web/../tests/web_test_helpers";
import { browser } from "@web/core/browser/browser";
import { tourState } from "@web_tour/js/tour_state";

import { framing } from "@tuqui_embed/no_tours_when_framed";

/**
 * La red del guard que evita que un tour se reanude dentro del panel.
 *
 * Por qué tiene test propio: su falla es SILENCIOSA. Si alguien lo rompe, nada
 * se pone rojo — simplemente vuelve a crashearse la pestaña de quien tenga un
 * onboarding a medias, y eso se descubre en un cliente. Lo que se fija acá es
 * el par completo, porque las dos mitades importan igual: dentro del frame no
 * se reanuda, y FUERA del frame se sigue reanudando. Un guard que apagara los
 * tours siempre pasaría la mitad del test y rompería el onboarding de todos.
 */
describe("tuqui_embed: no reanudar tours dentro de un frame", () => {
    test("dentro de un frame, no hay tour para reanudar", () => {
        patchWithCleanup(framing, { isFramed: () => true });
        patchWithCleanup(browser.localStorage, { getItem: () => "un_tour_a_medias" });

        expect(tourState.getCurrentTour()).toBe(null);
    });

    test("fuera de un frame, el tour guardado se sigue reanudando", () => {
        patchWithCleanup(framing, { isFramed: () => false });
        patchWithCleanup(browser.localStorage, { getItem: () => "un_tour_a_medias" });

        expect(tourState.getCurrentTour()).toBe("un_tour_a_medias");
    });

    test("el progreso guardado NO se borra por estar embebido", () => {
        // El guard miente hacia arriba, no destruye: si borrara el
        // `localStorage`, el usuario perdería el avance de su onboarding en su
        // propio Odoo. Se verifica que nadie llame a los `removeItem`.
        const borrados = [];
        patchWithCleanup(framing, { isFramed: () => true });
        patchWithCleanup(browser.localStorage, {
            getItem: () => "un_tour_a_medias",
            removeItem: (clave) => borrados.push(clave),
        });

        tourState.getCurrentTour();

        expect(borrados).toEqual([]);
    });
});
