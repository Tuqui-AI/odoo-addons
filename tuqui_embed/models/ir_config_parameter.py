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

Y VA EN `info`, NO EN `warning`. La primera versión usaba `warning` por lo
serio del cambio, y el runbot lo cazó: marca rojo cualquier build cuyo log
traiga un warning, y este módulo emitía uno en cada encendido legítimo del
interruptor —trece sólo en su propia batería de tests—. La convención de Odoo
es la correcta: `warning` es «algo anda mal y hay que mirarlo», y esto es una
acción deliberada de un administrador. Lo que hace auditable a la línea es que
diga quién, cuándo y de qué a qué, no su nivel de severidad.
"""

import logging
from urllib.parse import urlsplit

from odoo import api, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

EMBED_ORIGINS_PARAM = "tuqui.embed_origins"

#: `http://` is only trustworthy on a machine's own loopback — anywhere else
#: it means the cookie (and the frame permission) travel in the clear.
_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def _validate_embed_origin_token(token):
    """Return an error message for one invalid origin, or None if it's fine.

    `token` ends up verbatim inside `frame-ancestors` (see ir_http.py) and
    governs which sites get the loosened, `SameSite=None` session cookie —
    it's not a display string, it's an access-control entry. A wildcard, a
    bare scheme, or a plain-http host would open the frame (and the cookie)
    to more than whoever typed the value meant to.

    Plain strings, not `odoo._()`: this runs on every `write`/`create`,
    including from plain `TransactionCase` tests with no request/lang in
    context, and `_()` logs a WARNING there ("no translation language
    detected") — the exact kind of noise this module already learned to
    keep out of its own test run (see `_registrar_cambio_de_embed` above).
    """
    if "*" in token:
        return "no se aceptan comodines: %r" % token
    parsed = urlsplit(token)
    if parsed.scheme not in ("http", "https"):
        return "tiene que empezar con https:// (o http://localhost para desarrollo local): %r" % token
    if not parsed.netloc:
        return "le falta el host: %r" % token
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HTTP_HOSTS:
        return "tiene que ser https:// — http:// sólo se acepta en localhost: %r" % token
    if parsed.path or parsed.query or parsed.fragment:
        return "tiene que ser sólo esquema y host, sin barra ni ruta al final: %r" % token
    return None


def _validate_embed_origins(value):
    """Raise if `value` (space-separated origins) has an invalid entry."""
    for token in (value or "").split():
        error = _validate_embed_origin_token(token)
        if error:
            raise ValidationError("tuqui.embed_origins: %s" % error)


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
            _logger.info(
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
            for parametro in self:
                if parametro.key == EMBED_ORIGINS_PARAM:
                    _validate_embed_origins(vals["value"])
            self._registrar_cambio_de_embed(vals["value"])
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("key") == EMBED_ORIGINS_PARAM:
                _validate_embed_origins(vals.get("value"))
            if vals.get("key") == EMBED_ORIGINS_PARAM and (vals.get("value") or "").strip():
                _logger.info(
                    "tuqui_embed: %s creado por %s (uid=%s) con %r. Con un origen cargado, "
                    "Odoo se deja mostrar dentro de ese sitio y emite la sesión con "
                    "SameSite=None.",
                    EMBED_ORIGINS_PARAM,
                    self.env.user.display_name,
                    self.env.uid,
                    vals["value"],
                )
        return super().create(vals_list)
