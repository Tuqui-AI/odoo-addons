/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { patchWithCleanup } from "@web/../tests/web_test_helpers";
import { browser } from "@web/core/browser/browser";
import { tourState } from "@web_tour/js/tour_state";

import { framing } from "@tuqui_embed/no_tours_when_framed";

/**
 * The net under the guard that keeps a tour from resuming inside the panel.
 *
 * Why it earns its own test: its failure is SILENT. If someone breaks it,
 * nothing turns red — the tab simply starts dying again for whoever has a
 * half-finished onboarding, and that gets discovered at a customer. What is
 * pinned here is the complete pair, because both halves matter equally: inside
 * the frame it does not resume, and OUTSIDE the frame it still does. A guard
 * that switched tours off always would pass half of this test and break
 * everyone's onboarding.
 */
describe("tuqui_embed: do not resume tours inside a frame", () => {
    test("inside a frame there is no tour to resume", () => {
        patchWithCleanup(framing, { isFramed: () => true });
        patchWithCleanup(browser.localStorage, { getItem: () => "a_half_finished_tour" });

        expect(tourState.getCurrentTour()).toBe(null);
    });

    test("outside a frame the stored tour still resumes", () => {
        patchWithCleanup(framing, { isFramed: () => false });
        patchWithCleanup(browser.localStorage, { getItem: () => "a_half_finished_tour" });

        expect(tourState.getCurrentTour()).toBe("a_half_finished_tour");
    });

    test("stored progress is NOT erased by being embedded", () => {
        // The guard lies upwards, it does not destroy: if it cleared
        // `localStorage`, the user would lose their onboarding progress in
        // their own Odoo. We check that nobody calls the `removeItem`s.
        const removed = [];
        patchWithCleanup(framing, { isFramed: () => true });
        patchWithCleanup(browser.localStorage, {
            getItem: () => "a_half_finished_tour",
            removeItem: (key) => removed.push(key),
        });

        tourState.getCurrentTour();

        expect(removed).toEqual([]);
    });
});
