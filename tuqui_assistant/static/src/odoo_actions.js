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
 * Run the instruction `type` carries, and say whether it was one we know AND
 * what happened.
 *
 * WHY THE RESULT COMES BACK, and this is the point of the whole file: every
 * action here used to be one-way. The chat dispatched, this ran it, and whatever
 * happened stayed on this side of the glass — Odoo told the PERSON with a toast
 * and the assistant was never told. So anything the assistant said next about
 * the outcome was a guess, and measured across four implementations it guessed
 * optimistic every time: "I saved it" over a form Odoo had refused, "I marked
 * the button" over a mark that landed on a column header.
 *
 * Returning it HERE and not in each caller is what makes it universal: a new
 * action added to this switch inherits the way back without anyone remembering
 * to wire it.
 *
 * @param {object} service the `tuquiAssistant` service
 * @param {string} type the message type, as the protocol names it
 * @param {object} payload what came with it (never trusted as instructions)
 * @returns {Promise<{handled: boolean, result?: object}>} `handled: false` when
 *  this is not an Odoo action, so the caller can handle its own messages without
 *  a list of names to keep in sync. When it is ours, `result` carries what
 *  happened — `{ok, reason, detail?}` for the actions that already know.
 */
export async function runOdooAction(service, type, payload = {}) {
    switch (type) {
        case "apply":
            // `baseRevision` (opcional): la revisión del contexto sobre la que el
            // chat razonó. Con eso el servicio detecta si el usuario tocó alguno
            // de esos campos después y no lo pisa en silencio.
            service.applyProposal(payload.changes || {}, { baseRevision: payload.baseRevision });
            return { handled: true, result: { ok: null, reason: "dispatched" } };
        case "chatter":
            // Propuesta de contenido para el chatter: abre el compositor estándar
            // de Odoo ya escrito, y la persona lo revisa y lo manda. NUNCA se
            // postea solo — que lo dispare un humano es estructural.
            service.proposeChatter(payload);
            return { handled: true, result: { ok: null, reason: "dispatched" } };
        case "save":
            // El usuario pidió guardar. Odoo valida; si rechaza, el servicio lo
            // dice en vez de cantar victoria.
            // EL ÚNICO QUE YA SABE SU RESULTADO, y por eso es el piloto de este
            // camino: Odoo contesta si guardó y por qué no.
            return { handled: true, result: await service.saveRecord() };
        case "spotlight":
            // La gota: señalar en la pantalla dónde hay que hacer algo, con el
            // puntero de `web_tour` (el que la gente ya conoce del onboarding).
            // Si no cae, `spotlightOrWarn` se lo dice a la persona.
            service.spotlightOrWarn(payload);
            return { handled: true, result: { ok: null, reason: "dispatched" } };
        case "reload":
            // El turno escribió en Odoo por atrás: los datos de la vista quedaron
            // viejos. Relee sin recargar la página. El servicio se niega si el
            // form está sucio — no le pisamos al usuario lo que está escribiendo
            // por refrescar un dato.
            service.reloadView();
            return { handled: true, result: { ok: null, reason: "dispatched" } };
        case "navigate":
            // Abre un formulario NUEVO (create) o una vista filtrada por el
            // act_window estándar, que chequea permisos. No escribe nada.
            service.navigate(payload);
            return { handled: true, result: { ok: null, reason: "dispatched" } };
    }
    return { handled: false };
}
