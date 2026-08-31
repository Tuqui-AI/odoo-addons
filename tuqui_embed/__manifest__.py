{
    "name": "Tuqui Embed",
    "summary": "Dejar que Tuqui muestre la pantalla de Odoo dentro de su panel",
    "version": "19.0.1.0.0",
    "category": "Hidden",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    "depends": ["base", "web", "tuqui"],
    # En `demo` no: es configuración real. Va con la clave vacía, así que
    # instalarlo no habilita nada — sólo hace visible el parámetro y su
    # explicación en Ajustes → Técnico.
    "data": [
        "data/ir_config_parameter.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
