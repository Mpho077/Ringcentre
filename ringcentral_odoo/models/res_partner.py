from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    rc_call_ids = fields.One2many(
        "rc.call.log",
        "partner_id",
        string="Call History",
    )
    rc_call_count = fields.Integer(
        string="Calls",
        compute="_compute_rc_call_count",
        store=True,
    )
    rc_last_call = fields.Datetime(
        string="Last Call",
        compute="_compute_rc_call_count",
        store=True,
    )
    rc_negative_call_count = fields.Integer(
        string="Negative Calls",
        compute="_compute_rc_call_count",
        store=True,
    )

    @api.depends("rc_call_ids", "rc_call_ids.start_time", "rc_call_ids.sentiment_label")
    def _compute_rc_call_count(self):
        for partner in self:
            calls = partner.rc_call_ids
            partner.rc_call_count = len(calls)
            partner.rc_last_call = max(calls.mapped("start_time")) if calls else False
            partner.rc_negative_call_count = len(
                calls.filtered(lambda c: c.sentiment_label == "negative")
            )

    def action_view_calls(self):
        """Open call log list for this contact."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Calls — {self.name}",
            "res_model": "rc.call.log",
            "view_mode": "list,form",
            "domain": [("partner_id", "=", self.id)],
            "context": {"default_partner_id": self.id},
        }
