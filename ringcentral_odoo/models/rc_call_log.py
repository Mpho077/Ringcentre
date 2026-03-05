import base64
import json
import logging
import re
import requests
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class RCCallLog(models.Model):
    _name = "rc.call.log"
    _description = "RingCentral Call Log"
    _order = "start_time desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(string="Reference", readonly=True, default=lambda self: _("New"), copy=False)
    rc_call_id = fields.Char(string="RC Session ID", index=True, readonly=True)

    partner_id = fields.Many2one("res.partner", string="Contact", index=True, tracking=True)
    direction = fields.Selection(
        [("inbound", "Inbound"), ("outbound", "Outbound"), ("missed", "Missed")],
        required=True, tracking=True,
    )
    caller_number = fields.Char()
    callee_number = fields.Char()
    start_time = fields.Datetime(index=True)
    end_time = fields.Datetime()
    duration = fields.Integer(string="Duration (sec)")
    duration_display = fields.Char(compute="_compute_duration_display", store=True)
    user_id = fields.Many2one("res.users", string="Handled By")
    rc_extension_id = fields.Char(string="RC Extension")

    state = fields.Selection(
        [("new", "New"), ("processing", "Processing"), ("done", "Done"), ("failed", "Failed")],
        default="new", required=True, tracking=True,
    )
    error_message = fields.Text(readonly=True)

    recording_url = fields.Char(string="RC Recording URL")
    recording_attachment_id = fields.Many2one("ir.attachment", string="Recording", readonly=True)
    has_recording = fields.Boolean(compute="_compute_has_recording", store=True)

    transcript = fields.Text(readonly=True)
    transcript_attachment_id = fields.Many2one("ir.attachment", string="Transcript File", readonly=True)

    sentiment_score = fields.Float(string="AI Score", help="-1.0 to 1.0")
    sentiment_label = fields.Selection(
        [("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative")],
        string="AI Sentiment", compute="_compute_sentiment_label", store=True,
    )
    sentiment_reason = fields.Char(string="AI Reasoning", readonly=True)

    agent_sentiment = fields.Selection(
        [("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative")],
        string="Agent Override", tracking=True,
    )
    final_sentiment = fields.Selection(
        [("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative")],
        compute="_compute_final_sentiment", store=True,
    )

    ticket_id = fields.Integer(string="Ticket ID")
    ticket_ref = fields.Char(string="Ticket", readonly=True)
    notes = fields.Text()

    # ── Computed ─────────────────────────────────────────────────
    @api.depends("duration")
    def _compute_duration_display(self):
        for r in self:
            m, s = divmod(r.duration or 0, 60)
            r.duration_display = f"{m}m {s}s"

    @api.depends("recording_attachment_id")
    def _compute_has_recording(self):
        for r in self:
            r.has_recording = bool(r.recording_attachment_id)

    @api.depends("sentiment_score")
    def _compute_sentiment_label(self):
        for r in self:
            if r.sentiment_score > 0.2:
                r.sentiment_label = "positive"
            elif r.sentiment_score < -0.2:
                r.sentiment_label = "negative"
            else:
                r.sentiment_label = "neutral"

    @api.depends("sentiment_label", "agent_sentiment")
    def _compute_final_sentiment(self):
        for r in self:
            r.final_sentiment = r.agent_sentiment or r.sentiment_label

    # ── CRUD ─────────────────────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("rc.call.log") or _("New")
        return super().create(vals_list)

    def write(self, vals):
        result = super().write(vals)
        if vals.get("agent_sentiment") == "negative":
            for rec in self:
                if not rec.ticket_id and rec._has_helpdesk():
                    config = self.env["rc.config"].search([("company_id", "=", self.env.company.id)], limit=1)
                    if config and config.auto_create_ticket:
                        rec._create_ticket(config, source="Agent Flagged")
        return result

    # ── Phone Matching ───────────────────────────────────────────
    @api.model
    def _match_partner(self, phone_number):
        if not phone_number:
            return self.env["res.partner"]
        digits = re.sub(r"[^\d+]", "", phone_number)
        if not digits:
            return self.env["res.partner"]
        partner = self.env["res.partner"].search(
            ["|", ("phone", "ilike", digits[-10:]), ("mobile", "ilike", digits[-10:])], limit=1,
        )
        return partner

    # ── Process Call Event ───────────────────────────────────────
    def _process_call_event(self, event_data, config):
        rc_call_id = str(event_data.get("sessionId") or event_data.get("id", ""))
        if not rc_call_id:
            return
        existing = self.search([("rc_call_id", "=", rc_call_id)], limit=1)
        if existing and existing.state == "done":
            return existing

        result = event_data.get("result", "")
        if result in ("Missed", "No Answer", "Busy"):
            direction = "missed"
        else:
            direction = "inbound" if event_data.get("direction") == "Inbound" else "outbound"

        from_num = event_data.get("from", {}).get("phoneNumber", "")
        to_num = event_data.get("to", {}).get("phoneNumber", "")
        match_num = from_num if direction == "inbound" else to_num
        partner = self._match_partner(match_num)

        dur = event_data.get("duration", 0)
        start_str = event_data.get("startTime", "")
        start_time = end_time = False
        if start_str:
            try:
                start_time = fields.Datetime.to_datetime(start_str.replace("T", " ").replace("Z", "")[:19])
                if dur:
                    end_time = start_time + timedelta(seconds=dur)
            except Exception:
                pass

        vals = {
            "rc_call_id": rc_call_id,
            "direction": direction,
            "caller_number": from_num,
            "callee_number": to_num,
            "partner_id": partner.id if partner else False,
            "start_time": start_time,
            "end_time": end_time,
            "duration": dur,
            "recording_url": event_data.get("recording", {}).get("contentUri", ""),
            "state": "new",
        }

        if existing:
            existing.write(vals)
            call = existing
        else:
            call = self.create(vals)

        call._process_post_call(config.id)
        return call

    def _process_post_call(self, config_id):
        self.ensure_one()
        config = self.env["rc.config"].browse(config_id)
        if not config.exists():
            return
        self.write({"state": "processing"})
        try:
            if config.sync_recordings and self.recording_url:
                self._download_recording(config)
            self.write({"state": "done"})
        except Exception as e:
            _logger.error("Post-call failed for %s: %s", self.name, e)
            self.write({"state": "failed", "error_message": str(e)})

    # ── Recording ────────────────────────────────────────────────
    def _download_recording(self, config):
        self.ensure_one()
        if not self.recording_url:
            return
        token = config._get_access_token()
        url = self.recording_url + ("&" if "?" in self.recording_url else "?") + f"access_token={token}"
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "audio/mpeg")
        ext = "mp3" if "mpeg" in ct else "wav"
        att = self.env["ir.attachment"].create({
            "name": f"call_{self.name}.{ext}",
            "type": "binary",
            "datas": base64.b64encode(resp.content),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": ct,
        })
        self.write({"recording_attachment_id": att.id})
        if self.partner_id:
            att.copy({"res_model": "res.partner", "res_id": self.partner_id.id})

    # ── Helpdesk ─────────────────────────────────────────────────
    def _has_helpdesk(self):
        return "helpdesk.ticket" in self.env

    def action_create_ticket(self):
        self.ensure_one()
        if not self._has_helpdesk():
            raise UserError(_("Helpdesk module not installed (Enterprise only)."))
        if self.ticket_id:
            raise UserError(_("Ticket already exists: %s") % self.ticket_ref)
        config = self.env["rc.config"].search([("company_id", "=", self.env.company.id)], limit=1)
        self._create_ticket(config, source="Manual")
        return {
            "type": "ir.actions.act_window",
            "res_model": "helpdesk.ticket",
            "res_id": self.ticket_id,
            "view_mode": "form",
            "target": "current",
        }

    def _create_ticket(self, config, source="Auto"):
        self.ensure_one()
        partner_name = self.partner_id.name if self.partner_id else self.caller_number
        sentiment = self.final_sentiment or "neutral"
        desc = (
            f"<b>Created from call ({source})</b><br/><br/>"
            f"<b>Call:</b> {self.name}<br/>"
            f"<b>Direction:</b> {self.direction}<br/>"
            f"<b>Caller:</b> {self.caller_number}<br/>"
            f"<b>Callee:</b> {self.callee_number}<br/>"
            f"<b>Duration:</b> {self.duration_display}<br/>"
            f"<b>Contact:</b> {partner_name}<br/>"
            f"<b>Sentiment:</b> {sentiment}<br/>"
        )
        if self.notes:
            desc += f"<br/><b>Notes:</b><br/>{self.notes}<br/>"
        if self.transcript:
            desc += f"<br/><b>Transcript:</b><br/><pre>{self.transcript[:2000]}</pre>"

        vals = {
            "name": f"Call Follow-up — {partner_name} ({self.name})",
            "partner_id": self.partner_id.id if self.partner_id else False,
            "priority": config.escalation_priority if config else "1",
            "description": desc,
        }

        ticket = self.env["helpdesk.ticket"].create(vals)
        self.write({"ticket_id": ticket.id, "ticket_ref": ticket.name})

        if self.recording_attachment_id:
            self.recording_attachment_id.copy({"res_model": "helpdesk.ticket", "res_id": ticket.id})
        if self.transcript_attachment_id:
            self.transcript_attachment_id.copy({"res_model": "helpdesk.ticket", "res_id": ticket.id})

        self.message_post(body=f"<b>Ticket created ({source}):</b> {ticket.name}", subtype_xmlid="mail.mt_note")

    # ── Polling ──────────────────────────────────────────────────
    @api.model
    def _cron_poll_calls(self):
        for config in self.env["rc.config"].search([("polling_enabled", "=", True)]):
            try:
                self._poll_ringcentral(config)
            except Exception as e:
                _logger.error("Polling failed: %s", e)

    def _poll_ringcentral(self, config):
        date_from = config.last_poll_time or (fields.Datetime.now() - timedelta(hours=24))
        result = config._rc_api_request("GET", "/account/~/extension/~/call-log", params={
            "dateFrom": date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "view": "Detailed",
            "perPage": 100,
            "withRecording": "True",
        })
        for record in result.get("records", []):
            self._process_call_event(record, config)
        config.sudo().write({"last_poll_time": fields.Datetime.now()})

    @api.model
    def _cron_retry_failed(self):
        for call in self.search([("state", "=", "failed")], limit=50):
            config = self.env["rc.config"].search([("company_id", "=", call.create_uid.company_id.id)], limit=1)
            if config:
                call._process_post_call(config.id)
