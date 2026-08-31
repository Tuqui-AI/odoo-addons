"""Dejar rastro de cuándo se enciende el embed, y de quién lo encendió.

POR QUÉ. Cargar `tuqui.embed_origins` no es configurar una preferencia: es el
interruptor entre «Odoo normal» y «Odoo que se deja mostrar dentro de otro sitio
y afloja el SameSite de su sesión». Vacío, este módulo es indistinguible de no
estar instalado; con una dirección adentro, se resigna una protección contra
CSRF (acotada y medida — ver el README, y el invariante en
`tests/test_csrf_invariante.py`).

Una decisión así tiene que dejar rastro. Si mañana aparece un problema, la
primera pregunta es quién habilitó ese origen y cuándo.

VA EN EL LOG Y NO EN LA INTERFAZ, a propósito. Se evaluó dejar una nota en el
chatter de la compañía y se descartó: a quien administra no le aporta nada y
ensucia un lugar que se lee por otros motivos. El log lo consulta quien está
investigando algo, que es exactamente cuando este dato importa.
"""

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

EMBED_ORIGINS_PARAM = "tuqui.embed_origins"


class IrConfigParameter(models.Model):
    _inherit = "ir.config_parameter"

    def _registrar_cambio_de_embed(self, valor_nuevo):
        """Anotar en el log que el embed cambió de estado, y a manos de quién."""
        for parametro in self:
            if parametro.key != EMBED_ORIGINS_PARAM:
                continue
            antes = (parametro.value or "").strip()
            despues = (valor_nuevo or "").strip()
            if antes == despues:
                continue
            _logger.warning(
                "tuqui_embed: %s cambiado por %s (uid=%s) — antes=%r ahora=%r. "
                "Con un origen cargado, Odoo se deja mostrar dentro de ese sitio y "
                "emite la sesión con SameSite=None.",
                EMBED_ORIGINS_PARAM,
                self.env.user.display_name,
                self.env.uid,
                antes or "(vacío: embed apagado)",
                despues or "(vacío: embed apagado)",
            )

    def write(self, vals):
        if "value" in vals:
            self._registrar_cambio_de_embed(vals["value"])
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("key") == EMBED_ORIGINS_PARAM and (vals.get("value") or "").strip():
                _logger.warning(
                    "tuqui_embed: %s creado por %s (uid=%s) con %r. Con un origen cargado, "
                    "Odoo se deja mostrar dentro de ese sitio y emite la sesión con "
                    "SameSite=None.",
                    EMBED_ORIGINS_PARAM,
                    self.env.user.display_name,
                    self.env.uid,
                    vals["value"],
                )
        return super().create(vals_list)
