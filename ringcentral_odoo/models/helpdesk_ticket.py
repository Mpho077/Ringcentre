from odoo import api, fields, models


class HelpdeskTicket(models.Model):
    _inherit = "helpdesk.ticket"

    rc_call_count = fields.Integer(
        string="Calls",
        compute="_compute_rc_call_count",
    )
    rc_escalated = fields.Boolean(
        string="Sentiment Escalated",
        help="This ticket was created or escalated due to negative call sentiment",
        tracking=True,
    )

    def _compute_rc_call_count(self):
        CallLog = self.env["rc.call.log"]
        for ticket in self:
            ticket.rc_call_count = CallLog.search_count(
                [("ticket_id", "=", ticket.id)]
            )

    def action_view_calls(self):
        """Open call log for this ticket."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Calls — {self.name}",
            "res_model": "rc.call.log",
            "view_mode": "list,form",
            "domain": [("ticket_id", "=", self.id)],
        }
