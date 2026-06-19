/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
import { useService } from "@web/core/utils/hooks";
import { useEffect } from "@odoo/owl";

/**
 * Publica el record activo del formulario al servicio del asistente mientras
 * este FormController está montado. Espeja el patrón del módulo `ai` de Odoo
 * Enterprise (patch a FormController.prototype + acceso a this.model.root).
 *
 * `this.model.root` es el record raíz; expone resModel, resId, data (valores en
 * memoria, incluye cambios sin guardar) y fields.
 */
patch(FormController.prototype, {
    setup() {
        super.setup();
        this.tuquiAssistant = useService("tuquiAssistant");
        useEffect(
            () => {
                this.tuquiAssistant.setRecordContext(this, this.model.root);
                return () => this.tuquiAssistant.clearContext(this);
            },
            () => [this.model.root.resModel, this.model.root.resId]
        );
    },
});
