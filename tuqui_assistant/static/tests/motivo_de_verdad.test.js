/** El motivo real de un error del cliente web, desenvuelto.
 *
 *  El caso que lo motivó, medido en un Odoo real: el agente quiso abrir la
 *  configuración del sitio web en una base sin ese módulo y la persona leyó
 *  *"An error occured in the owl lifecycle (see this Error's cause property)"* —
 *  un cartel que la manda a mirar una propiedad de un objeto de JavaScript. El
 *  motivo de verdad estaba un nivel más abajo.
 */

import { describe, expect, test } from "@odoo/hoot";
import { patchTranslations } from "@web/../tests/web_test_helpers";
import {
    motivoDeVerdad,
    porQueNoSePudoAbrir,
} from "@tuqui_assistant/services/tuqui_assistant_service";

const ENVOLTORIO = "An error occured in the owl lifecycle (see this Error's \"cause\" property)";

describe("motivoDeVerdad", () => {
    test("desenvuelve el error de Owl y devuelve la causa", () => {
        const real = new Error("Unknown model: 'website'");
        const envuelto = new Error(ENVOLTORIO, { cause: real });
        expect(motivoDeVerdad(envuelto)).toBe("Unknown model: 'website'");
    });

    test("baja por toda la cadena, no sólo un nivel", () => {
        const raiz = new Error("Access Denied");
        const medio = new Error(ENVOLTORIO, { cause: raiz });
        const arriba = new Error(ENVOLTORIO, { cause: medio });
        expect(motivoDeVerdad(arriba)).toBe("Access Denied");
    });

    test("un error normal pasa tal cual", () => {
        // El control negativo: si esto cambiara, el arreglo estaría rompiendo el
        // caso común para arreglar el raro.
        expect(motivoDeVerdad(new Error("No se pudo guardar"))).toBe("No se pudo guardar");
        expect(motivoDeVerdad("un string pelado")).toBe("un string pelado");
    });

    test("sin causa útil devuelve algo, no una cadena vacía", () => {
        // Peor un mensaje malo que ninguno: un cartel en blanco es
        // indistinguible de una pantalla colgada.
        expect(motivoDeVerdad(new Error(ENVOLTORIO)).length > 0).toBe(true);
    });

    test("una cadena circular no lo cuelga", () => {
        const a = new Error(ENVOLTORIO);
        const b = new Error(ENVOLTORIO, { cause: a });
        a.cause = b;
        expect(typeof motivoDeVerdad(a)).toBe("string");
    });
});

describe("porQueNoSePudoAbrir", () => {
    /** Un `orm` de mentira: lo único que se le pregunta es si el modelo existe. */
    const ormQueDice = (cuantos, registro = []) => ({
        searchCount: async (modelo, dominio) => {
            registro.push([modelo, dominio]);
            return cuantos;
        },
    });

    test("un 404 sobre un modelo que no existe se cuenta en criollo", async () => {
        // El mensaje sale por `_t`, que hasta que las traducciones no están
        // cargadas tira al usarse como string (`translation.js`).
        patchTranslations();
        // El caso del reporte: la configuración del sitio web en una base sin ese
        // módulo. Odoo contesta `404: Not Found`, que no nombra nada.
        const registro = [];
        const envuelto = new Error("An error occured in the owl lifecycle", {
            cause: new Error("404: Not Found"),
        });
        const motivo = await porQueNoSePudoAbrir(envuelto, "website", ormQueDice(0, registro));
        expect(motivo.includes("website")).toBe(true);
        expect(motivo.includes("not installed")).toBe(true);
        // Y se preguntó por el modelo, no por otra cosa.
        expect(registro[0][0]).toBe("ir.model");
    });

    test("si el modelo SÍ existe, no se inventa que falta un módulo", async () => {
        // El control negativo: un 404 puede ser una vista o una acción que no
        // está, y decir "el módulo no está instalado" sería mentir con confianza.
        const motivo = await porQueNoSePudoAbrir(new Error("404: Not Found"), "sale.order", ormQueDice(1));
        expect(motivo.includes("not installed")).toBe(false);
        expect(motivo).toBe("404: Not Found");
    });

    test("un error que ya se explica solo no gasta una consulta", async () => {
        let consultas = 0;
        const orm = { searchCount: async () => { consultas += 1; return 0; } };
        const motivo = await porQueNoSePudoAbrir(new Error("Access Denied"), "hr.employee", orm);
        expect(motivo).toBe("Access Denied");
        expect(consultas).toBe(0);
    });

    test("y si la consulta revienta se devuelve lo que había", async () => {
        const orm = { searchCount: async () => { throw new Error("sin conexión"); } };
        const motivo = await porQueNoSePudoAbrir(new Error("404: Not Found"), "website", orm);
        expect(motivo).toBe("404: Not Found");
    });
});
