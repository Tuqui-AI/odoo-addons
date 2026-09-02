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
        //
        // LA ETIQUETA ES LA QUE ODOO EMITE, no un `<label>` pelado — y ahí estaba
        // el problema: `web.FormLabel` mete un `<sup>?</sup>` cuando el campo
        // tiene `help`, y en modo debug lo mete en TODOS. Con eso el
        // `textContent` es "Punto de venta?" y una comparación exacta no da
        // nunca. O sea que la marca por etiqueta estaba rota en casi todo
        // formulario de configuración, que es justo donde se usa.
        const root = dom(`
            <label class="o_form_label" for="pos">Punto de venta<sup class="text-info p-1">?</sup></label>
            <input id="pos" class="el-campo" />
        `);
        expect(findSpotlightTarget({ label: "Punto de venta" }, root)?.className).toBe("el-campo");
    });

    test("y no le importan las mayúsculas ni los espacios de más", () => {
        // Misma semántica que el `:contains` de Odoo, que es con lo que los tours
        // apuntan por texto. Un procedimiento escrito a mano no va a coincidir
        // carácter por carácter con la traducción de la UI.
        const root = dom(`
            <label class="o_form_label" for="pos">   Punto   de venta<sup>?</sup></label>
            <input id="pos" class="el-campo" />
        `);
        expect(findSpotlightTarget({ label: "punto de venta" }, root)?.className).toBe("el-campo");
    });

    test("en una vista LISTA no marca la celda de la primera fila", () => {
        // `name` no está calificado en Odoo: `field.xml` lo pone en un `<div>` y
        // `list_renderer.xml` en un `<td>`. Sin excluir las celdas, "señalá el
        // campo ref" con una lista abierta marcaba la primera fila — una marca
        // que dice "acá" sobre un lugar que no es.
        const root = dom(`
            <table><tbody>
                <tr><td name="ref" class="celda-1">R-1</td></tr>
                <tr><td name="ref" class="celda-2">R-2</td></tr>
            </tbody></table>
        `);
        expect(findSpotlightTarget({ field: "ref" }, root)).toBe(null);
    });

    test("ni la solapa de un notebook, que también lleva `name`", () => {
        // `web.Notebook` le pone `t-att-name` a la solapa. Un nombre de página
        // que coincida con un campo marcaba la pestaña.
        const root = dom(`
            <div class="o_notebook_headers"><a class="nav-link" name="ref">Referencias</a></div>
            <div name="ref" class="el-campo"></div>
        `);
        expect(findSpotlightTarget({ field: "ref" }, root)?.className).toBe("el-campo");
    });

    test("apunta un botón por lo que dice, no sólo por una lista cerrada", () => {
        // Es lo que hacen los tours de Odoo: `.modal button:contains(Confirm)`.
        // Permite que un procedimiento diga "apretá Confirmar" sin que nadie
        // escriba un selector — y sin que la lista de botones señalables
        // envejezca con los nombres de método de la contabilidad.
        const root = dom(`
            <button class="otro">Guardar borrador</button>
            <button class="el-boton">Confirmar</button>
        `);
        expect(findSpotlightTarget({ action: "Confirmar" }, root)?.className).toBe("el-boton");
        expect(findSpotlightTarget({ action: "confirmar" }, root)?.className).toBe("el-boton");
    });

    test("y también por su nombre técnico, que es lo que sabe un procedimiento", () => {
        const root = dom(`
            <button class="otro" name="action_draft">Borrador</button>
            <button class="el-boton" name="action_post">Contabilizar</button>
        `);
        expect(findSpotlightTarget({ action: "action_post" }, root)?.className).toBe("el-boton");
    });

    test("un botón que no está en la pantalla sigue devolviendo null", () => {
        // Que la forma se agrande no significa que ahora acepte cualquier cosa:
        // sólo botones, y sólo los que están.
        const root = dom(`<button class="unico">Guardar</button>`);
        expect(findSpotlightTarget({ action: "Contabilizar" }, root)).toBe(null);
        expect(findSpotlightTarget({ action: "div" }, root)).toBe(null);
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
        // Y "no está en la lista" incluye lo que el mapa HEREDA de Object: con un
        // acceso directo, `toString` devolvía una función y `querySelector` moría
        // con SyntaxError. Un nombre inventado no cubre este caso.
        expect(findSpotlightTarget({ action: "toString" }, root)).toBe(null);
        expect(findSpotlightTarget({ action: "constructor" }, root)).toBe(null);
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

    test("apunta la gota al elemento y deja el texto listo para cuando se acerque", async () => {
        // La gota queda CERRADA: un punto que dice "acá", no un cartel. Un globo
        // abierto permanente taparía los campos vecinos justo cuando la persona
        // los necesita para ubicarse.
        const el = document.createElement("div");
        el.setAttribute("name", "l10n_ar_afip_pos_number");
        document.body.appendChild(el);
        const { handle, calls } = harness();

        expect(await handle.spotlight({ field: "l10n_ar_afip_pos_number", hint: "El número que te dio ARCA" })).toBe(true);
        expect(calls[0].anchor).toBe(el);
        expect(calls[0].step.content).toBe("El número que te dio ARCA");
        expect(calls[1]).toEqual({ showContent: false });

        handle.destroy();
        el.remove();
    });

    test("una posición que Owl no entiende cae en 'bottom' en vez de reventar", async () => {
        // El par: la posición válida se respeta, la inválida no llega. Cualquier
        // valor fuera de las cuatro hace tirar a `computePosition` DENTRO del
        // efecto de posicionamiento de Owl, fuera de cualquier try nuestro: sin
        // marca y sin aviso, que es el peor de los dos.
        const el = document.createElement("div");
        el.setAttribute("name", "campo_posicion");
        document.body.appendChild(el);
        const { handle, calls } = harness();

        await handle.spotlight({ field: "campo_posicion", position: "top" });
        expect(calls[0].step.tooltipPosition).toBe("top");

        calls.length = 0;
        await handle.spotlight({ field: "campo_posicion", position: "arriba a la izquierda" });
        expect(calls[0].step.tooltipPosition).toBe("bottom");

        handle.destroy();
        el.remove();
    });

    test("el texto se despliega al acercarse AL CAMPO, no sólo a la gota", async () => {
        // El gesto natural de quien ve la marca es ir al lugar señalado, no al
        // puntito. Si el texto sólo saliera sobre la gota, casi nadie lo vería.
        const el = document.createElement("div");
        el.setAttribute("name", "campo_con_texto");
        document.body.appendChild(el);
        const { handle, calls } = harness();

        await handle.spotlight({ field: "campo_con_texto", hint: "algo que explica" });
        calls.length = 0;
        el.dispatchEvent(new MouseEvent("mouseenter"));
        expect(calls).toEqual([{ showContent: true }]);
        el.dispatchEvent(new MouseEvent("mouseleave"));
        expect(calls).toEqual([{ showContent: true }, { showContent: false }]);

        handle.destroy();
        el.remove();
    });

    test("el campo de la gota vieja deja de abrir un texto que ya no es suyo", async () => {
        // Sin despegar los listeners, pasar por el campo marcado hace diez
        // minutos desplegaría el texto de la marca actual, que habla de otra cosa.
        const viejo = document.createElement("div");
        viejo.setAttribute("name", "campo_viejo");
        const nuevo = document.createElement("div");
        nuevo.setAttribute("name", "campo_nuevo");
        document.body.append(viejo, nuevo);
        const { handle, calls } = harness();

        await handle.spotlight({ field: "campo_viejo", hint: "el de antes" });
        await handle.spotlight({ field: "campo_nuevo", hint: "el de ahora" });
        calls.length = 0;
        viejo.dispatchEvent(new MouseEvent("mouseenter"));
        expect(calls).toEqual([]);

        handle.destroy();
        viejo.remove();
        nuevo.remove();
    });

    test("sin texto, la gota no queda esperando un hover que no muestra nada", async () => {
        const el = document.createElement("div");
        el.setAttribute("name", "campo_pelado");
        document.body.appendChild(el);
        const { handle, calls } = harness();

        await handle.spotlight({ field: "campo_pelado" });
        calls.length = 0;
        el.dispatchEvent(new MouseEvent("mouseenter"));
        expect(calls).toEqual([]);

        handle.destroy();
        el.remove();
    });

    test("si no está en la pantalla, dice que no pudo y no monta nada", async () => {
        // El caso que hace que se le pueda avisar a la persona que está parada en
        // otra pantalla, en vez de dejarla buscando una marca que nunca apareció.
        // Y no montar el overlay importa: una sesión sin gotas no paga nada.
        const { added, calls, handle } = harness();

        expect(await handle.spotlight({ field: "no_existe_en_ningun_lado" })).toBe(false);
        expect(added.length).toBe(0);
        expect(calls.length).toBe(0);

        handle.destroy();
    });

    test("la gota se monta una sola vez aunque haya varias marcas", async () => {
        const el = document.createElement("div");
        el.setAttribute("name", "campo_x");
        document.body.appendChild(el);
        const { added, handle } = harness();

        await handle.spotlight({ field: "campo_x" });
        await handle.spotlight({ field: "campo_x" });
        expect(added.length).toBe(1);

        handle.destroy();
        el.remove();
    });

    test("busca el campo en las pestañas cerradas del formulario", async () => {
        // Media configuración de Odoo vive en una pestaña que no es la abierta, y
        // Odoo no renderiza el contenido de una cerrada. Medido con el caso real:
        // "ARCA POS Number" vive en "Advanced Settings", el agente hacía todo bien
        // y la marca no aparecía porque el campo no estaba en el DOM.
        // El markup es el de `web.Notebook`: `.o_notebook > .o_notebook_headers >
        // ul.nav > li > a.nav-link`, todo dentro de un `.o_form_view`. Un DOM
        // inventado hacía pasar el test con selectores que en Odoo no matchean.
        const form = document.createElement("div");
        form.className = "o_form_view";
        form.innerHTML = `
            <div class="o_notebook">
                <div class="o_notebook_headers">
                    <ul class="nav nav-tabs">
                        <li class="nav-item"><a class="nav-link active" data-p="1" name="entries">Journal Entries</a></li>
                        <li class="nav-item"><a class="nav-link" data-p="2" name="advanced">Advanced Settings</a></li>
                    </ul>
                </div>
                <div class="o_notebook_content tab-content"><div class="paginas tab-pane active"></div></div>
            </div>`;
        document.body.appendChild(form);
        const paginas = form.querySelector(".paginas");
        // El comportamiento de Odoo: sólo la pestaña activa tiene contenido.
        for (const tab of form.querySelectorAll(".nav-link")) {
            tab.addEventListener("click", () => {
                form.querySelectorAll(".nav-link").forEach((t) => t.classList.remove("active"));
                tab.classList.add("active");
                paginas.innerHTML =
                    tab.dataset.p === "2" ? '<div name="l10n_ar_afip_pos_number" class="el-campo"></div>' : "";
            });
        }
        const { handle, calls } = harness();

        expect(await handle.spotlight({ field: "l10n_ar_afip_pos_number" })).toBe(true);
        expect(calls[0].anchor?.className).toBe("el-campo");

        handle.destroy();
        form.remove();
    });

    test("si no está en ninguna pestaña, deja el formulario como estaba", async () => {
        // Dejarle a alguien el formulario en otra pestaña, sin marca y sin
        // explicación, es peor que no haber intentado.
        const form = document.createElement("div");
        form.innerHTML = `
            <div class="o_notebook">
                <a class="nav-link active" data-p="1">Primera</a>
                <a class="nav-link" data-p="2">Segunda</a>
            </div>`;
        document.body.appendChild(form);
        for (const tab of form.querySelectorAll(".nav-link")) {
            tab.addEventListener("click", () => {
                form.querySelectorAll(".nav-link").forEach((t) => t.classList.remove("active"));
                tab.classList.add("active");
            });
        }
        const { handle } = harness();

        expect(await handle.spotlight({ field: "campo_que_no_existe" })).toBe(false);
        expect(form.querySelector(".nav-link.active")?.dataset.p).toBe("1");

        handle.destroy();
        form.remove();
    });

    test("destroy saca la gota de la pantalla", async () => {
        const el = document.createElement("div");
        el.setAttribute("name", "campo_y");
        document.body.appendChild(el);
        const { added, calls, handle } = harness();

        await handle.spotlight({ field: "campo_y" });
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

/**
 * Las llamadas a `pointTo` de una lista de llamadas.
 *
 * `typeof c !== "string"` no es defensa por si acaso: `"hide".anchor` EXISTE —
 * es `String.prototype.anchor`, el método legacy de HTML— así que un filtro por
 * `c.anchor` cuenta también los `"hide"` y los `"destroy"`. Costó un rato de
 * diagnóstico creyendo que el código estaba mal cuando el test estaba mal.
 */
function apuntados(calls) {
    return calls.filter((c) => typeof c !== "string" && c.anchor);
}

describe("mientras la marca vive", () => {
    /** El mismo puntero de mentira que arriba, para poder mirar QUÉ se le pide. */
    function harnessConCampo(html = '<div name="campo_vivo" class="el-campo"></div>') {
        const form = document.createElement("div");
        form.className = "o_form_view";
        form.innerHTML = html;
        document.body.appendChild(form);

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
        const overlay = { add: () => () => {} };
        const handle = makeSpotlight(overlay, {
            createPointerState: () => pointer,
            Gota: "FakeGota",
        });
        return { form, handle, calls };
    }

    test("vuelve a apuntar en el frame siguiente: es lo que ve si el campo está fuera de pantalla", async () => {
        // La clasificación "dentro o fuera del área visible" la hace un
        // IntersectionObserver, que es asincrónico: en la PRIMERA llamada todavía
        // no observó nada y el getter de Odoo devuelve "in" por defecto. Sin este
        // segundo apuntado, un campo abajo del fold se marca fuera de pantalla y
        // se declara éxito — el chat dice "te lo marqué" y no hay nada.
        const { form, handle, calls } = harnessConCampo();

        expect(await handle.spotlight({ field: "campo_vivo" })).toBe(true);
        expect(apuntados(calls).length).toBe(1);

        await animationFrame();
        expect(apuntados(calls).length).toBe(2);

        handle.destroy();
        form.remove();
    });

    test("si el campo se va de la pantalla, la gota se esconde", async () => {
        // Sin esto queda flotando sobre la pantalla nueva, señalando cualquier
        // cosa: el anchor se desconecta y el puntero no se enteraba, porque su
        // `isVisible` sólo cambia por un evento que un re-render no dispara.
        const { form, handle, calls } = harnessConCampo();
        await handle.spotlight({ field: "campo_vivo" });
        await animationFrame();
        calls.length = 0;

        form.querySelector("[name=campo_vivo]").remove();
        await animationFrame();
        await animationFrame();

        expect(calls.includes("hide")).toBe(true);

        handle.destroy();
        form.remove();
    });

    test("y si el formulario se re-renderiza, vuelve a apuntar al campo nuevo", async () => {
        // El otro lado del par: que el loop no se limite a esconderse. Un
        // re-render reemplaza el nodo, y la marca tiene que ir al nuevo.
        const { form, handle, calls } = harnessConCampo();
        await handle.spotlight({ field: "campo_vivo" });
        await animationFrame();
        calls.length = 0;

        // Como un re-render de Owl: mismo campo, nodo distinto.
        form.innerHTML = '<div name="campo_vivo" class="el-campo-nuevo"></div>';
        await animationFrame();
        await animationFrame();

        expect(apuntados(calls).length > 0).toBe(true);
        expect(apuntados(calls).at(-1).anchor.className).toBe("el-campo-nuevo");
        expect(calls.includes("hide")).toBe(false);

        handle.destroy();
        form.remove();
    });

    test("deja de mirar cuando la gota se apaga", async () => {
        // El observador y el oído del scroll viven lo que vive la marca. Si
        // quedaran prendidos, cada cambio del DOM de Odoo pagaría una búsqueda
        // por una gota que ya no está.
        const { form, handle, calls } = harnessConCampo();
        await handle.spotlight({ field: "campo_vivo" });
        await animationFrame();
        handle.destroy();
        calls.length = 0;

        form.innerHTML = '<div name="campo_vivo" class="otro"></div>';
        await animationFrame();
        await animationFrame();

        expect(calls.length).toBe(0);

        form.remove();
    });
});

describe("la marca es de UN registro", () => {
    /** Como el harness de arriba, pero con la clave del registro bajo control. */
    function harnessConRegistro(clave = "account.journal:7") {
        const form = document.createElement("div");
        form.className = "o_form_view";
        form.innerHTML = '<div name="campo_vivo" class="el-campo"></div>';
        document.body.appendChild(form);

        const estado = { clave };
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
        const handle = makeSpotlight(
            { add: () => () => {} },
            { createPointerState: () => pointer, Gota: "FakeGota", recordKey: () => estado.clave },
        );
        return { form, handle, calls, estado };
    }

    test("si la persona se va a otro registro, la marca se apaga", async () => {
        // El modo de falla más caro de todos: el campo se llama igual en el
        // registro nuevo, así que el loop lo volvería a apuntar — señalando con
        // confianza el lugar correcto del registro equivocado. Un turno largo
        // entrega la marca justo así.
        const { form, handle, calls, estado } = harnessConRegistro();
        expect(await handle.spotlight({ field: "campo_vivo" })).toBe(true);
        await animationFrame();
        calls.length = 0;

        estado.clave = "account.journal:8";
        form.innerHTML = '<div name="campo_vivo" class="el-campo-del-otro"></div>';
        await animationFrame();
        await animationFrame();

        expect(apuntados(calls).length).toBe(0);
        expect(calls.includes("hide")).toBe(true);

        handle.destroy();
        form.remove();
    });

    test("y si sigue en el mismo, la sigue apuntando", async () => {
        // El otro lado del par: sin esto, el fix de arriba lo aprobaría también
        // un cambio que apagara la gota ante cualquier cosa.
        const { form, handle, calls } = harnessConRegistro();
        await handle.spotlight({ field: "campo_vivo" });
        await animationFrame();
        calls.length = 0;

        form.innerHTML = '<div name="campo_vivo" class="el-campo-nuevo"></div>';
        await animationFrame();
        await animationFrame();

        expect(apuntados(calls).length > 0).toBe(true);

        handle.destroy();
        form.remove();
    });
});
