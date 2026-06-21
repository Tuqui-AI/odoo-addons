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
 *  Odoo → SPA: { source: "tuqui-odoo", type: "auth",    payload: { client_id, nonce } }
 *              { source: "tuqui-odoo", type: "context", payload: PageContext }
 *  SPA → Odoo: { source: "tuqui-spa",  type: "ready" }
 *              { source: "tuqui-spa",  type: "apply",   payload: { changes, rationale } }
 *              { source: "tuqui-spa",  type: "chatter", payload: { mode, body, subject } }
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
            embedReady: false,
        });
        this.iframeRef = useRef("iframe");

        const loadBootstrap = async () => {
            const boot = await this.tuquiAssistant.getEmbedBootstrap();
            this.ui.connected = Boolean(boot?.connected);
            this.ui.baseUrl = boot?.base_url || null;
            this.ui.slug = boot?.slug || null;
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
        // re-empujarlo al iframe si ya está listo. Gateado por `followContext`
        // (Fase 3a): si el pin está OFF, navegar a otro registro NO empuja
        // contexto nuevo → la conversación congela el suyo. Se sigue dependiendo
        // de `state.followContext` para que reactivar el pin re-dispare el push.
        useEffect(
            () => {
                if (this.ui.connected && this.ui.embedReady && this.state.followContext) {
                    this._postContext();
                }
            },
            () => [this._contextKey(), this.state.followContext]
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
            return "*";
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
        if (this._authPosted) {
            return;
        }
        const win = this.iframeRef.el?.contentWindow;
        if (!win) {
            return;
        }
        const auth = await this.tuquiAssistant.getSsoAuth();
        if (!auth?.nonce || !auth?.client_id) {
            return;
        }
        this._authPosted = true;
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
            console.warn("[tuqui_assistant] no se pudo postear el contexto al iframe:", e);
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
        //    cayó a "*" (baseUrl inválida / sin resolver), NO validamos contra
        //    "*" (aceptaría cualquier origin) — sin origin concreto, descartamos.
        const spaOrigin = this._spaOrigin;
        if (spaOrigin === "*" || ev.origin !== spaOrigin) {
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
        }
    }

    // --- común ---

    // Etiquetas de los botones icon-only del header (title + aria-label, para que
    // los lectores de pantalla los anuncien). Wrapped en _t() → traducibles.
    get openInTuquiLabel() {
        return _t("Abrir en Tuqui");
    }
    get followContextLabel() {
        return _t("Seguir el contexto de Odoo");
    }
    get expandLabel() {
        return this.state.expanded ? _t("Contraer") : _t("Expandir");
    }
    get minimizeLabel() {
        return _t("Minimizar");
    }
    get closeLabel() {
        return _t("Cerrar");
    }
    get restoreLabel() {
        return _t("Abrir Tuqui");
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

    // Expandir / colapsar la card (Fase 2). La clase la aplica el template vía
    // t-att-class sobre state.expanded.
    toggleExpand() {
        this.tuquiAssistant.toggleExpand();
    }

    // Pin "Seguir contexto" (Fase 3a): togglea si los cambios de registro en Odoo
    // se empujan al iframe. El gate efectivo vive en el useEffect del _contextKey.
    toggleFollowContext() {
        this.tuquiAssistant.toggleFollowContext();
    }

    // URL de la web app COMPLETA del workspace (ruta canónica /w/:slug → dashboard;
    // ver main.tsx routes). Misma fuente que embedUrl (base_url + slug de
    // getEmbedBootstrap). "" si falta base/slug.
    get _tuquiAppUrl() {
        const base = (this.ui.baseUrl || "").replace(/\/+$/, "");
        if (!base || !this.ui.slug) {
            return "";
        }
        return `${base}/w/${encodeURIComponent(this.ui.slug)}`;
    }

    // "Abrir en Tuqui" (item CTO #1): abre la web app del workspace en una pestaña
    // nueva. Guard si falta slug/base (companion no conectado).
    openInTuqui() {
        const url = this._tuquiAppUrl;
        if (!url) {
            this.notification.add(
                _t("Tuqui no está conectado: no se puede abrir la app."),
                { type: "warning" }
            );
            return;
        }
        window.open(url, "_blank", "noopener");
    }

}

registry.category("main_components").add("tuqui_assistant.Panel", { Component: TuquiPanel });
