from odoo import api, fields, models

_DEFAULT_MAX_ROWS = 10000

# Sentinel for the (rare) case where the controller hits an error path
# before classification — we still want a stable, queryable value.
_UNKNOWN_OPERATION_TYPE = "any"


class TuquiAccessLog(models.Model):
    """Audit log for every call routed through ``/tuqui/rpc``.

    Each row records one logical RPC: who acted (``acting_user_id``),
    what was called (``model_name`` + ``method`` + ``operation_type``),
    how the policy ruled (``policy_allowed`` + ``policy_denied_reason``)
    and what happened next (``success`` + ``error_code`` + ``duration_ms``
    + ``result_count``).

    The two-axis design (policy vs runtime) makes audit queries
    unambiguous — ``policy_allowed=False`` always means the gateway
    blocked the call before reaching the ORM, while ``success=False``
    with ``policy_allowed=True`` means Odoo executed and raised. Mixing
    those two states into one field made the SQL ambiguous in practice.

    Capped by row count (``tuqui.access_log.max_rows``, default 10k) to
    keep the table bounded without coupling to a time-based retention.
    """

    _name = "tuqui.access.log"
    _description = "Tuqui Access Log"
    _order = "id desc"
    _rec_name = "method"

    acting_user_id = fields.Many2one(
        "res.users",
        ondelete="set null",
        index=True,
        help="The Odoo user resolved from the X-Tuqui-Acting-User header.",
    )
    model_name = fields.Char(string="Model", index=True)
    method = fields.Char(required=True, index=True)
    operation_type = fields.Selection(
        [
            ("read", "Read"),
            ("write", "Write"),
            ("execute", "Execute"),
            ("private_execute", "Private execute"),
            ("any", "Any"),
        ],
        required=True,
        default=_UNKNOWN_OPERATION_TYPE,
        index=True,
    )

    # ─── Policy axis ──────────────────────────────────────────────────
    policy_allowed = fields.Boolean(
        default=True,
        index=True,
        help="Whether the policy gate let the call proceed to the ORM.",
    )
    policy_denied_reason = fields.Char(
        help=(
            "Why the policy gate blocked the call. One of: method_blocked, "
            "private_method_blocked, deny_rule_matched, no_allow_rule. "
            "Empty when policy_allowed is True."
        ),
    )

    # ─── Runtime axis ─────────────────────────────────────────────────
    success = fields.Boolean(
        default=True,
        index=True,
        help="Whether the ORM call completed without raising.",
    )
    error_code = fields.Char(
        help=(
            "Error classification when success is False. One of: "
            "access_denied, validation_error, internal_error, timeout. "
            "Empty when success is True."
        ),
    )

    # ─── Telemetry ────────────────────────────────────────────────────
    duration_ms = fields.Integer(
        default=0,
        help="Wall-clock duration of the policy + ORM call, in milliseconds.",
    )
    result_count = fields.Integer(
        default=0,
        help="Size of the returned data (rows for lists, keys for dicts).",
    )

    @api.model
    def log(
        self,
        *,
        method,
        model_name=None,
        operation_type=_UNKNOWN_OPERATION_TYPE,
        acting_user_id=None,
        policy_allowed=True,
        policy_denied_reason=None,
        success=True,
        error_code=None,
        duration_ms=0,
        result_count=0,
    ):
        """Append one audit row and prune older rows beyond the configured cap.

        Keyword-only on purpose: the field list grew enough that positional
        calls would silently shift on schema changes.
        """
        rec = self.sudo().create(
            {
                "method": method,
                "model_name": model_name or False,
                "operation_type": operation_type,
                "acting_user_id": acting_user_id or False,
                "policy_allowed": policy_allowed,
                "policy_denied_reason": policy_denied_reason or False,
                "success": success,
                "error_code": error_code or False,
                "duration_ms": int(duration_ms or 0),
                "result_count": int(result_count or 0),
            }
        )
        self._prune()
        return rec

    @api.model
    def _max_rows(self):
        raw = self.env["ir.config_parameter"].sudo().get_param("tuqui.access_log.max_rows", _DEFAULT_MAX_ROWS)
        try:
            return max(int(raw), 100)
        except (TypeError, ValueError):
            return _DEFAULT_MAX_ROWS

    @api.model
    def _prune(self):
        max_rows = self._max_rows()
        total = self.sudo().search_count([])
        excess = total - max_rows
        if excess <= 0:
            return
        oldest = self.sudo().search([], order="id asc", limit=excess)
        oldest.sudo().unlink()
