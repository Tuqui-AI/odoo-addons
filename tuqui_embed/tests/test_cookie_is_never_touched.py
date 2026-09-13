"""The invariant this module now lives by: it NEVER touches the session cookie.

This is the load-bearing test of the whole design, and it exists because the
previous version of this module did the opposite. It reissued the session with
`SameSite=None` so it would travel to a panel on ANOTHER site, and that was
measured to open two channels CORS does not cover: a cross-origin WebSocket
that reads the user's live bus, and an `<img>` to `/web/become` that escalates
a logged-in admin to superuser with no click. `Partitioned` (CHIPS) was tried
as the mitigation — it closes the `<img>`, but it breaks the panel outright
(reopening it in a new tab loops forever against `/web/login`). Both measured
before/after against real Chrome.

The design that came out of that is: allow the frame, touch nothing else. The
session reaches the iframe because the panel is served on the SAME SITE as this
Odoo, so Odoo's ordinary cookie already applies there — `SameSite` is defined
per site, not per origin.

Which makes "the module does not touch the cookie" the property that separates
this module from the one that opened those two vectors. It can be broken by a
well-meaning change (someone re-adds a reissue to "make it work" for a
cross-site panel) without anything else failing, so it gets its own test.
"""

from odoo.tests import HttpCase, tagged

PARAM = "tuqui.embed_origins"
PANEL = "https://panel.tuqui.example.com"


@tagged("post_install", "-at_install")
class TestCookieIsNeverTouched(HttpCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)

    @staticmethod
    def _session_cookies(response):
        """Every `Set-Cookie: session_id=...`, in the order the server sent them.

        Read from the raw header list on purpose: `requests` collapses repeated
        `Set-Cookie` headers into one comma-joined string, which would hide a
        second, reissued cookie behind the first one's attributes.
        """
        raw = response.raw.headers.getlist("Set-Cookie")
        return [v for v in raw if v.startswith("session_id=")]

    def _get(self, headers=None):
        """A request that Odoo is guaranteed to answer WITH a session cookie.

        This needs care, and it is the part that made the first version of
        these tests wrong. Odoo emits `Set-Cookie: session_id` only when it
        has a session to announce — a plain anonymous GET gets none, measured.
        That used to be invisible, because the reissue this module no longer
        does put a cookie on EVERY response.

        Sending a `session_id` the server does not know forces it to mint a
        fresh session and announce it, in both states of the switch — which is
        what makes the on/off comparison below an apples-to-apples one.
        """
        self.opener.cookies.set("session_id", "not-a-real-session-id", domain="127.0.0.1", path="/")
        return self.url_open("/web/login", headers=headers or {})

    def test_only_one_session_cookie_is_ever_sent(self):
        """Two `Set-Cookie: session_id` in one response IS the signature of a
        reissue — that is exactly what the old version did, and the browser
        applied the lower one. One cookie means nobody re-added it."""
        self.env["ir.config_parameter"].sudo().set_param(PARAM, PANEL)
        cookies = self._session_cookies(self._get(headers={"Origin": PANEL}))
        self.assertEqual(
            len(cookies), 1, "the response carried %d session cookies — a reissue is back: %s" % (len(cookies), cookies)
        )

    def test_the_cookie_carries_no_loosened_samesite(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM, PANEL)
        cookie = "".join(self._session_cookies(self._get(headers={"Origin": PANEL}))).lower()
        self.assertTrue(cookie, "the response carried no session cookie")
        self.assertNotIn("samesite=none", cookie)
        self.assertNotIn("partitioned", cookie)

    def test_turning_the_module_on_does_not_change_the_cookie(self):
        """The strongest form: the cookie's attributes are identical whether the
        module is on or off. A test that only looked for `SameSite=None` would
        pass while some other attribute got rewritten (the old reissue also
        forced `Secure` and rewrote `Max-Age`)."""
        off = self._session_cookies(self._get())
        self.env["ir.config_parameter"].sudo().set_param(PARAM, PANEL)
        on = self._session_cookies(self._get(headers={"Origin": PANEL}))
        self.assertTrue(off, "the module-off response carried no session cookie")
        self.assertTrue(on, "the module-on response carried no session cookie")

        def attributes(raw):
            # Drop the value (it differs per session) and the expiry stamp
            # (it moves with the clock); compare the attribute set itself.
            parts = [p.strip().lower() for p in raw.split(";")[1:]]
            return sorted(p for p in parts if not p.startswith("expires="))

        self.assertEqual(
            attributes(on[-1]),
            attributes(off[-1]),
            "turning the embed on changed the session cookie's attributes",
        )
