"""Qué responde Odoo cuando el embed está apagado, encendido, y desde dónde.

Lo que hay que fijar acá no es que el header se escriba: es que **apagado sea
apagado**. Un módulo que afloja el framing y la cookie de sesión, instalado en el
Odoo de un cliente que nunca pidió embeber nada, tiene que ser indistinguible de
no estar instalado. Ese es el invariante que un cambio futuro podría romper sin
que nadie lo note, porque todo seguiría funcionando igual de bien.
"""

from odoo.tests import HttpCase, tagged

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
        self.assertEqual(resp.headers.get("Content-Security-Policy"), "frame-ancestors %s" % TUQUI)

    def test_un_origen_que_no_esta_en_la_lista_no_queda_habilitado(self):
        """`frame-ancestors` con la lista NO es lo mismo que permitir a
        cualquiera: es lo único que distingue esto de sacar la protección."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        csp = self._get().headers.get("Content-Security-Policy", "")
        self.assertNotIn("*", csp)
        self.assertNotIn("https://otro.example.com", csp)

    def test_la_cookie_se_afloja_sin_esperar_a_reconocer_el_pedido(self):
        """Por qué NO se acota al pedido que viene del panel, aunque se pueda.

        Es un huevo y gallina, medido contra un navegador de verdad. El primer
        pedido del iframe llega con `Referer` del panel —se lo puede detectar
        perfectamente— pero llega SIN COOKIE: la guardada en ese momento es la
        normal (`SameSite=Lax`), que no viaja dentro de un frame ajeno. Odoo lo
        toma como anónimo y redirige al login. La cookie se afloja en la
        respuesta de una navegación que ya terminó mal, y el usuario ve la
        pantalla de login adentro del panel.

        Acotar por origen era, entonces, aflojar siempre un pedido tarde.
        """
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)

        # Como llega la navegación inicial del iframe: puede traer Referer, pero
        # el módulo ya no depende de eso.
        sin_pistas = self._cookies(self._get())
        self.assertTrue(sin_pistas, "la respuesta no trajo cookie de sesión")
        self.assertIn("samesite=none", sin_pistas.lower())

        del_panel = self._cookies(self._get(headers={"Origin": TUQUI}))
        self.assertTrue(del_panel, "la respuesta no trajo cookie de sesión")
        self.assertIn("samesite=none", del_panel.lower())

    def test_apagado_la_cookie_no_se_toca(self):
        """El límite real, y el que importa: sin el parámetro cargado, la sesión
        sale como siempre. Ese es el interruptor — no el origen del pedido."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, "")
        cookies = self._cookies(self._get(headers={"Origin": TUQUI}))
        self.assertTrue(cookies, "la respuesta no trajo cookie de sesión")
        self.assertNotIn("samesite=none", cookies.lower())

    def test_la_cookie_aflojada_sigue_siendo_httponly(self):
        """Lo que NO se resigna: la página que embebe no puede leer la sesión.

        Sin esto, el sitio de al lado dejaría de necesitar CSRF — se llevaría la
        sesión directamente.
        """
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        cookies = self._cookies(self._get(headers={"Origin": TUQUI})).lower()
        self.assertIn("samesite=none", cookies)
        self.assertIn("httponly", cookies)
        self.assertIn("secure", cookies)

    @staticmethod
    def _cookies(response):
        """La ÚLTIMA cookie de sesión de la respuesta: la que el browser guarda.

        Dos precisiones que costaron un test inútil. Primero, mirar sólo
        `session_id`: juntar todos los Set-Cookie hacía que la palabra `HttpOnly`
        apareciera siempre, porque otras cookies de Odoo la traen. Segundo,
        quedarse con la ÚLTIMA: cuando el módulo reemite, la respuesta lleva DOS
        `Set-Cookie: session_id` —la de Odoo y la nuestra— y el browser aplica la
        de más abajo. Un test que leyera la primera aprobaría cualquier cosa que
        hiciéramos con la segunda, que es justamente la que cambia la seguridad.
        """
        # `response.headers` de requests COLAPSA los Set-Cookie repetidos en un
        # solo string separado por comas — leerlo de ahí devolvía las dos cookies
        # pegadas, así que cualquier flag presente en una tapaba su ausencia en la
        # otra. `raw.headers.getlist` conserva la lista tal como vino.
        crudas = response.raw.headers.getlist("Set-Cookie")
        cookies = [v for v in crudas if v.startswith("session_id=")]
        return cookies[-1] if cookies else ""


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

    def test_no_pisa_lo_que_ya_anunciaba(self):
        """El handshake es de `tuqui`: sumamos, no reemplazamos. Perder
        `rpc.execute_kw` dejaría a Tuqui creyendo que no puede leer nada."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        caps = self._caps()
        assert "rpc.execute_kw" in caps
        assert "access_log" in caps
