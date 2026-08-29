/** @odoo-module **/
import { describe, expect, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
import { queryAll, queryAllTexts } from "@odoo/hoot-dom";
import { mountWithCleanup } from "@web/../tests/web_test_helpers";

import { Gota } from "@tuqui_assistant/spotlight/gota";

import { findSpotlightTarget, makeSpotlight } from "@tuqui_assistant/services/spotlight";

/**
 * Dónde cae la gota.
 *
 * Es resolución sobre un DOM, así que el DOM se arma a mano: lo que hay que
 * fijar no es cómo Odoo dibuja un formulario, sino a cuál de varios elementos
 * parecidos apunta la marca — que es donde una gota deja de ayudar y empieza a
 * mentir.
 */

function dom(html) {
    const root = document.createElement("div");
    root.innerHTML = html;
    return root;
}

describe("findSpotlightTarget", () => {
    test("apunta por nombre técnico del campo", () => {
        const root = dom(`
            <div name="otro_campo" class="a"></div>
            <div name="l10n_ar_afip_pos_number" class="b"></div>
        `);
        expect(findSpotlightTarget({ field: "l10n_ar_afip_pos_number" }, root)?.className).toBe("b");
    });

    test("apunta por la etiqueta que la persona LEE", () => {
        // Quien escribe el procedimiento sabe "Punto de venta", no
        // `l10n_ar_afip_pos_number`. Si sólo aceptáramos el nombre técnico, cada
        // procedimiento nuevo necesitaría un dev.
        const root = dom(`
            <label for="pos">Punto de venta</label>
            <input id="pos" class="el-campo" />
        `);
        expect(findSpotlightTarget({ label: "Punto de venta" }, root)?.className).toBe("el-campo");
    });

    test("el nombre técnico GANA sobre la etiqueta", () => {
        // "Nombre" aparece en media pantalla. Resolver por etiqueta primero
        // señalaría el primer parecido en vez del campo pedido.
        const root = dom(`
            <label for="otro">Nombre</label>
            <input id="otro" class="el-otro" />
            <div name="name" class="el-correcto"></div>
        `);
        expect(findSpotlightTarget({ field: "name", label: "Nombre" }, root)?.className).toBe("el-correcto");
    });

    test("baja de la caja del campo al control que la persona toca", () => {
        // En Odoo `[name=x]` es el CONTENEDOR, y en muchos formularios ocupa toda
        // la columna. La gota se centra sobre lo que se le apunte: apuntarle a la
        // caja la deja flotando lejos del dato, señalando una zona en vez de un
        // lugar — que es justo lo que la gota vino a evitar.
        const root = dom(`
            <div name="l10n_ar_afip_pos_number" class="la-caja">
                <input class="el-control" />
            </div>
        `);
        expect(findSpotlightTarget({ field: "l10n_ar_afip_pos_number" }, root)?.className).toBe("el-control");
    });

    test("en un campo de sólo lectura marca la caja, que es todo lo que hay", () => {
        const root = dom(`<div name="estado" class="la-caja"><span>Confirmado</span></div>`);
        expect(findSpotlightTarget({ field: "estado" }, root)?.className).toBe("la-caja");
    });

    test("un botón se pide por acción, de una lista cerrada", () => {
        const root = dom(`<button class="o_form_button_save">Guardar</button>`);
        expect(findSpotlightTarget({ action: "save" }, root)?.className).toBe("o_form_button_save");
    });

    test("una acción que no está en la lista NO se señala", () => {
        // Sin lista cerrada, "señalá lo que yo te diga" es un puntero
        // teledirigido sobre la pantalla de otro. No es una función, es una
        // superficie de ataque.
        const root = dom(`<button class="o_form_button_save">Guardar</button>`);
        expect(findSpotlightTarget({ action: "cualquier_cosa" }, root)).toBe(null);
    });

    test("si el campo no está en la pantalla, devuelve null y no algo parecido", () => {
        // El caso que hace que el agente pueda decir "estás en otra pantalla" en
        // vez de esperar un "listo" sobre una marca que nunca apareció.
        const root = dom(`<div name="otra_cosa"></div>`);
        expect(findSpotlightTarget({ field: "no_existe" }, root)).toBe(null);
        expect(findSpotlightTarget({ label: "No existe" }, root)).toBe(null);
    });

    test("un payload vacío no señala nada", () => {
        expect(findSpotlightTarget({}, dom(`<div name="x"></div>`))).toBe(null);
        expect(findSpotlightTarget(null, dom(`<div name="x"></div>`))).toBe(null);
    });

    test("una comilla en el nombre del campo no rompe la búsqueda", () => {
        const root = dom(`<div name="x"></div>`);
        expect(() => findSpotlightTarget({ field: 'x"]' }, root)).not.toThrow();
    });
});

describe("makeSpotlight", () => {
    /** Puntero de mentira con la forma del de `web_tour`, para poder mirar QUÉ se
     *  le pide sin depender de cómo Odoo lo dibuja. */
    function harness() {
        const calls = [];
        const pointer = {
            calls,
            state: {},
            pointTo: (anchor, step) => calls.push({ anchor, step }),
            showContent: (open) => calls.push({ showContent: open }),
            setState: (st) => calls.push({ setState: st }),
            hide: () => calls.push("hide"),
            destroy: () => calls.push("destroy"),
        };
        const added = [];
        const overlay = { add: (...args) => (added.push(args), () => added.push("removed")) };
        const handle = makeSpotlight(overlay, {
            createPointerState: () => pointer,
            Gota: "FakeGota",
        });
        return { pointer, added, handle, calls };
    }

    test("apunta la gota al elemento y deja el texto listo para cuando se acerque", () => {
        // La gota queda CERRADA: un punto que dice "acá", no un cartel. Un globo
        // abierto permanente taparía los campos vecinos justo cuando la persona
        // los necesita para ubicarse.
        const el = document.createElement("div");
        el.setAttribute("name", "l10n_ar_afip_pos_number");
        document.body.appendChild(el);
        const { handle, calls } = harness();

        expect(handle.spotlight({ field: "l10n_ar_afip_pos_number", hint: "El número que te dio ARCA" })).toBe(true);
        expect(calls[0].anchor).toBe(el);
        expect(calls[0].step.content).toBe("El número que te dio ARCA");
        expect(calls[1]).toEqual({ showContent: false });

        handle.destroy();
        el.remove();
    });

    test("el texto se despliega al acercarse AL CAMPO, no sólo a la gota", () => {
        // El gesto natural de quien ve la marca es ir al lugar señalado, no al
        // puntito. Si el texto sólo saliera sobre la gota, casi nadie lo vería.
        const el = document.createElement("div");
        el.setAttribute("name", "campo_con_texto");
        document.body.appendChild(el);
        const { handle, calls } = harness();

        handle.spotlight({ field: "campo_con_texto", hint: "algo que explica" });
        calls.length = 0;
        el.dispatchEvent(new MouseEvent("mouseenter"));
        expect(calls).toEqual([{ showContent: true }]);
        el.dispatchEvent(new MouseEvent("mouseleave"));
        expect(calls).toEqual([{ showContent: true }, { showContent: false }]);

        handle.destroy();
        el.remove();
    });

    test("el campo de la gota vieja deja de abrir un texto que ya no es suyo", () => {
        // Sin despegar los listeners, pasar por el campo marcado hace diez
        // minutos desplegaría el texto de la marca actual, que habla de otra cosa.
        const viejo = document.createElement("div");
        viejo.setAttribute("name", "campo_viejo");
        const nuevo = document.createElement("div");
        nuevo.setAttribute("name", "campo_nuevo");
        document.body.append(viejo, nuevo);
        const { handle, calls } = harness();

        handle.spotlight({ field: "campo_viejo", hint: "el de antes" });
        handle.spotlight({ field: "campo_nuevo", hint: "el de ahora" });
        calls.length = 0;
        viejo.dispatchEvent(new MouseEvent("mouseenter"));
        expect(calls).toEqual([]);

        handle.destroy();
        viejo.remove();
        nuevo.remove();
    });

    test("sin texto, la gota no queda esperando un hover que no muestra nada", () => {
        const el = document.createElement("div");
        el.setAttribute("name", "campo_pelado");
        document.body.appendChild(el);
        const { handle, calls } = harness();

        handle.spotlight({ field: "campo_pelado" });
        calls.length = 0;
        el.dispatchEvent(new MouseEvent("mouseenter"));
        expect(calls).toEqual([]);

        handle.destroy();
        el.remove();
    });

    test("si no está en la pantalla, dice que no pudo y no monta nada", () => {
        // El caso que hace que se le pueda avisar a la persona que está parada en
        // otra pantalla, en vez de dejarla buscando una marca que nunca apareció.
        // Y no montar el overlay importa: una sesión sin gotas no paga nada.
        const { added, calls, handle } = harness();

        expect(handle.spotlight({ field: "no_existe_en_ningun_lado" })).toBe(false);
        expect(added.length).toBe(0);
        expect(calls.length).toBe(0);

        handle.destroy();
    });

    test("la gota se monta una sola vez aunque haya varias marcas", () => {
        const el = document.createElement("div");
        el.setAttribute("name", "campo_x");
        document.body.appendChild(el);
        const { added, handle } = harness();

        handle.spotlight({ field: "campo_x" });
        handle.spotlight({ field: "campo_x" });
        expect(added.length).toBe(1);

        handle.destroy();
        el.remove();
    });

    test("destroy saca la gota de la pantalla", () => {
        const el = document.createElement("div");
        el.setAttribute("name", "campo_y");
        document.body.appendChild(el);
        const { added, calls, handle } = harness();

        handle.spotlight({ field: "campo_y" });
        handle.destroy();
        expect(added).toInclude("removed");
        expect(calls).toInclude("destroy");

        el.remove();
    });
});

describe("la Gota real (template heredado de Odoo)", () => {
    /**
     * Estos dos montan el componente DE VERDAD, no un doble.
     *
     * Los tests de arriba usan un puntero falso para mirar qué se le pide, y eso
     * no diría nada si el template heredado se rompiera. Y el modo de falla que
     * importa acá es silencioso: si una versión futura de Odoo cambia el markup
     * del puntero, el `xpath` que saca el botón deja de matchear y el botón
     * VUELVE — un botón que le desactiva los tours al usuario y le recarga la
     * página encima de lo que está escribiendo. Nadie se enteraría hasta que le
     * pase a alguien.
     */
    async function montarGota(state = {}) {
        const pointerState = {
            anchor: document.body,
            content: "El número que te dio ARCA",
            isOpen: true,
            isVisible: true,
            ...state,
        };
        await mountWithCleanup(Gota, { props: { pointerState, bounce: true } });
        await animationFrame();
    }

    test("no tiene el botón que apaga los tours y recarga la página", async () => {
        await montarGota();
        expect(queryAll(".o_tour_pointer").length).toBe(1);
        expect(queryAllTexts("button")).not.toInclude("Stop Tour");
    });

    test("lleva la clase con la que se tiñe de salvia", async () => {
        // Sin la clase la gota sale con el color de Odoo, y ahí deja de decir
        // QUIÉN te está señalando: se lee como una función del sistema.
        await montarGota();
        expect(queryAll(".o_tour_pointer.o_tuqui_gota").length).toBe(1);
    });
});
