/** @odoo-module **/
import { beforeEach, describe, expect, test } from "@odoo/hoot";
import {
    contains,
    defineModels,
    fields,
    getService,
    models,
    mountView,
} from "@web/../tests/web_test_helpers";

/**
 * Tests de INTERACCIÓN REAL del propose-apply, contra el framework de Odoo.
 *
 * Sin LLM: la propuesta `{campo: valor}` se escribe a mano. El modelo la PRODUCE,
 * no es lo que hay que testear — meterlo sólo agrega costo y flakiness.
 *
 * Pero sí con Odoo de verdad donde importa: se monta un form view real, con el
 * record model real, el StaticList real y el onchange real. Esa es exactamente la
 * capa donde vivieron las fallas del propose-apply (tuplas comando de x2many,
 * líneas nuevas que no disparan onchange, m2o pasado como entero pelado), y la
 * que un mock no reproduce.
 *
 * Ver la spec `assistant-en-odoo-confiabilidad` §Tests reales — tarea 72555.
 */

class Partner extends models.Model {
    _name = "res.partner";

    name = fields.Char();
    email = fields.Char();
    ref = fields.Char({ readonly: true });
    active = fields.Boolean({ default: true });
    parent_id = fields.Many2one({ relation: "res.partner" });
    child_ids = fields.One2many({ relation: "res.partner", relation_field: "parent_id" });

    _records = [
        { id: 1, name: "Acme", email: "acme@example.com", ref: "R-1" },
        { id: 2, name: "Beta", email: "beta@example.com" },
    ];
}

defineModels([Partner]);

const FORM_ARCH = `
    <form>
        <field name="name"/>
        <field name="email"/>
        <field name="ref"/>
        <field name="parent_id"/>
        <field name="child_ids">
            <list editable="bottom">
                <field name="name"/>
            </list>
        </field>
    </form>`;

async function mountPartnerForm() {
    await mountView({ type: "form", resModel: "res.partner", resId: 1, arch: FORM_ARCH });
    return getService("tuquiAssistant");
}

describe("applyProposal — campos simples", () => {
    let assistant;
    beforeEach(async () => {
        assistant = await mountPartnerForm();
    });

    test("aplica un char y deja el registro sucio", async () => {
        const ok = await assistant.applyProposal({ name: "Acme SA" });
        expect(ok).toBe(true);
        // El valor tiene que estar EN LA UI, no sólo en el datapoint: lo que el
        // usuario revisa antes de Guardar es la pantalla.
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");
        const record = assistant.getActiveRecord();
        expect(record.dirty).toBe(true);
    });

    test("aplica varios campos de una", async () => {
        await assistant.applyProposal({ name: "Acme SA", email: "nuevo@example.com" });
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");
        expect(".o_field_widget[name=email] input").toHaveValue("nuevo@example.com");
    });
});

describe("applyProposal — lo que NO se puede aplicar", () => {
    let assistant;
    beforeEach(async () => {
        assistant = await mountPartnerForm();
    });

    test("un campo inexistente se descarta y no revienta", async () => {
        // Antes esto reventaba `_update` con un error JS sin atrapar.
        const ok = await assistant.applyProposal({ campo_que_no_existe: "x" });
        expect(ok).toBe(false);
    });

    test("un campo readonly se descarta, y lo aplicable igual se aplica", async () => {
        const ok = await assistant.applyProposal({ name: "Acme SA", ref: "R-999" });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");
        // El readonly quedó como estaba: nunca un "ok" silencioso sobre un cambio
        // que no ocurrió.
        expect(assistant.getActiveRecord().data.ref).toBe("R-1");
    });

    test("una propuesta que no es un objeto se rechaza", async () => {
        expect(await assistant.applyProposal(["name", "Acme"])).toBe(false);
        expect(await assistant.applyProposal(null)).toBe(false);
    });
});

describe("applyProposal — many2one", () => {
    test("un id pelado setea el m2o y muestra su nombre", async () => {
        const assistant = await mountPartnerForm();
        // Un entero pelado dejaba el campo visualmente vacío aunque el id estuviera.
        const ok = await assistant.applyProposal({ parent_id: 2 });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=parent_id] input").toHaveValue("Beta");
    });
});

describe("applyProposal — one2many", () => {
    test("una línea nueva como objeto plano se agrega igual", async () => {
        const assistant = await mountPartnerForm();
        // La forma "amigable" que manda un LLM: lista de objetos planos, no
        // tuplas-comando. Antes era un NO-OP SILENCIOSO — `_applyCommands` no
        // matchea ningún case y `_update` igual resolvía "ok".
        const ok = await assistant.applyProposal({ child_ids: [{ name: "Sucursal" }] });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(1);
    });

    test("una línea nueva como tupla-comando también", async () => {
        const assistant = await mountPartnerForm();
        const ok = await assistant.applyProposal({
            child_ids: [[0, false, { name: "Sucursal" }]],
        });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(1);
    });
});

describe("contexto vivo", () => {
    test("el contexto arranca en el record abierto", async () => {
        const assistant = await mountPartnerForm();
        const ctx = assistant.getContextPayload();
        expect(ctx.kind).toBe("record");
        expect(ctx.model).toBe("res.partner");
        expect(ctx.resId).toBe(1);
        expect(ctx.fields.name).toBe("Acme");
    });

    test("los valores SIN GUARDAR viajan en el contexto", async () => {
        const assistant = await mountPartnerForm();
        await contains(".o_field_widget[name=name] input").edit("Editado a mano");
        assistant.refreshRecordContext();
        const ctx = assistant.getContextPayload();
        // Es todo el punto del contexto vivo: el assistant trabaja sobre lo que el
        // usuario está escribiendo, no sobre lo último guardado.
        expect(ctx.fields.name).toBe("Editado a mano");
        expect(ctx.dirty).toBe(true);
    });

    test("la revisión sube cuando los valores cambian, y no cuando no", async () => {
        const assistant = await mountPartnerForm();
        const before = assistant.getContextPayload().revision;
        // Sin edición: un focusout no debe gastar una revisión.
        expect(assistant.refreshRecordContext()).toBe(false);
        expect(assistant.getContextPayload().revision).toBe(before);

        await contains(".o_field_widget[name=name] input").edit("Otro nombre");
        expect(assistant.refreshRecordContext()).toBe(true);
        expect(assistant.getContextPayload().revision).not.toBe(before);
    });
});

describe("conflicto de revisión", () => {
    test("no se pisa un campo que el usuario cambió después de la propuesta", async () => {
        const assistant = await mountPartnerForm();
        // El agente lee el contexto en la revisión N…
        const baseRevision = assistant.getContextPayload().revision;
        // …y mientras piensa, el usuario edita ese mismo campo a mano.
        await contains(".o_field_widget[name=name] input").edit("Lo que escribió el usuario");
        assistant.refreshRecordContext();

        const ok = await assistant.applyProposal({ name: "Lo que propuso el agente" }, { baseRevision });
        expect(ok).toBe(false);
        // Gana el usuario: su valor sigue ahí.
        expect(".o_field_widget[name=name] input").toHaveValue("Lo que escribió el usuario");
    });

    test("sin baseRevision se aplica como siempre (SPA viejo)", async () => {
        const assistant = await mountPartnerForm();
        await contains(".o_field_widget[name=name] input").edit("Lo del usuario");
        assistant.refreshRecordContext();
        const ok = await assistant.applyProposal({ name: "Lo del agente" });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=name] input").toHaveValue("Lo del agente");
    });

    test("un campo distinto al editado sí se aplica", async () => {
        const assistant = await mountPartnerForm();
        const baseRevision = assistant.getContextPayload().revision;
        await contains(".o_field_widget[name=name] input").edit("Lo del usuario");
        assistant.refreshRecordContext();
        // El conflicto es POR CAMPO, no por registro: tocar `name` no puede
        // bloquear una propuesta sobre `email`.
        const ok = await assistant.applyProposal({ email: "otro@example.com" }, { baseRevision });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=email] input").toHaveValue("otro@example.com");
    });
});
