{
    "name": "Tuqui Assistant",
    "version": "19.0.1.1.0",
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
            "tuqui_assistant/static/src/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
