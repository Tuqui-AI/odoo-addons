/** @odoo-module **/
import { describe, expect, getFixture, test } from "@odoo/hoot";

import { textoDeLoMarcado } from "@tuqui_assistant/services/tuqui_assistant_service";

/**
 * Qué palabras lee la persona donde cayó la marca.
 *
 * Es la mitad que faltaba del canal de vuelta: saber que la marca cayó no
 * alcanza, porque el resolver acierta por texto INCLUIDO y puede dar por buena
 * una marca sobre otro control. Sin esto, el aviso de "cayó sobre otra cosa"
 * queda inerte — y quedaba inerte justo donde más importa.
 */

function pantalla(html) {
    const root = document.createElement("div");
    root.innerHTML = html;
    getFixture().appendChild(root);
    return root;
}

describe("textoDeLoMarcado", () => {
    test("una casilla se lee por su etiqueta, que es hermana y no ancestro", () => {
        // EL CASO MEDIDO EN VIVO, en la pantalla de Ajustes: la marca cayó sobre
        // la casilla "Storage Locations" y el canal contestó `marked_on: null`,
        // porque un checkbox no tiene texto propio y su etiqueta no está arriba
        // en el árbol sino al costado.
        const root = pantalla(`
            <div class="o_setting_box">
                <div class="o_setting_left_pane">
                    <input type="checkbox" id="group_stock_multi_locations_0"/>
                </div>
                <div class="o_setting_right_pane">
                    <label for="group_stock_multi_locations_0">Storage Locations</label>
                    <div class="text-muted">Track which location the stock is in</div>
                </div>
            </div>
        `);
        const casilla = root.querySelector("input");
        expect(textoDeLoMarcado(casilla)).toBe("Storage Locations");
    });

    test("un botón se lee por su propio texto", () => {
        const root = pantalla(`<button class="btn">Save</button>`);
        expect(textoDeLoMarcado(root.querySelector("button"))).toBe("Save");
    });

    test("un ícono sin texto se lee por su aria-label", () => {
        // El caso de los botones de acción de una fila: un `<i>` dentro de un
        // botón sin palabras. Se pregunta como pregunta el navegador.
        const root = pantalla(`<button aria-label="Delete row"><i class="fa fa-trash"></i></button>`);
        expect(textoDeLoMarcado(root.querySelector("button"))).toBe("Delete row");
    });

    test("un campo de texto vacío se lee por su placeholder", () => {
        const root = pantalla(`<input type="text" placeholder="e.g. Sale Order"/>`);
        expect(textoDeLoMarcado(root.querySelector("input"))).toBe("e.g. Sale Order");
    });

    test("sin etiqueta propia sube hasta encontrar palabras, no tres niveles", () => {
        // Contar niveles era adivinar: entre el control y la fila con las
        // palabras hay dos niveles en un formulario y cinco en una casilla de
        // ajustes. Acá hay cuatro.
        const root = pantalla(`
            <div class="o_form_view">
                <div class="fila"><div><div><div><input type="checkbox"/></div></div></div>Cantidad</div>
            </div>
        `);
        expect(textoDeLoMarcado(root.querySelector("input"))).toBe("Cantidad");
    });

    test("se frena en el formulario y no devuelve la pantalla entera", () => {
        // Sin el freno, un control suelto devolvía todo el texto del formulario
        // recortado a 80 — que no es "sobre qué cayó", es ruido con forma de dato.
        const root = pantalla(`
            <div class="o_form_view"><span class="suelto"></span></div>
        `);
        expect(textoDeLoMarcado(root.querySelector(".suelto"))).toBe(null);
    });

    test("un id con puntos no rompe la búsqueda de la etiqueta", () => {
        // Los ids de Odoo llevan puntos y dos puntos. Sin escapar, el selector
        // revienta y se pierde la etiqueta que SÍ estaba: un error que se lee
        // igual que "no hay etiqueta".
        const root = pantalla(`
            <div><input type="checkbox" id="sale.group_x_0"/><label for="sale.group_x_0">Descuentos</label></div>
        `);
        expect(textoDeLoMarcado(root.querySelector("input"))).toBe("Descuentos");
    });

    test("sin nada que leer contesta null, no una cadena vacía", () => {
        expect(textoDeLoMarcado(null)).toBe(null);
    });
});
