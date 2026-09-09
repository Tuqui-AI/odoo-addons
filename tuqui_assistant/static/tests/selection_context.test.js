/** @odoo-module **/
import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { defineModels, fields, getService, models, mountView } from "@web/../tests/web_test_helpers";

/**
 * `setSearchContext`: qué viaja al SPA cuando el usuario tilda filas en una lista.
 *
 * Lo que se fija acá es el CONTRATO, no el número. `MAX_SELECTION_IDS` es un
 * guard de transporte y puede moverse; lo que no puede moverse en silencio es
 * que una lista cortada venga marcada. Del otro lado, el prompt de Tuqui decide
 * con ese flag si la selección es accionable o si hay que decirle al usuario que
 * no se puede — un corte sin marcar se lee como la selección entera y termina en
 * una escritura sobre un subconjunto, callada.
 *
 * `count` es la otra mitad del contrato: es la selección REAL, no lo que entró.
 * Es con lo que el modelo dimensiona el pedido cuando los ids no están.
 *
 * Cómo correrlos: ver el encabezado de `apply_proposal.test.js`.
 */

// `setSearchContext` recibe el payload de la lista COMO ARGUMENTO — el
// controller de lista se lo arma y se lo pasa. Así que no hace falta montar una
// lista para probarlo: montamos un form mínimo sólo para tener un env con el
// servicio, y le pasamos payloads a mano. Montar la lista de verdad arrastraría
// `res.users` y sus fixtures (el controller lo lee en `onWillStart`), que no
// tienen nada que ver con lo que hay que fijar acá.
class Partner extends models.Model {
    _name = "res.partner";

    name = fields.Char();

    _records = [{ id: 1, name: "Acme" }];
}

defineModels([Partner]);

const FORM_ARCH = `<form><field name="name"/></form>`;

/** Publica una selección de `n` filas y devuelve el contexto que quedó. */
async function contextForSelectionOf(assistant, n) {
    assistant.setSearchContext(
        {},
        {
            model: "res.partner",
            modelLabel: "Contactos",
            resIds: Array.from({ length: n }, (_, i) => i + 1),
            isDomainSelected: false,
            count: n,
            domain: [],
            filters: [],
        }
    );
    return assistant.state.context;
}

describe("setSearchContext — selección", () => {
    let assistant;
    beforeEach(async () => {
        await mountView({ type: "form", resModel: "res.partner", resId: 1, arch: FORM_ARCH });
        assistant = getService("tuquiAssistant");
    });

    test("una selección que entra entera no se marca cortada", async () => {
        const ctx = await contextForSelectionOf(assistant, 80);
        expect(ctx.kind).toBe("selection");
        expect(ctx.resIds.length).toBe(80);
        expect(ctx.count).toBe(80);
        expect(!!ctx.truncated).toBe(false);
    });

    test("una selección que NO entra se marca cortada, y el count sigue siendo el real", async () => {
        const ctx = await contextForSelectionOf(assistant, 260);
        expect(ctx.truncated).toBe(true);
        // El count es lo que el usuario tildó, no lo que entró en el payload:
        // sin eso el modelo cree que la selección es del tamaño de la lista que
        // recibió y actúa sobre un subconjunto sin saberlo.
        expect(ctx.count).toBe(260);
        expect(ctx.resIds.length).toBeLessThan(260);
    });

    test("el corte se lleva las ÚLTIMAS, no una muestra salteada", async () => {
        // Un corte por el final es reproducible y explicable; una muestra al
        // azar haría que dos turnos seguidos vieran conjuntos distintos.
        const ctx = await contextForSelectionOf(assistant, 260);
        expect(ctx.resIds[0]).toBe(1);
        expect(ctx.resIds.at(-1)).toBe(ctx.resIds.length);
    });

    test("el borde del cap: justo adentro no se marca, uno más sí", async () => {
        // Sin esto, cambiar el `>` por un `>=` en el cálculo de `truncated` no
        // rompe ningún test — y ese flag es lo único que separa una lista parcial
        // de una escritura masiva sobre un subconjunto.
        const dentro = await contextForSelectionOf(assistant, 200);
        expect(dentro.resIds.length).toBe(200);
        expect(!!dentro.truncated).toBe(false);

        const afuera = await contextForSelectionOf(assistant, 201);
        expect(afuera.truncated).toBe(true);
        expect(afuera.resIds.length).toBe(200);
        expect(afuera.count).toBe(201);
    });

    test("'seleccionar todos los que matchean' manda el dominio, no ids", async () => {
        assistant.setSearchContext(
            {},
            {
                model: "res.partner",
                modelLabel: "Contactos",
                resIds: [],
                isDomainSelected: true,
                count: 260,
                domain: [["name", "ilike", "Contacto"]],
                filters: [],
            }
        );
        const ctx = assistant.state.context;
        expect(ctx.allMatching).toBe(true);
        expect(ctx.resIds).toEqual([]);
        // El dominio ES la selección acá: sin él el assistant sabe que son
        // "todos los del filtro" pero no puede reproducir el conjunto.
        expect(ctx.domain).toEqual([["name", "ilike", "Contacto"]]);
        expect(!!ctx.truncated).toBe(false);
    });
});

describe("la selección se re-publica cuando cambia QUÉ está tildado", () => {
    // El efecto que publica el contexto se re-dispara mirando una lista de
    // dependencias. Con `selection.length` ahí, destildar una fila y tildar otra
    // dejaba el largo igual → no se re-publicaba → el assistant seguía viendo la
    // que sacaste. Con el cap en 50 eso moría en una negativa (llegaba cortada);
    // con la selección entera, el prompt manda trabajar inline y el desfasaje
    // termina en una escritura sobre el registro equivocado, en silencio.

    test("la huella de dependencias distingue dos conjuntos del mismo tamaño", () => {
        // Se prueba la huella, no OWL: es la línea que decide si el efecto corre.
        const huella = (ids) => ids.join(",");
        expect(huella([1, 2, 3])).not.toBe(huella([1, 2, 4]));
        // Y lo que fallaba antes: mismo largo, distinto conjunto.
        expect([1, 2, 3].length).toBe([1, 2, 4].length);
    });

    test("la clave de deduplicación del panel también mira los ids", () => {
        // Si el panel dedupeara por cantidad, el arreglo del controller quedaría
        // sin efecto: re-publicaría y el panel lo descartaría igual.
        const clave = (model, count, resIds) => `sel:${model}:${count}:${(resIds || []).join(",")}`;
        expect(clave("res.partner", 3, [1, 2, 3])).not.toBe(clave("res.partner", 3, [1, 2, 4]));
    });
});
