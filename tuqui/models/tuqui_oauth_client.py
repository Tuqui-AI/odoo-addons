import hashlib
import hmac
import secrets
import urllib.parse
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_STATE_SELECTION = [
    ("pending", "Pending activation"),
    ("active", "Active"),
    ("disconnected", "Disconnected"),
]


class TuquiOAuthClient(models.Model):
    """Singleton-style OAuth 2.0 client credentials store for Tuqui.

    One row per Odoo database. The plaintext ``client_secret`` is shown
    exactly once on creation/rotation and never persisted; only its salted
    SHA-256 hash is stored. Rotation invalidates the previous secret.
    """

    _name = "tuqui.oauth.client"
    _description = "Tuqui OAuth Client Credentials"
    _rec_name = "client_id"

    client_id = fields.Char(required=True, readonly=True, copy=False, index=True)
    client_secret_hash = fields.Char(required=True, readonly=True, copy=False)
    client_secret_salt = fields.Char(required=True, readonly=True, copy=False)
    state = fields.Selection(_STATE_SELECTION, default="pending", required=True, readonly=True)
    activated_at = fields.Datetime(readonly=True)
    last_seen_at = fields.Datetime(readonly=True)
    workspace_id_external = fields.Char(
        string="Tuqui Workspace ID",
        readonly=True,
        help="Identifier returned by Tuqui after the first successful handshake.",
    )
    access_count_7d = fields.Integer(
        string="Accesses (last 7 days)",
        compute="_compute_access_count_7d",
        help="Total RPC calls logged in tuqui.access.log during the last 7 days.",
    )

    _client_id_unique = models.Constraint(
        "unique(client_id)",
        "Tuqui client_id must be unique.",
    )

    # ---------- Singleton helpers ----------

    @api.model
    def _get_singleton(self):
        rec = self.search([], limit=1)
        return rec or self.env["tuqui.oauth.client"]

    @api.model
    def _get_or_create_singleton(self):
        rec = self._get_singleton()
        if rec:
            return rec, None
        plain_secret = secrets.token_urlsafe(48)
        client_id = uuid.uuid4().hex
        salt = secrets.token_hex(16)
        rec = self.create(
            {
                "client_id": client_id,
                "client_secret_hash": self._hash_secret(plain_secret, salt),
                "client_secret_salt": salt,
                "state": "pending",
            }
        )
        return rec, plain_secret

    # ---------- Hashing ----------

    @staticmethod
    def _hash_secret(plain, salt):
        return hashlib.sha256(f"{salt}::{plain}".encode()).hexdigest()

    def verify_secret(self, plain):
        self.ensure_one()
        if not plain or not self.client_secret_hash:
            return False
        candidate = self._hash_secret(plain, self.client_secret_salt)
        return hmac.compare_digest(candidate, self.client_secret_hash)

    # ---------- Actions ----------

    def action_rotate_secret(self):
        """Rotate the secret and surface the plaintext via a sticky notification."""
        self.ensure_one()
        plain_secret = self._rotate_secret_silent()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "warning",
                "title": _("Tuqui secret rotated"),
                "message": _("New client_secret (shown once): %s\n\nUpdate Tuqui with this value.") % plain_secret,
                "sticky": True,
            },
        }

    def _rotate_secret_silent(self) -> str:
        """Generate a new ``client_secret`` and return the plaintext.

        Used by activation flows that hand the secret to another system
        (the Tuqui frontend via the nonce exchange) rather than showing
        it to a human. The plaintext is returned exactly once — the
        caller is responsible for routing it without logging or persisting
        beyond the activation handshake.
        """
        self.ensure_one()
        plain_secret = secrets.token_urlsafe(48)
        salt = secrets.token_hex(16)
        self.write(
            {
                "client_secret_hash": self._hash_secret(plain_secret, salt),
                "client_secret_salt": salt,
            }
        )
        return plain_secret

    def action_start_activation(self):
        """Return an ``act_url`` action that opens ``/tuqui/activation/start``.

        The route mints a fresh nonce + secret and redirects the browser
        on to the Tuqui frontend with the nonce in the query string. UI
        glue only — the work happens server-side at the route.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/tuqui/activation/start",
            "target": "self",
        }

    def action_disconnect(self):
        self.ensure_one()
        self.write({"state": "disconnected"})

    @api.model
    def _get_tuqui_base_url(self):
        """Tuqui base URL — hardcoded for clients, overridable for dev.

        Not a user-facing setting: clients never point the module at a
        different Tuqui. The ``tuqui.base_url`` ir.config_parameter exists
        only so dev environments can target a local/staging instance,
        same pattern as ``tuqui.activation.frontend_url``.
        """
        param = self.env["ir.config_parameter"].sudo().get_param("tuqui.base_url", "https://tuqui.com")
        return (param or "https://tuqui.com").rstrip("/")

    def action_open_tuqui(self):
        """Open the connected Tuqui workspace in a new tab.

        Links straight to ``<base_url>/w/<workspace_id_external>`` when
        the handshake has reported back a workspace identifier. Falls
        back to the bare base URL otherwise — useful even pre-activation
        so the admin can see the destination before clicking Activate.
        """
        self.ensure_one()
        base = self._get_tuqui_base_url()
        if self.workspace_id_external:
            url = "{}/w/{}".format(base, urllib.parse.quote(self.workspace_id_external, safe=""))
        else:
            url = base
        return {
            "type": "ir.actions.act_url",
            "url": url,
            "target": "new",
        }

    def mark_active(self, workspace_id_external=None):
        self.ensure_one()
        vals = {"state": "active", "activated_at": fields.Datetime.now()}
        if workspace_id_external:
            vals["workspace_id_external"] = workspace_id_external
        self.write(vals)

    def touch_last_seen(self):
        self.ensure_one()
        self.write({"last_seen_at": fields.Datetime.now()})

    # ---------- Compute ----------

    def _compute_access_count_7d(self):
        AccessLog = self.env["tuqui.access.log"].sudo()
        since = fields.Datetime.now() - timedelta(days=7)
        for rec in self:
            rec.access_count_7d = AccessLog.search_count([("create_date", ">=", since)])

    # ---------- Guard ----------

    def _ensure_admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only Odoo administrators can manage Tuqui."))
