/** @odoo-module **/
import { Component, useState, useRef, useEffect, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";


/**
 * Panel lateral del asistente Tuqui.
 *
 * Embebe el SPA de Tuqui en un `<iframe>` (modo embed) y reusa TODA su UI
 * (composer, micrófono, artifacts, streaming). NO hay system param ni shell de
 * chat nativo de fallback: la URL del embed se DERIVA del companion (ADR 0001).
 *
 *  - La base de Tuqui (`tuqui.base_url`, default `https://tuqui.com`) y el slug
 *    del workspace activado salen de `getEmbedBootstrap()` → `embed_bootstrap`
 *    del modelo, que lee el oauth client del companion. Si el companion no está
 *    `active` (o no hay slug), `connected` es false y el panel muestra un prompt
 *    "conectá Tuqui desde Ajustes" en lugar del iframe — sin fallback.
 *  - Auth: SSO sin login. Al `ready`, el panel mintea un nonce single-use atado
 *    al usuario Odoo (`issue_for_current_user`) y se lo pasa al iframe por
 *    postMessage; el SPA lo canjea por una sesión.
 *  - Contexto del form baja por postMessage; las propuestas de cambio suben y se
 *    aplican en memoria con el bridge `record._update` (Guardar/Descartar nativo).
 *
 * Protocolo postMessage (alineado con el hook `useEmbedBridge` del SPA):
 *  Odoo → SPA: { source: "tuqui-odoo", type: "auth",     payload: { client_id, nonce } }
 *              { source: "tuqui-odoo", type: "context",  payload: PageContext }
 *              { source: "tuqui-odoo", type: "new-chat" }
 *  SPA → Odoo: { source: "tuqui-spa",  type: "ready" }
 *              { source: "tuqui-spa",  type: "apply",    payload: { changes, rationale } }
 *              { source: "tuqui-spa",  type: "chatter",  payload: { mode, body, subject } }
 *              { source: "tuqui-spa",  type: "navigate", payload: { model, mode, viewType, domain, defaults, title } }
 *              { source: "tuqui-spa",  type: "location", payload: { path } }
 *
 * El host valida que cada mensaje venga del iframe montado (`ev.source ===
 * iframe.contentWindow`) Y de un origin concreto que matchee el del SPA (nunca
 * "*"). Ver `_handleMessage`.
 */
export class TuquiPanel extends Component {
    static props = {};
    static template = "tuqui_assistant.Panel";

    setup() {
        this.tuquiAssistant = useService("tuquiAssistant");
        this.notification = useService("notification");
        this.state = useState(this.tuquiAssistant.state); // { panelOpen, context }
        this.ui = useState({
            // Resolved from the companion (oauth client): whether connected, the
            // Tuqui base URL (tuqui.base_url), and the activated workspace slug.
            // ADR 0001 §2.2 — the embed URL is no longer a hardcoded param.
            connected: false,
            baseUrl: null,
            slug: null,
            chatEnabled: false,
            embedReady: false,
            // Last SPA path (e.g. /w/:slug/chat/:id), persisted in localStorage so
            // the conversation resumes on page reload or panel reopen. Updated when
            // the SPA posts "location"; used in embedUrl to load the right
            // conversation in the iframe.
            storedPath: null,
        });
        this.iframeRef = useRef("iframe");

        const _storageKey = () => this.ui.slug ? `tuqui_embed_path_${this.ui.slug}` : null;

        const loadBootstrap = async () => {
            const boot = await this.tuquiAssistant.getEmbedBootstrap();
            this.ui.connected = Boolean(boot?.connected);
            this.ui.baseUrl = boot?.base_url || null;
            this.ui.slug = boot?.slug || null;
            this.ui.chatEnabled = Boolean(boot?.chat_enabled);
            // Restore the last conversation from localStorage (keyed by slug to
            // avoid cross-workspace contamination). Used in embedUrl so the iframe
            // loads where the user left off instead of starting a new chat.
            if (this.ui.slug) {
                this.ui.storedPath = localStorage.getItem(_storageKey()) || null;
            }
        };
        onWillStart(loadBootstrap);
        // Re-check on panel open/close:
        //   - Open: re-verify connection (admin may have disconnected) + reset auth.
        //   - Close: sync storedPath from localStorage so the next open loads the
        //     right conversation. Done on CLOSE (not open) because the iframe is
        //     already unmounted (t-if), so storedPath can change without causing a
        //     src reload. The SPA only posts "location" while mounted → localStorage
        //     is stable at close time.
        useEffect(
            () => {
                if (this.state.panelOpen) {
                    this.ui.embedReady = false;
                    this._authPosted = false;
                    this._lastSpaPath = null;
                    void loadBootstrap();
                } else if (this.ui.slug) {
                    // Panel closed → iframe unmounted → safe to update.
                    this.ui.storedPath = localStorage.getItem(_storageKey()) || null;
                }
            },
            () => [this.state.panelOpen]
        );

        // postMessage bridge with the SPA iframe.
        this._onMessage = this._handleMessage.bind(this);
        onMounted(() => window.addEventListener("message", this._onMessage));
        onWillUnmount(() => window.removeEventListener("message", this._onMessage));

        // When the context changes (open record / unsaved edits), re-push it
        // to the iframe if it is already ready.
        useEffect(
            () => {
                if (this.ui.connected && this.ui.embedReady) {
                    this._postContext();
                }
            },
            () => [this._contextKey()]
        );

        // Systray "new chat" (CTO item #4): systray increments newChatRequest
        // when the panel is already open. Post `new-chat` to the SPA so it
        // navigates internally (no iframe remount → no second SSO nonce spent).
        // Gated by embedReady: if the iframe hasn't posted "ready" yet, the SPA
        // has no listener; the "just opened" case starts a new chat anyway.
        useEffect(
            () => {
                if (this.state.newChatRequest > 0 && this.ui.connected && this.ui.embedReady) {
                    this._postNewChat();
                }
            },
            () => [this.state.newChatRequest]
        );
    }

    // --- iframe (L1) ---

    get embedUrl() {
        if (!this.ui.connected || !this.ui.baseUrl || !this.ui.slug) {
            return "";
        }
        const base = this.ui.baseUrl.replace(/\/+$/, "");
        const slug = encodeURIComponent(this.ui.slug);
        if (this.ui.storedPath) {
            // Convert standalone path /w/:slug/... → embed /embed/:slug/...
            // The SPA uses the same route structure in both modes; only the
            // prefix differs. Validate the substitution before using it.
            const embedded = this.ui.storedPath.replace(/^\/w\/[^/]+/, `/embed/${slug}`);
            if (embedded.startsWith(`/embed/${slug}`)) {
                return `${base}${embedded}?embed=1`;
            }
        }
        return `${base}/embed/${slug}?embed=1`;
    }

    get _spaOrigin() {
        try {
            return new URL(this.ui.baseUrl).origin;
        } catch {
            console.warn("[tuqui_assistant] Invalid baseUrl, cannot derive SPA origin:", this.ui.baseUrl);
            return null;
        }
    }

    _contextKey() {
        const c = this.state.context;
        if (!c) {
            return "";
        }
        switch (c.kind) {
            case "record":
                // Include dirty: when the user edits a field (many2one, etc.) the
                // form becomes dirty, state.context updates, and the panel re-pushes
                // fresh field values to the SPA.
                return `record:${c.model}:${c.resId}:${c.dirty}`;
            case "selection":
                return `sel:${c.model}:${c.count}:${(c.resIds || []).length}`;
            case "list":
                return `list:${c.model}:${c.count}:${JSON.stringify(c.domain || [])}`;
        }
        return "";
    }

    async _postAuth() {
        // Embedded SSO: mint a nonce bound to the Odoo user and pass it to the
        // iframe; the SPA redeems it for a session token (no login prompt). See
        // ADR 0001 / spec §2.2. If the companion is not active, getSsoAuth() → null.
        //
        // Only once per panel open: the SPA posts "ready" multiple times (gate +
        // ChatPage + re-renders) and an extra nonce gets spent twice → 401 on
        // exchange → apiFetch logs the user out. The flag is reset on every open.
        //
        // The lock is set BEFORE the await: two concurrent "ready" events would
        // both pass the check if the flag were set after, each minting its own nonce.
        // Only the first advances; subsequent ones see the flag already set.
        // Reset to false on all error paths to allow a retry.
        if (this._authPosted) {
            return;
        }
        this._authPosted = true;
        const win = this.iframeRef.el?.contentWindow;
        if (!win) {
            this._authPosted = false;
            return;
        }
        const auth = await this.tuquiAssistant.getSsoAuth();
        if (!auth?.nonce || !auth?.client_id) {
            this._authPosted = false;
            return;
        }
        try {
            win.postMessage(
                {
                    source: "tuqui-odoo",
                    type: "auth",
                    payload: { client_id: auth.client_id, nonce: auth.nonce },
                },
                this._spaOrigin
            );
        } catch {
            // different origin / iframe not yet navigated: retried on the next "ready"
            this._authPosted = false;
        }
    }

    _postContext() {
        const win = this.iframeRef.el?.contentWindow;
        if (!win) {
            return;
        }
        try {
            win.postMessage(
                {
                    source: "tuqui-odoo",
                    type: "context",
                    payload: this.tuquiAssistant.getContextPayload(),
                },
                this._spaOrigin
            );
        } catch (e) {
            // different origin / iframe not yet navigated: retried on next "ready".
            // Logged (warn, not silent): a DataCloneError here — payload containing a
            // non-cloneable reactive Proxy — once caused list/kanban context to
            // silently never reach the SPA. getContextPayload now returns plain JSON,
            // but if it breaks again it should surface.
            console.warn("[tuqui_assistant] Could not post context to iframe:", e);
        }
    }

    _postNewChat() {
        // Ask the SPA (already mounted and hydrated) to open a new chat as an
        // INTERNAL navigation (no src change → no second SSO nonce spent). Handled
        // in the SPA via useEmbedBridge (onNewChat). Concrete origin like all other
        // posts (never "*").
        const win = this.iframeRef.el?.contentWindow;
        if (!win) {
            return;
        }
        try {
            win.postMessage({ source: "tuqui-odoo", type: "new-chat" }, this._spaOrigin);
        } catch {
            // different origin / iframe not yet navigated: next click retries.
        }
    }

    _handleMessage(ev) {
        const data = ev.data;
        if (!data || data.source !== "tuqui-spa") {
            return;
        }
        // Security (defence in depth). An attacker can spoof `{source:"tuqui-spa"}`
        // from another window/origin, so checking source alone is NOT enough — both
        // conditions must hold, no shortcuts.
        //
        // 1) The message MUST come from OUR iframe. If the iframe is not mounted
        //    (no companion connected, panel closed), `iframeRef.el` is null →
        //    reject rather than accept blindly (the check used to be skipped when
        //    there was no iframe).
        const frame = this.iframeRef.el;
        if (!frame || ev.source !== frame.contentWindow) {
            return;
        }
        // 2) The origin MUST match the SPA's and be concrete. If `_spaOrigin`
        //    is null (invalid / unresolved baseUrl), discard.
        const spaOrigin = this._spaOrigin;
        if (!spaOrigin || ev.origin !== spaOrigin) {
            return;
        }
        switch (data.type) {
            case "ready":
                this.ui.embedReady = true;
                this._postAuth(); // SSO: send nonce + client_id to iframe (before context)
                this._postContext();
                break;
            case "apply":
                this.tuquiAssistant.applyProposal(data.payload?.changes || {});
                break;
            case "chatter":
                // Chatter content proposal: opens the standard Odoo composer
                // pre-filled (user reviews and sends). NEVER posted silently —
                // human dispatch is structural.
                this.tuquiAssistant.proposeChatter(data.payload || {});
                break;
            case "navigate":
                // Odoo navigation from chat: opens a NEW form (create) or a
                // filtered list/pivot/graph via standard act_window (checks
                // permissions). Does NOT write anything.
                this.tuquiAssistant.navigate(data.payload || {});
                break;
            case "location":
                // The SPA reports its current standalone-equivalent path
                // (`/w/:slug/...`) on every embed route change. Stored so "Open in
                // Tuqui" opens the conversation/view the user is on, not /settings/home.
                // Validated as a safe relative path (starts with "/" but not "//")
                // before trusting it for URL construction.
                // Also persisted to localStorage (keyed by slug) to resume the
                // conversation on page reload or reopen. storedPath is NOT updated
                // here (only on panel-close) to avoid a reactive change causing a
                // src reload while the iframe is mounted.
                {
                    const path = data.payload?.path;
                    if (typeof path === "string" && path.startsWith("/") && !path.startsWith("//")) {
                        this._lastSpaPath = path;
                        if (this.ui.slug) {
                            localStorage.setItem(`tuqui_embed_path_${this.ui.slug}`, path);
                        }
                    }
                }
                break;
        }
    }

    // --- común ---

    // Labels for icon-only header buttons (title + aria-label for screen readers).
    // Wrapped in _t() so they are translatable.
    get openInTuquiLabel() {
        return _t("Open in Tuqui");
    }
    get minimizeLabel() {
        return _t("Minimize");
    }
    get closeLabel() {
        return _t("Close");
    }
    get restoreLabel() {
        return _t("Open Tuqui");
    }

    close() {
        this.tuquiAssistant.togglePanel();
    }

    // Minimize to bubble: hides the card via CSS (does NOT unmount the iframe →
    // no second SSO nonce spent). Restore shows it again.
    minimize() {
        this.tuquiAssistant.minimize();
    }

    restore() {
        this.tuquiAssistant.restore();
    }

    // Full workspace web app URL to open in a new tab. Opens the user's CURRENT
    // view in the embed (conversation / projects / agents), not the dashboard: the
    // SPA posts its standalone-equivalent path (`_lastSpaPath`, e.g.
    // `/w/:slug/chat/:id`) on every route change. Falls back to `/w/:slug`
    // (→ /chat → workspace home) if no `location` has arrived yet (just mounted).
    // Returns "" when base or slug is missing (companion not connected).
    get _tuquiAppUrl() {
        const base = (this.ui.baseUrl || "").replace(/\/+$/, "");
        if (!base || !this.ui.slug) {
            return "";
        }
        const path = this._lastSpaPath || `/w/${encodeURIComponent(this.ui.slug)}`;
        return `${base}${path}`;
    }

    // "Open in Tuqui" (CTO item #1): opens the workspace web app at the CURRENT
    // view, in a new tab. No-op when base or slug is missing (companion not connected).
    openInTuqui() {
        const url = this._tuquiAppUrl;
        if (!url) {
            this.notification.add(
                _t("Tuqui is not connected: cannot open the app."),
                { type: "warning" }
            );
            return;
        }
        window.open(url, "_blank", "noopener");
    }

}

registry.category("main_components").add("tuqui_assistant.Panel", { Component: TuquiPanel });
