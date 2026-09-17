/** @odoo-module **/
import { describe, expect, getFixture, test } from "@odoo/hoot";

import { checkboxesOnScreen } from "@tuqui_assistant/services/tuqui_assistant_service";

/**
 * Qué casillas ve la persona en Ajustes, y cuáles ya están tildadas.
 *
 * ES UNA CAPACIDAD QUE FALTABA, no un comportamiento a corregir. El contexto le
 * mandaba al asistente los NOMBRES de 109 campos y, de cada uno, sólo si era
 * invisible / de sólo lectura / obligatorio — ningún valor. Y omitía los campos
 * normales, porque un campo sin nada raro producía un estado vacío y se
 * descartaba: la casilla de la que dependía toda la tarea no estaba en la lista.
 *
 * Medido dos veces con el modelo de producción: una vez concluyó "ya está
 * habilitado" sin poder verificarlo (estaba apagado), y otra mandó a tildar una
 * casilla YA TILDADA y la persona la apagó.
 */

function pantalla(html) {
    const root = document.createElement("div");
    root.innerHTML = html;
    getFixture().appendChild(root);
    return root;
}

describe("checkboxesOnScreen", () => {
    test("dice el nombre técnico y si YA está tildada", () => {
        const root = pantalla(`
            <div class="o_setting_box">
                <div class="o_field_widget" name="group_stock_multi_locations">
                    <input type="checkbox" checked/>
                </div>
                <label>Storage Locations</label>
            </div>
            <div class="o_setting_box">
                <div class="o_field_widget" name="group_stock_adv_location">
                    <input type="checkbox"/>
                </div>
                <label>Multi-Step Routes</label>
            </div>
        `);
        expect(checkboxesOnScreen(root)).toEqual({
            group_stock_multi_locations: true,
            group_stock_adv_location: false,
        });
    });

    test("una casilla sin nombre técnico no viaja", () => {
        // Sin nombre el asistente no puede hablar de ella ni proponerle un
        // cambio: mandarla sería ruido que además ocupa el contexto.
        const root = pantalla(`
            <div class="o_setting_box"><input type="checkbox" checked/></div>
        `);
        expect(checkboxesOnScreen(root)).toEqual({});
    });

    test("fuera de un bloque de ajustes no se lee nada", () => {
        // Un checkbox suelto en una lista o en un formulario común no es una
        // opción de configuración, y contarlo mezclaría dos cosas distintas.
        const root = pantalla(`
            <div class="o_list_view">
                <div class="o_field_widget" name="algo"><input type="checkbox" checked/></div>
            </div>
        `);
        expect(checkboxesOnScreen(root)).toEqual({});
    });

    test("sin casillas devuelve un objeto vacío, no null", () => {
        // Quien lo consume distingue "no hay casillas" de "no se pudo leer", y
        // eso sólo funciona si la ausencia tiene una forma estable.
        expect(checkboxesOnScreen(pantalla(`<div class="o_form_view"></div>`))).toEqual({});
    });
});
