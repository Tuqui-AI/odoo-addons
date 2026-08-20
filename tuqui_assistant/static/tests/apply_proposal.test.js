/** @odoo-module **/
import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
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
 * Cómo correrlos (el runner vive en `web`, no acá — ver .github/workflows/js-tests.yml):
 *
 *   odoo -d <db> -i tuqui_assistant -u web --test-enable --stop-after-init \
 *        --test-tags "/web:WebSuite.test_unit_desktop[@tuqui_assistant]"
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
        // `child_ids` se declara explícito: el mock NO lo puebla desde la relación
        // inversa, así que sin esto la lista arranca vacía y un LINK siempre la
        // hace crecer — que es justo lo contrario del caso que hay que probar.
        { id: 1, name: "Acme", email: "acme@example.com", ref: "R-1", child_ids: [3] },
        { id: 2, name: "Beta", email: "beta@example.com" },
        { id: 3, name: "Sucursal" },
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

/**
 * Aplica una propuesta y espera el re-render.
 *
 * `applyProposal` resuelve cuando el record model ya tiene los valores, pero OWL
 * pinta recién en el frame siguiente. Sin este await las aserciones leen la
 * pantalla vieja — y lo que hay que verificar acá es justamente lo que el usuario
 * VE antes de decidir si guarda.
 */
async function applyAndRender(assistant, changes, options) {
    const ok = await assistant.applyProposal(changes, options);
    await animationFrame();
    return ok;
}

describe("applyProposal — campos simples", () => {
    let assistant;
    beforeEach(async () => {
        assistant = await mountPartnerForm();
    });

    test("aplica un char y deja el registro sucio", async () => {
        const ok = await applyAndRender(assistant, { name: "Acme SA" });
        expect(ok).toBe(true);
        // El valor tiene que estar EN LA UI, no sólo en el datapoint: lo que el
        // usuario revisa antes de Guardar es la pantalla.
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");
        expect(assistant.getActiveRecord().dirty).toBe(true);
    });

    test("aplica varios campos de una", async () => {
        await applyAndRender(assistant, { name: "Acme SA", email: "nuevo@example.com" });
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
        expect(await applyAndRender(assistant, { campo_que_no_existe: "x" })).toBe(false);
    });

    test("un campo readonly se descarta, y lo aplicable igual se aplica", async () => {
        const ok = await applyAndRender(assistant, { name: "Acme SA", ref: "R-999" });
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
        const ok = await applyAndRender(assistant, { parent_id: 2 });
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
        const ok = await applyAndRender(assistant, { child_ids: [{ name: "Nueva" }] });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(2);
    });

    test("una línea nueva como tupla-comando también", async () => {
        const assistant = await mountPartnerForm();
        const ok = await applyAndRender(assistant, {
            child_ids: [[0, false, { name: "Nueva" }]],
        });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(2);
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
        const ctx = assistant.getContextPayload();
        // Es todo el punto del contexto vivo: el assistant trabaja sobre lo que el
        // usuario está escribiendo, no sobre lo último guardado.
        expect(ctx.fields.name).toBe("Editado a mano");
        expect(ctx.dirty).toBe(true);
    });

    test("editar un campo publica una revisión nueva sin que nadie la pida", async () => {
        const assistant = await mountPartnerForm();
        const before = assistant.getContextPayload().revision;
        await contains(".o_field_widget[name=name] input").edit("Otro nombre");
        // Nadie llama a refreshRecordContext acá: lo hace el FormController al
        // salir del campo. Que la revisión suba sola es el contrato del contexto
        // vivo — si esto se rompe, el assistant vuelve a razonar sobre lo guardado.
        expect(assistant.getContextPayload().revision).not.toBe(before);
    });

    test("salir de un campo sin editar no quema una revisión", async () => {
        const assistant = await mountPartnerForm();
        const before = assistant.getContextPayload().revision;
        // Un focusout sin edición: misma firma de valores, no se re-publica.
        expect(assistant.refreshRecordContext()).toBe(false);
        expect(assistant.getContextPayload().revision).toBe(before);
    });
});

describe("conflicto de revisión", () => {
    test("no se pisa un campo que el usuario cambió después de la propuesta", async () => {
        const assistant = await mountPartnerForm();
        // El agente lee el contexto en la revisión N…
        const baseRevision = assistant.getContextPayload().revision;
        // …y mientras piensa, el usuario edita ese mismo campo a mano.
        await contains(".o_field_widget[name=name] input").edit("Lo que escribió el usuario");

        const ok = await applyAndRender(
            assistant,
            { name: "Lo que propuso el agente" },
            { baseRevision }
        );
        expect(ok).toBe(false);
        // Gana el usuario: su valor sigue ahí.
        expect(".o_field_widget[name=name] input").toHaveValue("Lo que escribió el usuario");
    });

    test("sin baseRevision se aplica como siempre (SPA viejo)", async () => {
        const assistant = await mountPartnerForm();
        await contains(".o_field_widget[name=name] input").edit("Lo del usuario");
        const ok = await applyAndRender(assistant, { name: "Lo del agente" });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=name] input").toHaveValue("Lo del agente");
    });

    test("un campo distinto al editado sí se aplica", async () => {
        const assistant = await mountPartnerForm();
        const baseRevision = assistant.getContextPayload().revision;
        await contains(".o_field_widget[name=name] input").edit("Lo del usuario");
        // El conflicto es POR CAMPO, no por registro: tocar `name` no puede
        // bloquear una propuesta sobre `email`.
        const ok = await applyAndRender(assistant, { email: "otro@example.com" }, { baseRevision });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=email] input").toHaveValue("otro@example.com");
    });
});

describe("applyProposal — lo que se aplicó a medias", () => {
    /**
     * Una propuesta puede entrar en parte: los escalares se aplican y la línea
     * nueva no. `applyProposal` avisa y devuelve false, pero lo que SÍ entró está
     * en el formulario, así que el contexto publicado tiene que reflejarlo. Si se
     * queda en la revisión previa, el assistant sigue razonando sobre valores que
     * él mismo ya cambió.
     *
     * Para provocar el "no creció": un LINK a una fila que YA está en la lista.
     * El campo se marca como "se espera que crezca" y el conteo no sube, que es
     * exactamente la señal de "pedí agregar y no se agregó".
     *
     * Y el formulario arranca SUCIO a propósito. Con un record limpio, aplicar lo
     * ensucia, y esa transición ya hace que el FormController re-publique el
     * contexto por su cuenta — tapando el agujero sin arreglarlo. Sobre un
     * formulario que el usuario ya venía editando no hay transición que lo tape,
     * y es además el caso realista: se propone sobre algo a medio llenar.
     */
    test("el contexto refleja lo que sí entró, aunque el form ya estuviera sucio", async () => {
        const assistant = await mountPartnerForm();
        await contains(".o_field_widget[name=email] input").edit("editado@example.com");
        const revisionBefore = assistant.getContextPayload().revision;

        const ok = await applyAndRender(assistant, { name: "Acme SA", child_ids: [[4, 3]] });
        // La línea no se agregó (ya estaba), así que la propuesta NO entró entera.
        expect(ok).toBe(false);
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(1);
        // …pero el nombre sí, y eso es lo que hay que re-publicar.
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");

        const ctx = assistant.getContextPayload();
        expect(ctx.fields.name).toBe("Acme SA");
        expect(ctx.revision).not.toBe(revisionBefore);
    });
});
