/** @odoo-module **/
import { describe, expect, test } from "@odoo/hoot";

import { isNested, silenceAssistantWhenNested } from "@tuqui_assistant/nested_guard";
import { OPEN_SIGNAL_KEY, PANEL_STATE_KEY } from "@tuqui_assistant/storage_keys";

/**
 * The loop this cuts: Tuqui shows Odoo in its panel → that Odoo opens its own
 * Tuqui panel → that Tuqui restores its panel with Odoo → … Every level loads a
 * full web client and the whole browser hangs, not just the tab.
 *
 * Why it earns its own suite: the failure is SILENT in the other direction too.
 * A guard that silenced the assistant everywhere would pass half of these and
 * break the panel for every customer who never embeds anything — so each case
 * below has its opposite next to it.
 */

/** A fake `window` whose storages can be inspected afterwards. */
function fakeWindow({ nested, throwsSecurityError = false } = {}) {
    const session = { [PANEL_STATE_KEY]: '{"panelOpen":true}' };
    const local = { [OPEN_SIGNAL_KEY]: '{"at":1}' };
    const classes = [];
    const win = {
        self: "me",
        sessionStorage: { removeItem: (k) => delete session[k] },
        localStorage: { removeItem: (k) => delete local[k] },
        document: { documentElement: { classList: { add: (c) => classes.push(c) } } },
        _session: session,
        _local: local,
        _classes: classes,
    };
    if (throwsSecurityError) {
        Object.defineProperty(win, "top", {
            get() {
                throw new Error("SecurityError: Blocked a frame with origin…");
            },
        });
    } else {
        win.top = nested ? "someone else" : "me";
    }
    return win;
}

describe("Odoo shown inside something else", () => {
    test("when nested, the panel stops opening on its own", () => {
        const win = fakeWindow({ nested: true });
        expect(silenceAssistantWhenNested(win)).toBe(true);
        // BOTH doors the panel opens itself through:
        expect(win._session[PANEL_STATE_KEY]).toBe(undefined);
        expect(win._local[OPEN_SIGNAL_KEY]).toBe(undefined);
    });

    test("the localStorage signal is cleared too, and that is the real case", () => {
        // `sessionStorage` is per tab, but the open signal lives in
        // `localStorage`, shared across EVERY tab and iframe of this Odoo. That
        // is why merely having used the panel in another tab was enough for the
        // embedded Odoo to open its own.
        const win = fakeWindow({ nested: true });
        silenceAssistantWhenNested(win);
        expect(win._local[OPEN_SIGNAL_KEY]).toBe(undefined);
    });

    test("and the button is hidden, or they would open it by hand anyway", () => {
        const win = fakeWindow({ nested: true });
        silenceAssistantWhenNested(win);
        expect(win._classes).toInclude("o-tuqui-nested");
    });

    test("in a normal Odoo it touches NOTHING", () => {
        // This is what makes the module safe to install: outside an iframe the
        // file does not exist for practical purposes.
        const win = fakeWindow({ nested: false });
        expect(silenceAssistantWhenNested(win)).toBe(false);
        expect(win._session[PANEL_STATE_KEY]).toBe('{"panelOpen":true}');
        expect(win._local[OPEN_SIGNAL_KEY]).toBe('{"at":1}');
        expect(win._classes).toEqual([]);
    });

    test("if we cannot even look at who contains us, assume nested", () => {
        // Reading a cross-origin `window.top` throws SecurityError. That error
        // IS the answer: if we cannot look at it, it is someone else's.
        // Swallowing it and carrying on would leave the loop open in exactly
        // the cross-origin case — which is THE case, since Tuqui and Odoo are
        // different origins.
        const win = fakeWindow({ throwsSecurityError: true });
        expect(isNested(win)).toBe(true);
        expect(silenceAssistantWhenNested(win)).toBe(true);
    });

    test("a storage that blows up does not stop the button from being hidden", () => {
        // Private browsing: `removeItem` may throw. If that aborted the
        // function, the button would stay visible and the loop would come back
        // through the door next to it.
        const win = fakeWindow({ nested: true });
        win.sessionStorage.removeItem = () => {
            throw new Error("SecurityError");
        };
        win.localStorage.removeItem = () => {
            throw new Error("SecurityError");
        };
        expect(silenceAssistantWhenNested(win)).toBe(true);
        expect(win._classes).toInclude("o-tuqui-nested");
    });
});
