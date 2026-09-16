/** @odoo-module **/
import { describe, expect, getFixture, test } from "@odoo/hoot";

import {
    didItLandOnTheSection,
    openSettingsSection,
    settingsSectionsOnScreen,
} from "@tuqui_assistant/services/tuqui_assistant_service";

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

describe("openSettingsSection", () => {
    test("dice cuál está abierta, no sólo cuáles hay", () => {
        // EL CASO MEDIDO. Ajustes es UNA pantalla con muchas secciones, y el
        // contexto la identifica por modelo —`res.config.settings`— para todas.
        // Parado en la general y parado en contabilidad se ven idénticos desde
        // afuera, así que quien necesita contabilidad lee "ya estás en ajustes" y
        // no navega. Diez conversaciones con el mismo mensaje inicial: llegó a la
        // sección correcta 3 veces, y las 7 que fallaron arrancaban en Ajustes.
        const root = pantalla(`
            <div class="settings_tab">
                <div data-key="general_settings" class="selected">General Settings</div>
                <div data-key="account">Invoicing</div>
            </div>
        `);
        expect(openSettingsSection(root)).toBe("general_settings");
    });

    test("fuera de Ajustes no hay ninguna abierta", () => {
        const root = pantalla(`<div class="o_form_view"><div data-key="algo" class="selected">x</div></div>`);
        expect(openSettingsSection(root)).toBe(null);
    });

    test("y en Ajustes sin nada elegido tampoco inventa una", () => {
        // Pasa de verdad: pedir una sección que no existe abre Ajustes sin
        // ninguna seleccionada. Devolver la primera sería afirmar algo falso.
        const root = pantalla(`
            <div class="settings_tab"><div data-key="account">Invoicing</div></div>
        `);
        expect(openSettingsSection(root)).toBe(null);
    });
});

describe("didItLandOnTheSection", () => {
    test("una sección que la pantalla no ofrece NO es una llegada", () => {
        // EL CASO MEDIDO, y el que hace falta un test para creer: Odoo abre
        // Ajustes igual, sin ninguna sección elegida, así que el despacho sale
        // bien. Sin esta decisión el chat decía "listo, ya abrí la pantalla de
        // configuración de AFIP" sobre una pantalla que no contestaba nada.
        const llegada = didItLandOnTheSection("l10n_ar", ["general_settings", "account"]);
        expect(llegada.ok).toBe(false);
        expect(llegada.reason).toBe("no_such_section");
        // Y se dice QUÉ secciones hay: sin eso el asistente vuelve a probar a
        // ciegas, que es lo que se midió cuatro veces seguidas.
        expect(llegada.detail).toBe("general_settings, account");
    });

    test("la que sí está es una llegada", () => {
        const llegada = didItLandOnTheSection("account", ["general_settings", "account"]);
        expect(llegada.ok).toBe(true);
        expect(llegada.reason).toBe("opened");
        expect(llegada.detail).toBe("account");
    });

    test("no saber qué ofrece la pantalla no es una negativa", () => {
        // El borde que no hay que convertir en pesimismo: una lista vacía es
        // "no sé" —la pantalla no se dibujó, o no es la de Ajustes— y no prueba
        // que la sección falte. Negar acá rechazaría navegaciones que sí
        // funcionaron, que es el error más caro de los dos.
        expect(didItLandOnTheSection("account", []).ok).toBe(true);
    });

    test("sin sección pedida, Ajustes generales es la llegada", () => {
        const llegada = didItLandOnTheSection(undefined, []);
        expect(llegada.ok).toBe(true);
        expect(llegada.detail).toBe("general_settings");
    });
});
