/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
import { useService } from "@web/core/utils/hooks";
import { useEffect, useExternalListener, onWillUnmount } from "@odoo/owl";

// Espera antes de re-publicar el contexto después de que el usuario sale de un
// campo. On-blur + debounce, NUNCA por tecla: el caso de uso es "trabajemos
// sobre lo que escribí", no "mirame tipear" — una revisión por tecla infla el
// contexto de cada turno (costo de LLM y ruido) sin agregar valor. Ver la spec
// `assistant-en-odoo-confiabilidad` §Contexto vivo.
const CONTEXT_DEBOUNCE_MS = 400;

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

        // Contexto vivo. El `useEffect` de arriba solo reacciona a `dirty`, que es
        // un booleano: flipea una vez (false→true) en la PRIMERA edición y después
        // se queda quieto, así que el SPA veía los valores de esa primera edición
        // para siempre. Acá escuchamos el `focusout` (salir de un campo) y, con
        // debounce, le pedimos al servicio que re-publique. El servicio compara
        // contra lo último publicado y no hace nada si nada cambió, así que un blur
        // sin edición no cuesta una revisión.
        //
        // El listener va en `document` (capture) porque los campos del form viven
        // en subárboles variados (grillas x2many, notebooks, diálogos). Cada
        // FormController montado agenda lo suyo, pero `refreshRecordContext(this)`
        // no hace nada si este controller no es el dueño del contexto — así el form
        // de abajo no pisa el contexto del diálogo que tiene arriba.
        this._tuquiContextTimeout = null;
        useExternalListener(
            document,
            "focusout",
            () => {
                clearTimeout(this._tuquiContextTimeout);
                this._tuquiContextTimeout = setTimeout(() => {
                    this._tuquiContextTimeout = null;
                    this.tuquiAssistant.refreshRecordContext(this);
                }, CONTEXT_DEBOUNCE_MS);
            },
            { capture: true }
        );
        onWillUnmount(() => clearTimeout(this._tuquiContextTimeout));
    },
});
