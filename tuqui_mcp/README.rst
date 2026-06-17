================
Tuqui - Odoo MCP
================

Connect your Odoo to Claude, ChatGPT and any AI assistant via the Tuqui
Model Context Protocol (MCP) endpoint.

This is a data-only module. Installing it does not add any UI or background
process to your Odoo: it only registers the Tuqui MCP endpoint as system
parameters so the connection can be configured. The connector itself lives in
the companion ``tuqui`` module and at https://tuqui.com.

Features
========

* Registers the Tuqui website and MCP endpoint as ``ir.config_parameter``
  records (``tuqui.website`` and ``tuqui.mcp_endpoint``).
* Ships the listing description and icon used to publish Tuqui on
  https://apps.odoo.com.

How it works
============

Once your Odoo is connected to Tuqui, AI assistants reach your data through the
Tuqui MCP endpoint, always under standard Odoo access control:

* **Read-only by default.** The AI can never write to your Odoo. Every call
  goes through Odoo's standard ACL.
* **OAuth 2.0 with PKCE.** Industry-standard authentication; credentials can be
  rotated or revoked at any time.
* **Your data stays in your Odoo.** Tuqui never stores or caches your business
  information on external servers.
* **Access log.** Every query is recorded with user, model, method and result.

Configuration
=============

Install the companion ``tuqui`` module and visit https://tuqui.com to activate
the connection and obtain your MCP endpoint.

Credits
=======

Authors
~~~~~~~

* Tuqui-AI

Maintainers
~~~~~~~~~~~

This module is maintained by Tuqui-AI (https://tuqui.com).
