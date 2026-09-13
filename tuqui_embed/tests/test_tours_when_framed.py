"""Tours come back switched off when the request is a frame's navigation.

WHY IT EARNS ITS OWN TEST. This fix has two halves — server and client — and
only one is reachable from here; the other lives in `static/tests/`. The server
half was once gated on `tuqui.embed_origins` while the client half applied
always, because JS cannot read a server parameter. Two halves of one fix under
different rules is the kind of difference nobody notices until someone frames
this Odoo by another route and a user with a half-finished onboarding loses
their tab.

The rule is now a single one and it does not consult the switch: if the request
is a frame's navigation, tours go out off. This pins that.

THE PAIR THAT DISCRIMINATES. Inside the frame off, OUTSIDE on. A fix that
switched them off always would pass half of this test and break everyone's
onboarding — which is exactly the failure mode we do not want.

THE CALIBRATION IS NOT OPTIONAL. `tour_enabled` is computed with
`not modules.module.current_test`, so in a test run it is born False: without
writing it by hand, the "outside the frame" control would pass by virtue of
being off from the start and would prove nothing.
"""

import json

from odoo.tests import HttpCase, tagged

PARAM = "tuqui.embed_origins"
SESSION_INFO = "/web/session/get_session_info"


@tagged("post_install", "-at_install")
class TestToursWhenFramed(HttpCase):
    def setUp(self):
        super().setUp()
        self.authenticate("admin", "admin")
        # See the docstring: without this the control below is vacuous.
        self.env.ref("base.user_admin").sudo().tour_enabled = True
        self.env["ir.config_parameter"].sudo().set_param(PARAM, False)

    def _session_info(self, headers=None):
        resp = self.url_open(
            SESSION_INFO,
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}),
            headers={"Content-Type": "application/json", **(headers or {})},
            timeout=30,
        )
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        body = resp.json()
        self.assertNotIn("error", body, resp.text[:300])
        return body["result"]

    def test_a_framed_request_comes_back_without_tours(self):
        info = self._session_info({"Sec-Fetch-Dest": "iframe"})
        self.assertFalse(
            info.get("tour_enabled"),
            "a tour starting inside the frame kills the user's browser tab",
        )
        self.assertFalse(info.get("current_tour"))

    def test_a_top_level_request_keeps_tours(self):
        """The control. In their everyday Odoo the onboarding is intact."""
        self.assertTrue(
            self._session_info().get("tour_enabled"),
            "tours were switched off outside a frame: that breaks onboarding for everyone",
        )

    def test_the_switch_does_not_gate_this(self):
        """The crash is Odoo's and does not depend on this module being on.

        With the embed OFF — the default — a frame still has to come back
        without tours. If this ever turns red it will be because someone tied
        the fix back to the switch, and there the client half and the server
        half disagree again.
        """
        info = self._session_info({"Sec-Fetch-Dest": "iframe"})
        self.assertFalse(info.get("tour_enabled"))
