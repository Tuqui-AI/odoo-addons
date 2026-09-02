"""Lo que frena un CSRF desde el mismo sitio: las rutas de datos son JSON-only.

POR QUÉ SIGUE EXISTIENDO, con la premisa corregida. Este módulo **ya no afloja
el `SameSite`** —eso se sacó, ver `test_cookie_is_never_touched.py`—, así que
un sitio cualquiera de internet ya no logra que el browser mande la sesión.

Pero el diseño nuevo apoya la sesión en que el panel se sirva en el **mismo
sitio** que este Odoo, y `SameSite=Lax` **sí** viaja entre páginas del mismo
sitio. O sea: el riesgo se mudó de "cualquier sitio" a "cualquier página bajo
nuestro propio dominio" — un subdominio comprometido o mal apuntado. Es una
superficie mucho más chica y bajo nuestro control, pero no es cero, y un
`<form>` POST sigue siendo el vector que CORS no frena.

Lo que lo detiene es que las rutas de datos de Odoo **sólo aceptan JSON**, y un
`<form>` no puede emitir `application/json`: sólo `form-urlencoded`,
`multipart/form-data` o `text/plain`. Eso salió de MEDIRLO en una corrida (Odoo
devolvió 415 y no escribió nada), y una medición no protege nada: el día que
alguien agregue una ruta que acepte un formulario, la protección se cae **sin que
ningún test se ponga rojo**. Este lo pone.

No prueba nuestro código: prueba una propiedad de Odoo de la que dependemos. Es
deliberado — es exactamente la clase de suposición que hay que fijar cuando se
apoya una defensa en ella.
"""

import json

from odoo.tests import HttpCase, tagged

PARAM = "tuqui.embed_origins"
TUQUI = "https://tuqui.example.com"

#: Los tres tipos que un `<form>` HTML puede emitir. Si alguno escribe, el CSRF
#: vuelve a ser posible con la cookie aflojada.
TIPOS_DE_FORMULARIO = (
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
)


@tagged("post_install", "-at_install")
class TestCsrfInvariante(HttpCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        self.authenticate("admin", "admin")

    def _escribir_el_nombre_de_la_compania(self, content_type, cuerpo):
        """Intentar renombrar la compañía como lo haría un form de otro sitio."""
        return self.url_open(
            "/web/dataset/call_kw",
            data=cuerpo,
            headers={"Content-Type": content_type, "Origin": "https://sitio-ajeno.example.com"},
            timeout=30,
        )

    def test_un_formulario_no_puede_escribir_aunque_lleve_la_sesion(self):
        """El invariante. Con la sesión aflojada, los tres tipos de formulario
        tienen que ser rechazados por la ruta de datos."""
        compania = self.env.company
        nombre_original = compania.name
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "model": "res.company",
                "method": "write",
                "args": [[compania.id], {"name": "TOMADA POR CSRF"}],
                "kwargs": {},
            },
        }

        for content_type in TIPOS_DE_FORMULARIO:
            with self.subTest(content_type=content_type):
                resp = self._escribir_el_nombre_de_la_compania(content_type, json.dumps(payload))
                # 415 (Unsupported Media Type) y no un genérico "no 200": es el
                # código que dice que rechazó POR EL CONTENT-TYPE, que es el
                # invariante. Un "no 200" pasaría igual si la ruta empezara a
                # fallar por cualquier otro motivo — y ahí el test seguiría verde
                # mientras la protección real desaparece.
                self.assertEqual(
                    resp.status_code,
                    415,
                    "la ruta dejó de rechazar %s por content-type (status %s): con la cookie "
                    "aflojada, eso reabre el CSRF" % (content_type, resp.status_code),
                )

        # Y lo que de verdad importa: NO se escribió.
        compania.invalidate_recordset(["name"])
        self.assertEqual(
            compania.name,
            nombre_original,
            "la compañía se renombró desde un origen ajeno — la protección se cayó",
        )

    def test_el_mismo_pedido_en_json_sigue_funcionando(self):
        """Calibración: si el test de arriba pasara porque la ruta rechaza TODO,
        no estaría midiendo nada. En JSON tiene que funcionar."""
        resp = self.url_open(
            "/web/dataset/call_kw",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "model": "res.company",
                        "method": "read",
                        "args": [[self.env.company.id], ["name"]],
                        "kwargs": {},
                    },
                }
            ),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("error", resp.json(), resp.text[:200])
