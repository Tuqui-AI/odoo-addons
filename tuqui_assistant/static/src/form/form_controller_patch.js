/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
import { useService } from "@web/core/utils/hooks";
import { useEffect } from "@odoo/owl";

/**
 * Publishes the active form record to the assistant service while this
 * FormController is mounted. Mirrors the pattern from Odoo Enterprise's `ai`
 * module (patch on FormController.prototype + access to this.model.root).
 *
 * `this.model.root` is the root record; exposes resModel, resId, data (in-memory
 * values including unsaved changes), and fields.
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
            // Include dirty so context is re-pushed to the SPA when the user
            // edits a field (many2one, etc.) — without it the SPA sees stale values.
            () => [this.model.root.resModel, this.model.root.resId, this.model.root.dirty]
        );
    },
});
