{
    "name": "Tuqui Embed",
    "summary": "Dejar que Tuqui muestre la pantalla de Odoo dentro de su panel",
    "version": "19.0.1.0.0",
    "category": "Hidden",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    "depends": ["base", "web", "tuqui"],
    "assets": {
        "web.assets_backend": [
            # PRIMERO DE TODO, y no es una preferencia: el panel del asistente
            # lee su estado guardado al arrancar y decide si se abre solo. Si
            # esta guarda corre después, el panel ya se montó y esconderlo no
            # sirve — un iframe con `display:none` SIGUE CARGANDO. Odoo ordena
            # los assets por módulo, y `tuqui_assistant` va antes que
            # `tuqui_embed` alfabéticamente: sin `prepend` esto llega tarde
            # siempre. Verificado leyendo el bundle servido.
            ("prepend", "tuqui_embed/static/src/anidado.js"),
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
