/** @odoo-module **/
import { describe, expect, test } from "@odoo/hoot";
import { apagarElPanelAnidado, estamosAnidados } from "@tuqui_embed/anidado";

/**
 * El bucle que esto corta: Tuqui muestra Odoo en su panel → ese Odoo abre su
 * propio panel de Tuqui → ese Tuqui restaura su panel con Odoo → …
 * Cada nivel carga un cliente web completo y se cuelga el navegador entero.
 */

/** Un `window` de mentira, con storages que se pueden espiar. */
function ventana({ anidada, tiroSecurityError = false } = {}) {
    const session = { tuqui_panel_state: '{"panelOpen":true}' };
    const local = { tuqui_open_signal: '{"at":1}' };
    const clases = [];
    const win = {
        self: "yo",
        sessionStorage: { removeItem: (k) => delete session[k] },
        localStorage: { removeItem: (k) => delete local[k] },
        document: { documentElement: { classList: { add: (c) => clases.push(c) } } },
        _session: session,
        _local: local,
        _clases: clases,
    };
    if (tiroSecurityError) {
        Object.defineProperty(win, "top", {
            get() {
                throw new Error("SecurityError: Blocked a frame with origin…");
            },
        });
    } else {
        win.top = anidada ? "otro" : "yo";
    }
    return win;
}

describe("Odoo mostrado adentro de otra cosa", () => {
    test("estando anidado, el panel deja de abrirse solo", () => {
        const win = ventana({ anidada: true });
        expect(apagarElPanelAnidado(win)).toBe(true);
        // Las DOS puertas por las que el panel se abre solo:
        expect(win._session.tuqui_panel_state).toBe(undefined);
        expect(win._local.tuqui_open_signal).toBe(undefined);
    });

    test("la señal de localStorage también se limpia, y ese es el caso real", () => {
        // `sessionStorage` es por pestaña, pero la señal de apertura vive en
        // `localStorage`, que se comparte entre TODAS las pestañas y iframes de
        // este Odoo. Por eso alcanzaba con haber usado el panel en otra pestaña
        // para que el Odoo embebido abriera el suyo.
        const win = ventana({ anidada: true });
        apagarElPanelAnidado(win);
        expect(win._local.tuqui_open_signal).toBe(undefined);
    });

    test("y se esconde el botón, o lo abrirían a mano igual", () => {
        const win = ventana({ anidada: true });
        apagarElPanelAnidado(win);
        expect(win._clases).toInclude("tuqui-anidado");
    });

    test("en un Odoo normal no toca NADA", () => {
        // Esto es lo que hace que el módulo sea seguro de instalar: fuera de un
        // iframe, el archivo no existe a efectos prácticos.
        const win = ventana({ anidada: false });
        expect(apagarElPanelAnidado(win)).toBe(false);
        expect(win._session.tuqui_panel_state).toBe('{"panelOpen":true}');
        expect(win._local.tuqui_open_signal).toBe('{"at":1}');
        expect(win._clases).toEqual([]);
    });

    test("si ni siquiera se puede mirar quién nos contiene, se asume anidado", () => {
        // Leer `window.top` de otro origen tira SecurityError. Ese error ES la
        // respuesta: si no lo podemos mirar, es de otro. Tragárselo y seguir
        // como si nada dejaría el bucle abierto justo en el caso cross-origin,
        // que es EL caso (Tuqui y Odoo son orígenes distintos).
        const win = ventana({ tiroSecurityError: true });
        expect(estamosAnidados(win)).toBe(true);
        expect(apagarElPanelAnidado(win)).toBe(true);
    });

    test("un storage que explota no impide esconder el botón", () => {
        // Navegación privada: `removeItem` puede tirar. Si eso corta la función,
        // el botón queda visible y el bucle vuelve por la puerta de al lado.
        const win = ventana({ anidada: true });
        win.sessionStorage.removeItem = () => {
            throw new Error("SecurityError");
        };
        win.localStorage.removeItem = () => {
            throw new Error("SecurityError");
        };
        expect(apagarElPanelAnidado(win)).toBe(true);
        expect(win._clases).toInclude("tuqui-anidado");
    });
});
