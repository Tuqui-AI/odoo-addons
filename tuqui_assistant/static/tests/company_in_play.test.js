/** @odoo-module **/
import { describe, expect, test } from "@odoo/hoot";
import { patchWithCleanup } from "@web/../tests/web_test_helpers";
import { user } from "@web/core/user";

import { companyInPlay } from "@tuqui_assistant/services/tuqui_assistant_service";

/**
 * Qué compañía viaja en el contexto de la pantalla.
 *
 * Es el dato que le faltaba al asistente en el caso que originó esto: media
 * configuración de Odoo es dependiente de compañía, así que la MISMA pantalla
 * muestra bloques distintos según cuál esté activa. Sin esto, el asistente
 * explica una sección que la persona no tiene y no tiene cómo darse cuenta.
 */

function conCompanias(activa, permitidas) {
    patchWithCleanup(user, {
        get activeCompany() {
            return activa;
        },
        allowedCompanies: permitidas,
    });
}

describe("companyInPlay", () => {
    test("dice cuál está activa", () => {
        conCompanias({ id: 7, name: "(AR) Responsable Inscripto" }, [
            { id: 7, name: "(AR) Responsable Inscripto" },
        ]);
        expect(companyInPlay()).toEqual({ active: "(AR) Responsable Inscripto" });
    });

    test("y con cuáles otras puede laburar — la mitad que discrimina", () => {
        // EL CASO MEDIDO. La compañía activa era de Estados Unidos y la
        // localización argentina sólo se dibuja para una compañía argentina, así
        // que el botón que había que apretar no podía estar en la pantalla.
        // Saber sólo la activa dice "falta algo"; saber que hay una argentina
        // disponible dice QUÉ HACER.
        conCompanias({ id: 1, name: "My Company (San Francisco)" }, [
            { id: 1, name: "My Company (San Francisco)" },
            { id: 7, name: "(AR) Responsable Inscripto" },
            { id: 6, name: "(AR) Monotributista" },
        ]);
        expect(companyInPlay()).toEqual({
            active: "My Company (San Francisco)",
            canSwitchTo: ["(AR) Responsable Inscripto", "(AR) Monotributista"],
        });
    });

    test("con muchas compañías manda un puñado y el total", () => {
        // El objeto entero se serializa y se CORTA en 8000 caracteres del otro
        // lado, y esto viaja primero: una base con cincuenta compañías se comería
        // el resto del contexto. El total deja claro que la lista está cortada.
        const muchas = [{ id: 1, name: "La activa" }];
        for (let i = 2; i <= 15; i++) {
            muchas.push({ id: i, name: `Empresa ${i}` });
        }
        conCompanias({ id: 1, name: "La activa" }, muchas);

        const r = companyInPlay();
        expect(r.canSwitchTo).toHaveLength(8);
        expect(r.canSwitchToTotal).toBe(14);
    });

    test("sin compañías no devuelve un objeto a medias", () => {
        // Una clave `company` con un nombre vacío adentro es peor que ausente: del
        // otro lado se lee como un dato y no como "no sé".
        conCompanias(undefined, []);
        expect(companyInPlay()).toBe(null);
    });

    test("una compañía sin nombre tampoco cuenta como alternativa", () => {
        conCompanias({ id: 1, name: "La activa" }, [
            { id: 1, name: "La activa" },
            { id: 2 },
            { id: 3, name: "" },
        ]);
        expect(companyInPlay()).toEqual({ active: "La activa" });
    });
});
