/** @odoo-module **/

/**
 * What the chat can ask this Odoo to do, in one place.
 *
 * WHY IT IS ONE PLACE. The same six instructions arrive through two different
 * doors, and which door depends only on who is inside whom: through the panel
 * when Tuqui is framed by Odoo, through the nested bridge when Odoo is framed by
 * Tuqui. The instruction — apply this proposal, open that view, put the mark
 * there — is the same either way, and a second copy of this switch would be a
 * second place to forget a case. That is not hypothetical: it is exactly how the
 * mark ended up working in one direction and not the other.
 *
 * WHAT IS NOT HERE, and deliberately. Messages that are about the CHAT rather
 * than about Odoo — where the conversation navigated to, an external link about
 * to open, the iframe announcing it is ready — belong to whoever is showing that
 * chat, which is the panel and only the panel. They stay there.
 *
 * Nothing here validates WHO sent the message: by the time a caller gets here it
 * has already answered that, each in the way its own door allows (the panel
 * checks its iframe and the SPA's origin; the bridge checks our parent window
 * and the companion's origin). Keeping the gate at the door and the meaning here
 * is what lets a new door reuse the meaning without inheriting a check that does
 * not fit it.
 */

/**
 * Run the instruction `type` carries, and say whether it was one we know.
 *
 * @param {object} service the `tuquiAssistant` service
 * @param {string} type the message type, as the protocol names it
 * @param {object} payload what came with it (never trusted as instructions)
 * @returns {boolean} false when this is not an Odoo action, so the caller can
 *  handle its own messages without a list of names to keep in sync
 */
export function runOdooAction(service, type, payload = {}) {
    switch (type) {
        case "apply":
            // `baseRevision` (opcional): la revisión del contexto sobre la que el
            // chat razonó. Con eso el servicio detecta si el usuario tocó alguno
            // de esos campos después y no lo pisa en silencio.
            service.applyProposal(payload.changes || {}, { baseRevision: payload.baseRevision });
            return true;
        case "chatter":
            // Propuesta de contenido para el chatter: abre el compositor estándar
            // de Odoo ya escrito, y la persona lo revisa y lo manda. NUNCA se
            // postea solo — que lo dispare un humano es estructural.
            service.proposeChatter(payload);
            return true;
        case "save":
            // El usuario pidió guardar. Odoo valida; si rechaza, el servicio lo
            // dice en vez de cantar victoria.
            service.saveRecord();
            return true;
        case "spotlight":
            // La gota: señalar en la pantalla dónde hay que hacer algo, con el
            // puntero de `web_tour` (el que la gente ya conoce del onboarding).
            // Si no cae, `spotlightOrWarn` se lo dice a la persona.
            service.spotlightOrWarn(payload);
            return true;
        case "reload":
            // El turno escribió en Odoo por atrás: los datos de la vista quedaron
            // viejos. Relee sin recargar la página. El servicio se niega si el
            // form está sucio — no le pisamos al usuario lo que está escribiendo
            // por refrescar un dato.
            service.reloadView();
            return true;
        case "navigate":
            // Abre un formulario NUEVO (create) o una vista filtrada por el
            // act_window estándar, que chequea permisos. No escribe nada.
            service.navigate(payload);
            return true;
    }
    return false;
}
