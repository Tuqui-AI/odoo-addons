/** @odoo-module **/
import { Component, useState, useRef, useEffect, onWillStart, onMounted, onWillUnmount, onPatched } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

let _msgSeq = 0;

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
            draft: "",
            spaUrl: null,
            embedReady: false,
            messages: [
                {
                    id: ++_msgSeq,
                    role: "assistant",
                    text: _t(
                        "Hola, soy Tuqui. Todavía me estoy conectando a mi cerebro real; " +
                            "por ahora puedo proponer cambios al formulario que tengas abierto. " +
                            'Pegá un JSON de campos (ej. {"priority": "1"}) y te lo propongo ' +
                            "para que lo revises antes de guardar."
                    ),
                },
            ],
        });
        this.iframeRef = useRef("iframe");
        this.bodyRef = useRef("body");

        onWillStart(async () => {
            this.ui.spaUrl = await this.tuquiAssistant.getSpaUrl();
        });

        // Puente postMessage con el iframe del SPA.
        this._onMessage = this._handleMessage.bind(this);
        onMounted(() => window.addEventListener("message", this._onMessage));
        onWillUnmount(() => window.removeEventListener("message", this._onMessage));

        // Cuando cambia el contexto (record abierto / cambios sin guardar),
        // re-empujarlo al iframe si ya está listo.
        useEffect(
            () => {
                if (this.ui.spaUrl && this.ui.embedReady) {
                    this._postContext();
                }
            },
            () => [this._contextKey()]
        );

        onPatched(() => this._scrollToBottom());
    }

    // --- iframe (L1) ---

    get embedUrl() {
        if (!this.ui.spaUrl) {
            return "";
        }
        const sep = this.ui.spaUrl.includes("?") ? "&" : "?";
        return `${this.ui.spaUrl}${sep}embed=1`;
    }

    get _spaOrigin() {
        try {
            return new URL(this.ui.spaUrl).origin;
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

    _scrollToBottom() {
        const el = this.bodyRef.el;
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    }

    // --- shell nativo (fallback) ---

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.onSend();
        }
    }

    _addMessage(msg) {
        this.ui.messages.push({ id: ++_msgSeq, ...msg });
    }

    onSend() {
        const text = (this.ui.draft || "").trim();
        if (!text) {
            return;
        }
        this._addMessage({ role: "user", text });
        this.ui.draft = "";

        const changes = this._tryParseChanges(text);
        if (changes) {
            this._addMessage({
                role: "assistant",
                text: _t("Te propongo estos cambios para el formulario abierto:"),
                proposal: {
                    changes,
                    selected: Object.fromEntries(Object.keys(changes).map((k) => [k, true])),
                    applied: false,
                },
            });
        } else {
            this._addMessage({
                role: "assistant",
                text: _t(
                    "Todavía no estoy conectado a Tuqui real, así que no puedo razonar tu " +
                        "mensaje. Mientras tanto, pegá un JSON de campos y te propongo los " +
                        "cambios al formulario."
                ),
            });
        }
    }

    _tryParseChanges(text) {
        try {
            const obj = JSON.parse(text);
            if (obj && typeof obj === "object" && !Array.isArray(obj) && Object.keys(obj).length) {
                return obj;
            }
        } catch {
            // No es JSON: lo tratamos como mensaje de chat normal.
        }
        return null;
    }

    fieldsOf(proposal) {
        return Object.keys(proposal.changes);
    }

    setField(proposal, field, checked) {
        proposal.selected[field] = checked;
    }

    formatValue(value) {
        if (value === null || value === undefined) {
            return "∅";
        }
        if (typeof value === "object") {
            return JSON.stringify(value);
        }
        return String(value);
    }

    async applyProposal(message) {
        const proposal = message.proposal;
        const picked = {};
        for (const f of Object.keys(proposal.changes)) {
            if (proposal.selected[f]) {
                picked[f] = proposal.changes[f];
            }
        }
        if (!Object.keys(picked).length) {
            this.notification.add(_t("No seleccionaste ningún campo."), { type: "warning" });
            return;
        }
        const ok = await this.tuquiAssistant.applyProposal(picked);
        if (ok) {
            proposal.applied = true;
            this._addMessage({
                role: "system",
                text: _t(
                    "Apliqué %s campo(s) al formulario (sin guardar). Revisá y usá Guardar o " +
                        "Descartar de Odoo.",
                    Object.keys(picked).length
                ),
            });
        }
    }

    discardProposal(message) {
        message.proposal.applied = true;
        this._addMessage({ role: "system", text: _t("Propuesta descartada.") });
    }
}

registry.category("main_components").add("tuqui_assistant.Panel", { Component: TuquiPanel });
