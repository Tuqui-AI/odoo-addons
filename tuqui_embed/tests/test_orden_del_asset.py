"""La guarda de anidamiento tiene que cargar ANTES que el panel del asistente.

No es una preferencia de estilo: el panel lee su estado guardado al arrancar y
decide si se abre solo. Si la guarda llega después, el panel ya se montó — y
esconderlo no alcanza, porque **un iframe con `display:none` sigue cargando**.
El bucle continúa, invisible.

Odoo ordena los assets por módulo, y `tuqui_assistant` va antes que
`tuqui_embed` alfabéticamente: sin `prepend` esto llega tarde SIEMPRE. Se
verificó leyendo el bundle servido, donde la guarda aparecía en la línea 235054
contra 233350 del servicio.

Este test existe porque el `prepend` es fácil de perder en un merge y su
ausencia no rompe nada visible: el módulo instala, el asset carga, los tests de
JavaScript pasan — y el bucle vuelve.
"""

import ast
import pathlib

from odoo.tests.common import TransactionCase


class TestOrdenDelAsset(TransactionCase):
    def _assets_backend(self):
        manifest = pathlib.Path(__file__).resolve().parent.parent / "__manifest__.py"
        return ast.literal_eval(manifest.read_text())["assets"]["web.assets_backend"]

    def test_la_guarda_va_con_prepend(self):
        entradas = self._assets_backend()
        guarda = [e for e in entradas if isinstance(e, tuple) and e[1].endswith("anidado.js")]
        self.assertTrue(
            guarda,
            "anidado.js tiene que estar declarado con ('prepend', ...): cargado en el "
            "orden normal llega DESPUÉS del panel del asistente y no lo puede frenar.",
        )
        self.assertEqual(guarda[0][0], "prepend")

    def test_la_guarda_no_esta_declarada_dos_veces(self):
        """Declararla también sin prepend la volvería a cargar tarde, y la
        segunda vez pisa lo que hizo la primera."""
        entradas = self._assets_backend()
        sueltas = [e for e in entradas if isinstance(e, str) and e.endswith("anidado.js")]
        self.assertEqual(sueltas, [])
