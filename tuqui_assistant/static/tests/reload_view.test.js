/** @odoo-module **/
import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
import {
    contains,
    defineModels,
    fields,
    getService,
    models,
    MockServer,
    mountView,
} from "@web/../tests/web_test_helpers";

/**
 * `reloadView`: releer la vista después de que el assistant escribió POR ATRÁS.
 *
 * El caso: el agente usa una tool que escribe en Odoo (no una propuesta sobre el
 * form abierto), así que el dato cambia en el servidor y la pantalla sigue
 * mostrando lo viejo. Al terminar el turno el SPA pide un `reload`.
 *
 * Lo que se fija acá es el GUARD, no la feature. Un reload sobre un formulario
 * con cambios sin guardar los descarta sin preguntar: el usuario pierde lo que
 * estaba escribiendo y se entera tarde, o no se entera. Por eso `reloadView`
 * se niega cuando el record está sucio, y por eso eso tiene un test.
 *
 * Cómo correrlos: ver el encabezado de `apply_proposal.test.js`.
 */

class Partner extends models.Model {
    _name = "res.partner";

    name = fields.Char();
    email = fields.Char();

    _records = [{ id: 1, name: "Acme", email: "acme@example.com" }];
}

defineModels([Partner]);

const FORM_ARCH = `<form><field name="name"/><field name="email"/></form>`;

/** Escribe en el servidor sin tocar el form: simula el write "por atrás". */
function writeBehindTheForm(values) {
    MockServer.env["res.partner"].write([1], values);
}

describe("reloadView", () => {
    let assistant;
    beforeEach(async () => {
        await mountView({ type: "form", resModel: "res.partner", resId: 1, arch: FORM_ARCH });
        assistant = getService("tuquiAssistant");
    });

    test("trae a la pantalla lo que el assistant escribió por atrás", async () => {
        writeBehindTheForm({ name: "Acme (actualizado por el assistant)" });
        // Sin reload la pantalla se queda con el valor viejo — que es justo el
        // síntoma reportado: el dato ya está en Odoo pero no se ve.
        expect(".o_field_widget[name=name] input").toHaveValue("Acme");

        expect(await assistant.reloadView()).toBe(true);
        await animationFrame();
        expect(".o_field_widget[name=name] input").toHaveValue("Acme (actualizado por el assistant)");
    });

    test("NO pisa un formulario con cambios sin guardar", async () => {
        await contains(".o_field_widget[name=name] input").edit("Lo que el usuario venía escribiendo");
        writeBehindTheForm({ email: "otro@example.com" });

        // Recargar acá descartaría la edición del usuario sin avisar. Preferimos
        // no refrescar y decirlo (el servicio muestra un warning) antes que
        // perder algo que el usuario todavía no guardó.
        expect(await assistant.reloadView()).toBe(false);
        await animationFrame();
        expect(".o_field_widget[name=name] input").toHaveValue("Lo que el usuario venía escribiendo");
    });

    test("una propuesta DESPUÉS del reload llega a la pantalla", async () => {
        writeBehindTheForm({ email: "servidor@example.com" });
        await assistant.reloadView();
        await animationFrame();

        // El reload reemplaza el record del modelo. Si quedara publicado el
        // viejo, esto escribiría en un datapoint desconectado: la pantalla no
        // cambia y `applyProposal` igual devuelve true. Un ok silencioso, que es
        // el defecto que esta tarea existe para eliminar.
        expect(await assistant.applyProposal({ name: "Propuesto después del reload" })).toBe(true);
        await animationFrame();
        expect(".o_field_widget[name=name] input").toHaveValue("Propuesto después del reload");
    });

    test("el contexto que ve el assistant queda al día después del reload", async () => {
        writeBehindTheForm({ name: "Nombre nuevo" });
        await assistant.reloadView();
        await animationFrame();
        // Si el contexto siguiera con el valor viejo, el próximo turno razonaría
        // sobre datos que ya no existen.
        expect(assistant.getContextPayload().fields.name).toBe("Nombre nuevo");
    });
});
