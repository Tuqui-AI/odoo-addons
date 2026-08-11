from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        """Publish whether this user has Tuqui chat, for the systray gating.

        It rides on ``session_info`` rather than an RPC because the systray item
        has to be decided at page load: a request would either delay the first
        paint or make the icon pop in half a second late, which reads as a glitch
        (spec ``systray-solo-para-usuarios-con-chat``). This way the page load
        costs zero extra requests.

        Always present and always a boolean — never absent for some users — so the
        JS side has one thing to check. Non-internal users get ``False``: the
        systray only exists in the backend.
        """
        info = super().session_info()
        user = self.env.user
        info["tuqui_has_chat"] = bool(user._is_internal() and user.sudo().tuqui_has_chat)
        return info
