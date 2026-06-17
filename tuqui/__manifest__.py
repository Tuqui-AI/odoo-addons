{
    "name": "Tuqui",
    "version": "18.0.1.0.0",
    "category": "Productivity",
    "summary": "Connect this Odoo to Tuqui in one click.",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    "depends": [
        "base",
        "base_setup",
        "web",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_config_parameter_data.xml",
        "data/cron_activation_cleanup.xml",
        "views/tuqui_access_log_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
