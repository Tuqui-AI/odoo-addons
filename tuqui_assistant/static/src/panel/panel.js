/** @odoo-module **/
import { Component, useState, useRef, useEffect, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";


/**
 * Panel lateral del asistente Tuqui.
 *
 * Dos modos:
 *  - L1 (iframe): si está seteado el system parameter `tuqui_assistant.spa_url`,
 *    embebe el SPA de Tuqui (modo embed) y habla con él por `postMessage`:
 *    contexto del form baja, propuesta de cambios sube → se aplica con el bridge
 *    `record._update`. Reusa TODA la UI de Tuqui (composer, micrófono, artifacts).
 *  - Fallback nativo: shell de chat OWL (composer + tarjetas de propuesta con
 *    accept/reject por campo). Útil sin SPA configurado.
 *
 * Protocolo postMessage (alineado con el hook `useEmbedBridge` del SPA):
 *  Odoo → SPA: { source: "tuqui-odoo", type: "context", payload: PageContext }
 *  SPA → Odoo: { source: "tuqui-spa",  type: "ready" }
 *              { source: "tuqui-spa",  type: "apply", payload: { changes, rationale } }
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
        // re-empujarlo al iframe si ya está listo.
        useEffect(
            () => {
                if (this.ui.connected && this.ui.embedReady) {
                    this._postContext();
                }
            },
            () => [this._contextKey()]
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
        } catch {
            // origin distinto / iframe aún no navegado: se reintenta al próximo "ready"
        }
    }

    _handleMessage(ev) {
        const data = ev.data;
        if (!data || data.source !== "tuqui-spa") {
            return;
        }
        // Seguridad: el mensaje debe venir de nuestro iframe y del origin del SPA.
        if (this.iframeRef.el && ev.source !== this.iframeRef.el.contentWindow) {
            return;
        }
        if (this._spaOrigin !== "*" && ev.origin !== this._spaOrigin) {
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
        }
    }

    // --- común ---

    get contextLabel() {
        const c = this.state.context;
        if (!c) {
            return _t("Sin contexto");
        }
        switch (c.kind) {
            case "record": {
                const name = c.displayName ? ` · ${c.displayName}` : "";
                return `${c.model} #${c.resId}${name}`;
            }
            case "selection":
                return c.allMatching
                    ? _t("%s · %s seleccionados (todo el filtro)", c.model, c.count)
                    : _t("%s · %s seleccionados", c.model, c.count);
            case "list":
                return _t("%s · lista filtrada (~%s)", c.model, c.count ?? "?");
        }
        return c.model || "";
    }

    close() {
        this.tuquiAssistant.togglePanel();
    }

}

registry.category("main_components").add("tuqui_assistant.Panel", { Component: TuquiPanel });
