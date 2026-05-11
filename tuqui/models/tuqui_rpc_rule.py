from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


# Characters that fnmatch treats as wildcards. A pattern is "exact" when
# it contains none of them. We reject all four — sticking to '*' and '?'
# only would leak '[abc]' as a back door.
_GLOB_CHARS = frozenset("*?[]")


def _is_exact_pattern(pattern: str) -> bool:
    if not pattern:
        return False
    return not any(ch in _GLOB_CHARS for ch in pattern)


class TuquiRpcRule(models.Model):
    """Allow / deny rule evaluated by ``/tuqui/rpc`` in advanced mode.

    Each rule matches an inbound call by ``(model_pattern, method_pattern,
    operation_type)`` using fnmatch glob syntax. Evaluation:

    - Any matching ``deny`` blocks the call (deny wins).
    - For ``private_execute`` calls, an explicit matching ``allow`` is
      required; absence of one keeps the call blocked.
    - For other operation types, calls are allow-by-default; only denies
      need to be authored.

    Glob syntax is the full ``fnmatch`` grammar (``*``, ``?``, ``[abc]``,
    ``[!abc]``). UI documents the common cases (``*``, ``prefix.*``,
    ``*.suffix``); the rest works as a power-user bonus.

    Allow rules targeting ``private_execute`` are constrained to **exact**
    patterns on both ``model_pattern`` and ``method_pattern``. Private
    methods are an internal Odoo API surface — blanket allows would
    silently widen attack surface as Odoo evolves. Forcing exact rules
    means each authorization is an explicit, named carve-out.
    """

    _name = "tuqui.rpc.rule"
    _description = "Tuqui RPC Policy Rule"
    _order = "sequence, id"
    _rec_name = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    effect = fields.Selection(
        [("allow", "Allow"), ("deny", "Deny")],
        required=True,
    )
    model_pattern = fields.Char(
        required=True,
        help="fnmatch glob. Examples: 'res.partner' (exact), 'account.*' (prefix), '*' (any).",
    )
    method_pattern = fields.Char(
        required=True,
        help="fnmatch glob. Examples: 'search_read' (exact), 'action_*' (prefix), '*' (any).",
    )
    operation_type = fields.Selection(
        [
            ("read", "Read"),
            ("write", "Write"),
            ("execute", "Execute"),
            ("private_execute", "Private execute"),
            ("any", "Any"),
        ],
        required=True,
        default="any",
    )
    note = fields.Text(help="Optional — explain *why* this rule exists. Useful for auditors.")

    _rule_unique = models.Constraint(
        "unique(effect, model_pattern, method_pattern, operation_type)",
        "A rule with this (effect, model_pattern, method_pattern, operation_type) combination already exists.",
    )

    @api.constrains("effect", "operation_type", "model_pattern", "method_pattern")
    def _check_private_allow_is_exact(self):
        """Allow rules for private methods must use exact patterns."""
        for rec in self:
            if rec.effect == "allow" and rec.operation_type == "private_execute":
                if not _is_exact_pattern(rec.model_pattern):
                    raise ValidationError(
                        _(
                            "Allow rules for private_execute require an exact "
                            "model_pattern (no wildcards). Got: %s"
                        )
                        % rec.model_pattern
                    )
                if not _is_exact_pattern(rec.method_pattern):
                    raise ValidationError(
                        _(
                            "Allow rules for private_execute require an exact "
                            "method_pattern (no wildcards). Got: %s"
                        )
                        % rec.method_pattern
                    )
