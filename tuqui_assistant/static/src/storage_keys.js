/** @odoo-module **/

/**
 * The browser-storage keys the assistant reads to decide whether to open.
 *
 * They live in a leaf module of their own so that `nested_guard.js` can import
 * them. The guard has to run BEFORE the service and the panel evaluate — it
 * clears these very keys so those two never see them — so it cannot import
 * either of them. A module that holds nothing but constants has no side effects
 * to trigger, which makes it the one thing the guard may safely depend on.
 *
 * The alternative was to repeat the literals inside the guard and add a test
 * comparing both sides. Same protection against drift, one more moving part.
 */

/** Per-tab panel UI state (`panelOpen` / `minimized` / `expanded`), so it
 *  survives Ctrl+R. Written by the assistant service. */
export const PANEL_STATE_KEY = "tuqui_panel_state";

/** Short-lived "reopen the panel and resume this conversation" signal, written
 *  when the user follows an external link out of the chat. Lives in
 *  `localStorage`, so it is shared across every tab AND iframe of this Odoo —
 *  which is what let a panel opened in one tab reopen itself inside an embedded
 *  one. Written by the panel. */
export const OPEN_SIGNAL_KEY = "tuqui_open_signal";
