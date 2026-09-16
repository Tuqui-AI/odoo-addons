"""Let a declared origin show this Odoo screen inside an iframe.

THE PROBLEM, PLAINLY. Someone discussing a record with the assistant ends up
jumping between two tabs: the conversation on one side, the record on the
other. Seeing the record next to what is being said is the difference between
"tell me what it says" and "look at what it says". Odoo, by default, lets no
page of its own be shown inside another site.

WHY IT REFUSES, AND WHY THAT IS RIGHT. A foreign iframe can sit invisibly on
top of an Odoo screen and make the user click something they cannot see
(clickjacking). Odoo's default protects against that, and this module does NOT
remove it: it replaces it with a list of origins the administrator declares.
With that list empty, nothing changes.

IT DOES ONE THING, AND THAT IS THE DESIGN. It allows the frame, and NOTHING
else: it does not touch the session cookie. The session travelling into the
iframe is not this module's doing — it follows from serving the panel on the
SAME SITE as this Odoo (same registrable domain, even under another host).
``SameSite`` is defined per site, not per origin, so Odoo's normal cookie walks
into the iframe on its own.

WHY THIS DOES NOT LOOSEN THE COOKIE, AND WHY THAT MATTERS. An earlier version
reissued the session with ``SameSite=None`` so it would travel to a panel on
ANOTHER site. Measured: that opened two channels CORS does not cover — a
cross-origin WebSocket reading the user's live bus, and an ``<img>`` to
``/web/become`` escalating to superuser with no click. ``Partitioned`` (CHIPS)
was tried as a mitigation and it does close the ``<img>``, but it breaks the
panel: reopening it in another tab sends Odoo into an infinite loop against
``/web/login``. Both were measured before and after against a real Chrome.

The way out was not a better mitigation, it was removing the problem. With the
panel served same-site there is nothing to loosen, so neither vector ever
opens. The README carries the detail and the deployment precondition.
"""

import logging

from odoo import models
from odoo.http import request

_logger = logging.getLogger(__name__)

#: The parameter that turns the module on. A space-separated list of origins,
#: exactly as ``frame-ancestors`` expects them:
#:
#:     tuqui.embed_origins = "https://tuqui.com https://staging.tuqui.com"
#:
#: Empty or absent (the default) = Odoo behaves as it always did.
EMBED_ORIGINS_PARAM = "tuqui.embed_origins"


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _tuqui_embed_origins(cls):
        """The origins allowed to frame us, or ``None`` when switched off.

        Read per request. ``get_param`` is ormcached, but core
        ``ir.config_parameter`` clears that cache on every ``create``, ``write``
        and ``unlink`` (``registry.clear_cache('stable')``), and the registry
        signals the other workers — so revoking through the ORM, which is what
        Settings does, takes effect without a restart.

        An earlier version of this docstring claimed the opposite, on the
        strength of a measurement that must have changed the value out of band
        (raw SQL, or another session). It is worth writing down because it is
        the kind of claim somebody builds an incident response on: a value
        changed BEHIND the ORM does stay cached, and that is the only case.
        """
        try:
            value = request.env["ir.config_parameter"].sudo().get_param(EMBED_ORIGINS_PARAM)
        except Exception:
            # No env yet (``auth='none'`` routes, early dispatch errors). Unable
            # to read the list, the default is to allow nothing.
            return None
        # Re-joined from the SAME split the validator uses, and not merely
        # stripped. The two used to be parallel derivations of the raw value:
        # validation ran over `value.split()`, emission over `value.strip()`.
        # A value like "https://a.com\nhttps://b.com" split into two perfectly
        # valid tokens and got saved — and then emitted with the newline still
        # inside, into an HTTP header. Whether werkzeug catches that or not is
        # not the point: what is validated has to BE what is emitted.
        return " ".join((value or "").split()) or None

    def session_info(self):
        """Switch tours off when this screen is being shown framed.

        WHY, AND IT IS AN ODOO BUG. With a pending onboarding tour, the web
        client inside a cross-origin iframe **kills the browser tab**: the tour
        pointer reaches for the parent document, that throws ``SecurityError``
        in a loop and blows up the renderer's memory. Measured: with a single
        pending tour the browser goes down; with that tour consumed, it runs
        fine.

        Odoo ALREADY tries to prevent this — ``tour_service.js`` starts tours
        inside ``if (!window.frameElement)`` — but ``window.frameElement``
        returns ``null`` when the parent is of ANOTHER origin, so the guard
        holds precisely in the case it meant to prevent. It is one line, and it
        is Odoo's, not ours: it belongs upstream.

        THE EMBED SWITCH IS NOT CONSULTED, and that is deliberate. The crash is
        Odoo's and happens to anyone showing this web client inside a frame,
        whether this module authorised it or not. Gating it on
        ``tuqui.embed_origins`` left the client-side half (which cannot read a
        server parameter) applying always and the server-side half applying only
        sometimes: two halves of one fix under different rules.

        There is a single rule: if the request is a frame's navigation, the
        ``session_info`` goes out with tours off. Both doors are shut —
        ``tour_enabled`` and ``current_tour`` — because the JS starts a tour
        through either.

        The user's stored preference is NOT touched: this is per request, so
        their everyday Odoo keeps showing them the onboarding.
        """
        info = super().session_info()
        # `Sec-Fetch-Dest` is set by the browser and cannot be forged from JS.
        # `iframe` means exactly "this navigation is a frame's".
        if (request.httprequest.headers.get("Sec-Fetch-Dest") or "").lower() != "iframe":
            return info
        if "tour_enabled" in info:
            info["tour_enabled"] = False
        if "current_tour" in info:
            info["current_tour"] = False
        return info

    @classmethod
    def _post_dispatch(cls, response):
        super()._post_dispatch(response)
        origins = cls._tuqui_embed_origins()
        if not origins:
            return
        try:
            # `'self'` IS ALWAYS THERE. Odoo frames its own pages same-origin —
            # the PDF and text viewer (`file_viewer.xml`) and the report preview
            # — and a list without `'self'` leaves those blank for the WHOLE
            # database the moment the switch goes on.
            #
            # (Odoo's own default is NOT `frame-ancestors 'self'`, as this
            # comment used to say: the web client ships `X-Frame-Options: DENY`
            # and nothing else, and `frame-ancestors 'self'` shows up only on
            # `/web/login` — `web/controllers/home.py`. `'self'` is still the
            # right floor, it is just not a quote of the default.)
            #
            # And the CSP is COMPLETED, not replaced: `set_csp` puts
            # `default-src 'none'` on every `image/*` response (odoo/http.py),
            # which is what sandboxes an SVG uploaded as an attachment.
            # Overwriting the header left that SVG running script in Odoo's
            # origin — a hole with nothing to do with embedding, present on
            # every response and not only on framed ones.
            #
            # `getlist` and not `get`: werkzeug headers are multi-valued and
            # `get` returns only the first, so a second CSP set by another
            # module was silently dropped on reassignment.
            previous = "; ".join(v for v in response.headers.getlist("Content-Security-Policy") if v)
            directives = [
                d.strip()
                for d in previous.split(";")
                if d.strip() and not d.strip().lower().startswith("frame-ancestors")
            ]
            directives.append("frame-ancestors 'self' %s" % origins)
            policy = "; ".join(directives)

            # Order matters: the value is built BEFORE any header is touched.
            # The other way round, an exception between the `pop` and the
            # assignment left the response with no XFO and no `frame-ancestors`
            # — that is, with no framing protection at all, which is worse than
            # having done nothing.
            #
            # The XFO is dropped as well as setting the CSP: `frame-ancestors`
            # wins over X-Frame-Options in current browsers, but a browser that
            # only understands XFO has to see the list, not a DENY.
            response.headers.pop("X-Frame-Options", None)
            response.headers["Content-Security-Policy"] = policy

            # AND THE COOKIE IS NOT TOUCHED. See the module docstring: the
            # session travels because the panel is served on the SAME SITE as
            # this Odoo, not because anything is loosened here.
            # `tests/test_cookie_is_never_touched.py` pins that invariant, and
            # it is mutation-calibrated: reintroducing the reissue turns its
            # three tests red, and only those.
        except Exception:
            # A convenience module cannot take down an Odoo response. Nor fail
            # quietly: without the log, "the panel came up blank" would have
            # nowhere to be investigated.
            _logger.exception("tuqui_embed: could not apply the embed policy")
