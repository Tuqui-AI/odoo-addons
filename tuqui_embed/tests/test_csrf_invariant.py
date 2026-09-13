"""What stops a same-site CSRF: the data routes are JSON-only.

WHY IT STILL EXISTS, with the premise corrected. This module **no longer
loosens `SameSite`** — that was removed, see `test_cookie_is_never_touched.py` —
so an arbitrary site on the internet can no longer get the browser to send the
session.

But the new design rests the session on the panel being served on the **same
site** as this Odoo, and `SameSite=Lax` **does** travel between pages of one
site. So the risk moved from "any site" to "any page under our own domain" — a
compromised or mis-pointed subdomain. A much smaller surface and one we
control, but not zero, and a `<form>` POST is still the vector CORS does not
stop.

What stops it is that Odoo's data routes **only accept JSON**, and a `<form>`
cannot emit `application/json`: only `form-urlencoded`, `multipart/form-data` or
`text/plain`. That came out of MEASURING it in a run (Odoo answered 415 and
wrote nothing), and a measurement protects nothing: the day someone adds a route
that accepts a form, the protection falls **without any test turning red**. This
one turns red.

It does not test our code: it tests a property of Odoo we depend on. That is
deliberate — it is exactly the kind of assumption that must be pinned when a
defence rests on it.
"""

import json

from odoo.tests import HttpCase, tagged

PARAM = "tuqui.embed_origins"
TUQUI = "https://tuqui.example.com"

#: The three types an HTML `<form>` can emit. If any of them writes, CSRF is
#: possible again from a same-site page.
FORM_CONTENT_TYPES = (
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
)


@tagged("post_install", "-at_install")
class TestCsrfInvariant(HttpCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM, TUQUI)
        self.authenticate("admin", "admin")

    def _write_the_company_name(self, content_type, body):
        """Try to rename the company the way a form from another page would."""
        return self.url_open(
            "/web/dataset/call_kw",
            data=body,
            headers={"Content-Type": content_type, "Origin": "https://another-site.example.com"},
            timeout=30,
        )

    def test_a_form_cannot_write_even_carrying_the_session(self):
        """The invariant. All three form types have to be rejected by the data
        route, even when the request carries a valid session."""
        company = self.env.company
        original_name = company.name
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "model": "res.company",
                "method": "write",
                "args": [[company.id], {"name": "TAKEN OVER BY CSRF"}],
                "kwargs": {},
            },
        }

        for content_type in FORM_CONTENT_TYPES:
            with self.subTest(content_type=content_type):
                resp = self._write_the_company_name(content_type, json.dumps(payload))
                # 415 (Unsupported Media Type) and not a generic "not 200": it
                # is the code that says it refused BECAUSE OF THE CONTENT-TYPE,
                # which is the invariant. A "not 200" would pass just as well if
                # the route started failing for any other reason — and there the
                # test would stay green while the real protection disappears.
                self.assertEqual(
                    resp.status_code,
                    415,
                    "the route stopped rejecting %s by content-type (status %s): from a "
                    "same-site page, that reopens CSRF" % (content_type, resp.status_code),
                )

        # And what really matters: nothing was written.
        company.invalidate_recordset(["name"])
        self.assertEqual(
            company.name,
            original_name,
            "the company was renamed from a foreign origin — the protection fell",
        )

    def test_the_same_request_in_json_still_works(self):
        """Calibration: if the test above passed because the route rejects
        EVERYTHING, it would be measuring nothing. In JSON it has to work."""
        resp = self.url_open(
            "/web/dataset/call_kw",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "model": "res.company",
                        "method": "read",
                        "args": [[self.env.company.id], ["name"]],
                        "kwargs": {},
                    },
                }
            ),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("error", resp.json(), resp.text[:200])
