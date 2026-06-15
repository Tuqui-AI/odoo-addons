/** @odoo-module **/
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * Botón en el systray que abre/cierra el panel del asistente Tuqui.
 */
export class TuquiSystray extends Component {
    static props = {};
    static template = "tuqui_assistant.Systray";

    setup() {
        this.tuquiAssistant = useService("tuquiAssistant");
    }

    onClick() {
        this.tuquiAssistant.togglePanel();
    }
}

registry
    .category("systray")
    .add("tuqui_assistant.systray", { Component: TuquiSystray }, { sequence: 31 });
