"""Leave a trace of when the embed is switched on, and by whom.

WHY. Setting `tuqui.embed_origins` is not configuring a preference: it is the
switch between "normal Odoo" and "Odoo that lets itself be shown inside another
site". Empty, this module is indistinguishable from not being installed; with
one address in it, clickjacking protection is given up for that origin. The
session cookie is NOT touched — see the README and the invariant in
`tests/test_cookie_is_never_touched.py`.

A decision like that has to leave a trace. If a problem shows up tomorrow, the
first question is who enabled that origin and when.

IT GOES TO THE LOG AND NOT TO THE INTERFACE, on purpose. Leaving a note in the
company's chatter was considered and dropped: it tells an administrator
nothing and clutters a place people read for other reasons. The log is read by
whoever is investigating something, which is exactly when this matters.

AND IT GOES IN `info`, NOT `warning`. The first version used `warning` because
of how serious the change is, and runbot caught it: it marks any build red
whose log carries a warning, and this module emitted one on every legitimate
flip of the switch — thirteen in its own test suite alone. Odoo's convention is
the right one: `warning` means "something is wrong and needs looking at", and
this is a deliberate administrator action. What makes the line auditable is
that it says who, when and from what to what, not its severity level.
"""

import ipaddress
import logging
import re
from urllib.parse import urlsplit

from odoo import api, models
from odoo.exceptions import ValidationError

from .ir_http import EMBED_ORIGINS_PARAM

_logger = logging.getLogger(__name__)

#: A DNS host: labels of letters, digits and hyphens, separated by dots. It is
#: deliberately narrow, because what is being validated is not a display string
#: — it is the text that lands verbatim inside a `Content-Security-Policy`
#: header. `urlsplit` is happy to keep a `;` inside the host (`a.com;sandbox`
#: parses as scheme `https`, netloc `a.com;sandbox`, no path), and the browser
#: reads that `;` as the end of the directive: `frame-ancestors 'self'
#: https://a.com` followed by a brand new `sandbox` directive, applied to every
#: response of the database. Anything outside this alphabet is rejected.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def _is_loopback_host(host):
    """Is `host` a name or address that never leaves this machine?

    `.localhost` is reserved for loopback by RFC 6761, so any subdomain of it
    counts. Addresses go through `ipaddress` rather than a prefix test: the
    previous `startswith("127.")` accepted `127.evil.com`, a perfectly public
    host, as if it were loopback.
    """
    if not host:
        return False
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _deployment_is_plain_http(env):
    """Is this Odoo served over http, i.e. is it a development environment?

    We look at `web.base.url` — the identity the administrator declared for the
    deployment, and the one Odoo uses for its own links — and not at the
    request's scheme, which behind a proxy depends on `X-Forwarded-Proto` and
    `ProxyFix` being set up correctly. If it is unset, production is assumed and
    we stay strict.
    """
    base_url = (env["ir.config_parameter"].sudo().get_param("web.base.url") or "").strip().lower()
    return base_url.startswith("http://")


def _validate_embed_origin_token(token, allow_plain_http=False):
    """Return an error message for one invalid origin, or None if it's fine.

    `token` ends up verbatim inside `frame-ancestors` (see ir_http.py), so it
    decides who may frame this Odoo — it's not a display string, it's an
    access-control entry. A wildcard, a bare scheme, or a plain-http host
    would open the frame to more than whoever typed the value meant to.

    Plain strings, not `_()`: this is a module-level function with no `env` in
    the frame, and the bare `_()` resolves the language from there — it would log
    a WARNING on every call ("no translation language detected"), the noise this
    module already learned to keep out of its own test run (see
    `_log_embed_change` below). They stay untranslated on purpose for now; the
    fix is `env._()`, which takes the language explicitly, and it needs `env`
    threaded down from `_validate_embed_origins`.
    """
    if "*" in token:
        return "wildcards are not accepted: %r" % token
    parsed = urlsplit(token)
    if parsed.scheme not in ("http", "https"):
        return "must start with https:// (or http:// for local development): %r" % token
    if not parsed.netloc:
        return "the host is missing: %r" % token
    if parsed.username or parsed.password:
        # Not a valid CSP source expression: the panel would come up blank with
        # nothing saying why.
        return "must carry no user or password: %r" % token
    try:
        port = parsed.port
    except ValueError:
        return "the port is not a number: %r" % token
    host = (parsed.hostname or "").lower()
    # The host has to be a host and NOTHING else. See `_HOST_RE`: this is the
    # check that stops a `;` from opening a second CSP directive.
    try:
        is_address = bool(ipaddress.ip_address(host))
    except ValueError:
        is_address = False
    if not is_address and not _HOST_RE.match(host):
        return "the host has characters that do not belong in one: %r" % token
    # And the netloc has to be exactly that host and port, rebuilt. Checking the
    # hostname alone is not enough — `urlsplit` hands back the same hostname for
    # a netloc carrying extra text around it.
    bracketed = "[%s]" % host if ":" in host else host
    expected = bracketed if port is None else "%s:%d" % (bracketed, port)
    if parsed.netloc.lower() != expected:
        return "must be scheme and host only: %r" % token
    if parsed.scheme == "http" and not (allow_plain_http or _is_loopback_host(host)):
        return (
            "must be https:// — http:// is only accepted on loopback, or when this "
            "Odoo is itself served over http (web.base.url): %r" % token
        )
    if parsed.path or parsed.query or parsed.fragment:
        return "must be scheme and host only, with no trailing slash or path: %r" % token
    return None


def _validate_embed_origins(env, value):
    """Raise if `value` (space-separated origins) has an invalid entry.

    Plain `http://` outside loopback is accepted ONLY when this Odoo is served
    over http too, and that is not a convenience exception: if the session
    already travels in the clear, demanding https from the embedder protects
    nothing. And it is needed, because the design requires the panel to be on
    the SAME SITE as Odoo — two names under a common domain — and that cannot be
    assembled locally with `localhost` alone: browsers treat `.localhost`
    subdomains as separate sites (measured). Without this clause, developing the
    feature itself would require local TLS, which in turn requires the proxy
    that does not exist yet.
    """
    allow_plain_http = _deployment_is_plain_http(env)
    for token in (value or "").split():
        error = _validate_embed_origin_token(token, allow_plain_http=allow_plain_http)
        if error:
            raise ValidationError("tuqui.embed_origins: %s" % error)


class IrConfigParameter(models.Model):
    _inherit = "ir.config_parameter"

    def _log_embed_change(self, new_value):
        """Record in the log that the embed changed state, and at whose hands."""
        for parameter in self:
            if parameter.key != EMBED_ORIGINS_PARAM:
                continue
            before = (parameter.value or "").strip()
            after = (new_value or "").strip()
            if before == after:
                continue
            _logger.info(
                "tuqui_embed: %s changed by %s (uid=%s) — was=%r now=%r. "
                "With an origin loaded, Odoo lets itself be shown inside that site "
                "(the session cookie is not touched).",
                EMBED_ORIGINS_PARAM,
                self.env.user.display_name,
                self.env.uid,
                before or "(empty: embed off)",
                after or "(empty: embed off)",
            )

    def write(self, vals):
        # A record BECOMES the switch when the write carries the key, and IS the
        # switch when it already holds it. Both have to be validated and logged,
        # and the value to check is whichever one the record ends up with: the
        # one in `vals` when the write brings a new one, its current one when it
        # does not.
        #
        # Hanging all of this off `if "value" in vals` was the hole. A bare
        # `write({"key": "tuqui.embed_origins"})` renamed some other parameter
        # into the switch carrying whatever unvalidated text it already held,
        # and left no line in the log either — the comment here used to claim
        # that path was closed while it was open one `vals` key away.
        becomes_the_switch = vals.get("key") == EMBED_ORIGINS_PARAM
        for parameter in self:
            if not becomes_the_switch and parameter.key != EMBED_ORIGINS_PARAM:
                continue
            new_value = vals["value"] if "value" in vals else parameter.value
            _validate_embed_origins(self.env, new_value)
            parameter._log_embed_change(new_value)
        return super().write(vals)

    def unlink(self):
        """Deleting the parameter switches the embed off, so it gets logged too.

        Not a corner case: `set_param(key, False)` DELETES the record (core
        `ir.config_parameter.set_param`), so "switch the embed off" lands here
        and never in `write`. Without this, the one state change that leaves no
        trace is the one an incident starts from — "it stopped working, when did
        somebody turn it off?".
        """
        for parameter in self:
            if parameter.key == EMBED_ORIGINS_PARAM:
                parameter._log_embed_change("")
        return super().unlink()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("key") == EMBED_ORIGINS_PARAM:
                _validate_embed_origins(self.env, vals.get("value"))
            if vals.get("key") == EMBED_ORIGINS_PARAM and (vals.get("value") or "").strip():
                _logger.info(
                    "tuqui_embed: %s created by %s (uid=%s) with %r. With an origin loaded, "
                    "Odoo lets itself be shown inside that site (the session cookie "
                    "is not touched).",
                    EMBED_ORIGINS_PARAM,
                    self.env.user.display_name,
                    self.env.uid,
                    vals["value"],
                )
        return super().create(vals_list)
