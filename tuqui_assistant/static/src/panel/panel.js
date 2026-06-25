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
            // Resuelto del companion (oauth client): si está conectado, la base
            // de Tuqui (tuqui.base_url) y el slug del workspace activado. Ver
            // ADR 0001 §2.2 — la URL del embed ya no es un param hardcodeado.
            connected: false,
            baseUrl: null,
            slug: null,
            chatEnabled: false,
            embedReady: false,
        });
        this.iframeRef = useRef("iframe");

        const loadBootstrap = async () => {
            const boot = await this.tuquiAssistant.getEmbedBootstrap();
            this.ui.connected = Boolean(boot?.connected);
            this.ui.baseUrl = boot?.base_url || null;
            this.ui.slug = boot?.slug || null;
            this.ui.chatEnabled = Boolean(boot?.chat_enabled);
        };
        onWillStart(loadBootstrap);
        // Re-chequear al abrir el panel: si un admin desconectó companion mientras
        // estaba cerrado, refleja el estado nuevo (y el corte duro vive además en
        // issue_for_current_user, que exige state=='active').
        useEffect(
            () => {
                if (this.state.panelOpen) {
                    this.ui.embedReady = false;
                    this._authPosted = false;
                    // El iframe remonta en `/embed/:slug` (chat nuevo) al reabrir
                    // → la ruta vieja queda stale. La olvidamos hasta que el SPA
                    // re-postee `location` (lo hace al montar EmbedShell), para
                    // que "Abrir en Tuqui" no abra una conversación anterior.
                    this._lastSpaPath = null;
                    void loadBootstrap();
                }
            },
            () => [this.state.panelOpen]
        );

        // Puente postMessage con el iframe del SPA.
        this._onMessage = this._handleMessage.bind(this);
        onMounted(() => window.addEventListener("message", this._onMessage));
        onWillUnmount(() => window.removeEventListener("message", this._onMessage));

        // Cuando cambia el contexto (record abierto / cambios sin guardar),
        // re-empujarlo al iframe si ya está listo.
        useEffect(
            () => {
                if (this.ui.connected && this.ui.embedReady) {
                    this._postContext();
                }
            },
            () => [this._contextKey()]
        );

        // Systray "chat nuevo" (item CTO #4): el systray incrementa
        // newChatRequest cuando el panel YA estaba abierto. Le posteamos
        // `new-chat` al SPA para que navegue internamente a un chat nuevo (sin
        // remontar el iframe → sin gastar un 2º nonce SSO). Gateado por
        // embedReady: si el iframe todavía no avisó "ready", el SPA no tiene
        // listener montado; igual el caso "recién abierto" arranca en chat nuevo.
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
        return `${base}/embed/${encodeURIComponent(this.ui.slug)}?embed=1`;
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
                return `record:${c.model}:${c.resId}`;
            case "selection":
                return `sel:${c.model}:${c.count}:${(c.resIds || []).length}`;
            case "list":
                return `list:${c.model}:${c.count}:${JSON.stringify(c.domain || [])}`;
        }
        return "";
    }

    async _postAuth() {
        // SSO embebido: mintea un nonce atado al usuario Odoo y se lo pasa al
        // iframe; el SPA lo canjea por un token de sesión (sin login). Ver
        // ADR 0001 / spec §2.2. Sin companion activado, getSsoAuth() → null.
        //
        // UNA sola vez por apertura del panel: el SPA postea "ready" varias veces
        // (gate + ChatPage + re-renders) y un nonce de más se gasta dos veces →
        // 401 en el exchange → apiFetch desloguea. El flag se resetea al abrir.
        //
        // El lock se fija ANTES del await: dos "ready" concurrentes pasarían el
        // check juntos si el flag se pusiera después, cada uno minting su propio
        // nonce. Solo el primero avanza; los siguientes ven el flag ya activo.
        // Se resetea a false en todos los caminos de error para permitir reintento.
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
            // origin distinto / iframe aún no navegado: se reintenta al próximo "ready"
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
            // origin distinto / iframe aún no navegado: se reintenta al próximo
            // "ready". Lo logueamos (warn, no silencioso): un DataCloneError acá
            // —payload con un Proxy reactivo no clonable— hizo que el contexto de
            // lista/kanban NUNCA llegara al SPA sin dejar rastro. getContextPayload
            // ahora devuelve JSON plano, pero si vuelve a fallar, que se vea.
            console.warn("[tuqui_assistant] Could not post context to iframe:", e);
        }
    }

    _postNewChat() {
        // Le pide al SPA (ya montado e hidratado) que arranque un chat nuevo como
        // navegación INTERNA (no cambia el src del iframe → no gasta un 2º nonce
        // SSO). El SPA lo maneja en useEmbedBridge (onNewChat). Mismo origin
        // concreto que el resto de los posts (nunca "*").
        const win = this.iframeRef.el?.contentWindow;
        if (!win) {
            return;
        }
        try {
            win.postMessage({ source: "tuqui-odoo", type: "new-chat" }, this._spaOrigin);
        } catch {
            // origin distinto / iframe aún no navegado: el próximo click reintenta.
        }
    }

    _handleMessage(ev) {
        const data = ev.data;
        if (!data || data.source !== "tuqui-spa") {
            return;
        }
        // Seguridad (defensa en profundidad). Un atacante puede falsificar
        // `{source:"tuqui-spa"}` desde otra ventana/origin, así que NO alcanza
        // con mirar el source: exigimos las dos condiciones, sin atajos.
        //
        // 1) El mensaje TIENE que venir de NUESTRO iframe. Si el iframe no está
        //    montado (sin companion conectado, panel cerrado), `iframeRef.el` es
        //    null → rechazamos en vez de aceptar a ciegas (antes el check se
        //    salteaba cuando no había iframe).
        const frame = this.iframeRef.el;
        if (!frame || ev.source !== frame.contentWindow) {
            return;
        }
        // 2) El origin TIENE que ser el del SPA y ser concreto. Si `_spaOrigin`
        //    es null (baseUrl inválida / sin resolver), descartamos.
        const spaOrigin = this._spaOrigin;
        if (!spaOrigin || ev.origin !== spaOrigin) {
            return;
        }
        switch (data.type) {
            case "ready":
                this.ui.embedReady = true;
                this._postAuth(); // SSO: nonce + client_id al iframe (antes del contexto)
                this._postContext();
                break;
            case "apply":
                this.tuquiAssistant.applyProposal(data.payload?.changes || {});
                break;
            case "chatter":
                // Propuesta de contenido para el chatter: abre el compositor
                // estándar de Odoo pre-cargado (el usuario revisa y envía). NUNCA
                // se publica en silencio — el dispatch humano es estructural.
                this.tuquiAssistant.proposeChatter(data.payload || {});
                break;
            case "navigate":
                // Navegación de Odoo desde el chat: abre un formulario NUEVO
                // (crear) o una lista/pivot/gráfico filtrado, vía act_window
                // estándar (chequea permisos). NO escribe nada.
                this.tuquiAssistant.navigate(data.payload || {});
                break;
            case "location":
                // El SPA nos dice su ruta standalone-equivalente actual
                // (`/w/:slug/...`) en cada cambio de ruta del embed. La guardamos
                // para que "Abrir en Tuqui" abra la conversación/vista que el
                // usuario está mirando, no /settings/home. Validamos que sea un
                // path relativo seguro (empieza con "/" pero no "//") antes de
                // confiar en él para construir una URL absoluta.
                {
                    const path = data.payload?.path;
                    if (typeof path === "string" && path.startsWith("/") && !path.startsWith("//")) {
                        this._lastSpaPath = path;
                    }
                }
                break;
        }
    }

    // --- común ---

    // Etiquetas de los botones icon-only del header (title + aria-label, para que
    // los lectores de pantalla los anuncien). Wrapped en _t() → traducibles.
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

    // Minimizar a burbuja: oculta la card por CSS (NO desmonta el iframe → no
    // gasta un 2º nonce SSO). Restaurar vuelve a mostrarla.
    minimize() {
        this.tuquiAssistant.minimize();
    }

    restore() {
        this.tuquiAssistant.restore();
    }

    // URL de la web app COMPLETA del workspace en una pestaña nueva. Abre la
    // VISTA ACTUAL del usuario en el embed (conversación / proyectos / agentes),
    // no el dashboard: el SPA nos postea su ruta standalone-equivalente
    // (`_lastSpaPath`, p.ej. `/w/:slug/chat/:id`) en cada cambio de ruta. Si
    // todavía no llegó ningún `location` (recién montado), caemos al canónico
    // `/w/:slug` (→ /chat → home del workspace). "" si falta base/slug.
    get _tuquiAppUrl() {
        const base = (this.ui.baseUrl || "").replace(/\/+$/, "");
        if (!base || !this.ui.slug) {
            return "";
        }
        const path = this._lastSpaPath || `/w/${encodeURIComponent(this.ui.slug)}`;
        return `${base}${path}`;
    }

    // "Abrir en Tuqui" (item CTO #1): abre la web app del workspace en la VISTA
    // ACTUAL, en una pestaña nueva. Guard si falta slug/base (companion no conectado).
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
