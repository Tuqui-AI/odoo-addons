/** @odoo-module **/

/**
 * When this Odoo is being SHOWN INSIDE Tuqui, the chat out there can drive this
 * screen — point at a field, mark a button — the same way it already does when
 * Tuqui is the one inside Odoo.
 *
 * WHY THIS FILE EXISTS. The bridge between the two products was built in one
 * direction only: Odoo is the parent, Tuqui is the iframe, and `panel.js` both
 * sends the page context down and listens for what comes back up. Put Odoo in
 * the iframe instead — which is what `tuqui_embed` now allows — and every one of
 * those wires points the wrong way. Measured on 2026-09-12 with Sales Orders
 * open in Tuqui's side panel: asked to mark a button, the assistant answered
 * that it could not reach that screen. It was right. Nothing was listening.
 *
 * THE PIECES ARE NOT NEW, and that is the point: the mark, the page context and
 * the warning when the mark does not land all live in `tuquiAssistant` and are
 * reached here exactly as the panel reaches them. What this file adds is the
 * WIRE — who talks to whom — and nothing else. The protocol is the same one
 * `panel.js` documents, with the roles swapped:
 *
 *   Tuqui → Odoo: { source: "tuqui-spa",  type: "spotlight", payload }
 *   Odoo → Tuqui: { source: "tuqui-odoo", type: "ready" }
 *                 { source: "tuqui-odoo", type: "context", payload: PageContext }
 *
 * WHO IS ALLOWED TO DRIVE THIS SCREEN. Only the Tuqui this database is
 * connected to: the origin is derived from the companion's own `base_url`
 * (`embed_bootstrap`), the same value the panel uses to decide who it is talking
 * to. Two conditions, both required, no shortcuts — the message must come from
 * OUR parent window AND from that concrete origin, never "*". A page that frames
 * us without being that Tuqui gets nothing, which is also what happens today
 * when `tuqui_embed` is off: it cannot frame us at all.
 *
 * THE CONTEXT GOES UP ON ITS OWN. Without it the agent knows a URL and nothing
 * about what is drawn on the screen, so it would ask for a mark on a field it
 * guessed the name of. The panel pushes the context on every change through an
 * OWL effect; there is no component here, so the same job is done by watching
 * the service's reactive state with the same key the panel compares.
 *
 * NOTHING HAPPENS WHEN NOT NESTED: in normal Odoo use this service returns
 * immediately and never installs a listener.
 */

import { registry } from "@web/core/registry";
import { reactive } from "@odoo/owl";
import { isNested } from "@tuqui_assistant/nested_guard";
import { runOdooAction } from "@tuqui_assistant/odoo_actions";

/** What we answer to; what we send as. Same strings as the panel's protocol. */
const FROM_TUQUI = "tuqui-spa";
const FROM_ODOO = "tuqui-odoo";

/**
 * The identity of what is on screen, as a string that changes when it changes.
 *
 * Deliberately the same shape as the panel's `_contextKey`: the two are the same
 * decision ("is this a different screen than the one I already sent?") and a
 * screen that re-publishes on every keystroke would flood the other side.
 */
export function contextKey(context) {
    if (!context) {
        return "";
    }
    switch (context.kind) {
        case "record":
            // `revision` moves with every in-memory edit, `dirty` flips once —
            // both are needed for a context that stays live while typing.
            return `record:${context.model}:${context.resId}:${context.dirty}:${context.revision}`;
        case "selection":
            return `sel:${context.model}:${context.count}:${(context.resIds || []).join(",")}`;
        case "list":
            return `list:${context.model}:${context.count}:${JSON.stringify(context.domain || [])}`;
    }
    return "";
}

/** The origin of the Tuqui this database is connected to, or null.
 *
 *  The scheme is required rather than left to `new URL`, and that is not
 *  belt-and-braces: a value without one is a RELATIVE url, and `new URL` happily
 *  resolves it against the page it is running on — so a mistyped `base_url`
 *  would come back as this Odoo's own origin instead of as an error. An origin
 *  we compute from garbage is worse than no origin at all: this value is the
 *  only thing deciding who may drive the screen. */
export function tuquiOrigin(bootstrap) {
    const base = bootstrap?.connected ? bootstrap.base_url : null;
    if (typeof base !== "string" || !/^https?:\/\//i.test(base)) {
        return null;
    }
    try {
        return new URL(base).origin;
    } catch {
        return null;
    }
}

/**
 * Is this message one we should act on?
 *
 * Exported because it is the security boundary, and a boundary nobody can call
 * on its own is a boundary nobody tests. Both conditions hold or nothing runs:
 * `source` alone is forgeable from any window, and an origin of "*" would let
 * any page that manages to frame us drive the screen.
 */
export function isFromOurTuqui(ev, origin, win = window) {
    if (!origin || !ev || ev.origin !== origin) {
        return false;
    }
    if (ev.source !== win.parent || ev.source === win) {
        return false;
    }
    return Boolean(ev.data) && ev.data.source === FROM_TUQUI;
}

/**
 * Who this Odoo takes orders from, resolved when it is first needed and not once
 * and forever at start-up.
 *
 * WHY LAZY. The origin comes from a call to the server, and the first second of
 * a web client is the worst moment to depend on one. Resolving it eagerly and
 * giving up on a falsy answer is what actually happened: inside the panel the
 * bridge asked at start-up, got nothing usable, and never installed a listener —
 * while the very same call, made a minute later from the same frame, answered
 * fine. A decision that permanent should not be taken with one round trip.
 *
 * A resolved origin is remembered; a failure is NOT, so the next message asks
 * again. That is the whole retry policy, and it needs no timer: messages only
 * matter when one arrives.
 */
export function makeOriginResolver(tuquiAssistant) {
    let known = null;
    let pending = null;
    return () => {
        if (known) {
            return Promise.resolve(known);
        }
        pending ??= Promise.resolve(tuquiAssistant.getEmbedBootstrap())
            .then((bootstrap) => {
                known = tuquiOrigin(bootstrap);
                return known;
            })
            .catch(() => null)
            .finally(() => {
                pending = null;
            });
        return pending;
    };
}

export const tuquiNestedBridgeService = {
    dependencies: ["tuquiAssistant"],
    start(env, { tuquiAssistant }) {
        if (!isNested()) {
            return;
        }
        const resolveOrigin = makeOriginResolver(tuquiAssistant);

        const post = async (type, payload) => {
            const origin = await resolveOrigin();
            if (!origin) {
                // No companion connected, or a base_url we cannot read: there is
                // nobody to talk to. Not an error — an Odoo that is framed by
                // something which is not its Tuqui is a normal state.
                return;
            }
            try {
                window.parent.postMessage({ source: FROM_ODOO, type, payload }, origin);
            } catch (error) {
                // The parent navigated away, or is no longer that origin. Logged
                // rather than swallowed: the failure mode this replaces is a
                // context that silently never arrives, which reads from the chat
                // as an assistant that cannot see the screen.
                console.warn("[tuqui_assistant] could not post to the embedding Tuqui:", error);
            }
        };

        window.addEventListener("message", async (ev) => {
            // Cheap shape check before anything else: every page gets messages
            // from scripts that are none of our business, and the origin costs a
            // round trip the first time.
            if (!ev.data || ev.data.source !== FROM_TUQUI) {
                return;
            }
            const origin = await resolveOrigin();
            if (!isFromOurTuqui(ev, origin)) {
                return;
            }
            // The same dispatcher the panel uses in the other direction, so a
            // new instruction works on both doors the day it is written.
            runOdooAction(tuquiAssistant, ev.data.type, ev.data.payload || {});
        });

        // The reactive state is read inside the callback so OWL re-subscribes on
        // every run: a callback that stops reading stops being called.
        let last = null;
        let first = true;
        const publishIfChanged = () => {
            const key = contextKey(observed.context);
            // The first publish is unconditional: a screen that was already open
            // when this started has the same key as "nothing open", and the other
            // side has heard nothing yet.
            if (!first && key === last) {
                return;
            }
            first = false;
            last = key;
            post("context", tuquiAssistant.getContextPayload());
        };
        const observed = reactive(tuquiAssistant.state, publishIfChanged);

        // "ready" first and the context right after, always — including the
        // `{kind:"none"}` of a home or a dashboard. The other side needs to hear
        // something to know this Odoo can be driven at all, and "there is no
        // record open" is an answer, not silence.
        post("ready", {});
        publishIfChanged();
    },
};

registry.category("services").add("tuquiNestedBridge", tuquiNestedBridgeService);
