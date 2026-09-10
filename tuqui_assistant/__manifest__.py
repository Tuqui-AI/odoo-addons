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
            # Named ahead of the glob so it is FIRST in the bundle, which is
            # belt to the braces: what actually guarantees the ordering is that
            # the service imports `nested_guard`, and a module is evaluated
            # before whoever imports it. Bundle position alone would not do it —
            # Odoo's loader starts a job when its dependencies are ready, not in
            # file order — and the guard has to run before the service reads the
            # storage keys it clears.
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
