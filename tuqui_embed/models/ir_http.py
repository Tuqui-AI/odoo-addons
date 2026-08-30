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
reduce una protección contra CSRF que hoy existe. Este módulo la aplica sólo
cuando el pedido viene del origen declarado, pero la cookie es una sola y el
browser la guarda con el último atributo que vio. El diseño que evita eso —una
cookie aparte, de vida corta, sólo para las rutas del embed— es más trabajo y
toca el manejo de sesión de Odoo.
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

    @classmethod
    def _tuqui_embed_requested(cls, origins):
        """¿Este pedido viene de una pantalla embebida por un origen declarado?

        Se mira el ``Origin``/``Referer`` del pedido contra la lista. Sirve para
        no aflojarle la cookie a TODA la navegación normal de Odoo — sólo a la
        que efectivamente ocurre dentro del iframe autorizado.

        No es una defensa por sí sola: los dos headers los manda el browser y un
        atacante puede omitirlos (nunca falsificarlos hacia un valor de la
        lista). Por eso el módulo no toma decisiones de PERMISO con esto; sólo lo
        usa para acotar dónde aplica el cambio de cookie.
        """
        permitidos = origins.split()
        origin = request.httprequest.headers.get("Origin")
        if origin and origin in permitidos:
            return True
        referer = request.httprequest.headers.get("Referer") or ""
        return any(referer.startswith(o.rstrip("/") + "/") or referer == o for o in permitidos)

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
            response.headers["Content-Security-Policy"] = "frame-ancestors %s" % origins

            # 2. QUE LA SESIÓN VIAJE dentro del iframe. Sin esto, el paso 1 sólo
            #    logra que se vea la pantalla de login en vez de un rectángulo en
            #    blanco: el browser no manda una cookie `SameSite=Lax` en un
            #    frame de otro sitio.
            #
            #    Sólo cuando el pedido viene del origen declarado, para no
            #    aflojarle el SameSite a la navegación normal de quien nunca
            #    abrió el panel. Ver el README: la cookie es una sola, así que
            #    esto acota el alcance pero no lo elimina.
            if not cls._tuqui_embed_requested(origins):
                return

            # `response.headers` de Odoo es un proxy sin `__delitem__`, así que la
            # cookie no se reescribe a mano: se vuelve a setear con los flags que
            # hacen falta, y un `Set-Cookie` posterior para la misma clave gana.
            #
            # `SameSite=None` EXIGE `Secure`; sobre http:// eso sólo lo acepta el
            # browser en localhost (origen "potentially trustworthy"). Cualquier
            # despliegue real es HTTPS, así que no cambia nada ahí.
            sid = getattr(request.session, "sid", None)
            if sid:
                response.set_cookie(
                    "session_id",
                    sid,
                    httponly=True,
                    secure=True,
                    samesite="None",
                )
        except Exception:
            # Un módulo de conveniencia no puede tumbar una respuesta de Odoo.
            # Tampoco fallar callado: sin el log, "el panel se ve en blanco" no
            # tendría dónde investigarse.
            _logger.exception("tuqui_embed: no se pudo aplicar la política de embed")
