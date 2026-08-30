"""Declarar que este Odoo se deja mostrar dentro de Tuqui.

POR QUÉ ACÁ Y NO DE OTRA FORMA. El módulo `tuqui` ya tiene un handshake donde
ANUNCIA lo que sabe hacer, y Tuqui lo lee para decidir — así resuelve, por
ejemplo, si las escrituras están apagadas (`policy.read_only`). Poder mostrarse
en un panel es exactamente la misma clase de hecho, así que viaja por el mismo
canal en vez de inventar uno.

La alternativa era que Tuqui adivinara pidiendo la página y mirando sus headers.
Además de costar una petición por cada apertura, ese camino se equivoca justo
cuando este módulo está instalado: la política mira el ORIGEN del pedido, y una
sonda anónima no lleva el origen de Tuqui — así que recibiría el "no" que
corresponde a cualquier otro sitio, y concluiría que no se puede mostrar algo
que sí se puede.

La capability aparece SÓLO si hay orígenes cargados. Con el módulo instalado y
sin configurar, este Odoo sigue diciendo que no se deja embeber, que es la
verdad.
"""

import json
import logging

from odoo import http
from odoo.addons.tuqui.controllers.health import TuquiHealth

from ..models.ir_http import EMBED_ORIGINS_PARAM

_logger = logging.getLogger(__name__)

#: Lo que Tuqui lee para saber que puede mostrar este Odoo en su panel.
EMBED_CAPABILITY = "embed.frame"


class TuquiEmbedHealth(TuquiHealth):
    """Suma `embed.frame` al anuncio del companion cuando el embed está encendido."""

    def health(self, **kwargs):
        response = super().health(**kwargs)
        try:
            origins = (
                http.request.env["ir.config_parameter"].sudo().get_param(EMBED_ORIGINS_PARAM) or ""
            ).strip()
            if not origins:
                return response
            body = json.loads(response.data)
            caps = body.get("capabilities") or []
            if EMBED_CAPABILITY not in caps:
                caps.append(EMBED_CAPABILITY)
            body["capabilities"] = caps
            response.data = json.dumps(body)
        except Exception:
            # El health es una sonda: nunca puede dejar de responder por esto.
            # Sin la capability, Tuqui simplemente no ofrece mostrar la pantalla.
            _logger.exception("tuqui_embed: no se pudo anunciar la capability de embed")
        return response
