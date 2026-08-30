{
    "name": "Tuqui Embed",
    "summary": "Dejar que Tuqui muestre la pantalla de Odoo dentro de su panel",
    "version": "19.0.1.0.0",
    "category": "Hidden",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    "depends": ["base", "web", "tuqui"],
    # El JS se carga con el backend porque tiene que correr ANTES de que el
    # panel del asistente lea su estado guardado y decida abrirse solo.
    "assets": {
        "web.assets_backend": [
            "tuqui_embed/static/src/anidado.js",
            "tuqui_embed/static/src/anidado.scss",
        ],
        "web.assets_unit_tests": [
            "tuqui_embed/static/tests/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
