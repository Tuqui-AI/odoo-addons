{
    "name": "Tuqui Assistant",
    "version": "19.0.1.3.0",
    "category": "Productivity",
    "summary": (
        "AI assistant embedded in your Odoo backend: a side panel that already knows the "
        "record, list or kanban you have open. Ask in plain language or by voice about "
        "sales, invoicing, inventory, helpdesk or any model, and get the answer with real "
        "data from your database. The assistant proposes changes on the open form and "
        "drafts chatter replies, but never saves or posts on its own: you keep the Save "
        "and Discard decision. Requires the free Tuqui Companion module. For Odoo.sh and "
        "On-Premise."
    ),
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    "images": ["static/description/portada.png"],
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
