"""Qué responde Odoo cuando el embed está apagado, encendido, y desde dónde.

Lo que hay que fijar acá no es que el header se escriba: es que **apagado sea
apagado**. Un módulo que afloja el framing, instalado en el Odoo de un cliente
que nunca pidió embeber nada, tiene que ser indistinguible de no estar
instalado. Ese es el invariante que un cambio futuro podría romper sin que
nadie lo note, porque todo seguiría funcionando igual de bien.
"""

from odoo.tests import HttpCase, tagged

from ..controllers.health import TuquiEmbedHealth

PARAM = "tuqui.embed_origins"
TUQUI = "https://tuqui.example.com"


@tagged("post_install", "-at_install")
class TestEmbedHeaders(HttpCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)

    def _get(self, headers=None):
        return self.url_open("/web/login", headers=headers or {})

    def test_apagado_odoo_sigue_rechazando_el_frame(self):
        """El default. Instalar el módulo sin configurarlo no puede cambiar nada:
        quien lo instala 'por las dudas' no debería quedar más expuesto."""
        resp = self._get()
        self.assertIn("X-Frame-Options", resp.headers)
        self.assertNotIn("frame-ancestors %s" % TUQUI, resp.headers.get("Content-Security-Policy", ""))

    def test_encendido_permite_solo_a_los_origenes_declarados(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        resp = self._get()
        # El XFO se saca además de poner el CSP: un browser que sólo entienda
        # XFO tiene que ver la lista, no un DENY que le gane igual.
        self.assertNotIn("X-Frame-Options", resp.headers)
        self.assertEqual(
            resp.headers.get("Content-Security-Policy"),
            "frame-ancestors 'self' %s" % TUQUI,
        )

    def test_un_origen_que_no_esta_en_la_lista_no_queda_habilitado(self):
        """`frame-ancestors` con la lista NO es lo mismo que permitir a
        cualquiera: es lo único que distingue esto de sacar la protección."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        csp = self._get().headers.get("Content-Security-Policy", "")
        self.assertNotIn("*", csp)
        self.assertNotIn("https://otro.example.com", csp)

    def test_odoo_sigue_pudiendo_embeberse_a_si_mismo(self):
        """`'self'` no es cortesía: Odoo muestra sus propias páginas en iframes
        del mismo origen —el visor de PDF y de texto, el preview de reportes— y
        una lista sin `'self'` los deja en blanco para TODA la base en cuanto se
        prende el switch. El default de Odoo es exactamente `frame-ancestors
        'self'`; esto lo AMPLÍA, no lo reemplaza."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        self.assertIn("'self'", self._get().headers.get("Content-Security-Policy", ""))

    def test_la_csp_que_puso_odoo_no_se_pierde(self):
        """El header no se reemplaza, se completa.

        Odoo le pone `default-src 'none'` a toda respuesta `image/*`: es lo que
        sandboxea un SVG subido como adjunto. Escribir la CSP encima dejaba ese
        SVG ejecutando script en el origen de Odoo — un agujero que no tiene nada
        que ver con embeber y que aparecía en todas las respuestas.
        """
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        resp = self.url_open("/web/binary/company_logo")
        self.assertTrue(
            resp.headers.get("Content-Type", "").startswith("image/"),
            "el control no sirve si la respuesta no es una imagen: %s" % resp.headers.get("Content-Type"),
        )
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors", csp)

    # Los tests de la cookie aflojada vivían acá y se fueron con ella: este
    # módulo ya no la toca. El invariante que ocupó su lugar —que NO la
    # toque, ni prendido ni apagado— vive en `test_cookie_is_never_touched.py`.


@tagged("post_install", "-at_install")
class TestEmbedCapability(HttpCase):
    """Lo que este Odoo le ANUNCIA a Tuqui sobre si se deja mostrar.

    El módulo `tuqui` ya tiene un handshake donde declara lo que sabe hacer, y
    Tuqui lo lee para decidir. Que el embed viaje por ahí en vez de que Tuqui lo
    adivine pidiendo la página no es una preferencia de estilo: una sonda anónima
    no lleva el origen de Tuqui, y esta política mira el origen — así que
    adivinar da el veredicto equivocado justo cuando el módulo está encendido.
    """

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)

    def _caps(self):
        return self.url_open("/tuqui/health").json().get("capabilities", [])

    def test_apagado_no_se_anuncia_como_embebible(self):
        """Instalado y sin configurar, este Odoo sigue diciendo que no. Es la
        verdad: sin orígenes cargados no permite el frame."""
        assert "embed.frame" not in self._caps()

    def test_encendido_se_anuncia(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        assert "embed.frame" in self._caps()

    def test_los_endpoints_heredados_estan_re_decorados(self):
        """Un override sin `@http.route()` anda igual y pinta el build de rojo.

        Odoo lo re-decora solo y avisa con un WARNING por worker; runbot toma
        cualquier warning como resultado `warn` y eso llega a GitHub como error,
        con la lista de checks en rojo y ningún test fallado que lo explique.
        El test no mira el log —no lo tiene— sino lo único que lo produce: que
        el método propio de la clase lleve su `original_routing`.
        """
        for cls in (TuquiEmbedHealth,):
            for name, method in vars(cls).items():
                if not callable(method):
                    continue
                hereda_una_ruta = any(
                    hasattr(getattr(ancestro, name, None), "original_routing") for ancestro in cls.mro()[1:]
                )
                if not hereda_una_ruta:
                    continue
                assert hasattr(method, "original_routing"), (
                    "%s.%s sobrescribe un endpoint ruteado sin re-decorarlo con @http.route()" % (cls.__name__, name)
                )

    def test_no_pisa_lo_que_ya_anunciaba(self):
        """El handshake es de `tuqui`: sumamos, no reemplazamos. Perder
        `rpc.execute_kw` dejaría a Tuqui creyendo que no puede leer nada."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        caps = self._caps()
        assert "rpc.execute_kw" in caps
        assert "access_log" in caps
