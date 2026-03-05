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

    # Recording
    recording_url = fields.Char(string="RC Recording URL")
    recording_attachment_id = fields.Many2one("ir.attachment", string="Recording", readonly=True)
    has_recording = fields.Boolean(compute="_compute_has_recording", store=True)

    # Transcript
    transcript = fields.Text(readonly=True)
    transcript_attachment_id = fields.Many2one("ir.attachment", string="Transcript File", readonly=True)

    # Sentiment - AI
    sentiment_score = fields.Float(string="AI Score")
    sentiment_label = fields.Selection(
        [("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative")],
        string="AI Sentiment", compute="_compute_sentiment_label", store=True,
    )
    sentiment_reason = fields.Char(string="AI Reasoning", readonly=True)
    sentiment_provider_id = fields.Many2one("rc.sentiment.provider", string="Analyzed By", readonly=True)

    # Sentiment - Agent
    agent_sentiment = fields.Selection(
        [("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative")],
        string="Agent Override", tracking=True,
    )
    final_sentiment = fields.Selection(
        [("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative")],
        compute="_compute_final_sentiment", store=True,
    )

    # Ticket
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
        # Exact
        p = self.env["res.partner"].search(["|", ("phone", "=", phone_number), ("mobile", "=", phone_number)], limit=1)
        if p:
            return p
        # Last 10 digits
        p = self.env["res.partner"].search(["|", ("phone", "ilike", digits[-10:]), ("mobile", "ilike", digits[-10:])], limit=1)
        if p:
            return p
        # Strip country code
        if digits.startswith("+") and len(digits) > 4:
            local = "0" + digits[3:]
            p = self.env["res.partner"].search(["|", ("phone", "ilike", local), ("mobile", "ilike", local)], limit=1)
            if p:
                return p
        return self.env["res.partner"]

    # ── Process Call ─────────────────────────────────────────────
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
        partner = self._match_partner(from_num if direction == "inbound" else to_num)

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
            # Step 1: Recording
            if config.sync_recordings and self.recording_url:
                self._download_recording(config)

            # Step 2: Transcript (via AI provider if available)
            if config.sync_transcripts and self.recording_attachment_id:
                self._generate_transcript()

            # Step 3: Sentiment
            if self.transcript:
                self._analyze_sentiment()

            # Step 4: Auto-ticket on negative
            if config.auto_create_ticket and self.final_sentiment == "negative" and not self.ticket_id:
                if self._has_helpdesk():
                    if config.auto_escalate_ticket and self.partner_id:
                        existing = self.env["helpdesk.ticket"].search(
                            [("partner_id", "=", self.partner_id.id), ("stage_id.fold", "=", False)],
                            order="create_date desc", limit=1,
                        )
                        if existing:
                            self._escalate_ticket(existing, config)
                        else:
                            self._create_ticket(config, source="Auto (AI Negative)")
                    else:
                        self._create_ticket(config, source="Auto (AI Negative)")

            self.write({"state": "done"})
        except Exception as e:
            _logger.error("Post-call failed for %s: %s", self.name, e)
            self.write({"state": "failed", "error_message": str(e)})

    # ── Recording ────────────────────────────────────────────────
    def _download_recording(self, config):
        self.ensure_one()
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
            "res_model": self._name, "res_id": self.id,
            "mimetype": ct,
        })
        self.write({"recording_attachment_id": att.id})
        if self.partner_id:
            att.copy({"res_model": "res.partner", "res_id": self.partner_id.id})

    # ── Transcript ───────────────────────────────────────────────
    def _generate_transcript(self):
        """Generate transcript using the default AI provider."""
        self.ensure_one()
        provider = self.env["rc.sentiment.provider"].get_default_provider()
        if not provider or not provider.api_key:
            return
        if not self.recording_attachment_id:
            return

        # Only Anthropic supports audio input currently
        if provider.provider_type != "anthropic":
            return

        try:
            audio_b64 = self.recording_attachment_id.datas.decode("utf-8") if isinstance(
                self.recording_attachment_id.datas, bytes) else self.recording_attachment_id.datas

            resp = requests.post(
                provider.api_url or "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": provider.api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={
                    "model": provider.api_model or "claude-sonnet-4-20250514",
                    "max_tokens": 4096,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "document", "source": {"type": "base64", "media_type": self.recording_attachment_id.mimetype or "audio/mpeg", "data": audio_b64}},
                            {"type": "text", "text": "Transcribe this call recording as dialogue with speaker labels."},
                        ],
                    }],
                },
                timeout=120,
            )
            resp.raise_for_status()
            text = "".join(b["text"] for b in resp.json().get("content", []) if b.get("type") == "text")
            if text:
                self._store_transcript(text)
        except Exception as e:
            _logger.warning("Transcript generation failed for %s: %s", self.name, e)

    def _store_transcript(self, text):
        self.ensure_one()
        self.write({"transcript": text})
        att = self.env["ir.attachment"].create({
            "name": f"transcript_{self.name}.txt",
            "type": "binary",
            "datas": base64.b64encode(text.encode("utf-8")),
            "res_model": self._name, "res_id": self.id,
            "mimetype": "text/plain",
        })
        self.write({"transcript_attachment_id": att.id})
        if self.partner_id:
            att.copy({"res_model": "res.partner", "res_id": self.partner_id.id})

    # ── Sentiment ────────────────────────────────────────────────
    def _analyze_sentiment(self):
        self.ensure_one()
        if not self.transcript:
            return
        provider = self.env["rc.sentiment.provider"].get_default_provider()
        if not provider:
            return
        try:
            result = provider.analyze_sentiment(self.transcript)
            self.write({
                "sentiment_score": result.get("score", 0.0),
                "sentiment_reason": result.get("reason", ""),
                "sentiment_provider_id": provider.id,
            })
            if result.get("reason"):
                self.message_post(
                    body=f"<b>Sentiment ({provider.name}):</b> {result['score']:.2f} — {result['reason']}",
                    subtype_xmlid="mail.mt_note",
                )
        except Exception as e:
            _logger.warning("Sentiment failed for %s: %s", self.name, e)

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
        sentiment_source = "Agent" if self.agent_sentiment else (
            self.sentiment_provider_id.name if self.sentiment_provider_id else "None"
        )

        desc = (
            f"<b>Created from call ({source})</b><br/><br/>"
            f"<b>Call:</b> {self.name}<br/>"
            f"<b>Direction:</b> {self.direction}<br/>"
            f"<b>Caller:</b> {self.caller_number}<br/>"
            f"<b>Callee:</b> {self.callee_number}<br/>"
            f"<b>Duration:</b> {self.duration_display}<br/>"
            f"<b>Contact:</b> {partner_name}<br/>"
            f"<b>Sentiment:</b> {sentiment} ({sentiment_source})<br/>"
        )
        if self.sentiment_score:
            desc += f"<b>AI Score:</b> {self.sentiment_score:.2f}<br/>"
        if self.sentiment_reason:
            desc += f"<b>AI Reason:</b> {self.sentiment_reason}<br/>"
        if self.notes:
            desc += f"<br/><b>Agent Notes:</b><br/>{self.notes}<br/>"
        if self.transcript:
            desc += f"<br/><b>Transcript:</b><br/><pre>{self.transcript[:2000]}</pre>"

        vals = {
            "name": f"Call Follow-up — {partner_name} ({self.name})",
            "partner_id": self.partner_id.id if self.partner_id else False,
            "priority": config.escalation_priority if config else "1",
            "description": desc,
            "rc_escalated": sentiment == "negative",
        }
        if config and config.helpdesk_team_id:
            vals["team_id"] = config.helpdesk_team_id.id

        # Populate call detail fields on ticket
        vals.update(self._get_ticket_call_vals(sentiment_source))

        ticket = self.env["helpdesk.ticket"].create(vals)
        self.write({"ticket_id": ticket.id, "ticket_ref": ticket.name})

        # Add tag (find by name — tags may already exist in DB)
        tag = False
        if sentiment == "negative":
            tag = self.env["helpdesk.tag"].search([("name", "=", "Negative Call Sentiment")], limit=1)
            if not tag:
                tag = self.env["helpdesk.tag"].create({"name": "Negative Call Sentiment"})
        elif self.direction == "missed":
            tag = self.env["helpdesk.tag"].search([("name", "=", "Missed Call")], limit=1)
            if not tag:
                tag = self.env["helpdesk.tag"].create({"name": "Missed Call"})
        if tag:
            ticket.write({"tag_ids": [(4, tag.id)]})

        self._attach_call_files_to_ticket(ticket)
        self.message_post(body=f"<b>Ticket created ({source}):</b> {ticket.name}", subtype_xmlid="mail.mt_note")

    def _escalate_ticket(self, ticket, config):
        self.ensure_one()
        sentiment_source = "Agent" if self.agent_sentiment else (
            self.sentiment_provider_id.name if self.sentiment_provider_id else "None"
        )
        new_priority = str(min(int(ticket.priority or "0") + 1, 3))

        write_vals = {"priority": new_priority, "rc_escalated": True}
        write_vals.update(self._get_ticket_call_vals(sentiment_source))
        ticket.write(write_vals)

        self._attach_call_files_to_ticket(ticket)
        ticket.message_post(
            body=(
                f"<b>Priority escalated</b> due to negative call.<br/>"
                f"Call: {self.name} | Score: {self.sentiment_score:.2f}<br/>"
                f"<pre>{(self.transcript or '')[:500]}</pre>"
            ),
            subtype_xmlid="mail.mt_note",
        )
        self.write({"ticket_id": ticket.id, "ticket_ref": ticket.name})

    def _get_ticket_call_vals(self, sentiment_source=""):
        """Get call detail values to write on a helpdesk ticket."""
        self.ensure_one()
        return {
            "rc_call_ref": self.name,
            "rc_direction": self.direction,
            "rc_caller_number": self.caller_number,
            "rc_callee_number": self.callee_number,
            "rc_call_duration": self.duration_display,
            "rc_call_time": self.start_time,
            "rc_sentiment": self.final_sentiment or self.agent_sentiment or self.sentiment_label,
            "rc_sentiment_score": self.sentiment_score,
            "rc_sentiment_source": sentiment_source,
            "rc_transcript": self.transcript,
            "rc_agent_notes": self.notes,
        }

    def _attach_call_files_to_ticket(self, ticket):
        """Copy recording and transcript attachments to the ticket."""
        self.ensure_one()
        recording_copy = None
        if self.recording_attachment_id:
            recording_copy = self.recording_attachment_id.copy({"res_model": "helpdesk.ticket", "res_id": ticket.id})
        if self.transcript_attachment_id:
            self.transcript_attachment_id.copy({"res_model": "helpdesk.ticket", "res_id": ticket.id})
        if recording_copy:
            ticket.write({"rc_recording_attachment_id": recording_copy.id})

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
            "view": "Detailed", "perPage": 100, "withRecording": "True",
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
