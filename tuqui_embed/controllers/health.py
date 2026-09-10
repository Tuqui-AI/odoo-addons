"""Announce that this Odoo lets itself be shown inside Tuqui.

WHY HERE AND NOT SOME OTHER WAY. The `tuqui` module already has a handshake
where it ANNOUNCES what it can do, and Tuqui reads it to decide — that is how it
resolves, for instance, whether writes are switched off (`policy.read_only`).
Being able to show itself in a panel is exactly the same class of fact, so it
travels down the same channel instead of inventing one.

The alternative was for Tuqui to guess by fetching the page and looking at its
headers. Besides costing one request per open, that path gets it wrong
precisely when this module is installed: the policy looks at the ORIGIN of the
request, and an anonymous probe does not carry Tuqui's origin — so it would
receive the "no" meant for any other site, and conclude that something which
can be shown cannot.

The capability appears ONLY when origins are loaded. With the module installed
and unconfigured, this Odoo keeps saying it does not let itself be embedded,
which is the truth.
"""

import json
import logging

from odoo import http
from odoo.addons.tuqui.controllers.health import TuquiHealth

from ..models.ir_http import EMBED_ORIGINS_PARAM

_logger = logging.getLogger(__name__)

#: What Tuqui reads to know it may show this Odoo in its panel.
EMBED_CAPABILITY = "embed.frame"


class TuquiEmbedHealth(TuquiHealth):
    """Add `embed.frame` to the companion's announcement when the embed is on."""

    # Odoo REQUIRES re-decorating an inherited endpoint, even with an empty
    # decorator: the parent's routing is preserved. Without this it still boots
    # — auto-decorating and writing a WARNING per worker — and that warning
    # leaves the runbot build amber, which reaches GitHub as red.
    @http.route()
    def health(self, **kwargs):
        response = super().health(**kwargs)
        try:
            origins = (http.request.env["ir.config_parameter"].sudo().get_param(EMBED_ORIGINS_PARAM) or "").strip()
            if not origins:
                return response
            body = json.loads(response.data)
            caps = body.get("capabilities") or []
            if EMBED_CAPABILITY not in caps:
                caps.append(EMBED_CAPABILITY)
            body["capabilities"] = caps
            response.data = json.dumps(body)
        except Exception:
            # The health route is a probe: it can never stop answering because
            # of this. Without the capability, Tuqui simply does not offer to
            # show the screen.
            _logger.exception("tuqui_embed: could not announce the embed capability")
        return response
