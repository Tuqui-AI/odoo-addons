/** @odoo-module **/
import { describe, expect, getFixture, test } from "@odoo/hoot";

import { settingsSectionsOnScreen } from "@tuqui_assistant/services/tuqui_assistant_service";

/**
 * Qué secciones ofrece la pantalla de Ajustes.
 *
 * El dato existe para que el nombre de la sección no se adivine. Pedir una que
 * no está NO es un error para Odoo: abre Ajustes sin ninguna elegida, que se lee
 * como "te llevé" de los dos lados mientras la persona mira una pantalla que no
 * contesta nada.
 */

function pantalla(html) {
    const root = document.createElement("div");
    root.innerHTML = html;
    getFixture().appendChild(root);
    return root;
}

describe("settingsSectionsOnScreen", () => {
    test("lee las claves que la propia solapa usa", () => {
        const root = pantalla(`
            <div class="settings_tab">
                <div data-key="general_settings">General Settings</div>
                <div data-key="account">Invoicing</div>
                <div data-key="sale_management">Sales</div>
            </div>
        `);
        expect(settingsSectionsOnScreen(root)).toEqual([
            "general_settings",
            "account",
            "sale_management",
        ]);
    });

    test("no confunde la lista con los módulos instalados", () => {
        // EL CASO MEDIDO contra una base real: `sale` está instalado y NO hay
        // sección `sale`; la que existe se llama `sale_management`. Por eso la
        // lista se lee de la pantalla y no de `ir.module.module` — validar contra
        // los módulos instalados habría dejado pasar exactamente este error.
        const root = pantalla(`
            <div class="settings_tab"><div data-key="sale_management">Sales</div></div>
        `);
        const hay = settingsSectionsOnScreen(root);
        expect(hay).toInclude("sale_management");
        expect(hay).not.toInclude("sale");
    });

    test("fuera de Ajustes no devuelve nada", () => {
        // Un `data-key` suelto en cualquier otra pantalla no es una sección de
        // ajustes: sin acotar a la solapa, el contexto de un formulario común
        // viajaría diciendo que hay secciones que no existen.
        const root = pantalla(`<div class="o_form_view"><div data-key="algo">x</div></div>`);
        expect(settingsSectionsOnScreen(root)).toEqual([]);
    });

    test("una clave repetida cuenta una sola vez", () => {
        // Odoo dibuja la solapa dos veces —la de escritorio y la de mobile—, así
        // que sin esto la lista sale duplicada y ocupa el doble en un contexto
        // que se corta por tamaño.
        const root = pantalla(`
            <div class="settings_tab">
                <div data-key="general_settings">General</div>
                <div data-key="general_settings">General</div>
            </div>
        `);
        expect(settingsSectionsOnScreen(root)).toEqual(["general_settings"]);
    });
});
