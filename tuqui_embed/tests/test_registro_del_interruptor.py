"""Encender el embed deja rastro en el log, y apagarlo también.

Es la contraparte de resignar el `SameSite`: la decisión queda registrada con
quién y cuándo. Si mañana aparece un problema, la primera pregunta es quién
habilitó ese origen.

POR QUÉ NO SE PRUEBA A TRAVÉS DE `set_param`. Fue el primer intento y no
discriminaba nada: `set_param` decide sola si hace `create` o `write`, y con un
valor igual al actual puede no escribir — así que una mutación en el código no
llegaba a ejecutarse y los tests seguían en verde. Verificado con tres mutaciones
que no detectaron.

Se prueban las dos mitades por separado, que es lo que hace falta:
  · la función registra lo que tiene que registrar, y calla cuando corresponde;
  · y está enchufada a `write` y a `create`, o la función correcta no se llama
    nunca.
"""

import logging

from odoo.tests import TransactionCase, tagged

LOGGER = "odoo.addons.tuqui_embed.models.ir_config_parameter"
PARAM = "tuqui.embed_origins"
TUQUI = "https://tuqui.example.com"


@tagged("post_install", "-at_install")
class TestRegistroDelInterruptor(TransactionCase):
    def _parametro(self, valor=""):
        """El registro del parámetro, con el valor que se le pida."""
        p = self.env["ir.config_parameter"].sudo()
        existente = p.search([("key", "=", PARAM)], limit=1)
        if existente:
            existente.value = valor
            return existente
        return p.create({"key": PARAM, "value": valor})

    def test_encenderlo_queda_en_el_log_con_quien_lo_hizo(self):
        parametro = self._parametro("")
        with self.assertLogs(LOGGER, logging.WARNING) as capturado:
            parametro._registrar_cambio_de_embed(TUQUI)
        registro = "\n".join(capturado.output)
        self.assertIn(PARAM, registro)
        self.assertIn(TUQUI, registro)
        # Quién: sin eso el registro no sirve para lo que se lo quiere.
        self.assertIn(self.env.user.display_name, registro)

    def test_apagarlo_tambien(self):
        """Importa igual: explica por qué un embed dejó de funcionar."""
        parametro = self._parametro(TUQUI)
        with self.assertLogs(LOGGER, logging.WARNING) as capturado:
            parametro._registrar_cambio_de_embed("")
        self.assertIn(PARAM, "\n".join(capturado.output))

    def test_el_mismo_valor_no_ensucia_el_log(self):
        """Un guardado sin cambio no es una decisión: registrarlo llena el log de
        ruido y hace que deje de leerse."""
        parametro = self._parametro(TUQUI)
        with self.assertNoLogs(LOGGER, logging.WARNING):
            parametro._registrar_cambio_de_embed(TUQUI)

    def test_otro_parametro_no_se_registra(self):
        """El módulo no tiene nada que decir sobre el resto de la configuración."""
        otro = self.env["ir.config_parameter"].sudo().create({"key": "tuqui.otra_cosa", "value": "algo"})
        with self.assertNoLogs(LOGGER, logging.WARNING):
            otro._registrar_cambio_de_embed("otro valor")

    # ── Y que esté enchufado, o lo de arriba no se ejecuta nunca ──────────────

    def test_write_pasa_por_el_registro(self):
        parametro = self._parametro("")
        with self.assertLogs(LOGGER, logging.WARNING) as capturado:
            parametro.write({"value": TUQUI})
        self.assertIn(TUQUI, "\n".join(capturado.output))

    def test_crear_el_parametro_ya_encendido_se_registra(self):
        """El camino que `write` no cubre: en una instalación nueva el parámetro
        no existe, así que la primera vez es un `create`."""
        self.env["ir.config_parameter"].sudo().search([("key", "=", PARAM)]).unlink()
        with self.assertLogs(LOGGER, logging.WARNING) as capturado:
            self.env["ir.config_parameter"].sudo().create({"key": PARAM, "value": TUQUI})
        self.assertIn(TUQUI, "\n".join(capturado.output))

    def test_crear_otro_parametro_no_se_registra(self):
        with self.assertNoLogs(LOGGER, logging.WARNING):
            self.env["ir.config_parameter"].sudo().create({"key": "tuqui.otra_cosa_mas", "value": "algo"})
