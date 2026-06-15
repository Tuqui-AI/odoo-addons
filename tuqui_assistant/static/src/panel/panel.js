/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

/**
 * Panel lateral del asistente. En el spike: muestra el contexto del registro
 * abierto y permite "proponer" cambios a mano (JSON { campo: valor }) que se
 * aplican al formulario en memoria vía el servicio. Reemplaza al iframe del SPA
 * de Tuqui, que se integra después (capa 1 / L1).
 */
export class TuquiPanel extends Component {
    static props = {};
    static template = "tuqui_assistant.Panel";

    setup() {
        this.tuquiAssistant = useService("tuquiAssistant");
        this.notification = useService("notification");
        this.state = useState(this.tuquiAssistant.state);
        this.ui = useState({ rawChanges: "" });
    }

    close() {
        this.tuquiAssistant.togglePanel();
    }

    async onApply() {
        let changes;
        try {
            changes = JSON.parse(this.ui.rawChanges || "{}");
        } catch {
            this.notification.add(_t("JSON inválido en la propuesta."), { type: "danger" });
            return;
        }
        await this.tuquiAssistant.applyProposal(changes);
    }
}

registry.category("main_components").add("tuqui_assistant.Panel", { Component: TuquiPanel });
