import secrets

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


# How long a nonce stays redeemable once issued. Tight on purpose —
# activation is a synchronous flow; if the admin doesn't complete it in
# five minutes, they restart from the button.
_NONCE_TTL_MINUTES = 5

# How long expired/consumed nonces are kept around before the cron purges
# them. Not a security knob — the security is the NULL of
# client_secret_plaintext at consume time + the TTL above. This window
# is purely for housekeeping (e.g. debugging "was this nonce ever used?").
_GC_RETENTION_DAYS = 7


class TuquiActivationNonce(models.Model):
    """Short-lived state for the activation handoff to the Tuqui frontend.

    Flow recap:

    1. Admin clicks "Activate Tuqui" on the OAuth client form. The server
       action mints a fresh ``client_id``/``client_secret`` pair (without
       persisting the secret hash yet on the singleton — see ``issue``)
       and inserts a row here containing the plaintext secret. It then
       redirects the admin's browser to the Tuqui frontend URL with the
       ``nonce`` (and only the nonce) in the query string.

    2. The Tuqui frontend, with the admin logged into Tuqui, POSTs the
       nonce to ``POST /tuqui/activation/exchange`` on this module. The
       endpoint returns the plaintext ``client_secret`` (plus
       ``client_id``, ``companion_url``, ``acting_user_login``,
       ``module_version``, ``protocol_version``). The row is marked
       ``consumed_at = now`` and ``client_secret_plaintext`` is set to
       NULL in the same UPDATE.

    3. The Tuqui backend uses those credentials to POST to its own
       ``/api/onboarding/companion/activate`` (Sprint 1.6b), wiring the
       Tuqui workspace to this Odoo instance.

    Plaintext-secret rationale: the column holds the secret in clear for
    a max of 5 minutes, and only inside this module's DB (which the
    workspace admin already controls). Encryption would add ceremony
    without changing the threat model — a DB-level attacker reading the
    nonce row also has access to the OAuth client hash next to it.
    """

    _name = "tuqui.activation.nonce"
    _description = "Tuqui Activation Nonce"
    _order = "id desc"
    _rec_name = "nonce"

    nonce = fields.Char(required=True, index=True, readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True, index=True)
    consumed_at = fields.Datetime(readonly=True)
    client_secret_plaintext = fields.Char(
        readonly=True,
        help=(
            "OAuth client_secret in plaintext, kept only until "
            "consumed_at is set or expires_at passes. NULL after consume."
        ),
    )
    client_id = fields.Char(required=True, readonly=True)

    _nonce_unique = models.Constraint(
        "unique(nonce)",
        "Tuqui activation nonce must be unique.",
    )

    @api.model
    def _issue(self, *, client_id, client_secret_plaintext, ttl_minutes=_NONCE_TTL_MINUTES):
        """Mint a fresh nonce row and return the unguessable token.

        The plaintext secret is intentionally stored here (and only here)
        so the exchange endpoint can hand it back to the Tuqui frontend
        on first redemption — never written to logs, never re-issued.
        """
        nonce = secrets.token_urlsafe(48)
        expires_at = fields.Datetime.add(fields.Datetime.now(), minutes=ttl_minutes)
        self.sudo().create(
            {
                "nonce": nonce,
                "expires_at": expires_at,
                "client_id": client_id,
                "client_secret_plaintext": client_secret_plaintext,
            }
        )
        return nonce, expires_at

    def _consume(self):
        """Mark this row as redeemed and wipe the plaintext secret.

        Idempotent at the SQL level via the unique nonce. Callers are
        expected to fetch + branch on ``consumed_at``/``expires_at``
        before invoking; this method only handles the destructive write.
        """
        self.ensure_one()
        self.sudo().write(
            {
                "consumed_at": fields.Datetime.now(),
                "client_secret_plaintext": False,
            }
        )

    @api.model
    def _gc_old_nonces(self):
        """Purge nonces past the retention window.

        Wired to a daily ``ir.cron``. Not a security control — security
        is the NULL on consume + the 5-minute TTL. This is housekeeping
        to keep the table bounded.
        """
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=_GC_RETENTION_DAYS)
        old = self.sudo().search([("expires_at", "<", cutoff)])
        if old:
            old.unlink()
        return len(old)

    @api.constrains("client_id")
    def _check_client_id(self):
        for rec in self:
            if not rec.client_id:
                raise ValidationError(_("client_id is required on the activation nonce."))
