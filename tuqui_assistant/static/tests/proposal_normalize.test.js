/** @odoo-module **/
import { describe, expect, test } from "@odoo/hoot";
import { mockTimeZone } from "@odoo/hoot-mock";
import {
    coerceDateValue,
    coerceX2manyCommandVals,
    isX2manyCommandTuple,
    normalizeProposalX2many,
    normalizeX2manyValue,
    splitX2manyCreates,
    x2manyFieldsExpectingGrowth,
} from "@tuqui_assistant/services/tuqui_assistant_service";

/**
 * Tests de las transformaciones que convierten una propuesta del LLM en algo que
 * el modelo de records de Odoo entiende.
 *
 * Son funciones puras y sin LLM: la propuesta se escribe a mano, que es todo el
 * punto — el modelo PRODUCE el dict de cambios, no es lo que hay que testear.
 * Acá se fija la capa donde vivían las fallas conocidas del propose-apply.
 *
 * Los casos de interacción real (montar un form y aplicar) están en
 * `apply_proposal.test.js`.
 */

describe("isX2manyCommandTuple", () => {
    test("reconoce una tupla-comando y descarta un objeto plano", () => {
        expect(isX2manyCommandTuple([0, false, { product_id: 1 }])).toBe(true);
        expect(isX2manyCommandTuple([4, 7])).toBe(true);
        // Un objeto plano tiene command[0] === undefined: `_applyCommands` no lo
        // matchea, no tiene `default`, y la línea se pierde EN SILENCIO. Esa es la
        // falla que esta distinción existe para evitar.
        expect(isX2manyCommandTuple({ product_id: 1 })).toBe(false);
        expect(isX2manyCommandTuple(["0", false, {}])).toBe(false);
    });
});

describe("normalizeX2manyValue", () => {
    test("un objeto plano se vuelve CREATE", () => {
        expect(normalizeX2manyValue([{ product_id: 1, product_uom_qty: 2 }])).toEqual([
            [0, false, { product_id: 1, product_uom_qty: 2 }],
        ]);
    });

    test("una tupla-comando queda intacta", () => {
        const already = [[1, 42, { name: "x" }]];
        expect(normalizeX2manyValue(already)).toEqual(already);
    });

    test("un id suelto se vuelve LINK", () => {
        expect(normalizeX2manyValue([7, 9])).toEqual([
            [4, 7],
            [4, 9],
        ]);
    });

    test("una lista mixta se normaliza elemento por elemento", () => {
        expect(normalizeX2manyValue([{ name: "a" }, [2, 5, false], 7])).toEqual([
            [0, false, { name: "a" }],
            [2, 5, false],
            [4, 7],
        ]);
    });

    test("lo que no es array no se toca", () => {
        expect(normalizeX2manyValue(false)).toBe(false);
        expect(normalizeX2manyValue(42)).toBe(42);
    });

    test("una forma desconocida se deja pasar para que falle visible", () => {
        // Preferimos un error del modelo antes que un no-op silencioso.
        expect(normalizeX2manyValue(["raro"])).toEqual(["raro"]);
    });
});

describe("normalizeProposalX2many", () => {
    const fieldDefs = {
        name: { type: "char" },
        partner_id: { type: "many2one" },
        order_line: { type: "one2many" },
        tag_ids: { type: "many2many" },
    };

    test("normaliza sólo los x2many y deja el resto igual", () => {
        const out = normalizeProposalX2many(
            {
                name: "Pedido",
                partner_id: 7,
                order_line: [{ product_id: 3 }],
                tag_ids: [1, 2],
            },
            fieldDefs
        );
        expect(out.name).toBe("Pedido");
        expect(out.partner_id).toBe(7);
        expect(out.order_line).toEqual([[0, false, { product_id: 3 }]]);
        expect(out.tag_ids).toEqual([
            [4, 1],
            [4, 2],
        ]);
    });

    test("no muta la propuesta original", () => {
        const input = { order_line: [{ product_id: 3 }] };
        normalizeProposalX2many(input, fieldDefs);
        expect(input.order_line).toEqual([{ product_id: 3 }]);
    });

    test("sin defs de campo trata todo como escalar", () => {
        expect(normalizeProposalX2many({ order_line: [{ a: 1 }] }, undefined)).toEqual({
            order_line: [{ a: 1 }],
        });
    });
});

describe("coerceX2manyCommandVals", () => {
    const subFields = { product_id: { type: "many2one" }, name: { type: "char" } };

    test("un m2o pelado dentro de un CREATE se envuelve en {id}", () => {
        // Sin esto la línea queda SIN producto y el onchange no calcula
        // name/precio/totales: `parseServerValue` devuelve el entero tal cual.
        expect(coerceX2manyCommandVals([[0, false, { product_id: 1 }]], subFields)).toEqual([
            [0, false, { product_id: { id: 1 } }],
        ]);
    });

    test("no toca los campos que no son relacionales", () => {
        expect(coerceX2manyCommandVals([[0, false, { name: "x" }]], subFields)).toEqual([
            [0, false, { name: "x" }],
        ]);
    });

    test("sin defs del sub-modelo devuelve los comandos intactos", () => {
        const cmds = [[0, false, { product_id: 1 }]];
        expect(coerceX2manyCommandVals(cmds, undefined)).toEqual(cmds);
    });

    test("una fecha dentro de un comando queda en formato SERVIDOR, no en Luxon", () => {
        // Las vals de un comando las vuelve a parsear Odoo: el case UPDATE de
        // `_applyCommands` hace `record._parseServerValues(changes)`, que llama a
        // `deserializeDate` = `DateTime.fromSQL(...)`. Con un objeto Luxon adentro
        // eso da `Invalid DateTime` — y no tira: la celda queda con ese texto y al
        // guardar se manda el literal al servidor. La conversión a Luxon vive sólo
        // donde se llama `line._update`, el único camino que NO re-parsea.
        const defs = { fecha: { type: "date" } };
        const [[, , vals]] = coerceX2manyCommandVals([[0, false, { fecha: "2026-09-30" }]], defs);
        expect(vals.fecha).toBe("2026-09-30");
    });

    test("un ISO con T se NORMALIZA a formato servidor", () => {
        // El modelo puede mandar la forma que ve en el contexto (`toISO()`), y esa
        // `deserializeDate` no la parsea. Normalizar acá es lo que hace que los dos
        // formatos terminen igual del lado de Odoo.
        const defs = { fecha: { type: "date" } };
        const [[, , vals]] = coerceX2manyCommandVals([[0, false, { fecha: "2026-09-30T14:30:00Z" }]], defs);
        expect(vals.fecha).toBe("2026-09-30");
    });

    test("una fecha ilegible se descarta y se reporta, no viaja cruda", () => {
        const defs = { fecha: { type: "date" }, name: { type: "char" } };
        const bad = [];
        const [[, , vals]] = coerceX2manyCommandVals(
            [[0, false, { fecha: "cuando puedas", name: "Nueva" }]],
            defs,
            bad
        );
        expect(vals).toEqual({ name: "Nueva" });
        expect(bad).toEqual(["fecha"]);
    });
});

describe("splitX2manyCreates", () => {
    test("separa los CREATE del resto de los comandos", () => {
        const { creates, rest } = splitX2manyCreates([
            [0, false, { name: "nueva" }],
            [1, 5, { name: "editada" }],
            [2, 9, false],
        ]);
        // Los CREATE van por `list.addNewRecord` (dispara el onchange de la línea);
        // el resto sí anda por `_update`.
        expect(creates).toEqual([{ name: "nueva" }]);
        expect(rest).toEqual([
            [1, 5, { name: "editada" }],
            [2, 9, false],
        ]);
    });

    test("sin CREATE devuelve todo como resto", () => {
        const { creates, rest } = splitX2manyCreates([[4, 7]]);
        expect(creates).toEqual([]);
        expect(rest).toEqual([[4, 7]]);
    });
});

describe("x2manyFieldsExpectingGrowth", () => {
    const fieldDefs = { order_line: { type: "one2many" }, name: { type: "char" } };

    test("un CREATE hace esperar que el campo crezca", () => {
        const grow = x2manyFieldsExpectingGrowth(
            { order_line: [[0, false, { product_id: 1 }]] },
            fieldDefs
        );
        expect(grow.map((g) => g.name)).toEqual(["order_line"]);
    });

    test("cuenta CUÁNTAS líneas nuevas se pidieron", () => {
        // El `adds` es el oráculo del chequeo post-apply (`after >= before + adds`).
        // Si sólo dijera "este campo debería crecer", pedir 3 y que entre 1 pasaría
        // como éxito.
        const grow = x2manyFieldsExpectingGrowth(
            {
                order_line: [
                    [0, false, { product_id: 1 }],
                    [0, false, { product_id: 2 }],
                    [0, false, { product_id: 3 }],
                ],
            },
            fieldDefs
        );
        expect(grow).toEqual([{ name: "order_line", adds: 3 }]);
    });

    test("un LINK no: re-linkear lo que ya está es un no-op legítimo", () => {
        // El count no sube y está bien que no suba — lo pedido (que el registro
        // quede vinculado) ya era cierto. Contarlo sería una falsa alarma. El caso
        // que sí es un error, LINK a un id inexistente, falla visible por su
        // propio camino.
        expect(x2manyFieldsExpectingGrowth({ order_line: [[4, 7]] }, fieldDefs)).toEqual([]);
    });

    test("un CREATE mezclado con LINKs cuenta sólo el CREATE", () => {
        const grow = x2manyFieldsExpectingGrowth(
            { order_line: [[4, 7], [0, false, { product_id: 1 }], [4, 9]] },
            fieldDefs
        );
        expect(grow).toEqual([{ name: "order_line", adds: 1 }]);
    });

    test("un DELETE no", () => {
        expect(x2manyFieldsExpectingGrowth({ order_line: [[2, 7, false]] }, fieldDefs)).toEqual([]);
    });

    test("un campo escalar nunca", () => {
        expect(x2manyFieldsExpectingGrowth({ name: "x" }, fieldDefs)).toEqual([]);
    });
});

describe("coerceDateValue", () => {
    // El modelo OWL guarda las fechas como objetos Luxon y el widget las
    // renderiza con `value.toFormat()`. Un string crudo no revienta al aplicar:
    // revienta al RENDERIZAR, con "value.toFormat is not a function", y se lleva
    // puesto el formulario entero. Estos tests fijan las dos formas que llegan.

    test("una fecha en formato servidor se parsea", () => {
        const parsed = coerceDateValue("2026-09-30", "date");
        expect(parsed.isValid).toBe(true);
        expect(parsed.toISODate()).toBe("2026-09-30");
    });

    test("un datetime en formato servidor se parsea", () => {
        const parsed = coerceDateValue("2026-09-30 14:30:00", "datetime");
        expect(parsed.isValid).toBe(true);
    });

    test("un datetime en ISO con T también: es lo que el modelo VE", () => {
        // Nuestro propio serializador de salida emite `toISO()` para los
        // datetime, así que el modelo devuelve esa forma — que NO es la que
        // parsea `deserializeDateTime`. Si esto se rompe, vuelve el crash.
        const parsed = coerceDateValue("2026-09-30T14:30:00+00:00", "datetime");
        expect(parsed.isValid).toBe(true);
        expect(parsed.toUTC().toFormat("yyyy-LL-dd HH:mm")).toBe("2026-09-30 14:30");
    });

    test("un campo date toma la fecha tal como está escrita, sin correrla de zona", () => {
        // En UTC-3, las 01:00Z del 1/10 son el 30/9 local: convertir a la zona
        // del usuario correría la fecha un día para atrás. Un campo date no
        // tiene hora ni zona — vale lo que dice el string.
        mockTimeZone(-3);
        expect(coerceDateValue("2026-10-01T01:00:00Z", "date").toISODate()).toBe("2026-10-01");
    });

    test("limpiar el campo es legítimo y no es un error", () => {
        expect(coerceDateValue(false, "date")).toBe(false);
        expect(coerceDateValue(null, "datetime")).toBe(false);
        expect(coerceDateValue("", "date")).toBe(false);
        expect(coerceDateValue("   ", "date")).toBe(false);
    });

    test("lo que no se puede leer da null, no un DateTime inválido", () => {
        // Un DateTime inválido no tira: pinta "Invalid DateTime" en el campo.
        // Preferimos descartarlo y decirlo.
        expect(coerceDateValue("el martes que viene", "date")).toBe(null);
        expect(coerceDateValue("30/09/2026", "datetime")).toBe(null);
        expect(coerceDateValue(20260930, "date")).toBe(null);
    });
});
