/** @odoo-module **/

/**
 * When this Odoo screen is being SHOWN inside something else, the assistant
 * does not open.
 *
 * THE PROBLEM, PLAINLY. `tuqui_embed` lets Tuqui show Odoo inside its panel.
 * But the Odoo being shown may have THIS module installed — and this panel
 * reopens itself if it was open before. So: Tuqui shows Odoo, that Odoo opens
 * its own Tuqui, that Tuqui restores its panel with Odoo, and so on. Every
 * level loads a full web client. It **takes down the whole browser**, not the
 * tab: measured, with Chrome freezing within seconds.
 *
 * WHY IT LIVES HERE AND NOT IN `tuqui_embed`. An earlier version lived there
 * and could not hold: to run BEFORE this panel it had to declare
 * `('before', 'tuqui_assistant/…')`, which made `tuqui_embed` impossible to
 * install without this module ("Addon tuqui_assistant is not installed"). A
 * customer who wants Odoo in the panel but not the assistant was locked out.
 *
 * Here that problem does not exist: the guard and the panel are the SAME
 * module, so ordering is settled by declaring this file first in the manifest.
 * And the rule ends up where it belongs — the assistant deciding not to mount,
 * instead of another module forbidding it from the outside.
 *
 * WHAT IT DOES, AND WHY THAT IS ENOUGH. The panel decides to open by reading
 * two places: `sessionStorage` (it was open in this tab) and a signal in
 * `localStorage` (shared across ALL tabs and iframes of this Odoo — which is
 * why having used the panel in another tab was enough to trigger the loop).
 *
 * Only the first is cleared here. The second is shared, so clearing it would
 * eat a signal meant for a legitimate top-level tab; the service skips reading
 * it while nested instead. Same outcome for this page, no collateral for the
 * others.
 *
 * AND THE BUTTON IS HIDDEN TOO, because clearing state only prevents the
 * automatic open: the systray button would still open it by hand, with the same
 * result. It is done in CSS rather than by removing the registry entry, and
 * that is not laziness: removing it requires running AFTER the systray
 * registers, while this file must run BEFORE the panel reads its state. A class
 * on `<html>` depends on nobody's ordering.
 *
 * WHAT IT DOES **NOT** DO. It does not touch the guidance layer — the pointer
 * Tuqui uses to point at things on screen — which has to stay alive precisely
 * here: an Odoo shown in the panel is the case where pointing matters most.
 * What gets silenced is the duplicated conversation, not the ability to be
 * driven.
 *
 * NOTHING IS TOUCHED WHEN NOT NESTED: in normal Odoo use this file does
 * absolutely nothing.
 */

// Safe to import despite the ordering rule above: `storage_keys` holds nothing
// but constants, so evaluating it does not pull in the service or the panel.
import { PANEL_STATE_KEY } from "@tuqui_assistant/storage_keys";

/** The class that hides the button. See `nested_guard.scss`. */
const NESTED_CLASS = "o-tuqui-nested";

/** Are we being shown inside another page?
 *
 *  A cross-origin `window.top` makes reading it throw SecurityError — and that
 *  IS the answer: if we cannot even look at it, it belongs to someone else.
 *  Hence the catch returns `true` instead of swallowing the error.
 */
export function isNested(win = window) {
    try {
        return win.top !== win.self;
    } catch {
        return true;
    }
}

export function silenceAssistantWhenNested(win = window) {
    if (!isNested(win)) {
        return false;
    }
    try {
        win.sessionStorage?.removeItem(PANEL_STATE_KEY);
    } catch {
        // Private browsing or blocked storage: the panel will not be able to
        // read its state either, so it starts closed all the same.
    }
    // The open signal is NOT removed, on purpose. It lives in `localStorage`,
    // shared with every tab of this Odoo, so deleting it here would eat the one
    // a legitimate top-level tab was about to consume — leaving that tab
    // without the panel it was promised. The service skips reading it while
    // nested instead: the decision belongs to the reader, not to whoever
    // happens to load first.
    win.document?.documentElement?.classList?.add(NESTED_CLASS);
    return true;
}

silenceAssistantWhenNested();
