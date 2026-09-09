/** @odoo-module **/

/**
 * Do not resume tours while this screen is being shown inside another site.
 *
 * THE PROBLEM, MEASURED. With an onboarding tour in progress, the web client
 * inside a cross-origin iframe **kills the browser tab**: the tour pointer
 * reaches for the parent document, that throws `SecurityError` in a loop (59
 * counted within seconds) and takes the renderer's memory with it.
 *
 * And the tour does not start in the panel: it starts in the everyday Odoo. The
 * user goes in, the tour begins and leaves its state in `localStorage`; later
 * they open the panel — same origin, same `localStorage` — and the tour is
 * RESUMED inside the iframe. Which is why switching it off server-side is not
 * enough: resumption reads `localStorage`, not `session_info`.
 *
 * IT IS AN ODOO BUG, AND IT IS ONE LINE. `tour_service.js` already tries to
 * prevent this: it starts and resumes tours inside `if (!window.frameElement)`.
 * But `window.frameElement` returns `null` when the parent is of ANOTHER
 * origin, so the guard holds precisely in the case it meant to prevent. The
 * condition that does work cross-origin is `window.top !== window.self`, the
 * one used here. It belongs upstream.
 *
 * IT IS NOT GATED ON THE EMBED SWITCH, and that is deliberate. The crash is
 * Odoo's and happens to anyone framing this web client. This half could not
 * read a server parameter anyway, so gating the server half on it left the two
 * halves of one fix under different rules.
 *
 * WHY `tourState` IS PATCHED AND THE SERVICE IS NOT REMOVED. Removing
 * `tour_service` from the registry would break whoever asks for it: the
 * onboarding widget and the POS call `useService("tour_service")` and would
 * blow up on render. Returning `null` here keeps the service alive and
 * `startTour` available to anyone calling it by hand; only automatic
 * resumption is cut. That matters beyond politeness — Tuqui drives the pointer
 * on purpose inside the panel, and that has to keep working.
 *
 * AND THE USER'S PROGRESS IS NOT ERASED. `null` is returned only inside the
 * frame: `localStorage` is left intact, so in their everyday Odoo the tour
 * carries on where they left it.
 *
 * FAKING `window.frameElement` WAS ALSO DISCARDED, so Odoo's own guard would
 * work by itself. It is one line and it is tempting, but `website` and the
 * editor USE that element (`dispatchEvent`, `ownerDocument`): it would have
 * traded a tour crash for breakage elsewhere.
 */

import { patch } from "@web/core/utils/patch";
import { tourState } from "@web_tour/js/tour_state";

/**
 * It lives on an object — rather than as a loose function — so the test can
 * substitute it. The patch is applied ALWAYS and the decision is taken on every
 * call: that way it does not depend on the frame's state at import time, which
 * is precisely what cannot be simulated in a test.
 */
export const framing = {
    /** Is this page inside a frame? Holds cross-origin too. */
    isFramed() {
        try {
            return window.top !== window.self;
        } catch {
            // Reading `window.top` cross-origin does not throw, but if it ever
            // did, being framed is the conservative answer.
            return true;
        }
    },
};

patch(tourState, {
    getCurrentTour() {
        if (framing.isFramed()) {
            return null;
        }
        return super.getCurrentTour();
    },
});
