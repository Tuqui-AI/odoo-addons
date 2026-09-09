{
    "name": "Tuqui Embed",
    "summary": "Let Tuqui show the Odoo screen inside its panel",
    "version": "19.0.1.0.0",
    "category": "Hidden",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    # `web_tour` is here so an embedded screen can have its tours switched off
    # (see `static/src/no_tours_when_framed.js`). It adds no weight: it is
    # `auto_install: True` and depends only on `web`, so it is already installed
    # in any Odoo with a web client. Declaring it also pins the asset load
    # order: ours has to run AFTER theirs.
    "depends": ["base", "web", "tuqui", "web_tour"],
    # Not under `demo`: this is real configuration. It ships with an empty
    # value, so installing it enables nothing — it only makes the parameter and
    # its explanation visible under Settings → Technical.
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
