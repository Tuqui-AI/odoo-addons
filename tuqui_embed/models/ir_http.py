"""Dejar que un origen declarado muestre la pantalla de Odoo dentro de un iframe.

EL PROBLEMA, EN CRIOLLO. Quien conversa con el asistente sobre un pedido termina
saltando entre dos pestañas: la conversación de un lado, el registro del otro.
Poder ver el registro al lado de lo que se está hablando es la diferencia entre
"contame qué dice" y "mirá lo que dice". Odoo, por defecto, no deja que ninguna
página suya se muestre dentro de otro sitio.

POR QUÉ NO LO DEJA, Y POR QUÉ ESO ESTÁ BIEN. Un iframe ajeno puede ponerse
encima de la pantalla de Odoo, invisible, y hacer que el usuario le haga clic a
algo que no ve (clickjacking). El default de Odoo protege de eso, y este módulo
NO lo saca: lo cambia por una lista de orígenes que el administrador declara. Sin
esa lista cargada, no cambia nada.

QUÉ HACE FALTA, Y SON DOS COSAS. Permitir el frame no alcanza: la cookie de
sesión de Odoo sale ``SameSite=Lax``, que el browser NO manda en un iframe de
otro sitio. Con sólo lo primero, el iframe se ve — pero mostrando el login.

EL PUNTO A DECIDIR (ver README): aflojar el ``SameSite`` de la sesión normal
reduce una protección contra CSRF que hoy existe. Y se aplica a TODA respuesta de
este Odoo, no sólo a las que vienen del panel: acotarlo por origen es imposible
sin llegar tarde —el primer pedido del iframe llega sin cookie— y la cookie es
una sola, que el browser guarda con el último atributo que vio. El diseño que
evita eso —una
cookie aparte, de vida corta, sólo para las rutas del embed— es más trabajo y
toca el manejo de sesión de Odoo.
"""

import logging

from odoo import models
from odoo.http import get_session_max_inactivity, request

_logger = logging.getLogger(__name__)

#: Parámetro que enciende el módulo. Lista de orígenes separados por espacios,
#: exactamente como los espera ``frame-ancestors``:
#:
#:     tuqui.embed_origins = "https://tuqui.com https://staging.tuqui.com"
#:
#: Vacío o ausente (el default) = Odoo se comporta como siempre.
EMBED_ORIGINS_PARAM = "tuqui.embed_origins"


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _tuqui_embed_origins(cls):
        """Los orígenes autorizados a embeber, o ``None`` si está apagado.

        Se lee por request y no se cachea a propósito: revocar un permiso de
        embeber tiene que surtir efecto cuando el administrador lo saca, no
        cuando alguien reinicie el servidor.
        """
        try:
            value = request.env["ir.config_parameter"].sudo().get_param(EMBED_ORIGINS_PARAM)
        except Exception:
            # Todavía no hay env (rutas ``auth='none'``, errores tempranos del
            # dispatch). Sin poder leer la lista, el default es no permitir.
            return None
        value = (value or "").strip()
        return value or None

    @classmethod
    def _post_dispatch(cls, response):
        super()._post_dispatch(response)
        origins = cls._tuqui_embed_origins()
        if not origins:
            return
        try:
            # 1. QUIÉN puede embeber. `frame-ancestors` le gana a X-Frame-Options
            #    en los browsers actuales, pero igual se saca el XFO: un browser
            #    que sólo entienda XFO tiene que ver la lista, no un DENY.
            response.headers.pop("X-Frame-Options", None)
            # `'self'` VA SIEMPRE. Odoo embebe sus propias páginas en iframes del
            # mismo origen —el visor de PDF y de texto (`file_viewer.xml`) y el
            # preview de reportes— y una lista sin `'self'` los deja en blanco
            # para TODA la base en cuanto se prende el switch. El default de Odoo
            # es, justamente, `frame-ancestors 'self'`.
            #
            # Y la CSP se COMPLETA, no se reemplaza: `set_csp` le pone
            # `default-src 'none'` a toda respuesta `image/*` (odoo/http.py), que
            # es lo que sandboxea un SVG subido como adjunto. Sobrescribir el
            # header dejaba ese SVG ejecutando script en el origen de Odoo — un
            # agujero que no tiene nada que ver con embeber, y que aparecía en
            # todas las respuestas, no sólo en las embebidas.
            csp = response.headers.get("Content-Security-Policy") or ""
            directivas = [
                d.strip() for d in csp.split(";") if d.strip() and not d.strip().lower().startswith("frame-ancestors")
            ]
            directivas.append("frame-ancestors 'self' %s" % origins)
            response.headers["Content-Security-Policy"] = "; ".join(directivas)

            # 2. QUE LA SESIÓN VIAJE dentro del iframe. Sin esto, el paso 1 sólo
            #    logra que se vea la pantalla de login en vez de un rectángulo en
            #    blanco: el browser no manda una cookie `SameSite=Lax` en un
            #    frame de otro sitio.
            #
            #    SE APLICA A TODA RESPUESTA, no sólo a las que se detectan como
            #    embebidas, y no por comodidad: **detectarlo llega tarde**.
            #
            #    Es un huevo y gallina, medido contra un navegador de verdad. El
            #    primer pedido que hace el iframe llega con `Referer` del panel
            #    —o sea, se lo puede detectar perfectamente— pero llega SIN
            #    COOKIE, porque la que estaba guardada en ese momento era la
            #    normal (`SameSite=Lax`) y esa no viaja dentro de un frame ajeno.
            #    Odoo lo trata como anónimo y redirige al login. La cookie se
            #    afloja en la respuesta… de una navegación que ya terminó mal.
            #
            #    Aflojarla sólo "cuando hace falta" es, entonces, aflojarla
            #    siempre un pedido tarde. Ver el README: el riesgo de que la
            #    sesión salga `SameSite=None` está medido —desde otro origen no
            #    se puede leer (CORS) ni escribir (rutas JSON-only)— y esto lo
            #    amplía de "los pedidos que vienen del panel" a "todos los de
            #    este Odoo". Sigue gobernado por el parámetro: **sin
            #    `tuqui.embed_origins` cargado, este método ya salió arriba y no
            #    toca ninguna cookie.**

            # `response.headers` de Odoo es un proxy sin `__delitem__`, así que la
            # cookie no se reescribe a mano: se vuelve a setear con los flags que
            # hacen falta, y un `Set-Cookie` posterior para la misma clave gana.
            #
            # `SameSite=None` EXIGE `Secure`; sobre http:// eso sólo lo acepta el
            # browser en localhost (origen "potentially trustworthy"). Cualquier
            # despliegue real es HTTPS, así que no cambia nada ahí.
            sess = request.session
            # Las mismas dos condiciones que pone Odoo antes de emitir la cookie
            # (`_save_session`), porque aflojarle el SameSite no puede además
            # cambiar CUÁNDO se emite ni CUÁNTO vive.
            #
            # 1. Una sesión que el servidor no guarda no se anuncia. `can_save`
            #    es False en los pedidos stateless —header `x-odoo-database`,
            #    `auth="bearer"`— y ahí devolver un `session_id` inventa del lado
            #    del cliente una sesión que del otro lado no existe.
            if not getattr(sess, "can_save", True):
                return
            # 2. Una respuesta cacheable en público no lleva `Set-Cookie`. Los
            #    assets salen `public, max-age=1 año, immutable`; un CDN o un
            #    nginx con `proxy_ignore_headers Set-Cookie` guardaría la
            #    respuesta CON la cookie y le serviría la sesión de una persona a
            #    las demás.
            if "public" in (response.headers.get("Cache-Control") or "").lower():
                return
            sid = getattr(sess, "sid", None)
            if sid:
                response.set_cookie(
                    "session_id",
                    sid,
                    # Sin `max_age`, el `set_cookie` de Odoo usa `expires=-1` y la
                    # cookie queda con UN AÑO de vida, reemplazando la ventana de
                    # inactividad configurada. Aflojar el SameSite no puede, de
                    # paso, desarmar la caducidad de la sesión.
                    max_age=get_session_max_inactivity(request.env),
                    httponly=True,
                    secure=True,
                    samesite="None",
                )
                cls._tuqui_partition_last_session_cookie(response)
        except Exception:
            # Un módulo de conveniencia no puede tumbar una respuesta de Odoo.
            # Tampoco fallar callado: sin el log, "el panel se ve en blanco" no
            # tendría dónde investigarse.
            _logger.exception("tuqui_embed: no se pudo aplicar la política de embed")

    @classmethod
    def _tuqui_partition_last_session_cookie(cls, response):
        """Bind the loosened cookie to (Odoo, the site that framed it) — CHIPS.

        `SameSite=None` makes the cookie travel to ANY window that embeds
        Odoo, not just the one declared in `tuqui.embed_origins` — the
        module only decides who is allowed to *show the frame*, it doesn't
        stop the browser from sending the cookie into someone else's frame.
        Two vectors slip through that gap regardless of `embed_origins`,
        because neither is blocked by CORS or by the frame check:

          * a cross-origin WebSocket handshake (`/websocket` is
            `auth="public", cors="*"`) reads the user's live bus;
          * a bare `<img src=".../web/become">` on any page a logged-in
            admin happens to open escalates them to superuser — no click,
            no form, a single GET.

        `Partitioned` closes both without touching either endpoint: the
        browser keys the cookie by (top-level site, this origin) instead of
        just this origin, so a page on a DIFFERENT top-level site gets an
        empty partition — the WebSocket and the `<img>` never see this
        cookie at all, no matter what `embed_origins` says.

        werkzeug 2.2 (what this Odoo pins) has no `partitioned=` kwarg on
        `set_cookie` yet, so it's appended to the header by hand. Only the
        LAST `Set-Cookie: session_id=...` is touched — Odoo's own
        `SameSite=Lax` cookie, set earlier in the same response, is left
        alone.

        The scan runs backwards instead of assuming our reissue is the very
        last header: if some other `_post_dispatch` ever appends a cookie
        after ours, an index-based guard would fall through and ship the
        loosened cookie WITHOUT its partition — the insecure state, and
        silently. Not finding it at all is logged rather than ignored, for
        the same reason: this is the mitigation, so it fails loud.
        """
        cookies = response.headers.getlist("Set-Cookie")
        target = next(
            (i for i in range(len(cookies) - 1, -1, -1) if cookies[i].startswith("session_id=")),
            None,
        )
        if target is None:
            _logger.warning(
                "tuqui_embed: la sesión se reemitió pero no se encontró su Set-Cookie para "
                "particionar; la cookie sale SameSite=None SIN Partitioned"
            )
            return
        if "Partitioned" in cookies[target]:
            return
        cookies[target] = cookies[target] + "; Partitioned"
        response.headers.setlist("Set-Cookie", cookies)
