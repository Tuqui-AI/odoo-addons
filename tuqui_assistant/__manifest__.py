{
    "name": "Tuqui Assistant",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Asistente Tuqui embebido en Odoo: chat contextual + propose-then-apply sobre el formulario",
    "description": """
Superficie embebida del asistente Tuqui dentro del backend de Odoo.

Capa 1 — chat contextual: botón en el systray + panel lateral que conoce el
registro abierto (modelo, id, valores en memoria del formulario).

Capa 2 — propose-then-apply: Tuqui propone cambios al formulario abierto que se
aplican EN MEMORIA (record._update); el formulario queda "dirty" y el usuario
Guarda o Descarta con los controles nativos de Odoo. No se escribe a la base
hasta que el humano guarda.

Spike inicial: la "propuesta" se simula desde el panel (sin Tuqui real). El
iframe del SPA de Tuqui y la tool `propose_odoo_form_changes` se integran luego
(depende del companion `tuqui`).
""",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    # Spike aislado: solo `web`. Al integrar con el connector se suma `tuqui`.
    "depends": ["web"],
    "data": [],
    "assets": {
        "web.assets_backend": [
            "tuqui_assistant/static/src/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
