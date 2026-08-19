/** @odoo-module **/
import { describe, expect, test } from "@odoo/hoot";
import {
    coerceX2manyCommandRelations,
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

describe("coerceX2manyCommandRelations", () => {
    const subFields = { product_id: { type: "many2one" }, name: { type: "char" } };

    test("un m2o pelado dentro de un CREATE se envuelve en {id}", () => {
        // Sin esto la línea queda SIN producto y el onchange no calcula
        // name/precio/totales: `parseServerValue` devuelve el entero tal cual.
        expect(coerceX2manyCommandRelations([[0, false, { product_id: 1 }]], subFields)).toEqual([
            [0, false, { product_id: { id: 1 } }],
        ]);
    });

    test("no toca los campos que no son relacionales", () => {
        expect(coerceX2manyCommandRelations([[0, false, { name: "x" }]], subFields)).toEqual([
            [0, false, { name: "x" }],
        ]);
    });

    test("sin defs del sub-modelo devuelve los comandos intactos", () => {
        const cmds = [[0, false, { product_id: 1 }]];
        expect(coerceX2manyCommandRelations(cmds, undefined)).toEqual(cmds);
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

    test("un LINK también", () => {
        const grow = x2manyFieldsExpectingGrowth({ order_line: [[4, 7]] }, fieldDefs);
        expect(grow.map((g) => g.name)).toEqual(["order_line"]);
    });

    test("un DELETE no", () => {
        expect(x2manyFieldsExpectingGrowth({ order_line: [[2, 7, false]] }, fieldDefs)).toEqual([]);
    });

    test("un campo escalar nunca", () => {
        expect(x2manyFieldsExpectingGrowth({ name: "x" }, fieldDefs)).toEqual([]);
    });
});
