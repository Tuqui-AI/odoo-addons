/** @odoo-module **/
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { session } from "@web/session";
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

// El ícono existe SOLO para quien tiene chat en Tuqui. Una sola regla, sin
// excepción para admins: antes de conectar el companion no lo ve nadie, y el
// camino de descubrimiento es Ajustes (spec systray-solo-para-usuarios-con-chat).
// Antes se registraba incondicionalmente y el que no tenía acceso terminaba en
// un 404 que se lee como un bug.
//
// El chequeo va acá, en tiempo de import del asset, y no como t-if en el
// template: así el item no llega a entrar al registry y el DOM no lo contiene
// —no queda escondido por CSS—. session_info ya viene resuelto en el page load,
// así que esto no cuesta un request ni hace aparecer el ícono medio segundo
// tarde. El precio es que el cambio se ve en el próximo reload de la página; el
// botón "Sync chat access" de Ajustes recarga justamente por eso.
if (session.tuqui_has_chat) {
    registry
        .category("systray")
        .add("tuqui_assistant.systray", { Component: TuquiSystray }, { sequence: 31 });
}
