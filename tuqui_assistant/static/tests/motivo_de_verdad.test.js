/** El motivo real de un error del cliente web, desenvuelto.
 *
 *  El caso que lo motivó, medido en un Odoo real: el agente quiso abrir la
 *  configuración del sitio web en una base sin ese módulo y la persona leyó
 *  *"An error occured in the owl lifecycle (see this Error's cause property)"* —
 *  un cartel que la manda a mirar una propiedad de un objeto de JavaScript. El
 *  motivo de verdad estaba un nivel más abajo.
 */

import { describe, expect, test } from "@odoo/hoot";
import { motivoDeVerdad } from "@tuqui_assistant/services/tuqui_assistant_service";

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
