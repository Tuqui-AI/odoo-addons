/** @odoo-module **/
import { Component, useState, useRef, useEffect, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { listenSizeChange, MEDIAS_BREAKPOINTS, SIZES } from "@web/core/ui/ui_service";
import { _t } from "@web/core/l10n/translation";

/**
 * The size Odoo would report for a viewport of `width`.
 *
 * Asked of Odoo's own table instead of a copy of it: the index into
 * MEDIAS_BREAKPOINTS IS the SIZES value (that is how `utils.getSize` reads it),
 * and the table is not the same across versions — 18.0 has seven bands and puts
 * XXL at 1534, 19.0 has six and puts it at 1400. A mirrored list would give the
 * wrong answer on the other version while looking perfectly right.
 *
 * @returns {number} one of SIZES
 */
export function sizeForWidth(width) {
    // Floored on the way in: the widths this gets come from
    // `getBoundingClientRect`, which is fractional, and Odoo's table leaves a
    // 1px hole between bands (…1199 | 1200…). A width of 1199.4 matched no band
    // at all, `findIndex` returned -1 and a wide screen was reported as the
    // smallest size. The observer samples every frame of the 340ms morph, so on
    // a 1920px screen the remaining room travels 1528→1152 and lands in that
    // hole on the way.
    const floored = Math.floor(width);
    const index = MEDIAS_BREAKPOINTS.findIndex(
        ({ minWidth, maxWidth }) =>
            (minWidth === undefined || floored >= minWidth) &&
            (maxWidth === undefined || floored <= maxWidth)
    );
    return index === -1 ? SIZES.XS : index;
}

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
 *              { source: "tuqui-odoo", type: "resume",   payload: { path } }
 *  SPA → Odoo: { source: "tuqui-spa",  type: "ready" }
 *              { source: "tuqui-spa",  type: "apply",    payload: { changes, rationale } }
 *              { source: "tuqui-spa",  type: "chatter",  payload: { mode, body, subject } }
 *              { source: "tuqui-spa",  type: "navigate", payload: { model, mode, viewType, domain, defaults, title } }
 *              { source: "tuqui-spa",  type: "reload" }   // releer la vista tras una escritura por atrás
 *              { source: "tuqui-spa",  type: "save" }     // guardar el form abierto (lo pidió el usuario)
 *              { source: "tuqui-spa",  type: "location", payload: { path } }
 *              { source: "tuqui-spa",  type: "external-link-opening" }
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
            embedReady: false,
            // Last SPA path (e.g. /w/:slug/chat/:id), persisted in localStorage so
            // the conversation resumes on page reload or panel reopen. Updated when
            // the SPA posts "location"; used in embedUrl to load the right
            // conversation in the iframe.
            storedPath: null,
        });
        this.iframeRef = useRef("iframe");
        this.panelRef = useRef("panel");
        this.uiService = useService("ui");

        const _storageKey = () => this.ui.slug ? `tuqui_embed_path_${this.ui.slug}` : null;

        const loadBootstrap = async () => {
            const boot = await this.tuquiAssistant.getEmbedBootstrap();
            this.ui.connected = Boolean(boot?.connected);
            this.ui.baseUrl = boot?.base_url || null;
            this.ui.slug = boot?.slug || null;
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
                    if (this._panelObserver && this.panelRef.el) {
                        this._panelObserver.observe(this.panelRef.el);
                    }
                } else {
                    // Panel closed → iframe unmounted → safe to update stored path.
                    if (this.ui.slug) {
                        this.ui.storedPath = localStorage.getItem(_storageKey()) || null;
                    }
                    // Reset expanded state; the useEffect on state.expanded removes
                    // the html class automatically.
                    if (this.state.expanded) {
                        this.tuquiAssistant.contract();
                    }
                }
            },
            () => [this.state.panelOpen]
        );

        // postMessage bridge with the SPA iframe.
        this._onMessage = this._handleMessage.bind(this);

        // Odoo stacks its own chat launchers/windows pinned to the bottom-right
        // corner — exactly where our floating card and bubble live, so they overlap.
        // Instead of a fixed gap, measure how much room that cluster takes on the
        // right edge and slide Tuqui left by that much via the --o-tuqui-chat-offset
        // CSS var. With no chat open the cluster is empty → offset 0 → Tuqui sits
        // flush right; when a window or bubble appears Tuqui slides left beside it.
        //
        // NOTE: the .o-mail-ChatHub root is a zero-size STATIC wrapper — it has no
        // position/size of its own; every visible piece inside it is position:fixed
        // (each .o-mail-ChatWindow and the .o-mail-ChatHub-bubbles pile). Measuring
        // the hub returns a 0×0 rect, so the offset never kicked in (#70191). Measure
        // the fixed children instead and take the leftmost edge of the whole cluster.
        const _syncChatOffset = () => {
            let offset = 0;
            const parts = document.querySelectorAll(
                ".o-mail-ChatWindow, .o-mail-ChatHub-bubbles"
            );
            let minLeft = Infinity;
            for (const el of parts) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    minLeft = Math.min(minLeft, rect.left);
                }
            }
            if (minLeft !== Infinity) {
                offset = Math.max(0, Math.round(window.innerWidth - minLeft) + 12);
            }
            document.documentElement.style.setProperty("--o-tuqui-chat-offset", `${offset}px`);
        };
        // Coalesce bursts of DOM mutations into a single measurement per frame.
        let _offsetRaf = null;
        const _scheduleSyncChatOffset = () => {
            if (_offsetRaf !== null) {
                return;
            }
            _offsetRaf = requestAnimationFrame(() => {
                _offsetRaf = null;
                _syncChatOffset();
            });
        };
        this._chatHubObserver = null;

        // Odoo lays out for the VIEWPORT, not for the room it actually has. The
        // chatter sits beside the form only while `ui.size` is XXL (see
        // `mail/…/chatter/web/form_renderer.js`), and expanding the panel does not
        // change the viewport — it only takes width away through the body padding.
        // The chatter therefore stayed on the side and got squeezed together with
        // the breadcrumb and the pager. Reporting the size of what is LEFT makes
        // Odoo lay out for that instead: the chatter drops below the form, exactly
        // as it would in a narrower window, and every other view follows.
        //
        // The ui service recomputes `size` on every window resize, so this has to
        // run again after it — hence listenSizeChange, which fires once the
        // service has already written its own value.
        //
        // And it cannot be measured once: the card takes 340ms to travel from
        // floating to docked, so a single read right after the click returns the
        // width it is LEAVING. On a 1920px screen that was 392 instead of 768 —
        // 1528px of room instead of 1152 — which reads as XXL and left the
        // chatter on the side, exactly the case this whole thing exists for. A
        // ResizeObserver on the card answers on every frame of the morph, and
        // keeps answering if the width ever changes for another reason.
        // Three things this has to get right, and each one bit:
        //
        //  - `isSmall` is a plain field of the same reactive, not derived from
        //    `size`: the service writes both together and `env.isSmall` reads
        //    that field. Writing only `size` left ~480 call sites across addons
        //    reading a stale boolean that now contradicts the size.
        //  - Nobody re-renders on `size` alone. The chatter redraws on a
        //    debounce hung off the window `resize`, so a value written after the
        //    first frame — which is every value the observer produces while the
        //    card travels — reached no one without saying so.
        //  - The dispatch would loop: the service recomputes from the viewport,
        //    finds a different value, writes it and fires the bus, which calls
        //    this back. Announcing only when OUR value actually changed breaks
        //    the cycle while still re-asserting the value the service just
        //    overwrote.
        let _lastAnnounced = null;
        const _applySize = (size) => {
            const changed = size !== _lastAnnounced;
            _lastAnnounced = size;
            if (this.uiService.size !== size) {
                this.uiService.size = size;
            }
            const small = size <= SIZES.SM;
            if (this.uiService.isSmall !== small) {
                this.uiService.isSmall = small;
            }
            if (changed) {
                window.dispatchEvent(new Event("resize"));
            }
        };

        this._sizeOverridden = false;
        const _syncHostSize = () => {
            const covering = this.state.expanded && !this.state.minimized;
            if (!covering) {
                if (this._sizeOverridden) {
                    this._sizeOverridden = false;
                    _applySize(sizeForWidth(window.innerWidth));
                }
                return;
            }
            const panelWidth = this.panelRef.el?.getBoundingClientRect().width || 0;
            this._sizeOverridden = true;
            _applySize(sizeForWidth(Math.max(0, window.innerWidth - panelWidth)));
        };

        onMounted(() => {
            window.addEventListener("message", this._onMessage);
            this._stopListeningSize = listenSizeChange(_syncHostSize);
            // The bus is not enough: the service compares against the value WE
            // wrote, so when the real size happens to equal the override it
            // never fires — and the panel would keep reporting a stale one.
            window.addEventListener("resize", _syncHostSize);
            this._panelObserver = new ResizeObserver(_syncHostSize);
            if (this.panelRef.el) {
                this._panelObserver.observe(this.panelRef.el);
            }
            // A chat window opening/closing adds or removes nodes under <body>,
            // changing the ChatHub footprint — re-measure on any such mutation.
            _syncChatOffset();
            this._chatHubObserver = new MutationObserver(_scheduleSyncChatOffset);
            this._chatHubObserver.observe(document.body, { childList: true, subtree: true });
            window.addEventListener("resize", _scheduleSyncChatOffset);
        });
        onWillUnmount(() => {
            window.removeEventListener("message", this._onMessage);
            this._stopListeningSize?.();
            window.removeEventListener("resize", _syncHostSize);
            this._panelObserver?.disconnect();
            // Give Odoo its real size back: leaving the override in place would
            // keep the chatter below the form after the panel is gone.
            if (this._sizeOverridden) {
                this._sizeOverridden = false;
                _applySize(sizeForWidth(window.innerWidth));
            }
            document.documentElement.classList.remove("o-tuqui-expanded");
            this._chatHubObserver?.disconnect();
            window.removeEventListener("resize", _scheduleSyncChatOffset);
            if (_offsetRaf !== null) {
                cancelAnimationFrame(_offsetRaf);
            }
            document.documentElement.style.removeProperty("--o-tuqui-chat-offset");
        });

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

        // Manage the split-screen class on <html> via a useEffect so it fires
        // AFTER OWL has updated the DOM (panel already has o-tuqui-expanded class
        // and its expanded layout). If classList.add were called in expand() directly,
        // it would run before OWL's render → body padding kicks in while the panel
        // is still in its small position → layout doesn't reflow correctly on the
        // first expand (Ctrl+R was needed to fix it).
        //
        // The split-screen gutter only makes sense while the card is visible.
        // Hiding the card keeps `expanded` set so showing it again brings the
        // expanded layout back — so the gutter has to be released on hide and
        // re-applied on show. Gating on !minimized (and depending on it) fixes
        // the reserved empty space that lingered when hiding from the expanded
        // state.
        useEffect(
            () => {
                if (this.state.expanded && !this.state.minimized) {
                    document.documentElement.classList.add("o-tuqui-expanded");
                } else {
                    document.documentElement.classList.remove("o-tuqui-expanded");
                }
                requestAnimationFrame(() => {
                    // First read after the class lands; the ResizeObserver keeps
                    // reading through the morph. Both go through _applySize,
                    // which announces the change itself.
                    _syncHostSize();
                    window.dispatchEvent(new Event("resize"));
                });
            },
            () => [this.state.expanded, this.state.minimized]
        );
    }

    // --- iframe (L1) ---

    get embedUrl() {
        if (!this.ui.connected || !this.ui.baseUrl || !this.ui.slug) {
            return "";
        }
        const base = this.ui.baseUrl.replace(/\/+$/, "");
        const slug = encodeURIComponent(this.ui.slug);
        // Always load the root embed URL. Deep-linking to a conversation path
        // causes the SPA to try fetching it before authentication (the nonce
        // arrives only after "ready"), so it renders a 404. The stored path is
        // sent as a "resume" postMessage right after the auth message instead.
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
                // Include revision: `dirty` es booleano y flipea UNA vez, así que no
                // alcanza para el contexto vivo. La revisión sube cada vez que los
                // valores en memoria cambian (on-blur, debounced) → re-push.
                return `record:${c.model}:${c.resId}:${c.dirty}:${c.revision}`;
            case "selection":
                // Los ids, no su cantidad — misma razón que las deps del efecto en
                // `list_controller_patch.js`. Si acá quedara la longitud, el
                // controller re-publicaría al cambiar QUÉ está tildado y el panel
                // lo deduplicaría igual, dejando el arreglo de allá sin efecto.
                // Y con el cap de ids alto la clave vieja degeneraba: para toda
                // selección que entra entera `count === resIds.length`, así que
                // sus dos mitades eran el mismo número.
                return `sel:${c.model}:${c.count}:${(c.resIds || []).join(",")}`;
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
            // After auth, tell the SPA to resume at the last conversation — but
            // only on "restore" opens (page reload with panel already open, or a
            // tab triggered by an external-link signal). Deliberate close → reopen
            // from the systray should start a fresh chat, not resume. The one-time
            // flag is consumed here so any subsequent open in this session starts fresh.
            if (this.ui.storedPath && this.tuquiAssistant.consumeResumeOnOpen()) {
                win.postMessage(
                    { source: "tuqui-odoo", type: "resume", payload: { path: this.ui.storedPath } },
                    this._spaOrigin
                );
            }
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
                // `baseRevision` (opcional): la revisión del contexto sobre la que
                // el SPA razonó. Con eso el servicio detecta si el usuario tocó
                // alguno de esos campos después y no lo pisa en silencio.
                this.tuquiAssistant.applyProposal(data.payload?.changes || {}, {
                    baseRevision: data.payload?.baseRevision,
                });
                break;
            case "chatter":
                // Chatter content proposal: opens the standard Odoo composer
                // pre-filled (user reviews and sends). NEVER posted silently —
                // human dispatch is structural.
                this.tuquiAssistant.proposeChatter(data.payload || {});
                break;
            case "save":
                // El usuario pidió guardar. Odoo valida; si rechaza, el servicio
                // lo dice en vez de cantar victoria.
                this.tuquiAssistant.saveRecord();
                break;
            case "reload":
                // El turno escribió en Odoo por atrás: los datos de la vista
                // quedaron viejos. Relee sin recargar la página. El servicio se
                // niega si el form está sucio — no le pisamos al usuario lo que
                // está escribiendo por refrescar un dato.
                this.tuquiAssistant.reloadView();
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
            case "external-link-opening":
                // The user clicked an external link in the chat. Write a short-lived
                // signal to localStorage so the new Odoo tab (opened by target="_blank")
                // knows to auto-open the panel and resume this conversation.
                // The signal is consumed and cleared on the next service init; the TTL
                // guards against stale signals in case the user opens a new Odoo tab
                // independently later.
                try {
                    localStorage.setItem("tuqui_open_signal", JSON.stringify({ at: Date.now() }));
                } catch {
                    // Private browsing or quota exceeded — skip silently.
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
    get hideLabel() {
        return _t("Hide");
    }
    get expandLabel() {
        return _t("Expand");
    }
    get contractLabel() {
        return _t("Contract");
    }

    // Hides the card via CSS (it does NOT unmount the iframe → no second SSO
    // nonce spent, and the conversation is still there when it comes back). The
    // systray icon brings it back: there is no bubble launcher any more, so the
    // corner is left to Odoo's own chat windows.
    hide() {
        this.tuquiAssistant.minimize();
    }

    expand() {
        this.tuquiAssistant.expand();
    }

    contract() {
        this.tuquiAssistant.contract();
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
