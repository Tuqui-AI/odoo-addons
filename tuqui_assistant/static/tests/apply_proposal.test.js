/** @odoo-module **/
import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { animationFrame, mockTimeZone } from "@odoo/hoot-mock";
import {
    contains,
    defineModels,
    fields,
    getService,
    models,
    mountView,
    onRpc,
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
    fecha = fields.Date();
    fecha_hora = fields.Datetime();
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
        <field name="fecha"/>
        <field name="fecha_hora"/>
        <field name="child_ids">
            <list editable="bottom">
                <field name="name"/>
                <field name="fecha"/>
            </list>
        </field>
    </form>`;

async function mountPartnerForm() {
    await mountView({ type: "form", resModel: "res.partner", resId: 1, arch: FORM_ARCH });
    return getService("tuquiAssistant");
}

/** Un formulario con los tres estados que importan, y uno de ellos CONDICIONAL:
 *  `email` se esconde según el valor de otro campo, que es el caso que la
 *  definición estática del campo no puede contar. */
const FORM_ARCH_ESTADOS = `
    <form>
        <field name="name" required="1"/>
        <field name="active"/>
        <field name="email" invisible="active"/>
        <field name="ref"/>
        <field name="fecha"/>
    </form>`;

async function mountFormConEstados(resId = 1) {
    await mountView({ type: "form", resModel: "res.partner", resId, arch: FORM_ARCH_ESTADOS });
    return getService("tuquiAssistant");
}

/** Un formulario donde el que se esconde es el ANCESTRO, no el campo.
 *
 *  `email` no tiene modificador propio: el `invisible` está en el `<group>` que
 *  lo contiene. Es el caso que el evaluador de Odoo no reporta, porque ese
 *  `invisible` se compila a un `t-if` del template y nunca toca `activeFields`.
 *
 *  `fecha` es el CONTROL NEGATIVO: mismo formulario, un group sin condición.
 *  Y el `<field name="child_ids">` con su lista adentro es el borde: los campos
 *  de una vista embebida no son campos de este formulario. */
const FORM_ARCH_ANCESTRO = `
    <form>
        <field name="active"/>
        <field name="name"/>
        <group invisible="active">
            <field name="email"/>
            <field name="child_ids">
                <list editable="bottom">
                    <field name="name"/>
                </list>
            </field>
        </group>
        <group>
            <field name="fecha"/>
        </group>
    </form>`;

/** El mismo campo DOS VECES, en dos ramas mutuamente excluyentes.
 *
 *  Es un patrón común en los formularios de Odoo: dos `<div>`/`<group>` con
 *  condiciones opuestas y el mismo campo en cada uno. Con `active` en true, la
 *  primera rama está escondida y la segunda visible — así que el campo SE VE. */
const FORM_ARCH_DOS_RAMAS = `
    <form>
        <field name="active"/>
        <group invisible="active">
            <field name="email"/>
            <field name="fecha"/>
        </group>
        <group invisible="not active">
            <field name="email"/>
        </group>
    </form>`;

async function mountFormDosRamas(resId = 1) {
    await mountView({ type: "form", resModel: "res.partner", resId, arch: FORM_ARCH_DOS_RAMAS });
    return getService("tuquiAssistant");
}

/** Un campo que se esconde a SÍ MISMO, no por su grupo. */
const FORM_ARCH_INVISIBLE_PROPIO = `
    <form>
        <field name="active"/>
        <field name="email" invisible="active"/>
        <field name="fecha"/>
    </form>`;

async function mountFormInvisiblePropio(resId = 1) {
    await mountView({ type: "form", resModel: "res.partner", resId, arch: FORM_ARCH_INVISIBLE_PROPIO });
    return getService("tuquiAssistant");
}

async function mountFormConAncestro(resId = 1) {
    await mountView({ type: "form", resModel: "res.partner", resId, arch: FORM_ARCH_ANCESTRO });
    return getService("tuquiAssistant");
}

/** `email` es editable o no SEGÚN el valor de otro campo del mismo formulario.
 *  Es el caso que la definición estática del campo no puede contar: `email` no
 *  es readonly en el modelo, así que el chequeo viejo lo dejaba pasar siempre. */
const FORM_ARCH_READONLY_CONDICIONAL = `
    <form>
        <field name="active"/>
        <field name="email" readonly="active"/>
        <field name="name"/>
    </form>`;

async function mountFormReadonlyCondicional(resId = 1) {
    await mountView({
        type: "form",
        resModel: "res.partner",
        resId,
        arch: FORM_ARCH_READONLY_CONDICIONAL,
    });
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

describe("applyProposal — fechas", () => {
    let assistant;
    beforeEach(async () => {
        assistant = await mountPartnerForm();
    });

    // El modelo OWL guarda las fechas como objetos Luxon y el widget las pinta
    // con `value.toFormat()`. Antes mandábamos el string crudo del SPA: `_update`
    // lo aceptaba sin chistar y el form reventaba DESPUÉS, al renderizar, con
    // "value.toFormat is not a function" — un OwlError que se llevaba puesta la
    // vista entera. Estos tests fallan (error no verificado) sin la coerción.

    // OJO con el selector: el widget de fecha pinta un <button> cuando TIENE
    // valor y un <input> sólo cuando está vacío. Por eso los tests de "se aplicó"
    // miran el button y los de "no se aplicó" miran el input — no es un descuido.
    test("aplica una fecha y el formulario la muestra", async () => {
        const ok = await applyAndRender(assistant, { fecha: "2026-09-30" });
        expect(ok).toBe(true);
        expect(".o_field_date button").toHaveAttribute("value", "09/30/2026");
    });

    // Los dos siguientes son el MISMO instante escrito de dos formas. Con la zona
    // fijada en UTC tienen que pintar exactamente lo mismo: es lo que prueba que
    // aceptar las dos formas no es aceptar cualquier cosa.
    test("aplica un datetime en formato servidor", async () => {
        mockTimeZone(0);
        const ok = await applyAndRender(assistant, { fecha_hora: "2026-09-30 14:30:00" });
        expect(ok).toBe(true);
        expect(".o_field_datetime button").toHaveAttribute("value", "09/30/2026 14:30:00");
    });

    test("aplica un datetime en ISO con T, que es lo que el modelo VE", async () => {
        // Nuestro serializador de salida emite `toISO()`, así que el modelo
        // devuelve esa forma — y NO es la que parsea `deserializeDateTime`.
        mockTimeZone(0);
        const ok = await applyAndRender(assistant, { fecha_hora: "2026-09-30T14:30:00+00:00" });
        expect(ok).toBe(true);
        expect(".o_field_datetime button").toHaveAttribute("value", "09/30/2026 14:30:00");
    });

    test("una fecha que no se puede leer no se aplica, y el campo queda como estaba", async () => {
        const ok = await applyAndRender(assistant, { fecha: "el martes que viene" });
        expect(ok).toBe(false);
        expect(".o_field_date input").toHaveValue("");
    });

    test("una fecha ilegible no arrastra a los campos que sí se entienden", async () => {
        const ok = await applyAndRender(assistant, { name: "Acme SA", fecha: "cuando puedas" });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");
        expect(".o_field_date input").toHaveValue("");
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

describe("applyProposal — readonly condicional", () => {
    /* La rama nueva: el readonly se pregunta EVALUADO contra este registro.
     *
     * Los otros tests de descarte usan `ref`, que es readonly en la DEFINICIÓN
     * del campo — ese caso ya lo agarraba el chequeo viejo, así que pasan igual
     * con la rama nueva borrada. Estos dos no: el par mide justo el
     * discriminador, el mismo campo del mismo formulario en los dos sentidos.
     */

    test("con la condición cumplida NO se escribe, aunque la definición diga que sí", async () => {
        const assistant = await mountFormReadonlyCondicional();
        const ok = await applyAndRender(assistant, {
            email: "nuevo@example.com",
            name: "Acme SA",
        });
        // Lo aplicable se aplica igual: descartar uno no aborta la propuesta.
        expect(ok).toBe(true);
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");
        // Y el condicional quedó como estaba: nunca un "ok" silencioso sobre un
        // cambio que no ocurrió.
        expect(assistant.getActiveRecord().data.email).toBe("acme@example.com");
    });

    test("y con la condición falsa sí se escribe: mismo campo, mismo formulario", async () => {
        // El otro lado del par. Sin esto, al test de arriba lo aprobaría también
        // un chequeo que descartara `email` siempre.
        const assistant = await mountFormReadonlyCondicional();
        await contains(".o_field_widget[name=active] input").click();
        const ok = await applyAndRender(assistant, { email: "nuevo@example.com" });
        expect(ok).toBe(true);
        expect(assistant.getActiveRecord().data.email).toBe("nuevo@example.com");
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

    test("el contexto dice qué campos NO están en la pantalla", async () => {
        // El caso que motivó esto: un campo que existe, tiene valor, y Odoo no lo
        // muestra para ESTE registro. Con sólo los valores, el assistant lo daba
        // por presente y decía haberlo señalado sobre una pantalla donde no está.
        const assistant = await mountFormConEstados();
        const ctx = assistant.getContextPayload();
        expect(ctx.fields.email).toBe("acme@example.com"); // el valor está…
        expect(ctx.fieldState.email.invisible).toBe(true); // …y la pantalla, no
    });

    test("y qué campos no se pueden escribir, aunque la condición sea de este registro", async () => {
        const assistant = await mountFormConEstados();
        const ctx = assistant.getContextPayload();
        expect(ctx.fieldState.ref.readonly).toBe(true);
        expect(ctx.fieldState.name.required).toBe(true);
    });

    test("los campos normales NO ocupan lugar en el mapa", async () => {
        // Un formulario tiene decenas de campos y casi todos son comunes: mandar
        // tres booleanos por cada uno engordaría cada mensaje para no decir nada.
        const assistant = await mountFormConEstados();
        const ctx = assistant.getContextPayload();
        expect(ctx.fieldState.fecha).toBe(undefined);
    });

    test("el estado sigue la condición: si cambia el registro, cambia lo que se ve", async () => {
        // Lo que hace que esto no se pueda reemplazar por `fields_get`: la misma
        // definición da distinto según el valor del registro.
        const assistant = await mountFormConEstados();
        expect(assistant.getContextPayload().fieldState.email?.invisible).toBe(true);
        await contains(".o_field_widget[name=active] input").click();
        expect(assistant.getContextPayload().fieldState.email?.invisible).toBe(undefined);
    });

    test("un campo que esconde su ANCESTRO también viaja como oculto", async () => {
        // El agujero que este chequeo cierra: el evaluador de Odoo mira sólo el
        // modificador del propio `<field>`, así que un campo dentro de un
        // `<group invisible="…">` se reportaba como normal — el valor viajaba y
        // el modelo lo leía visible sobre algo que no está en la pantalla.
        const assistant = await mountFormConAncestro();
        const ctx = assistant.getContextPayload();
        expect(ctx.fields.email).toBe("acme@example.com"); // el valor está…
        expect(ctx.fieldState.email.invisible).toBe(true); // …y la pantalla, no
        // Control negativo: el mismo formulario, un group SIN condición.
        expect(ctx.fieldState.fecha).toBe(undefined);
    });

    test("y sigue la condición del ancestro: si cambia el registro, cambia", async () => {
        // El otro lado del par. Sin esto, al test de arriba lo aprobaría también
        // un cambio que marcara como oculto todo lo que esté dentro de un group.
        const assistant = await mountFormConAncestro();
        expect(assistant.getContextPayload().fieldState.email?.invisible).toBe(true);
        await contains(".o_field_widget[name=active] input").click();
        expect(assistant.getContextPayload().fieldState.email?.invisible).toBe(undefined);
    });

    test("los campos de una vista embebida NO son campos de este formulario", async () => {
        // El borde que hay que medir, y está armado para que pueda fallar: `name`
        // aparece DOS veces en el arch — arriba, visible, como campo del
        // formulario; y adentro de la lista del x2many, que está en el group
        // escondido. Un barrido que no distinga las dos cosas marca el `name` del
        // formulario como oculto y el modelo deja de ver un campo que la persona
        // sí tiene delante.
        const assistant = await mountFormConAncestro();
        const ctx = assistant.getContextPayload();
        expect(ctx.fieldState.email.invisible).toBe(true); // el x2many y su vecino sí
        expect(ctx.fieldState.child_ids.invisible).toBe(true);
        expect(ctx.fieldState.name).toBe(undefined); // el de arriba, NO
    });

    test("una propuesta sobre un campo que esconde su ancestro se descarta", async () => {
        // Mismo argumento que un campo de otra vista: escribirlo deja un cambio
        // que la persona no puede ver ni revisar antes de guardar.
        const assistant = await mountFormConAncestro();
        const ok = await applyAndRender(assistant, { email: "nuevo@example.com" });
        expect(ok).toBe(false);
        expect(assistant.getActiveRecord().data.email).toBe("acme@example.com");
    });

    test("un campo que aparece en DOS ramas está oculto sólo si las dos lo esconden", async () => {
        // El patrón de dos ramas mutuamente excluyentes. Marcar el campo con que
        // UNA esté escondida lo daba por invisible mientras la persona lo tiene
        // delante en la otra — y de paso hacía descartar una propuesta legítima.
        // Es la misma regla que usa Odoo al fusionar dos nodos del mismo campo:
        // `patchActiveFields` combina el `invisible` con AND.
        const assistant = await mountFormDosRamas();
        const ctx = assistant.getContextPayload();
        // `email` está en las dos ramas: la escondida y la visible.
        expect(ctx.fieldState.email).toBe(undefined);
        // `fecha` está SÓLO en la rama escondida: ésa sí está oculta.
        expect(ctx.fieldState.fecha.invisible).toBe(true);
    });

    test("y una propuesta sobre el campo de las dos ramas se aplica", async () => {
        // El otro lado del par: no alcanza con reportarlo bien en el contexto si
        // el camino de escritura lo descarta igual.
        const assistant = await mountFormDosRamas();
        const ok = await applyAndRender(assistant, { email: "nuevo@example.com" });
        expect(ok).toBe(true);
        expect(assistant.getActiveRecord().data.email).toBe("nuevo@example.com");
    });

    test("un campo que se esconde a SÍ MISMO tampoco se escribe", async () => {
        // El chequeo era inconsistente: rechazaba lo que esconde un ancestro y
        // dejaba pasar lo que se esconde a sí mismo. En los dos casos la persona
        // no lo tiene delante y no puede revisar el cambio antes de guardar.
        const assistant = await mountFormInvisiblePropio();
        expect(assistant.getContextPayload().fieldState.email.invisible).toBe(true);
        const ok = await applyAndRender(assistant, { email: "nuevo@example.com" });
        expect(ok).toBe(false);
        expect(assistant.getActiveRecord().data.email).toBe("acme@example.com");
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
     * nueva revienta. `applyProposal` avisa y devuelve false, pero lo que SÍ entró
     * está en el formulario, así que el contexto publicado tiene que reflejarlo.
     * Si se queda en la revisión previa, el assistant sigue razonando sobre
     * valores que él mismo ya cambió — y la próxima propuesta contra esa misma
     * `baseRevision` lee el cambio propio como una edición del usuario.
     *
     * El orden es lo que hace posible el estado a medias: los escalares van
     * primero, por `record._update`; las líneas nuevas después, una por una vía
     * `addNewRecord`. Cuando esa segunda parte falla —acá, el onchange de la línea
     * tirando desde el servidor, que es como se rompe de verdad— el `name` ya está
     * puesto en el formulario.
     *
     * Y el formulario arranca SUCIO a propósito. Con un record limpio, aplicar lo
     * ensucia, y esa transición ya hace que el FormController re-publique el
     * contexto por su cuenta — tapando el agujero sin arreglarlo. Sobre un
     * formulario que el usuario ya venía editando no hay transición que lo tape,
     * y es además el caso realista: se propone sobre algo a medio llenar.
     */
    test("el contexto refleja lo que sí entró, aunque el form ya estuviera sucio", async () => {
        onRpc("onchange", () => {
            throw new Error("el onchange de la línea falló");
        });
        const assistant = await mountPartnerForm();
        await contains(".o_field_widget[name=email] input").edit("editado@example.com");
        const revisionBefore = assistant.getContextPayload().revision;

        const ok = await applyAndRender(assistant, {
            name: "Acme SA",
            child_ids: [[0, false, { name: "Nueva" }]],
        });
        // La línea no se pudo agregar, así que la propuesta NO entró entera.
        expect(ok).toBe(false);
        // …pero el nombre sí, y eso es lo que hay que re-publicar.
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");

        const ctx = assistant.getContextPayload();
        expect(ctx.fields.name).toBe("Acme SA");
        expect(ctx.revision).not.toBe(revisionBefore);
    });
});

describe("applyProposal — el guard de líneas nuevas", () => {
    let assistant;
    beforeEach(async () => {
        assistant = await mountPartnerForm();
    });

    /**
     * El guard sólo mira CREATE (op 0). Un LINK a una fila que YA está en la lista
     * deja el conteo igual, y está bien que lo deje: lo que se pidió —que el
     * registro quede vinculado— es verdad antes y después. Avisar ahí es mandar al
     * usuario a revisar algo que está bien.
     */
    test("re-linkear una fila que ya está no es un error", async () => {
        const ok = await applyAndRender(assistant, { name: "Acme SA", child_ids: [[4, 3]] });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(1);
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");
    });

    test("todas las líneas pedidas aparecen", async () => {
        const ok = await applyAndRender(assistant, {
            child_ids: [
                [0, false, { name: "Nueva A" }],
                [0, false, { name: "Nueva B" }],
            ],
        });
        expect(ok).toBe(true);
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(3);
    });

    /**
     * Hace que el StaticList ACEPTE cada línea nueva y materialice sólo las
     * primeras `materialize`. El resto devuelve un record de mentira cuyo
     * `_update` no hace nada: la lista dijo que sí y no agregó nada.
     *
     * Hace falta un doble porque el `StaticList` real no falla así: `addNewRecord`
     * siempre agrega la fila, y una tupla CREATE mal formada tira en vez de ser un
     * no-op (probado: `[0, false, null]`, vals string y vals ausente revientan las
     * tres). El no-op silencioso que le dio origen al guard era el OBJETO PLANO, y
     * ese ya no llega hasta acá — `normalizeProposalX2many` lo convierte en tupla
     * antes. Tampoco entra por el fallback defensivo: con `addNewRecord` fuera de
     * juego, `_update` manda la tupla cruda y `_applyCommands` la aplica bien
     * (verificado: 3 CREATE → 3 filas).
     *
     * O sea que el modo de falla que el guard cubre hoy es "la API de la lista
     * cambió, o un widget la reemplazó, y acepta sin agregar". Eso es lo que el
     * doble reproduce, y sin él el oráculo no tiene forma de ejercitarse.
     */
    function acceptButOnlyAdd(list, materialize) {
        const real = list.addNewRecord.bind(list);
        let seen = 0;
        list.addNewRecord = async (params) => {
            seen += 1;
            return seen <= materialize ? await real(params) : { _update: async () => {} };
        };
    }

    test("avisa cuando la lista acepta la línea y no la agrega", async () => {
        acceptButOnlyAdd(assistant.getActiveRecord().data.child_ids, 0);
        const ok = await applyAndRender(assistant, {
            name: "Acme SA",
            child_ids: [[0, false, { name: "Nueva" }]],
        });
        expect(ok).toBe(false);
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(1);
        // Lo que sí entró queda en el formulario: el aviso dice "Changes applied,
        // but…", no "no se aplicó nada".
        expect(".o_field_widget[name=name] input").toHaveValue("Acme SA");
    });

    /**
     * El caso que separa los dos oráculos, y la razón de que el chequeo cuente.
     * Se piden 3 líneas y entra 1: la lista CRECIÓ, así que el `after > before`
     * anterior lo daba por bueno. Con `after >= before + adds` se ve el faltante.
     */
    test("avisa cuando entran algunas líneas y otras no", async () => {
        acceptButOnlyAdd(assistant.getActiveRecord().data.child_ids, 1);
        const ok = await applyAndRender(assistant, {
            child_ids: [
                [0, false, { name: "A" }],
                [0, false, { name: "B" }],
                [0, false, { name: "C" }],
            ],
        });
        expect(ok).toBe(false);
        // 1 de las 3 entró — creció, pero no lo suficiente.
        expect(".o_field_widget[name=child_ids] .o_data_row").toHaveCount(2);
    });
});

describe("applyProposal — fechas dentro de líneas x2many", () => {
    let assistant;
    beforeEach(async () => {
        assistant = await mountPartnerForm();
    });

    // Las vals de un comando x2many las vuelve a parsear Odoo: el case UPDATE de
    // `_applyCommands` hace `record._parseServerValues(changes)`, y eso llama a
    // `deserializeDate` = `DateTime.fromSQL(...)`. Con un objeto Luxon adentro,
    // `fromSQL` devuelve `Invalid DateTime` — y no tira: la celda queda con ese
    // texto y al guardar se manda el literal al servidor. Por eso las vals van en
    // formato SERVIDOR y la conversión a Luxon vive sólo donde se llama
    // `line._update`, que es el único camino que no re-parsea.

    test("actualizar la fecha de una línea existente la deja legible", async () => {
        const ok = await applyAndRender(assistant, {
            child_ids: [[1, 3, { fecha: "2026-09-30" }]],
        });
        expect(ok).toBe(true);
        const cells = [...document.querySelectorAll(".o_data_row .o_data_cell")].map((c) => c.textContent);
        // La celda READONLY de la grilla renderiza "Sep 30, 2026" (el
        // "09/30/2026" es el formato del input al editar). Lo que importa acá es
        // que la fecha llegó legible: sin el fix dice "Invalid DateTime", que no
        // contiene el año — así que asertar el año discrimina.
        expect(cells.join(" | ")).not.toInclude("Invalid");
        expect(cells.join(" | ")).toInclude("2026");
    });

    test("una línea nueva con fecha la deja legible", async () => {
        const ok = await applyAndRender(assistant, {
            child_ids: [{ name: "Nueva", fecha: "2026-09-30" }],
        });
        expect(ok).toBe(true);
        const cells = [...document.querySelectorAll(".o_data_row")].map((r) => r.textContent).join(" | ");
        expect(cells).not.toInclude("Invalid");
    });

    test("una línea cuya única fecha es ilegible no entra vacía", async () => {
        // Descartar el campo ilegible dejaba `{}` como vals: `addNewRecord` metía
        // una fila en blanco y el guard de crecimiento la daba por buena.
        const before = document.querySelectorAll(".o_data_row").length;
        await applyAndRender(assistant, { child_ids: [{ fecha: "cuando puedas" }] });
        expect(document.querySelectorAll(".o_data_row")).toHaveCount(before);
    });
});

describe("applyProposal — lo que Luxon acepta y no debería", () => {
    let assistant;
    beforeEach(async () => {
        assistant = await mountPartnerForm();
    });

    // `DateTime.fromSQL("14:30")` devuelve HOY a las 14:30, y `"2026"` se lee como
    // la hora 20:26. Sin un guard, "poné la reunión a las 14:30" entra como un
    // valor plausible y equivocado, sin que aparezca ningún aviso.
    test("una hora suelta no es una fecha", async () => {
        const ok = await applyAndRender(assistant, { fecha_hora: "14:30" });
        expect(ok).toBe(false);
        expect(".o_field_datetime input").toHaveValue("");
    });

    test("un año pelado tampoco", async () => {
        const ok = await applyAndRender(assistant, { fecha: "2026" });
        expect(ok).toBe(false);
        expect(".o_field_date input").toHaveValue("");
    });
});
