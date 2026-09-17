{
    "name": "Relevance Search",
    "version": "19.0.1.0.0",
    "category": "Technical",
    "summary": "Ranked text search over the models an administrator enables, on an index table of its own",
    "author": "Tuqui-AI",
    "website": "https://tuqui.com",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "data/cron_search_relevant.xml",
        "views/search_relevant_config_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
