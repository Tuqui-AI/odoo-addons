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
 */

/** A double of the service with only the surface the dispatcher touches. */
function fakeService() {
    const calls = [];
    const record = (name) => (...args) => calls.push([name, ...args]);
    return {
        calls,
        applyProposal: record("applyProposal"),
        proposeChatter: record("proposeChatter"),
        saveRecord: record("saveRecord"),
        spotlightOrWarn: record("spotlightOrWarn"),
        reloadView: record("reloadView"),
        navigate: record("navigate"),
    };
}

describe("what the chat can ask this Odoo to do", () => {
    test("each message runs its action", () => {
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
            expect(runOdooAction(service, type, payload)).toBe(true);
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

    test("a message that is not an Odoo action says so instead of eating it", () => {
        // The panel handles "location", "ready" and "external-link-opening"
        // itself. If this returned true for them they would stop happening.
        const service = fakeService();
        for (const type of ["location", "ready", "external-link-opening", "", "spotlights"]) {
            expect(runOdooAction(service, type, {})).toBe(false);
        }
        expect(service.calls.length).toBe(0);
    });

    test("a missing payload is an empty one, not a crash", () => {
        // The payload comes off a postMessage: the far side can omit it, and the
        // action that reads it hardest ("apply") must still get an object.
        const service = fakeService();
        expect(runOdooAction(service, "apply")).toBe(true);
        expect(service.calls[0]).toEqual(["applyProposal", {}, { baseRevision: undefined }]);
    });
});
