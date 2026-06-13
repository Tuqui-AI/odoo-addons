from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# Default deny rules created by the "Solo lectura" preset. Keep the
# tuples in (effect, model_pattern, method_pattern, operation_type)
# order so the idempotency check below stays trivial.
_READ_ONLY_PRESET_RULES = (
    ("deny", "*", "*", "write", "Preset Solo lectura — bloqueo de write"),
    ("deny", "*", "*", "execute", "Preset Solo lectura — bloqueo de execute"),
    ("deny", "*", "*", "private_execute", "Preset Solo lectura — bloqueo de private"),
)


class TuquiRpcPolicy(models.Model):
    """Singleton config for how ``/tuqui/rpc`` evaluates incoming calls.

    Two modes:

    - **default**: hardcoded behavior — absolute blocks (``sudo``,
      ``with_*``, ``flush*``, ``invalidate*``) plus ``private_execute``
      bypass. No rule table consulted. Works out of the box, no setup.

    - **advanced**: enables the ``tuqui.rpc.rule`` allow/deny table.
      Deny rules win. ``private_execute`` requires an exact allow rule
      (no wildcards) AND ``allow_private_methods=True``; otherwise
      stays blocked.

    Singleton because the policy is workspace-scoped (one Odoo DB = one
    workspace from Tuqui's perspective). Multi-policy support would
    overload the activation model and isn't on the roadmap.
    """

    _name = "tuqui.rpc.policy"
    _description = "Tuqui RPC Policy"
    _rec_name = "policy_mode"

    policy_mode = fields.Selection(
        [("default", "Default"), ("advanced", "Advanced")],
        required=True,
        default="default",
        help=(
            "default: hardcoded behavior, private methods always blocked. "
            "advanced: rules table is consulted; deny wins."
        ),
    )
    allow_private_methods = fields.Boolean(
        default=False,
        help=(
            "Only meaningful in advanced mode. When True, private "
            "(_-prefixed) methods can be called IF an exact allow rule "
            "matches. Stays blocked otherwise."
        ),
    )

    @api.model
    def _get_singleton(self):
        """Return the singleton, creating it with defaults on first access."""
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = self.sudo().create({})
        return rec

    def action_apply_read_only_preset(self):
        """Idempotent: add three deny-all rules covering write/execute/private.

        Designed to be the one-click way to lock down a Tuqui-connected
        Odoo to read-only. Existing custom rules are not touched; the
        preset only adds what's missing.
        """
        self.ensure_one()
        rule_model = self.env["tuqui.rpc.rule"].sudo()
        created = 0
        for effect, mp, mep, op_type, name in _READ_ONLY_PRESET_RULES:
            domain = [
                ("effect", "=", effect),
                ("model_pattern", "=", mp),
                ("method_pattern", "=", mep),
                ("operation_type", "=", op_type),
            ]
            if rule_model.search_count(domain):
                continue
            rule_model.create(
                {
                    "name": name,
                    "effect": effect,
                    "model_pattern": mp,
                    "method_pattern": mep,
                    "operation_type": op_type,
                    "active": True,
                    "sequence": 10,
                }
            )
            created += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "title": _("Read-only preset applied"),
                "message": _("Added %d new rule(s). Existing rules were preserved.") % created,
                "sticky": False,
            },
        }

    @api.constrains("policy_mode", "allow_private_methods")
    def _check_allow_private_only_in_advanced(self):
        """``allow_private_methods`` has no meaning in default mode — keep them in sync.

        Enforced as a constraint instead of a button guard so the admin
        can edit either field via the radio/checkbox directly without
        a footgun where the flag is left True after switching back to
        default and silently does nothing.
        """
        for rec in self:
            if rec.policy_mode == "default" and rec.allow_private_methods:
                raise ValidationError(
                    _(
                        "'allow_private_methods' only takes effect in advanced mode. "
                        "Disable it before switching back to default."
                    )
                )
