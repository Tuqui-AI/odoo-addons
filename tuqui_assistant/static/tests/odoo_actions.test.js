/** @odoo-module **/
import { describe, expect, test } from "@odoo/hoot";

import { runOdooAction } from "@tuqui_assistant/odoo_actions";

/**
 * The chat's instructions land on the same six actions, whichever door they came
 * through.
 *
 * WHY THIS IS WORTH A SUITE. The failure it guards against has already happened
 * once, in the shape this file exists to make impossible: the mark worked when
 * Tuqui was inside Odoo and did nothing when Odoo was inside Tuqui, because the
 * second door had been given a switch of its own with one case in it. A door
 * that knows five of six instructions is worse than one that knows none — the
 * chat says "listo" and the screen does not move.
 *
 * So what is pinned here is the MAPPING, not the plumbing: which message runs
 * which method, and that an unknown message is reported as unknown rather than
 * swallowed (the caller needs that answer to handle its own messages).
 *
 * AND SINCE THE CHANNEL GOES BOTH WAYS, what each action ANSWERS. Every action
 * here used to be one-way: the chat dispatched and whatever happened stayed on
 * this side of the glass, so anything the assistant said next about the outcome
 * was a guess — measured over four implementations, an optimistic one every
 * time. The dispatcher is where the way back lives, so a new action inherits it
 * instead of needing someone to remember.
 */

/** A double of the service with only the surface the dispatcher touches. */
function fakeService() {
    const calls = [];
    const record = (name) => (...args) => calls.push([name, ...args]);
    return {
        calls,
        applyProposal: record("applyProposal"),
        proposeChatter: record("proposeChatter"),
        // El guardado es el único que ya sabe su resultado, así que el doble lo
        // devuelve: es lo que el despachador tiene que dejar pasar.
        saveRecord: (...args) => {
            calls.push(["saveRecord", ...args]);
            return Promise.resolve({ ok: false, reason: "rejected", detail: "faltan campos" });
        },
        spotlightOrWarn: record("spotlightOrWarn"),
        reloadView: record("reloadView"),
        navigate: record("navigate"),
    };
}

describe("what the chat can ask this Odoo to do", () => {
    test("each message runs its action", async () => {
        const cases = [
            ["apply", { changes: { partner_id: 3 } }, "applyProposal"],
            ["chatter", { mode: "note", body: "hola" }, "proposeChatter"],
            ["save", {}, "saveRecord"],
            ["spotlight", { field: "partner_id" }, "spotlightOrWarn"],
            ["reload", {}, "reloadView"],
            ["navigate", { model: "sale.order", mode: "new" }, "navigate"],
        ];
        for (const [type, payload, method] of cases) {
            const service = fakeService();
            const despacho = await runOdooAction(service, type, payload);
            expect(despacho.handled).toBe(true);
            expect(service.calls.length).toBe(1);
            expect(service.calls[0][0]).toBe(method);
        }
    });

    test("a proposal carries the revision it was reasoned about", () => {
        // Without it the service cannot tell a field the user changed in the
        // meantime from one it may overwrite, and silently overwrites it.
        const service = fakeService();
        runOdooAction(service, "apply", { changes: { partner_id: 3 }, baseRevision: 4 });
        expect(service.calls[0]).toEqual(["applyProposal", { partner_id: 3 }, { baseRevision: 4 }]);
    });

    test("a message that is not an Odoo action says so instead of eating it", async () => {
        // The panel handles "location", "ready" and "external-link-opening"
        // itself. If this returned true for them they would stop happening.
        const service = fakeService();
        for (const type of ["location", "ready", "external-link-opening", "", "spotlights"]) {
            expect((await runOdooAction(service, type, {})).handled).toBe(false);
        }
        expect(service.calls.length).toBe(0);
    });

    test("a missing payload is an empty one, not a crash", async () => {
        // The payload comes off a postMessage: the far side can omit it, and the
        // action that reads it hardest ("apply") must still get an object.
        const service = fakeService();
        expect((await runOdooAction(service, "apply")).handled).toBe(true);
        expect(service.calls[0]).toEqual(["applyProposal", {}, { baseRevision: undefined }]);
    });

    test("el guardado devuelve QUÉ pasó, no que se pidió", async () => {
        // EL CASO QUE ORIGINÓ ESTO. La pantalla mostraba "Missing required
        // fields" con dos campos obligatorios vacíos y el chat decía "ya deberías
        // ver el botón Save marcado". El dato estaba de este lado del vidrio y se
        // lo contábamos sólo a la persona, con un cartel.
        const service = fakeService();
        const despacho = await runOdooAction(service, "save", {});
        expect(despacho.result.ok).toBe(false);
        expect(despacho.result.reason).toBe("rejected");
        expect(despacho.result.detail).toBe("faltan campos");
    });

    test("y las que todavía no lo saben lo dicen, en vez de afirmar un ok", async () => {
        // `ok: null` no es un detalle: es la diferencia entre "no sé" y "salió
        // bien". Un `true` acá reconstruiría exactamente el problema que esto
        // viene a resolver, pero con una capa más de por medio.
        const service = fakeService();
        for (const type of ["apply", "chatter", "spotlight", "reload", "navigate"]) {
            const despacho = await runOdooAction(service, type, {});
            expect(despacho.result.ok).toBe(null, { message: type });
            expect(despacho.result.reason).toBe("dispatched", { message: type });
        }
    });
});
