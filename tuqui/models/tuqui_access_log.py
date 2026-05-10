from odoo import api, fields, models


_DEFAULT_MAX_ROWS = 10000


class TuquiAccessLog(models.Model):
    """Access log for Tuqui RPC calls. Capped by row count to avoid unbounded growth."""

    _name = "tuqui.access.log"
    _description = "Tuqui Access Log"
    _order = "id desc"
    _rec_name = "operation"

    timestamp = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    acting_user_id = fields.Many2one("res.users", ondelete="set null", index=True)
    operation = fields.Char(required=True, index=True)
    model_name = fields.Char(string="Model", index=True)
    record_count = fields.Integer(default=0)
    success = fields.Boolean(default=True, index=True)
    error_code = fields.Char()

    @api.model
    def log(self, operation, acting_user_id=None, model_name=None, record_count=0, success=True, error_code=None):
        """Append a record and prune oldest rows beyond the configured cap."""
        rec = self.sudo().create(
            {
                "operation": operation,
                "acting_user_id": acting_user_id or False,
                "model_name": model_name or False,
                "record_count": record_count,
                "success": success,
                "error_code": error_code or False,
            }
        )
        self._prune()
        return rec

    @api.model
    def _max_rows(self):
        raw = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("tuqui.access_log.max_rows", _DEFAULT_MAX_ROWS)
        )
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
