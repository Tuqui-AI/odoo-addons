/** @odoo-module **/
import { describe, expect, test } from "@odoo/hoot";

import {
    contextKey,
    isFromOurTuqui,
    makeOriginResolver,
    tuquiOrigin,
} from "@tuqui_assistant/nested_bridge";

/**
 * Who may drive this screen, and when the other side gets told it changed.
 *
 * The suite is about the two decisions that are NOT the wiring: the wiring
 * itself (a listener, a postMessage) is exercised by the panel's own protocol
 * in the opposite direction and has nothing new in it. What is new here is that
 * this Odoo now ACCEPTS instructions from a page that frames it — so the gate
 * gets the same treatment as the panel's: every case has its opposite next to
 * it, because a gate that only ever says yes passes half a suite.
 */

/** A message as `postMessage` delivers it, with the window it claims to be from. */
function message({ origin, source, data }) {
    return { origin, source, data };
}

const TUQUI = "https://tuqui.com";
const OTHER = "https://evil.example.com";

describe("who is allowed to drive this screen", () => {
    test("our Tuqui, from the window that frames us", () => {
        const win = { parent: "the parent" };
        const ev = message({ origin: TUQUI, source: "the parent", data: { source: "tuqui-spa" } });
        expect(isFromOurTuqui(ev, TUQUI, win)).toBe(true);
    });

    test("another origin is refused even saying the right words", () => {
        // The payload is forgeable from any window: `source: "tuqui-spa"` is not
        // a credential, which is why the origin is checked at all.
        const win = { parent: "the parent" };
        const ev = message({ origin: OTHER, source: "the parent", data: { source: "tuqui-spa" } });
        expect(isFromOurTuqui(ev, TUQUI, win)).toBe(false);
    });

    test("the right origin from a window that is not our parent is refused", () => {
        // A popup we opened, or another frame on the page, can post from the same
        // origin. Only the window we are inside of drives this screen.
        const win = { parent: "the parent" };
        const ev = message({ origin: TUQUI, source: "some other window", data: { source: "tuqui-spa" } });
        expect(isFromOurTuqui(ev, TUQUI, win)).toBe(false);
    });

    test("with no origin resolved, nothing is accepted", () => {
        // No companion connected: there is no Tuqui we would take orders from,
        // and the absence must not read as "anyone".
        const win = { parent: "the parent" };
        const ev = message({ origin: TUQUI, source: "the parent", data: { source: "tuqui-spa" } });
        expect(isFromOurTuqui(ev, null, win)).toBe(false);
    });

    test("a message from ourselves is refused", () => {
        // Top-level, `window.parent === window`: an unguarded check would make
        // every page its own driver.
        const win = {};
        win.parent = win;
        const ev = message({ origin: TUQUI, source: win, data: { source: "tuqui-spa" } });
        expect(isFromOurTuqui(ev, TUQUI, win)).toBe(false);
    });

    test("a message that is not ours is ignored", () => {
        const win = { parent: "the parent" };
        for (const data of [null, {}, { source: "something-else" }]) {
            expect(isFromOurTuqui(message({ origin: TUQUI, source: "the parent", data }), TUQUI, win)).toBe(
                false
            );
        }
    });
});

describe("which Tuqui that is", () => {
    test("the origin comes from the companion's base url", () => {
        expect(tuquiOrigin({ connected: true, base_url: "https://tuqui.com/" })).toBe("https://tuqui.com");
    });

    test("a companion that is not connected drives nothing", () => {
        // `base_url` can be present and the companion still inactive. Reading the
        // url without the flag would accept orders from a Tuqui this database is
        // no longer connected to.
        expect(tuquiOrigin({ connected: false, base_url: "https://tuqui.com" })).toBe(null);
    });

    test("a base url without a scheme is nobody, not this Odoo", () => {
        // `new URL` resolves a relative value against the current page, so
        // "tuqui.com" or "not a url" would come back as THIS Odoo's own origin —
        // an origin computed from garbage, in the one value that decides who may
        // drive the screen. Caught by this test running under Hoot, where the
        // page is `https://www.hoot.test`.
        for (const base of ["not a url", "tuqui.com", "/w/acme", ""]) {
            expect(tuquiOrigin({ connected: true, base_url: base })).toBe(null);
        }
        expect(tuquiOrigin(null)).toBe(null);
        expect(tuquiOrigin({ connected: true, base_url: 7 })).toBe(null);
    });
});

describe("when the other side gets told the screen changed", () => {
    test("a record that is edited publishes again", () => {
        // `revision` moves on every in-memory edit. Without it in the key the
        // agent would keep reading the values the form had when it was opened.
        const before = { kind: "record", model: "sale.order", resId: 7, dirty: false, revision: 1 };
        const after = { ...before, dirty: true, revision: 2 };
        expect(contextKey(before)).not.toBe(contextKey(after));
    });

    test("the same screen twice does not", () => {
        const ctx = { kind: "record", model: "sale.order", resId: 7, dirty: false, revision: 1 };
        expect(contextKey(ctx)).toBe(contextKey({ ...ctx }));
    });

    test("a selection changes with WHICH rows are ticked, not how many", () => {
        // Ticking one row and unticking another leaves the count identical. A key
        // built on the count would never republish, and the agent would act on
        // the rows that were selected a minute ago.
        const a = { kind: "selection", model: "sale.order", count: 2, resIds: [1, 2] };
        const b = { kind: "selection", model: "sale.order", count: 2, resIds: [1, 3] };
        expect(contextKey(a)).not.toBe(contextKey(b));
    });

    test("a list changes with its filters", () => {
        const a = { kind: "list", model: "sale.order", count: 10, domain: [] };
        const b = { kind: "list", model: "sale.order", count: 10, domain: [["state", "=", "sale"]] };
        expect(contextKey(a)).not.toBe(contextKey(b));
    });

    test("no screen open is a stable key, not an error", () => {
        // A home or a dashboard publishes `{kind:"none"}`, which has no key of its
        // own — and must not republish on every unrelated state change.
        expect(contextKey(null)).toBe("");
        expect(contextKey({ kind: "none" })).toBe("");
    });
});

describe("resolving who we take orders from", () => {
    test("a failed first answer is not remembered as 'nobody'", async () => {
        // The measured failure: inside the panel the bridge asked at start-up,
        // got nothing usable, and never listened again — while the same call a
        // minute later, from the same frame, answered fine. The first second of
        // a web client is the worst moment to depend on a round trip, and giving
        // up for good on one is not a policy.
        let intentos = 0;
        const servicio = {
            getEmbedBootstrap: async () => {
                intentos += 1;
                return intentos === 1 ? { connected: false } : { connected: true, base_url: "https://tuqui.com" };
            },
        };
        const resolver = makeOriginResolver(servicio);

        expect(await resolver()).toBe(null);
        expect(await resolver()).toBe("https://tuqui.com");
        expect(intentos).toBe(2);
    });

    test("a resolved origin is asked for only once", async () => {
        // The other half: retrying forever would put a round trip on every
        // message a framed page receives, and pages receive plenty.
        let intentos = 0;
        const servicio = {
            getEmbedBootstrap: async () => {
                intentos += 1;
                return { connected: true, base_url: "https://tuqui.com" };
            },
        };
        const resolver = makeOriginResolver(servicio);

        expect(await resolver()).toBe("https://tuqui.com");
        expect(await resolver()).toBe("https://tuqui.com");
        expect(intentos).toBe(1);
    });

    test("two questions at once share one round trip", async () => {
        // `ready` and the first context are posted back to back at start-up.
        let intentos = 0;
        const servicio = {
            getEmbedBootstrap: async () => {
                intentos += 1;
                return { connected: true, base_url: "https://tuqui.com" };
            },
        };
        const resolver = makeOriginResolver(servicio);

        const [a, b] = await Promise.all([resolver(), resolver()]);
        expect(a).toBe("https://tuqui.com");
        expect(b).toBe("https://tuqui.com");
        expect(intentos).toBe(1);
    });

    test("a call that throws is an answer of 'not yet', not a crash", async () => {
        let intentos = 0;
        const servicio = {
            getEmbedBootstrap: async () => {
                intentos += 1;
                if (intentos === 1) {
                    throw new Error("network");
                }
                return { connected: true, base_url: "https://tuqui.com" };
            },
        };
        const resolver = makeOriginResolver(servicio);

        expect(await resolver()).toBe(null);
        expect(await resolver()).toBe("https://tuqui.com");
    });
});
