/** @odoo-module **/
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * Botón en el systray del asistente Tuqui. Click → SIEMPRE abre el panel en un
 * chat NUEVO (item CTO #4): ya no togglea. Cerrar/minimizar vive en los botones
 * propios de la card. openFreshChat resuelve el caso "ya abierto" sin remontar
 * el iframe (no gasta un 2º nonce SSO).
 */
export class TuquiSystray extends Component {
    static props = {};
    static template = "tuqui_assistant.Systray";

    setup() {
        this.tuquiAssistant = useService("tuquiAssistant");
    }

    onClick() {
        this.tuquiAssistant.openFreshChat();
    }
}

registry
    .category("systray")
    .add("tuqui_assistant.systray", { Component: TuquiSystray }, { sequence: 31 });
