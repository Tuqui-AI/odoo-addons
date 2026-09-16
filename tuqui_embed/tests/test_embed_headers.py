"""What Odoo answers with the embed off, on, and from where.

What has to be pinned here is not that the header gets written: it is that
**off means off**. A module that loosens framing, installed on the Odoo of a
customer who never asked to embed anything, has to be indistinguishable from
not being installed. That is the invariant a future change could break without
anyone noticing, because everything would keep working just as well.
"""

from odoo.tests import HttpCase, tagged

from ..controllers.health import TuquiEmbedHealth

PARAM = "tuqui.embed_origins"
TUQUI = "https://tuqui.example.com"


@tagged("post_install", "-at_install")
class TestEmbedHeaders(HttpCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)

    def _get(self, headers=None):
        return self.url_open("/web/login", headers=headers or {})

    def test_switched_off_odoo_still_refuses_the_frame(self):
        """The default. Installing the module without configuring it cannot
        change anything: whoever installs it "just in case" should not end up
        more exposed."""
        resp = self._get()
        self.assertIn("X-Frame-Options", resp.headers)
        # `frame-ancestors` and not `frame-ancestors <origin>`: the switched-ON
        # header is `frame-ancestors 'self' <origin>`, so looking for the pair
        # without `'self'` in between finds nothing either way. The assertion
        # could not fail, and the whole "off means off" guarantee was resting on
        # the line above it.
        self.assertNotIn("frame-ancestors", resp.headers.get("Content-Security-Policy", ""))

    def test_switched_on_it_allows_only_the_declared_origins(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        resp = self._get()
        # The XFO is dropped as well as setting the CSP: a browser that only
        # understands XFO has to see the list, not a DENY that wins anyway.
        self.assertNotIn("X-Frame-Options", resp.headers)
        self.assertEqual(
            resp.headers.get("Content-Security-Policy"),
            "frame-ancestors 'self' %s" % TUQUI,
        )

    def test_an_origin_not_on_the_list_is_not_enabled(self):
        """`frame-ancestors` with the list is NOT the same as allowing anyone:
        it is the only thing that separates this from removing the
        protection."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        csp = self._get().headers.get("Content-Security-Policy", "")
        self.assertNotIn("*", csp)
        self.assertNotIn("https://another.example.com", csp)

    def test_odoo_can_still_frame_itself(self):
        """`'self'` is not a courtesy: Odoo shows its own pages in same-origin
        iframes — the PDF and text viewer, the report preview — and a list
        without `'self'` leaves those blank for the WHOLE database the moment
        the switch goes on. (Odoo's default is NOT `frame-ancestors 'self'` — the
        web client ships `X-Frame-Options: DENY` and nothing else — but `'self'`
        is still the floor this has to keep.)"""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        self.assertIn("'self'", self._get().headers.get("Content-Security-Policy", ""))

    def test_the_csp_odoo_set_is_not_lost(self):
        """The header is completed, not replaced.

        Odoo puts `default-src 'none'` on every `image/*` response: that is what
        sandboxes an SVG uploaded as an attachment. Writing the CSP over it left
        that SVG running script in Odoo's origin — a hole with nothing to do
        with embedding, present on every response.
        """
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        resp = self.url_open("/web/binary/company_logo")
        self.assertTrue(
            resp.headers.get("Content-Type", "").startswith("image/"),
            "the control is worthless if the response is not an image: %s" % resp.headers.get("Content-Type"),
        )
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors", csp)

    def test_a_newline_between_two_origins_never_reaches_the_header(self):
        """What is validated has to BE what is emitted.

        Validation runs over `value.split()`, which splits on any whitespace, so
        a newline between two valid origins produced two good tokens and the
        value was saved. Emission, meanwhile, came from the raw value — with the
        newline still in it — on its way into an HTTP header.

        This does not test that werkzeug catches it. It tests that it never gets
        there.
        """
        self.env["ir.config_parameter"].sudo().set_param(PARAM, "%s\nhttps://other.example.com" % TUQUI)
        csp = self._get().headers.get("Content-Security-Policy", "")
        self.assertEqual(csp, "frame-ancestors 'self' %s https://other.example.com" % TUQUI)
        self.assertNotIn("\n", csp)

    def test_tabs_and_double_spaces_are_normalised_too(self):
        """Same path, with no malice in it: pasted out of a document."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, "  %s \t https://other.example.com  " % TUQUI)
        self.assertEqual(
            self._get().headers.get("Content-Security-Policy", ""),
            "frame-ancestors 'self' %s https://other.example.com" % TUQUI,
        )

    # The loosened-cookie tests used to live here and left with it: this module
    # no longer touches it. The invariant that took their place — that it must
    # NOT touch it, on or off — lives in `test_cookie_is_never_touched.py`.


@tagged("post_install", "-at_install")
class TestEmbedCapability(HttpCase):
    """What this Odoo ANNOUNCES to Tuqui about letting itself be shown.

    The `tuqui` module already has a handshake where it declares what it can do,
    and Tuqui reads it to decide. That the embed travels there instead of Tuqui
    guessing by fetching the page is not a matter of style: an anonymous probe
    does not carry Tuqui's origin, and this policy looks at the origin — so
    guessing returns the wrong verdict precisely when the module is on.
    """

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)

    def _caps(self):
        return self.url_open("/tuqui/health").json().get("capabilities", [])

    def test_switched_off_it_does_not_announce_itself_as_embeddable(self):
        """Installed and unconfigured, this Odoo keeps saying no. That is the
        truth: with no origins loaded it does not allow the frame."""
        self.assertNotIn("embed.frame", self._caps())

    def test_switched_on_it_announces_itself(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        self.assertIn("embed.frame", self._caps())

    def test_inherited_endpoints_are_re_decorated(self):
        """An override without `@http.route()` works fine and turns the build red.

        Odoo re-decorates it on its own and warns with a WARNING per worker;
        runbot takes any warning as a `warn` result and that reaches GitHub as
        an error, with the check list red and no failing test to explain it.
        The test does not look at the log — it has none — but at the only thing
        that produces it: that the class's own method carries its
        `original_routing`.
        """
        for cls in (TuquiEmbedHealth,):
            for name, method in vars(cls).items():
                if not callable(method):
                    continue
                inherits_a_route = any(
                    hasattr(getattr(ancestor, name, None), "original_routing") for ancestor in cls.mro()[1:]
                )
                if not inherits_a_route:
                    continue
                self.assertTrue(
                    hasattr(method, "original_routing"),
                    "%s.%s overrides a routed endpoint without re-decorating it with @http.route()"
                    % (cls.__name__, name),
                )

    def test_it_does_not_overwrite_what_was_already_announced(self):
        """The handshake belongs to `tuqui`: we add, we do not replace. Losing
        `rpc.execute_kw` would leave Tuqui believing it cannot read anything."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        caps = self._caps()
        self.assertIn("rpc.execute_kw", caps)
        self.assertIn("access_log", caps)
