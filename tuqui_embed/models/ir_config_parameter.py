"""Dejar rastro de cuándo se enciende el embed, y de quién lo encendió.

POR QUÉ. Cargar `tuqui.embed_origins` no es configurar una preferencia: es el
interruptor entre «Odoo normal» y «Odoo que se deja mostrar dentro de otro
sitio». Vacío, este módulo es indistinguible de no estar instalado; con una
dirección adentro, se resigna la protección contra clickjacking para ese
origen. La cookie de sesión NO se toca — ver el README y el invariante en
`tests/test_cookie_is_never_touched.py`.

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

#: Loopback names and addresses, where `http://` is always fine because the
#: traffic never leaves the machine. `.localhost` is reserved for loopback by
#: RFC 6761, so any subdomain of it counts.
_LOOPBACK_HTTP_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _is_loopback_host(host):
    if not host:
        return False
    host = host.lower()
    return host in _LOOPBACK_HTTP_HOSTS or host == "localhost" or host.endswith(".localhost") or host.startswith("127.")


def _deployment_is_plain_http(env):
    """¿Este Odoo se sirve por http, o sea es un entorno de desarrollo?

    Se mira `web.base.url` —la identidad que el administrador le declaró al
    deployment, y la que Odoo usa para sus propios links— y no el esquema del
    request, que detrás de un proxy depende de que `X-Forwarded-Proto` y
    `ProxyFix` estén bien puestos. Si no está seteada, se asume producción y
    se es estricto.
    """
    base_url = (env["ir.config_parameter"].sudo().get_param("web.base.url") or "").strip().lower()
    return base_url.startswith("http://")


def _validate_embed_origin_token(token, allow_plain_http=False):
    """Return an error message for one invalid origin, or None if it's fine.

    `token` ends up verbatim inside `frame-ancestors` (see ir_http.py), so it
    decides who may frame this Odoo — it's not a display string, it's an
    access-control entry. A wildcard, a bare scheme, or a plain-http host
    would open the frame to more than whoever typed the value meant to.

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
        return "tiene que empezar con https:// (o http:// en desarrollo local): %r" % token
    if not parsed.netloc:
        return "le falta el host: %r" % token
    if parsed.scheme == "http" and not (allow_plain_http or _is_loopback_host(parsed.hostname)):
        return (
            "tiene que ser https:// — http:// sólo se acepta en loopback, o si este "
            "Odoo también se sirve por http (web.base.url): %r" % token
        )
    if parsed.path or parsed.query or parsed.fragment:
        return "tiene que ser sólo esquema y host, sin barra ni ruta al final: %r" % token
    return None


def _validate_embed_origins(env, value):
    """Raise if `value` (space-separated origins) has an invalid entry.

    `http://` fuera de loopback se acepta SÓLO si este Odoo también se sirve
    por http, y eso no es una excepción de comodidad: si la sesión ya viaja en
    claro, exigirle https al embebedor no protege nada. Y hace falta, porque el
    diseño nuevo pide que el panel sea del MISMO SITIO que Odoo — o sea dos
    nombres bajo un dominio común—, y eso en local no se puede armar sólo con
    `localhost`: los subdominios de `.localhost` el browser los trata como
    sitios distintos (medido). Sin esta cláusula, desarrollar la propia feature
    exigiría TLS local, que a su vez exige el proxy que todavía no existe.
    """
    allow_plain_http = _deployment_is_plain_http(env)
    for token in (value or "").split():
        error = _validate_embed_origin_token(token, allow_plain_http=allow_plain_http)
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
                "Con un origen cargado, Odoo se deja mostrar dentro de ese sitio "
                "(la cookie de sesión no se toca).",
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
                    _validate_embed_origins(self.env, vals["value"])
            self._registrar_cambio_de_embed(vals["value"])
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("key") == EMBED_ORIGINS_PARAM:
                _validate_embed_origins(self.env, vals.get("value"))
            if vals.get("key") == EMBED_ORIGINS_PARAM and (vals.get("value") or "").strip():
                _logger.info(
                    "tuqui_embed: %s creado por %s (uid=%s) con %r. Con un origen cargado, "
                    "Odoo se deja mostrar dentro de ese sitio (la cookie de sesión "
                    "no se toca).",
                    EMBED_ORIGINS_PARAM,
                    self.env.user.display_name,
                    self.env.uid,
                    vals["value"],
                )
        return super().create(vals_list)
