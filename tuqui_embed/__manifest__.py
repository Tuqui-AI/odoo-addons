{
    "name": "Tuqui Embed",
    "summary": "Dejar que Tuqui muestre la pantalla de Odoo dentro de su panel",
    "description": """
Tuqui Embed
===========

Permite que un origen declarado —el Tuqui de la empresa— muestre las pantallas
de este Odoo dentro de un iframe, para que quien está conversando con el
asistente vea el registro del que están hablando al lado de la conversación, en
vez de saltar entre pestañas.

**Viene apagado.** Sin el parámetro ``tuqui.embed_origins`` cargado, este módulo
no cambia absolutamente nada: Odoo sigue rechazando el framing como siempre.

Ver el README para lo que hay que decidir antes de encenderlo.
    """,
    "version": "19.0.1.0.0",
    "category": "Hidden",
    "author": "ADHOC SA",
    "website": "https://www.adhoc.com.ar",
    "license": "AGPL-3",
    "depends": ["base", "web"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
