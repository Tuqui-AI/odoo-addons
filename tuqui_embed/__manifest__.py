{
    "name": "Tuqui Embed",
    "summary": "Dejar que Tuqui muestre la pantalla de Odoo dentro de su panel",
    "version": "19.0.1.0.0",
    "category": "Hidden",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    # `web_tour` está para poder apagarle los tours a una pantalla embebida
    # (ver `static/src/no_tours_when_framed.js`). No agrega peso: es
    # `auto_install: True` y sólo depende de `web`, así que ya está instalado
    # en cualquier Odoo con webclient. Declararlo además fija el orden de
    # carga del asset: el nuestro tiene que correr DESPUÉS del suyo.
    "depends": ["base", "web", "tuqui", "web_tour"],
    # En `demo` no: es configuración real. Va con la clave vacía, así que
    # instalarlo no habilita nada — sólo hace visible el parámetro y su
    # explicación en Ajustes → Técnico.
    "data": [
        "data/ir_config_parameter.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "tuqui_embed/static/src/no_tours_when_framed.js",
        ],
        "web.assets_unit_tests": [
            "tuqui_embed/static/tests/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
