{
    "name": "Tuqui Assistant",
    "version": "19.0.1.9.0",
    "category": "Productivity",
    "summary": "Asistente Tuqui embebido en Odoo: chat contextual + propose-then-apply sobre el formulario",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    # Requiere el connector companion `tuqui` (ADR 0001): el embed usa SSO con la
    # identidad del companion (sin login en el iframe, sin compat jsonrpc-embed).
    "depends": ["web", "tuqui"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
    ],
    "assets": {
        "web.assets_backend": [
            # Order matters here and is not cosmetic: the guard clears the very
            # storage keys the panel and the service read while loading, so it
            # has to be evaluated before either of them. Naming it ahead of the
            # glob pins that down — Odoo keeps a file at its first position and
            # ignores the repeat, so the glob below does not move it.
            "tuqui_assistant/static/src/nested_guard.js",
            "tuqui_assistant/static/src/**/*",
        ],
        # Tests de interacción (Hoot). Corren con el runner del propio Odoo, sin
        # LLM y sin navegador externo: montan un form view real y ejercitan el
        # propose-apply contra el record model de verdad.
        "web.assets_unit_tests": [
            "tuqui_assistant/static/tests/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
