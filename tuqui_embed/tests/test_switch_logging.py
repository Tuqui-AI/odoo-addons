"""Switching the embed on leaves a trace in the log, and so does switching it off.

It is the counterpart of giving up clickjacking protection: the decision is
recorded with who and when. If a problem shows up tomorrow, the first question
is who enabled that origin.

WHY IT IS NOT TESTED THROUGH `set_param`. That was the first attempt and it
discriminated nothing: `set_param` decides on its own whether to `create` or
`write`, and with a value equal to the current one it may not write at all — so
a mutation in the code never got to run and the tests stayed green. Verified
with three mutations it failed to detect.

The two halves are tested separately, which is what is needed:
  · the function records what it must record, and stays quiet when it should;
  · and it is wired into `write` and `create`, or the right function is never
    called at all.
"""

import logging

from odoo.tests import TransactionCase, tagged

LOGGER = "odoo.addons.tuqui_embed.models.ir_config_parameter"
PARAM = "tuqui.embed_origins"
TUQUI = "https://tuqui.example.com"


@tagged("post_install", "-at_install")
class TestSwitchLogging(TransactionCase):
    def _parameter(self, value=""):
        """The parameter record, carrying whatever value is asked for."""
        params = self.env["ir.config_parameter"].sudo()
        existing = params.search([("key", "=", PARAM)], limit=1)
        if existing:
            existing.value = value
            return existing
        return params.create({"key": PARAM, "value": value})

    def test_switching_it_on_is_logged_with_who_did_it(self):
        parameter = self._parameter("")
        with self.assertLogs(LOGGER, logging.INFO) as captured:
            parameter._log_embed_change(TUQUI)
        record = "\n".join(captured.output)
        self.assertIn(PARAM, record)
        self.assertIn(TUQUI, record)
        # Who: without that, the record does not serve what it is wanted for.
        self.assertIn(self.env.user.display_name, record)

    def test_switching_it_off_too(self):
        """It matters just as much: it explains why an embed stopped working."""
        parameter = self._parameter(TUQUI)
        with self.assertLogs(LOGGER, logging.INFO) as captured:
            parameter._log_embed_change("")
        self.assertIn(PARAM, "\n".join(captured.output))

    def test_the_same_value_does_not_clutter_the_log(self):
        """A save with no change is not a decision: logging it fills the log
        with noise and makes it stop being read."""
        parameter = self._parameter(TUQUI)
        with self.assertNoLogs(LOGGER, logging.INFO):
            parameter._log_embed_change(TUQUI)

    def test_another_parameter_is_not_logged(self):
        """This module has nothing to say about the rest of the configuration."""
        other = self.env["ir.config_parameter"].sudo().create({"key": "tuqui.other_thing", "value": "something"})
        with self.assertNoLogs(LOGGER, logging.INFO):
            other._log_embed_change("another value")

    # ── And that it is wired in, or none of the above ever runs ──────────────

    def test_write_goes_through_the_log(self):
        parameter = self._parameter("")
        with self.assertLogs(LOGGER, logging.INFO) as captured:
            parameter.write({"value": TUQUI})
        self.assertIn(TUQUI, "\n".join(captured.output))

    def test_creating_the_parameter_already_on_is_logged(self):
        """The path `write` does not cover: on a fresh install the parameter
        does not exist, so the first time is a `create`."""
        self.env["ir.config_parameter"].sudo().search([("key", "=", PARAM)]).unlink()
        with self.assertLogs(LOGGER, logging.INFO) as captured:
            self.env["ir.config_parameter"].sudo().create({"key": PARAM, "value": TUQUI})
        self.assertIn(TUQUI, "\n".join(captured.output))

    def test_creating_another_parameter_is_not_logged(self):
        with self.assertNoLogs(LOGGER, logging.INFO):
            self.env["ir.config_parameter"].sudo().create({"key": "tuqui.yet_another_thing", "value": "something"})
