{
    "name": "Tuqui",
    "version": "19.0.0.2.0",
    "category": "Productivity",
    "summary": "Connect this Odoo to Tuqui in one click.",
    "description": """
Tuqui — Companion module for Odoo
==================================

Activates this Odoo instance against a Tuqui workspace using OAuth 2.0
client_credentials. Replaces per-user API keys with a single workspace-level
client/secret pair that can be rotated or revoked at any time.

After installation, an admin (group_system) can activate Tuqui from the
**Tuqui** menu and the workspace becomes reachable from claude.ai, ChatGPT,
or any MCP-compatible client.
""",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_config_parameter_data.xml",
        "views/tuqui_oauth_client_views.xml",
        "views/tuqui_access_log_views.xml",
        "views/tuqui_rpc_rule_views.xml",
        "views/tuqui_rpc_policy_views.xml",
        "views/tuqui_menus.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
