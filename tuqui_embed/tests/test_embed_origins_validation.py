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

    def test_accepts_http_on_a_localhost_subdomain(self):
        """`.localhost` is reserved for loopback (RFC 6761), so any subdomain
        of it is as local as bare `localhost`."""
        param = self._param("")
        param.write({"value": "http://panel.localhost:9400"})
        self.assertEqual(param.value, "http://panel.localhost:9400")

    def test_accepts_a_plain_http_origin_when_this_odoo_is_also_plain_http(self):
        """The missing case, and it turned up while using the module.

        The new design requires the panel to be on the SAME SITE as Odoo — two
        names under a common domain — and that cannot be assembled locally with
        `localhost` alone (browsers treat `.localhost` subdomains as separate
        sites). Under the previous rule, a development panel at
        `http://panel.mydomain.test` was impossible to declare, so developing
        the feature itself required local TLS.

        If this Odoo is already served over http, the session travels in the
        clear anyway: demanding https from the embedder protected nothing.
        """
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "http://odoo.midominio.test:8069")
        param = self._param("")
        param.write({"value": "http://panel.midominio.test:9400"})
        self.assertEqual(param.value, "http://panel.midominio.test:9400")

    def test_rejects_plain_http_when_this_odoo_is_https(self):
        """The discriminator for the test above: the same address, rejected
        when the deployment IS https. Without this pair, the development clause
        would be a hole rather than an exception."""
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://odoo.midominio.test")
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "http://panel.midominio.test:9400"})

    def test_rejects_plain_http_when_the_base_url_says_nothing(self):
        """With no `web.base.url` saying what the deployment is, the default
        has to be the strict one.

        It is emptied rather than deleted because Odoo does not allow deleting
        that record (`unlink_default_parameters`), and empty exercises the same
        path.
        """
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "")
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "http://panel.midominio.test:9400"})

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
        """On an https deployment, `http://` outside loopback hands framing
        permission to a host nobody can authenticate.

        `web.base.url` is set explicitly: a test database usually carries it as
        `http://localhost:8069`, and with that the development clause would let
        any http through — the test would pass while measuring nothing.
        """
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://odoo.example.com")
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

    def test_rejects_a_semicolon_that_would_open_a_second_csp_directive(self):
        """The one that matters, and it looked like a valid origin.

        `urlsplit("https://a.com;sandbox")` gives scheme `https`, netloc
        `a.com;sandbox` and no path — so it walked past every check. The browser
        reads that `;` as the end of the directive: `frame-ancestors 'self'
        https://a.com` followed by a brand new `sandbox` directive, which is the
        strictest sandbox there is, applied to EVERY response of the database.
        """
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://a.com;sandbox"})
        self.assertFalse(param.value, "a rejected write must not partially apply")

    def test_rejects_a_comma_too(self):
        """Same family: a CSP source list is whitespace-separated, so a comma
        inside one token is not a separator anybody parses the way it looks."""
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://a.com,https://b.com"})

    def test_rejects_userinfo(self):
        """`https://u:p@a.com` is not a valid CSP source expression: the panel
        would come up blank with nothing saying why."""
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "https://u:p@a.com"})

    def test_a_public_host_that_merely_starts_with_127_is_not_loopback(self):
        """`127.evil.com` is a perfectly public name. The previous check was
        `startswith("127.")`, so it handed the http exemption to anyone who
        registered one."""
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://odoo.example.com")
        param = self._param("")
        with self.assertRaises(ValidationError):
            param.write({"value": "http://127.evil.com"})

    def test_real_loopback_addresses_are_still_accepted(self):
        """The control for the test above: tightening the check must not have
        cost the case it exists for."""
        param = self._param("")
        param.write({"value": "http://127.0.0.1:8069"})
        self.assertEqual(param.value, "http://127.0.0.1:8069")

    def test_renaming_a_parameter_into_the_switch_is_validated(self):
        """The door that stayed open after the first fix.

        Everything hung off `if "value" in vals`, so a bare
        `write({"key": ...})` renamed some other parameter into the switch
        carrying whatever unvalidated text it already held — and left no line in
        the log either.
        """
        other = (
            self.env["ir.config_parameter"].sudo().create({"key": "tuqui.some_other", "value": "https://a.com;sandbox"})
        )
        self.env["ir.config_parameter"].sudo().search([("key", "=", PARAM)]).unlink()
        with self.assertRaises(ValidationError):
            other.write({"key": PARAM})

    # ── scope ──────────────────────────────────────────────────────────────

    def test_unrelated_parameters_are_not_validated(self):
        """This module has no opinion on the rest of the configuration."""
        param = self.env["ir.config_parameter"].sudo().create({"key": "tuqui.other_thing", "value": "*"})
        self.assertEqual(param.value, "*")
