from odoo import api, fields, models


class HelpdeskTicket(models.Model):
    _inherit = "helpdesk.ticket"

    # Link
    rc_call_count = fields.Integer(compute="_compute_rc_call_count")
    rc_escalated = fields.Boolean(string="Sentiment Escalated", tracking=True)

    # Call details stored on ticket
    rc_call_ref = fields.Char(string="Call Reference", readonly=True)
    rc_direction = fields.Selection(
        [("inbound", "Inbound"), ("outbound", "Outbound"), ("missed", "Missed")],
        string="Call Direction", readonly=True,
    )
    rc_caller_number = fields.Char(string="Caller Number", readonly=True)
    rc_callee_number = fields.Char(string="Callee Number", readonly=True)
    rc_call_duration = fields.Char(string="Call Duration", readonly=True)
    rc_call_time = fields.Datetime(string="Call Time", readonly=True)
    rc_sentiment = fields.Selection(
        [("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative")],
        string="Call Sentiment", readonly=True,
    )
    rc_sentiment_score = fields.Float(string="Sentiment Score", readonly=True)
    rc_sentiment_source = fields.Char(string="Sentiment Source", readonly=True)
    rc_transcript = fields.Text(string="Call Transcript", readonly=True)
    rc_recording_attachment_id = fields.Many2one("ir.attachment", string="Call Recording", readonly=True)
    rc_agent_notes = fields.Text(string="Agent Call Notes", readonly=True)

    def _compute_rc_call_count(self):
        CallLog = self.env["rc.call.log"]
        for ticket in self:
            ticket.rc_call_count = CallLog.search_count([("ticket_id", "=", ticket.id)])

    def action_view_calls(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Calls — {self.name}",
            "res_model": "rc.call.log",
            "view_mode": "list,form",
            "domain": [("ticket_id", "=", self.id)],
        }
