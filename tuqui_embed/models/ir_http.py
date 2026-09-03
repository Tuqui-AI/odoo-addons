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

HACE UNA SOLA COSA, Y ESO ES EL DISEÑO. Permite el frame, y NADA MÁS: no toca
la cookie de sesión. Que la sesión viaje adentro del iframe no es trabajo de
este módulo — es consecuencia de servir el panel en el MISMO SITIO que este
Odoo (mismo dominio registrable, aunque sea otro host). ``SameSite`` se define
por sitio, no por origen, así que la cookie normal de Odoo entra al iframe sola.

POR QUÉ ESTO NO AFLOJA LA COOKIE, Y POR QUÉ IMPORTA. Una versión anterior
reemitía la sesión con ``SameSite=None`` para que viajara a un panel de OTRO
sitio. Medido: eso abría dos canales que CORS no cubre —un WebSocket
cross-origin que lee el bus en vivo del usuario, y un ``<img>`` a
``/web/become`` que escala a superusuario sin un clic—. Se probó
``Partitioned`` (CHIPS) como mitigante y cierra el ``<img>``, pero rompe el
panel: al reabrirlo en otra pestaña, Odoo entra en bucle infinito contra
``/web/login``. Ambas cosas están medidas antes/después contra Chrome real.

La salida no fue un mitigante mejor: fue sacarle el problema de encima. Con el
panel servido same-site, no hay nada que aflojar, así que esos dos vectores
nunca se abren. El README tiene el detalle y la precondición de despliegue.
"""

import logging

from odoo import models
from odoo.http import request

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

    def session_info(self):
        """Apagar los tours cuando esta pantalla se está mostrando embebida.

        POR QUÉ, Y ES UN BUG DE ODOO. Con un tour de onboarding pendiente, el
        webclient dentro de un iframe de otro origen **le crashea la pestaña**:
        el puntero del tour busca el documento del padre, eso tira
        ``SecurityError`` en bucle y explota la memoria del renderer. Medido:
        con un solo tour pendiente el navegador se cae; con ese tour consumido,
        anda perfecto.

        Odoo YA intenta evitarlo — ``tour_service.js`` arranca los tours dentro
        de ``if (!window.frameElement)`` —, pero ``window.frameElement``
        devuelve ``null`` cuando el padre es de OTRO origen, así que la guarda
        se cumple justo en el caso que quería prevenir. Es una línea, y es de
        Odoo, no nuestra: corresponde reportarla arriba.

        Mientras tanto, acá se corta de raíz: si el pedido es la navegación de
        un iframe, el ``session_info`` sale con los tours apagados. Se apagan
        las DOS puertas —``tour_enabled`` y ``current_tour``— porque el JS
        arranca un tour por cualquiera de las dos.

        NO se toca la preferencia guardada del usuario: esto es por pedido, así
        que su Odoo de siempre sigue mostrándole el onboarding igual.
        """
        info = super().session_info()
        if not self._tuqui_embed_origins():
            return info
        # `Sec-Fetch-Dest` lo pone el browser y no se puede falsificar desde
        # JS. `iframe` es exactamente "esta navegación es la de un frame".
        if (request.httprequest.headers.get("Sec-Fetch-Dest") or "").lower() != "iframe":
            return info
        if "tour_enabled" in info:
            info["tour_enabled"] = False
        if "current_tour" in info:
            info["current_tour"] = False
        return info

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

            # Y LA COOKIE NO SE TOCA. Ver el docstring del módulo: la sesión
            # viaja porque el panel se sirve en el MISMO SITIO que este Odoo, no
            # porque acá se afloje nada. `tests/test_cookie_is_never_touched.py`
            # fija ese invariante, y está calibrado por mutación: reintroducir
            # la reemisión pone en rojo sus tres tests, y sólo esos.
        except Exception:
            # Un módulo de conveniencia no puede tumbar una respuesta de Odoo.
            # Tampoco fallar callado: sin el log, "el panel se ve en blanco" no
            # tendría dónde investigarse.
            _logger.exception("tuqui_embed: no se pudo aplicar la política de embed")
