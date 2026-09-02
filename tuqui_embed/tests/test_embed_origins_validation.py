"""`tuqui.embed_origins` is an access-control list, not free text.

Each entry ends up verbatim inside `frame-ancestors` (see ir_http.py), so it
decides who may frame this Odoo. Before this, the field accepted whatever was
typed — a wildcard, a bare scheme, a typo'd `http://` — and any of those would
have opened the frame to more than whoever wrote the value meant to.
"""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

PARAM = "tuqui.embed_origins"


@tagged("post_install", "-at_install")
class TestEmbedOriginsValidation(TransactionCase):
    def _param(self, value=""):
        record = self.env["ir.config_parameter"].sudo().search([("key", "=", PARAM)], limit=1)
        if record:
            record.value = value
            return record
        return self.env["ir.config_parameter"].sudo().create({"key": PARAM, "value": value})

    # ── accepted ───────────────────────────────────────────────────────────

    def test_accepts_a_plain_https_origin(self):
        param = self._param("")
        param.write({"value": "https://tuqui.example.com"})
        self.assertEqual(param.value, "https://tuqui.example.com")

    def test_accepts_several_space_separated_origins(self):
        param = self._param("")
        param.write({"value": "https://tuqui.example.com https://staging.tuqui.example.com"})
        self.assertEqual(param.value, "https://tuqui.example.com https://staging.tuqui.example.com")

    def test_accepts_http_localhost_for_local_dev(self):
        param = self._param("")
        param.write({"value": "http://localhost:5173"})
        self.assertEqual(param.value, "http://localhost:5173")

    def test_accepts_http_on_a_localhost_subdomain(self):
        """`.localhost` está reservado para loopback (RFC 6761), así que
        cualquier subdominio suyo es tan local como `localhost` a secas."""
        param = self._param("")
        param.write({"value": "http://panel.localhost:9400"})
        self.assertEqual(param.value, "http://panel.localhost:9400")

    def test_accepts_a_plain_http_origin_when_this_odoo_is_also_plain_http(self):
        """El caso que faltaba, y que apareció usando el módulo.

        El diseño nuevo pide que el panel sea del MISMO SITIO que Odoo — dos
        nombres bajo un dominio común—, y eso en local no se arma sólo con
        `localhost` (los subdominios de `.localhost` el browser los trata como
        sitios distintos). Con la regla anterior, un panel de desarrollo en
        `http://panel.midominio.test` era imposible de declarar, o sea que
        desarrollar la propia feature exigía TLS local.

        Si este Odoo ya se sirve por http, la sesión viaja en claro de todos
        modos: exigirle https al embebedor no protegía nada.
        """
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "http://odoo.midominio.test:8069")
        param = self._param("")
        param.write({"value": "http://panel.midominio.test:9400"})
        self.assertEqual(param.value, "http://panel.midominio.test:9400")

    def test_rejects_plain_http_when_this_odoo_is_https(self):
        """El discriminador del test de arriba: la misma dirección, rechazada
        cuando el deployment sí es https. Sin este par, la cláusula de
        desarrollo sería un agujero y no una excepción."""
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://odoo.midominio.test")
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "http://panel.midominio.test:9400"})

    def test_rejects_plain_http_when_the_base_url_says_nothing(self):
        """Sin un `web.base.url` que diga qué es el deployment, el default
        tiene que ser el estricto.

        Se vacía en vez de borrarse porque Odoo no deja borrar ese registro
        (`unlink_default_parameters`), y vacío ejerce el mismo camino.
        """
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "")
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "http://panel.midominio.test:9400"})

    def test_accepts_clearing_it(self):
        param = self._param("https://tuqui.example.com")
        param.write({"value": ""})
        self.assertFalse(param.value)

    # ── rejected ───────────────────────────────────────────────────────────

    def test_rejects_a_wildcard(self):
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://*.tuqui.com"})

    def test_rejects_a_bare_scheme(self):
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https:"})

    def test_rejects_plain_http_on_a_real_host(self):
        """En un deployment https, `http://` fuera de loopback entrega el
        permiso de framing a un host que nadie puede autenticar.

        El `web.base.url` se fija explícitamente: una base de test suele
        traerlo en `http://localhost:8069`, y con eso la cláusula de
        desarrollo dejaría pasar cualquier http — el test aprobaría sin medir
        nada.
        """
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://odoo.example.com")
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "http://tuqui.example.com"})

    def test_rejects_a_trailing_slash(self):
        """The module's own README documents origins without a trailing slash
        — this turns that from a convention nobody checks into a rule."""
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://tuqui.example.com/"})

    def test_rejects_a_path(self):
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://tuqui.example.com/embed"})

    def test_one_bad_entry_blocks_the_whole_write(self):
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://tuqui.example.com *"})
        self.assertFalse(param.value, "a rejected write must not partially apply")

    def test_invalid_value_is_rejected_on_create_too(self):
        """The other path a validation-only-on-write misses: on a fresh
        install the parameter doesn't exist yet, so the first save is a
        `create`, not a `write`."""
        self.env["ir.config_parameter"].sudo().search([("key", "=", PARAM)]).unlink()
        with self.assertRaises(ValidationError):
            self.env["ir.config_parameter"].sudo().create({"key": PARAM, "value": "*"})

    # ── scope ──────────────────────────────────────────────────────────────

    def test_unrelated_parameters_are_not_validated(self):
        """This module has no opinion on the rest of the configuration."""
        param = self.env["ir.config_parameter"].sudo().create({"key": "tuqui.other_thing", "value": "*"})
        self.assertEqual(param.value, "*")
