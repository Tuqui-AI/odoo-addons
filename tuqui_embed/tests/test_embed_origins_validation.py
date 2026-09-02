"""`tuqui.embed_origins` is an access-control list, not free text.

Each entry ends up verbatim inside `frame-ancestors` (see ir_http.py), so it
decides who may frame this Odoo. Before this, the field accepted whatever was
typed — a wildcard, a bare scheme, a typo'd `http://` — and any of those would
have opened the frame to more than whoever wrote the value meant to.
"""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

PARAM = "tuqui.embed_origins"


@tagged("post_install", "-at_install")
class TestEmbedOriginsValidation(TransactionCase):
    def _param(self, value=""):
        record = self.env["ir.config_parameter"].sudo().search([("key", "=", PARAM)], limit=1)
        if record:
            record.value = value
            return record
        return self.env["ir.config_parameter"].sudo().create({"key": PARAM, "value": value})

    # ── accepted ───────────────────────────────────────────────────────────

    def test_accepts_a_plain_https_origin(self):
        param = self._param("")
        param.write({"value": "https://tuqui.example.com"})
        self.assertEqual(param.value, "https://tuqui.example.com")

    def test_accepts_several_space_separated_origins(self):
        param = self._param("")
        param.write({"value": "https://tuqui.example.com https://staging.tuqui.example.com"})
        self.assertEqual(param.value, "https://tuqui.example.com https://staging.tuqui.example.com")

    def test_accepts_http_localhost_for_local_dev(self):
        param = self._param("")
        param.write({"value": "http://localhost:5173"})
        self.assertEqual(param.value, "http://localhost:5173")

    def test_accepts_clearing_it(self):
        param = self._param("https://tuqui.example.com")
        param.write({"value": ""})
        self.assertFalse(param.value)

    # ── rejected ───────────────────────────────────────────────────────────

    def test_rejects_a_wildcard(self):
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://*.tuqui.com"})

    def test_rejects_a_bare_scheme(self):
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https:"})

    def test_rejects_plain_http_on_a_real_host(self):
        """`http://` anywhere but loopback means the cookie — and the frame
        permission — travel in the clear."""
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "http://tuqui.example.com"})

    def test_rejects_a_trailing_slash(self):
        """The module's own README documents origins without a trailing slash
        — this turns that from a convention nobody checks into a rule."""
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://tuqui.example.com/"})

    def test_rejects_a_path(self):
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://tuqui.example.com/embed"})

    def test_one_bad_entry_blocks_the_whole_write(self):
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://tuqui.example.com *"})
        self.assertFalse(param.value, "a rejected write must not partially apply")

    def test_invalid_value_is_rejected_on_create_too(self):
        """The other path a validation-only-on-write misses: on a fresh
        install the parameter doesn't exist yet, so the first save is a
        `create`, not a `write`."""
        self.env["ir.config_parameter"].sudo().search([("key", "=", PARAM)]).unlink()
        with self.assertRaises(ValidationError):
            self.env["ir.config_parameter"].sudo().create({"key": PARAM, "value": "*"})

    # ── scope ──────────────────────────────────────────────────────────────

    def test_unrelated_parameters_are_not_validated(self):
        """This module has no opinion on the rest of the configuration."""
        param = self.env["ir.config_parameter"].sudo().create({"key": "tuqui.other_thing", "value": "*"})
        self.assertEqual(param.value, "*")
