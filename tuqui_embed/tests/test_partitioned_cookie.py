"""CHIPS: the reissued cookie is bound to (Odoo, the site that framed it).

`SameSite=None` alone makes the session cookie travel to ANY window that
embeds Odoo, not just the one declared in `tuqui.embed_origins` — the frame
check in `ir_http.py` decides who is allowed to *show the frame*, but it
can't stop the browser from sending the cookie into someone else's frame.
Two vectors slip through that gap regardless of what `embed_origins` says,
because neither is blocked by CORS or by the frame check (see
`ir_http.py::_tuqui_partition_last_session_cookie` for the detail): a
cross-origin WebSocket read of the user's live bus, and a zero-click GET
that escalates a logged-in admin to superuser. `Partitioned` closes both by
keying the cookie to the pairing (top-level site, this origin) instead of
just this origin — a page on any OTHER top-level site gets an empty
partition, not this session.
"""

from odoo.tests import HttpCase, tagged

PARAM = "tuqui.embed_origins"
TUQUI = "https://tuqui.example.com"


@tagged("post_install", "-at_install")
class TestPartitionedCookie(HttpCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)

    @staticmethod
    def _session_cookies(response):
        """All `Set-Cookie: session_id=...` headers, in the order the server sent
        them — `requests` collapses repeated `Set-Cookie` into one comma-joined
        string, so this reads the raw header list instead (see
        test_embed_headers.py for the same gotcha)."""
        raw = response.raw.headers.getlist("Set-Cookie")
        return [v for v in raw if v.startswith("session_id=")]

    def test_partitioned_travels_alongside_samesite_none(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        cookies = self._session_cookies(self.url_open("/web/login", headers={"Origin": TUQUI}))
        self.assertTrue(cookies, "response carried no session cookie")
        last = cookies[-1].lower()
        self.assertIn("samesite=none", last)
        self.assertIn("partitioned", last)

    def test_off_by_default_no_partitioned_anywhere(self):
        """The invariant this module lives by: apagado es apagado."""
        cookies = self._session_cookies(self.url_open("/web/login"))
        self.assertTrue(cookies, "response carried no session cookie")
        self.assertNotIn("partitioned", cookies[-1].lower())

    def test_odoo_own_earlier_cookie_is_left_alone(self):
        """Only the module's OWN reissue gets `Partitioned` — not whatever Odoo
        already wrote earlier in the same response. When the module reissues,
        the response carries the cookie TWICE (Odoo's own `SameSite=Lax` one,
        then this module's `SameSite=None` one); only the second is ours to
        touch, and a `setlist` that clobbered the first would leave Odoo's own
        cookie broken for every response, embed or not."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        cookies = self._session_cookies(self.url_open("/web/login", headers={"Origin": TUQUI}))
        self.assertGreaterEqual(len(cookies), 2, "expected Odoo's own cookie plus the reissue")
        self.assertNotIn("partitioned", cookies[0].lower())
        self.assertIn("partitioned", cookies[-1].lower())
