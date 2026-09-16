/** @odoo-module **/

/**
 * Hacer algo cuando la pantalla DEJÓ DE MOVERSE, no cuando el estado cambió.
 *
 * POR QUÉ NO ALCANZA CON EL AVISO. El aviso de que la pantalla cambió llega
 * ANTES de que se dibuje, así que leerla ahí lee la anterior. Esperar un frame
 * tampoco alcanza: en un formulario, el chatter y las pestañas los montan otros
 * componentes, más tarde.
 *
 * QUÉ SE ROMPÍA CON ESO, medido dos veces y en las dos direcciones del embebido:
 *
 *  · En una ficha de contacto viajaba UN botón —el de la barra de alrededor—
 *    mientras el formulario tenía cuatro, y sin "Log note" el agente no puede
 *    ofrecer la nota interna.
 *  · Y peor: el mapa de qué campos están ocultos se leía a medio dibujar, así que
 *    campos que SÍ se ven salían marcados como invisibles. Con eso el agente se
 *    niega a señalarlos, con toda la razón desde su lado: "el campo `list_price`
 *    está oculto en la vista actual" sobre una pantalla donde "Sales Price" se
 *    está viendo. Un dato de más es peor que ninguno — el vacío se nota, el
 *    equivocado no.
 *
 * SE MIDE, NO SE ADIVINA. En vez de un número mágico de milisegundos, se mira si
 * la cantidad de cosas señalables dejó de cambiar entre dos vistazos. Con tope,
 * porque una pantalla que se mueve sola —un contador, un reloj— no puede
 * postergar el aviso para siempre.
 *
 * @param {() => void} fn
 * @param {{steps?: number, every?: number}} [opciones]
 */
export function whenTheScreenSettles(fn, { steps = 12, every = 60 } = {}) {
    let anterior = -1;
    let quedan = steps;
    const mirar = () => {
        const ahora = document.querySelectorAll("button, a.btn, .o_field_widget").length;
        if (ahora === anterior || quedan-- <= 0) {
            fn();
            return;
        }
        anterior = ahora;
        setTimeout(mirar, every);
    };
    requestAnimationFrame(mirar);
}
