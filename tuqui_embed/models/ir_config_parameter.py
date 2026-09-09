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

import logging
from urllib.parse import urlsplit

from odoo import api, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

EMBED_ORIGINS_PARAM = "tuqui.embed_origins"

#: Loopback names and addresses, where `http://` is always fine because the
#: traffic never leaves the machine. `.localhost` is reserved for loopback by
#: RFC 6761, so any subdomain of it counts.
_LOOPBACK_HTTP_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _is_loopback_host(host):
    if not host:
        return False
    host = host.lower()
    return host in _LOOPBACK_HTTP_HOSTS or host == "localhost" or host.endswith(".localhost") or host.startswith("127.")


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

    Plain strings, not `odoo._()`: this runs on every `write`/`create`,
    including from plain `TransactionCase` tests with no request/lang in
    context, and `_()` logs a WARNING there ("no translation language
    detected") — the exact kind of noise this module already learned to
    keep out of its own test run (see `_log_embed_change` below).
    """
    if "*" in token:
        return "wildcards are not accepted: %r" % token
    parsed = urlsplit(token)
    if parsed.scheme not in ("http", "https"):
        return "must start with https:// (or http:// for local development): %r" % token
    if not parsed.netloc:
        return "the host is missing: %r" % token
    if parsed.scheme == "http" and not (allow_plain_http or _is_loopback_host(parsed.hostname)):
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
        if "value" in vals:
            # `vals` may CARRY the key: renaming any parameter to
            # `tuqui.embed_origins` turned it into the switch without going
            # through validation. It is an odd path, but what validates cannot
            # depend on which door the value came in through.
            becomes_the_switch = vals.get("key") == EMBED_ORIGINS_PARAM
            for parameter in self:
                if becomes_the_switch or parameter.key == EMBED_ORIGINS_PARAM:
                    _validate_embed_origins(self.env, vals["value"])
            self._log_embed_change(vals["value"])
        return super().write(vals)

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
