from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    rc_call_ids = fields.One2many("rc.call.log", "partner_id", string="Call History")
    rc_call_count = fields.Integer(compute="_compute_rc_call_count", store=True)
    rc_last_call = fields.Datetime(compute="_compute_rc_call_count", store=True)

    @api.depends("rc_call_ids", "rc_call_ids.start_time")
    def _compute_rc_call_count(self):
        for p in self:
            calls = p.rc_call_ids
            p.rc_call_count = len(calls)
            p.rc_last_call = max(calls.mapped("start_time")) if calls else False

    def action_view_calls(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Calls — {self.name}",
            "res_model": "rc.call.log",
            "view_mode": "list,form",
            "domain": [("partner_id", "=", self.id)],
        }
