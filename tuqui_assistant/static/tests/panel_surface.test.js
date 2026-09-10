/** @odoo-module **/
import { after, beforeEach, describe, expect, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
import { MEDIAS_BREAKPOINTS, SIZES } from "@web/core/ui/ui_service";
import { MainComponentsContainer } from "@web/core/main_components_container";
import {
    defineModels,
    fields,
    getService,
    models,
    mountWithCleanup,
    onRpc,
} from "@web/../tests/web_test_helpers";
import { sizeForWidth } from "@tuqui_assistant/panel/panel";

/**
 * El panel como SUPERFICIE: una tarjeta que flota, se acopla y se esconde.
 *
 * Lo que se fija acá no es la estética (eso lo mira una persona), son las dos
 * promesas que el rediseño le hace al usuario y que se rompen en silencio:
 *
 *  1. Al acoplarse, el panel le SACA ancho a Odoo — y Odoo no se entera, porque
 *     mira el viewport y el viewport no cambió. Sin el override, el chatter se
 *     queda al costado apretado. El test fija que el tamaño que Odoo reporta
 *     salga del espacio que QUEDA, no de la ventana.
 *  2. Esconder guarda la conversación; el ícono del systray la trae de vuelta.
 *     Como la burbuja ya no existe, si el systray abriera un chat nuevo la
 *     conversación escondida se perdería y esconder no serviría de nada.
 *
 * Cómo correrlos: ver el encabezado de `apply_proposal.test.js`.
 */

class Partner extends models.Model {
    _name = "res.partner";
    name = fields.Char();
    _records = [{ id: 1, name: "Acme" }];
}
defineModels([Partner]);

/** Fuerza el ancho del panel acoplado, para que la medida no dependa de la pantalla.
 *
 * Se limpia sola al terminar el test: Hoot no borra los estilos inline de
 * `<html>`, así que sin esto la variable quedaba puesta para todo lo que
 * corriera después en el mismo bundle.
 */
function setExpandedWidth(value) {
    document.documentElement.style.setProperty("--o-tuqui-expanded-width", value);
    after(() => document.documentElement.style.removeProperty("--o-tuqui-expanded-width"));
}

describe("sizeForWidth", () => {
    // Los casos SALEN de la tabla de Odoo, no de una lista escrita a mano: la
    // tabla cambia entre versiones (18.0 tiene siete franjas y pone XXL en 1534;
    // 19.0 tiene seis y lo pone en 1400), así que una lista fija haría pasar el
    // test en una versión y fallar en la otra por el motivo equivocado.
    test("cada franja de Odoo devuelve su propio tamaño, en los dos bordes", () => {
        MEDIAS_BREAKPOINTS.forEach(({ minWidth, maxWidth }, i) => {
            if (minWidth !== undefined) {
                expect(sizeForWidth(minWidth)).toBe(i);
            }
            if (maxWidth !== undefined) {
                expect(sizeForWidth(maxWidth)).toBe(i);
            }
        });
    });

    test("un píxel antes de una franja cae en la anterior", () => {
        MEDIAS_BREAKPOINTS.forEach(({ minWidth }, i) => {
            if (i > 0 && minWidth !== undefined) {
                expect(sizeForWidth(minWidth - 1)).toBe(i - 1);
            }
        });
    });

    test("el tamaño más grande no tiene techo", () => {
        expect(sizeForWidth(99999)).toBe(MEDIAS_BREAKPOINTS.length - 1);
    });

    test("un ancho fraccionario cae en su franja, no en la nada", () => {
        // `getBoundingClientRect` devuelve fracciones y la tabla de Odoo deja un
        // hueco de 1 px entre franjas (…1199 | 1200…). Sin redondear, 1199.4 no
        // matcheaba ninguna y una pantalla ancha se reportaba como la más chica.
        MEDIAS_BREAKPOINTS.forEach(({ maxWidth }, i) => {
            if (maxWidth !== undefined) {
                expect(sizeForWidth(maxWidth + 0.4)).toBe(i);
                expect(sizeForWidth(maxWidth + 0.9)).toBe(i);
            }
        });
    });

    test("un ancho de cero no rompe: es el más chico", () => {
        expect(sizeForWidth(0)).toBe(SIZES.XS);
    });
});

describe("panel acoplado", () => {
    let assistant;
    let ui;

    beforeEach(async () => {
        onRpc("tuqui.assistant.sso.nonce", "embed_bootstrap", () => ({
            connected: false,
            base_url: null,
            slug: null,
        }));
        await mountWithCleanup(MainComponentsContainer);
        assistant = getService("tuquiAssistant");
        ui = getService("ui");
    });

    test("acoplarse le avisa a Odoo con cuánto lugar se quedó", async () => {
        const realSize = sizeForWidth(window.innerWidth);
        assistant.toggleVisibility();
        await animationFrame();
        expect(ui.size).toBe(realSize);

        // Un panel que ocupa todo el ancho no le deja nada a Odoo: el tamaño que
        // Odoo reporta tiene que caer al mínimo, que es lo que manda al chatter
        // abajo del formulario.
        setExpandedWidth("100vw");
        assistant.expand();
        await animationFrame();
        await animationFrame();
        expect(ui.size).toBe(SIZES.XS);

        assistant.contract();
        await animationFrame();
        await animationFrame();
        expect(ui.size).toBe(realSize);
    });

    test("y sigue midiendo mientras la tarjeta viaja, no una sola vez", async () => {
        // El morph dura 340 ms: una medición única, tomada apenas se hace clic,
        // devuelve el ancho que la tarjeta está DEJANDO. En una pantalla de 1920
        // eso daba 392 en vez de 768 — 1528 px de lugar en vez de 1152 — y el
        // chatter se quedaba al costado, que es justo lo que esto viene a evitar.
        const realSize = sizeForWidth(window.innerWidth);
        setExpandedWidth("100vw");
        assistant.toggleVisibility();
        await animationFrame();
        assistant.expand();
        await animationFrame();
        await animationFrame();
        expect(ui.size).toBe(SIZES.XS);

        // El panel se angosta DESPUÉS del primer cálculo: si sólo se midiera una
        // vez, el tamaño se quedaría clavado en el de recién.
        setExpandedWidth("0px");
        await animationFrame();
        await animationFrame();
        await animationFrame();
        expect(ui.size).toBe(realSize);
    });

    test("le avisa a Odoo con un resize, que es lo que el chatter escucha", async () => {
        // Escribir `ui.size` no re-dibuja a nadie por sí solo: el chatter se
        // redibuja con un debounce colgado del resize de la ventana. Sin este
        // aviso, las medidas que llegan DESPUÉS del primer cuadro —o sea todas
        // las del viaje de la tarjeta— no las ve nadie.
        let avisos = 0;
        const contar = () => { avisos += 1; };
        window.addEventListener("resize", contar);
        after(() => window.removeEventListener("resize", contar));

        setExpandedWidth("100vw");
        assistant.toggleVisibility();
        await animationFrame();
        assistant.expand();
        await animationFrame();
        await animationFrame();
        expect(avisos).toBeGreaterThan(0);
    });

    test("y también mantiene isSmall, que es un campo aparte", async () => {
        // `env.isSmall` no se deriva de `size`: es otro campo del mismo
        // reactive, y lo leen cientos de lugares. Escribir sólo uno los deja
        // contradiciéndose.
        setExpandedWidth("100vw");
        assistant.toggleVisibility();
        await animationFrame();
        assistant.expand();
        await animationFrame();
        await animationFrame();
        expect(ui.size).toBe(SIZES.XS);
        expect(ui.isSmall).toBe(true);

        assistant.contract();
        await animationFrame();
        await animationFrame();
        expect(ui.isSmall).toBe(sizeForWidth(window.innerWidth) <= SIZES.SM);
    });

    test("esconder el panel le devuelve el tamaño real a Odoo", async () => {
        const realSize = sizeForWidth(window.innerWidth);
        setExpandedWidth("100vw");
        assistant.toggleVisibility();
        assistant.expand();
        await animationFrame();
        await animationFrame();
        expect(ui.size).toBe(SIZES.XS);

        assistant.minimize();
        await animationFrame();
        await animationFrame();
        expect(ui.size).toBe(realSize);
    });
});

describe("esconder y volver", () => {
    let assistant;

    beforeEach(async () => {
        onRpc("tuqui.assistant.sso.nonce", "embed_bootstrap", () => ({
            connected: false,
            base_url: null,
            slug: null,
        }));
        await mountWithCleanup(MainComponentsContainer);
        assistant = getService("tuquiAssistant");
    });

    test("esconder no desmonta la tarjeta y no deja una burbuja en la esquina", async () => {
        assistant.toggleVisibility();
        await animationFrame();
        expect(".o-tuqui-panel").toHaveCount(1);

        assistant.minimize();
        await animationFrame();
        // Sigue montada (el iframe vivo: un remount gastaría un 2º nonce SSO),
        // sólo oculta.
        expect(".o-tuqui-panel").toHaveCount(1);
        expect(".o-tuqui-panel").toHaveClass("o-tuqui-away");
        expect(".o-tuqui-bubble-launcher").toHaveCount(0);
    });

    test("el ícono de la barra es un interruptor: esconde lo que está a la vista", async () => {
        assistant.toggleVisibility();
        await animationFrame();
        expect(".o-tuqui-panel").not.toHaveClass("o-tuqui-away");

        assistant.toggleVisibility();
        await animationFrame();
        expect(".o-tuqui-panel").toHaveClass("o-tuqui-away");
    });

    test("y devuelve la conversación escondida, sin remontar el iframe", async () => {
        assistant.toggleVisibility();
        await animationFrame();
        assistant.minimize();
        await animationFrame();

        assistant.toggleVisibility();
        await animationFrame();

        // La misma tarjeta de siempre: si se remontara, el 2º nonce SSO daría 401
        // y la conversación anterior se perdería.
        expect(".o-tuqui-panel").toHaveCount(1);
        expect(".o-tuqui-panel").not.toHaveClass("o-tuqui-away");
        expect(assistant.state.panelOpen).toBe(true);
    });
});
