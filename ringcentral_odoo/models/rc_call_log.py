import base64
import json
import logging
import re
import requests
from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class RCCallLog(models.Model):
    _name = "rc.call.log"
    _description = "RingCentral Call Log"
    _order = "start_time desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    # ── Identification ───────────────────────────────────────────
    name = fields.Char(
        string="Reference",
        readonly=True,
        default=lambda self: _("New"),
        copy=False,
    )
    rc_call_id = fields.Char(
        string="RC Session ID",
        index=True,
        readonly=True,
        help="Unique RingCentral call session identifier",
    )

    # ── Contact Matching ─────────────────────────────────────────
    partner_id = fields.Many2one(
        "res.partner",
        string="Contact",
        index=True,
        tracking=True,
    )

    # ── Call Details ─────────────────────────────────────────────
    direction = fields.Selection(
        [
            ("inbound", "Inbound"),
            ("outbound", "Outbound"),
            ("missed", "Missed"),
        ],
        string="Direction",
        required=True,
        tracking=True,
    )
    caller_number = fields.Char(string="Caller Number")
    callee_number = fields.Char(string="Callee Number")
    start_time = fields.Datetime(string="Start Time", index=True)
    end_time = fields.Datetime(string="End Time")
    duration = fields.Integer(string="Duration (seconds)")
    duration_display = fields.Char(
        string="Duration",
        compute="_compute_duration_display",
        store=True,
    )
    rc_extension_id = fields.Char(string="RC Extension")
    user_id = fields.Many2one(
        "res.users",
        string="Handled By",
        help="Odoo user mapped to the RingCentral extension",
    )

    # ── State ────────────────────────────────────────────────────
    state = fields.Selection(
        [
            ("new", "New"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="new",
        required=True,
        tracking=True,
    )
    error_message = fields.Text(string="Error Details", readonly=True)

    # ── Recording ────────────────────────────────────────────────
    recording_url = fields.Char(string="RC Recording URL")
    recording_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Recording File",
        readonly=True,
    )
    has_recording = fields.Boolean(
        compute="_compute_has_recording",
        store=True,
    )

    # ── Transcript ───────────────────────────────────────────────
    transcript = fields.Text(string="Transcript")
    transcript_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Transcript File",
        readonly=True,
    )

    # ── Sentiment ────────────────────────────────────────────────
    sentiment_score = fields.Float(
        string="AI Sentiment Score",
        help="-1.0 (very negative) to 1.0 (very positive)",
    )
    sentiment_label = fields.Selection(
        [
            ("positive", "Positive"),
            ("neutral", "Neutral"),
            ("negative", "Negative"),
        ],
        string="AI Sentiment",
        compute="_compute_sentiment_label",
        store=True,
        tracking=True,
    )
    sentiment_provider_id = fields.Many2one(
        "rc.sentiment.provider",
        string="Analyzed By",
        readonly=True,
    )
    sentiment_reason = fields.Char(string="AI Reasoning", readonly=True)

    # ── Agent Override ───────────────────────────────────────────
    agent_sentiment = fields.Selection(
        [
            ("positive", "Positive"),
            ("neutral", "Neutral"),
            ("negative", "Negative"),
        ],
        string="Agent Override",
        help="Agent can override AI sentiment. This value takes priority.",
        tracking=True,
    )
    final_sentiment = fields.Selection(
        [
            ("positive", "Positive"),
            ("neutral", "Neutral"),
            ("negative", "Negative"),
        ],
        string="Final Sentiment",
        compute="_compute_final_sentiment",
        store=True,
        help="Agent override if set, otherwise AI sentiment",
    )

    # ── Ticket Link (works with/without Helpdesk module) ────────
    ticket_id = fields.Integer(
        string="Helpdesk Ticket ID",
        help="ID of the linked helpdesk ticket (Enterprise only)",
    )
    ticket_ref = fields.Char(
        string="Ticket Reference",
        readonly=True,
    )

    # ── Notes ────────────────────────────────────────────────────
    notes = fields.Text(string="Notes")

    # ── Computed Fields ──────────────────────────────────────────
    @api.depends("duration")
    def _compute_duration_display(self):
        for rec in self:
            if rec.duration:
                mins, secs = divmod(rec.duration, 60)
                rec.duration_display = f"{mins}m {secs}s"
            else:
                rec.duration_display = "0m 0s"

    @api.depends("recording_attachment_id")
    def _compute_has_recording(self):
        for rec in self:
            rec.has_recording = bool(rec.recording_attachment_id)

    @api.depends("sentiment_score")
    def _compute_sentiment_label(self):
        for rec in self:
            if rec.sentiment_score > 0.2:
                rec.sentiment_label = "positive"
            elif rec.sentiment_score < -0.2:
                rec.sentiment_label = "negative"
            else:
                rec.sentiment_label = "neutral"

    @api.depends("sentiment_label", "agent_sentiment")
    def _compute_final_sentiment(self):
        for rec in self:
            rec.final_sentiment = rec.agent_sentiment or rec.sentiment_label

    # ── CRUD ─────────────────────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "rc.call.log"
                ) or _("New")
        return super().create(vals_list)

    def write(self, vals):
        """Override write to trigger ticket creation when agent marks sentiment as negative."""
        result = super().write(vals)

        # Check if agent_sentiment was just set to negative
        if vals.get("agent_sentiment") == "negative":
            for rec in self:
                # Only create ticket if no ticket exists yet
                if not rec.ticket_id and rec._has_helpdesk():
                    config = self.env["rc.config"].search(
                        [("company_id", "=", self.env.company.id)], limit=1
                    )
                    if config and config.auto_create_ticket:
                        # Check for existing open ticket first
                        if rec.partner_id and config.auto_escalate_ticket:
                            existing = self.env["helpdesk.ticket"].search(
                                [
                                    ("partner_id", "=", rec.partner_id.id),
                                    ("stage_id.fold", "=", False),
                                ],
                                order="create_date desc",
                                limit=1,
                            )
                            if existing:
                                rec._escalate_ticket(existing, config)
                                continue
                        rec._create_ticket_from_agent(config)

        return result

    # ── Manual Ticket Creation Button ────────────────────────────
    def action_create_ticket(self):
        """Button action: manually create a helpdesk ticket from this call."""
        self.ensure_one()

        if not self._has_helpdesk():
            raise UserError(
                _("Helpdesk module is not installed. "
                  "Install Helpdesk (Enterprise) to create tickets.")
            )

        if self.ticket_id:
            raise UserError(
                _("A ticket already exists for this call: %s") % self.ticket_ref
            )

        config = self.env["rc.config"].search(
            [("company_id", "=", self.env.company.id)], limit=1
        )

        partner_name = self.partner_id.name if self.partner_id else self.caller_number
        sentiment = self.final_sentiment or self.agent_sentiment or self.sentiment_label or "neutral"

        ticket_vals = {
            "name": f"Call Follow-up — {partner_name} ({self.name})",
            "partner_id": self.partner_id.id if self.partner_id else False,
            "priority": config.escalation_priority if config else "1",
            "description": self._build_ticket_description(),
        }
        if config and config.helpdesk_team_id:
            ticket_vals["team_id"] = config.helpdesk_team_id

        if "rc_escalated" in self.env["helpdesk.ticket"]._fields:
            ticket_vals["rc_escalated"] = sentiment == "negative"

        # Add call detail fields
        ticket_vals.update(self._get_ticket_call_vals())

        ticket = self.env["helpdesk.ticket"].create(ticket_vals)
        self.write({"ticket_id": ticket.id, "ticket_ref": ticket.name})

        # Attach files
        self._attach_call_files_to_ticket(ticket)

        self.message_post(
            body=f"<b>Ticket created manually:</b> {ticket.name}",
            subtype_xmlid="mail.mt_note",
        )

        # Open the new ticket
        return {
            "type": "ir.actions.act_window",
            "name": ticket.name,
            "res_model": "helpdesk.ticket",
            "res_id": ticket.id,
            "view_mode": "form",
            "target": "current",
        }

    def _create_ticket_from_agent(self, config):
        """Create ticket triggered by agent override (not manual button)."""
        self.ensure_one()
        partner_name = self.partner_id.name if self.partner_id else self.caller_number
        ticket_vals = {
            "name": f"Negative Call (Agent Flagged) — {partner_name} ({self.name})",
            "partner_id": self.partner_id.id if self.partner_id else False,
            "priority": config.escalation_priority,
            "description": self._build_ticket_description(),
        }
        if config.helpdesk_team_id:
            ticket_vals["team_id"] = config.helpdesk_team_id

        if "rc_escalated" in self.env["helpdesk.ticket"]._fields:
            ticket_vals["rc_escalated"] = True

        # Add call detail fields
        ticket_vals.update(self._get_ticket_call_vals())

        ticket = self.env["helpdesk.ticket"].create(ticket_vals)
        self.write({"ticket_id": ticket.id, "ticket_ref": ticket.name})

        self._attach_call_files_to_ticket(ticket)

        self.message_post(
            body=(
                f"<b>Ticket auto-created</b> — agent flagged call as negative.<br/>"
                f"Ticket: {ticket.name}"
            ),
            subtype_xmlid="mail.mt_note",
        )
        _logger.info("Agent-triggered ticket %s for call %s", ticket.name, self.name)

    # ── Phone Number Matching ────────────────────────────────────
    @api.model
    def _normalize_phone(self, number):
        """Strip a phone number to just digits, optionally remove leading country code."""
        if not number:
            return ""
        digits = re.sub(r"[^\d+]", "", number)
        return digits

    @api.model
    def _match_partner(self, phone_number):
        """Find res.partner by phone number with fuzzy matching."""
        if not phone_number:
            return self.env["res.partner"]

        normalized = self._normalize_phone(phone_number)
        if not normalized:
            return self.env["res.partner"]

        # 1. Exact match on phone or mobile
        partner = self.env["res.partner"].search(
            ["|", ("phone", "=", phone_number), ("mobile", "=", phone_number)],
            limit=1,
        )
        if partner:
            return partner

        # 2. Try normalized (digits only)
        partner = self.env["res.partner"].search(
            ["|", ("phone", "ilike", normalized[-10:]), ("mobile", "ilike", normalized[-10:])],
            limit=1,
        )
        if partner:
            return partner

        # 3. Try without country code (e.g. +27 → 0)
        if normalized.startswith("+") and len(normalized) > 4:
            local = "0" + normalized[3:]  # assumes 2-digit country code
            partner = self.env["res.partner"].search(
                ["|", ("phone", "ilike", local), ("mobile", "ilike", local)],
                limit=1,
            )
            if partner:
                return partner

        return self.env["res.partner"]

    # ── Process a call event from webhook or polling ─────────────
    def _process_call_event(self, event_data, config):
        """
        Process a single call event from RingCentral.
        event_data: dict from RC API (call log record or webhook body)
        config: rc.config record
        """
        rc_call_id = event_data.get("sessionId") or event_data.get("id")
        if not rc_call_id:
            return

        # Check if already exists
        existing = self.search([("rc_call_id", "=", str(rc_call_id))], limit=1)
        if existing and existing.state == "done":
            return existing

        # Determine direction
        direction_map = {
            "Inbound": "inbound",
            "Outbound": "outbound",
        }
        rc_direction = event_data.get("direction", "Inbound")
        result = event_data.get("result", "")

        if result in ("Missed", "No Answer", "Busy"):
            direction = "missed"
        else:
            direction = direction_map.get(rc_direction, "inbound")

        # Extract phone numbers
        from_data = event_data.get("from", {})
        to_data = event_data.get("to", {})
        caller_number = from_data.get("phoneNumber", "")
        callee_number = to_data.get("phoneNumber", "")

        # Match to contact
        match_number = caller_number if direction == "inbound" else callee_number
        partner = self._match_partner(match_number)

        # Parse times
        start_str = event_data.get("startTime", "")
        duration = event_data.get("duration", 0)

        start_time = False
        end_time = False
        if start_str:
            try:
                start_time = fields.Datetime.to_datetime(
                    start_str.replace("T", " ").replace("Z", "")[:19]
                )
                if duration:
                    end_time = start_time + timedelta(seconds=duration)
            except Exception:
                pass

        vals = {
            "rc_call_id": str(rc_call_id),
            "direction": direction,
            "caller_number": caller_number,
            "callee_number": callee_number,
            "partner_id": partner.id if partner else False,
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "rc_extension_id": event_data.get("extension", {}).get("id", ""),
            "recording_url": event_data.get("recording", {}).get("contentUri", ""),
            "state": "new",
        }

        if existing:
            existing.write(vals)
            call_log = existing
        else:
            call_log = self.create(vals)

        # Async processing: recording, transcript, sentiment
        call_log.with_delay_or_direct(config)

        return call_log

    def with_delay_or_direct(self, config):
        """Process recording/transcript/sentiment. Uses queue_job if available, else direct."""
        self.ensure_one()
        try:
            # If queue_job module is installed, use it
            self.with_delay()._process_post_call(config.id)
        except Exception:
            # Fallback: process directly
            self._process_post_call(config.id)

    def _process_post_call(self, config_id):
        """Download recording, get transcript, run sentiment, create ticket if needed."""
        self.ensure_one()
        config = self.env["rc.config"].browse(config_id)
        if not config.exists():
            return

        self.write({"state": "processing"})

        try:
            # Step 1: Download recording
            if config.sync_recordings and self.recording_url:
                self._download_recording(config)

            # Step 2: Get transcript
            if config.sync_transcripts and self.recording_attachment_id:
                self._fetch_transcript(config)

            # Step 3: Sentiment analysis
            if config.sync_transcripts and self.transcript:
                self._analyze_sentiment(config)

            # Step 4: Auto-ticket on negative sentiment (uses final_sentiment = agent override or AI)
            if (
                config.auto_create_ticket
                and self.final_sentiment == "negative"
            ):
                self._handle_negative_sentiment(config)

            self.write({"state": "done"})

        except Exception as e:
            _logger.error("Post-call processing failed for %s: %s", self.name, e)
            self.write({"state": "failed", "error_message": str(e)})

    # ── Recording Download ───────────────────────────────────────
    def _download_recording(self, config):
        """Download call recording from RingCentral and attach to record."""
        self.ensure_one()
        if not self.recording_url:
            return

        try:
            token = config._get_access_token()
            # RC recording URLs need the access token appended
            url = self.recording_url
            if "?" in url:
                url += f"&access_token={token}"
            else:
                url += f"?access_token={token}"

            resp = requests.get(url, timeout=120)
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "audio/mpeg")
            ext = "mp3" if "mpeg" in content_type else "wav"
            filename = f"call_{self.name}_{self.rc_call_id}.{ext}"

            attachment = self.env["ir.attachment"].create(
                {
                    "name": filename,
                    "type": "binary",
                    "datas": base64.b64encode(resp.content),
                    "res_model": self._name,
                    "res_id": self.id,
                    "mimetype": content_type,
                }
            )
            self.write({"recording_attachment_id": attachment.id})

            # Also attach to partner if exists
            if self.partner_id:
                attachment.copy(
                    {
                        "res_model": "res.partner",
                        "res_id": self.partner_id.id,
                    }
                )

            _logger.info("Recording downloaded for call %s", self.name)
        except Exception as e:
            _logger.warning("Recording download failed for %s: %s", self.name, e)

    # ── Transcript ───────────────────────────────────────────────
    def _fetch_transcript(self, config):
        """Get call transcript — try RingSense first, fallback to Anthropic."""
        self.ensure_one()

        if config.sentiment_provider == "ringsense":
            self._fetch_ringsense_transcript(config)
        elif config.sentiment_provider == "anthropic":
            self._transcribe_with_anthropic(config)

    def _fetch_ringsense_transcript(self, config):
        """Fetch transcript from RingSense AI API."""
        self.ensure_one()
        try:
            # RingSense AI endpoint for call analysis
            result = config._rc_api_request(
                "GET",
                f"/ai/insights/v1/account/~/telephony/sessions/{self.rc_call_id}",
            )
            transcript_text = ""
            for utterance in result.get("utterances", []):
                speaker = utterance.get("speaker", "Unknown")
                text = utterance.get("text", "")
                transcript_text += f"{speaker}: {text}\n"

            if transcript_text:
                self._store_transcript(transcript_text)
        except Exception as e:
            _logger.warning("RingSense transcript failed for %s: %s", self.name, e)

    def _transcribe_with_anthropic(self, config):
        """Transcribe recording using Anthropic Claude (audio description)."""
        self.ensure_one()
        if not config.anthropic_api_key or not self.recording_attachment_id:
            return

        try:
            # Read the audio file
            audio_data = base64.b64decode(self.recording_attachment_id.datas)
            audio_b64 = base64.b64encode(audio_data).decode("utf-8")

            # Use Claude to transcribe/summarize
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": config.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4096,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "document",
                                    "source": {
                                        "type": "base64",
                                        "media_type": self.recording_attachment_id.mimetype or "audio/mpeg",
                                        "data": audio_b64,
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": (
                                        "Transcribe this phone call recording. "
                                        "Format as a dialogue with speakers labeled. "
                                        "Include timestamps if possible."
                                    ),
                                },
                            ],
                        }
                    ],
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            transcript_text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    transcript_text += block["text"]

            if transcript_text:
                self._store_transcript(transcript_text)

        except Exception as e:
            _logger.warning("Anthropic transcription failed for %s: %s", self.name, e)

    def _store_transcript(self, transcript_text):
        """Store transcript as text field and as file attachment."""
        self.ensure_one()
        self.write({"transcript": transcript_text})

        attachment = self.env["ir.attachment"].create(
            {
                "name": f"transcript_{self.name}.txt",
                "type": "binary",
                "datas": base64.b64encode(transcript_text.encode("utf-8")),
                "res_model": self._name,
                "res_id": self.id,
                "mimetype": "text/plain",
            }
        )
        self.write({"transcript_attachment_id": attachment.id})

        # Also attach to partner
        if self.partner_id:
            attachment.copy(
                {
                    "res_model": "res.partner",
                    "res_id": self.partner_id.id,
                }
            )

    # ── Sentiment Analysis ───────────────────────────────────────
    def _analyze_sentiment(self, config):
        """Run sentiment analysis using the configured provider."""
        self.ensure_one()
        if not self.transcript:
            return

        provider = self.env["rc.sentiment.provider"].get_default_provider()
        if not provider:
            _logger.info("No sentiment provider configured, skipping analysis for %s", self.name)
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
                    body=(
                        f"<b>Sentiment Analysis ({provider.name}):</b> "
                        f"{result['score']:.2f} — {result['reason']}"
                    ),
                    subtype_xmlid="mail.mt_note",
                )
        except Exception as e:
            _logger.warning("Sentiment analysis failed for %s: %s", self.name, e)

    # ── Helpdesk Check ───────────────────────────────────────────
    def _has_helpdesk(self):
        """Check if Helpdesk module is installed (Enterprise)."""
        return "helpdesk.ticket" in self.env

    # ── Ticket Creation / Escalation ─────────────────────────────
    def _handle_negative_sentiment(self, config):
        """Create or escalate helpdesk ticket for negative sentiment calls."""
        self.ensure_one()
        if not self._has_helpdesk():
            _logger.info("Helpdesk not installed — skipping ticket creation for %s", self.name)
            self.message_post(
                body="<b>⚠️ Negative sentiment detected</b> but Helpdesk module is not installed. "
                     "Install Helpdesk (Enterprise) to enable auto-ticket creation.",
                subtype_xmlid="mail.mt_note",
            )
            return

        # Check for existing open ticket for this contact
        if self.partner_id and config.auto_escalate_ticket:
            existing_ticket = self.env["helpdesk.ticket"].search(
                [
                    ("partner_id", "=", self.partner_id.id),
                    ("stage_id.fold", "=", False),
                ],
                order="create_date desc",
                limit=1,
            )
            if existing_ticket:
                self._escalate_ticket(existing_ticket, config)
                return

        # Create new ticket
        if config.auto_create_ticket:
            self._create_ticket(config)

    def _get_ticket_call_vals(self):
        """Get call detail values to populate on a helpdesk ticket."""
        self.ensure_one()
        ticket_fields = self.env["helpdesk.ticket"]._fields
        vals = {}

        # Only populate fields that exist (in case helpdesk_ticket.py wasn't loaded)
        field_map = {
            "rc_call_ref": self.name,
            "rc_direction": self.direction,
            "rc_caller_number": self.caller_number,
            "rc_callee_number": self.callee_number,
            "rc_call_duration": self.duration_display,
            "rc_call_time": self.start_time,
            "rc_sentiment": self.final_sentiment or self.agent_sentiment or self.sentiment_label,
            "rc_sentiment_score": self.sentiment_score,
            "rc_sentiment_source": (
                "Agent" if self.agent_sentiment
                else (self.sentiment_provider_id.name if self.sentiment_provider_id else "None")
            ),
            "rc_transcript": self.transcript,
            "rc_agent_notes": self.notes,
        }
        for field_name, value in field_map.items():
            if field_name in ticket_fields:
                vals[field_name] = value

        return vals

    def _attach_call_files_to_ticket(self, ticket):
        """Copy recording and transcript attachments to the ticket."""
        self.ensure_one()
        recording_copy = None
        if self.recording_attachment_id:
            recording_copy = self.recording_attachment_id.copy(
                {"res_model": "helpdesk.ticket", "res_id": ticket.id}
            )
        if self.transcript_attachment_id:
            self.transcript_attachment_id.copy(
                {"res_model": "helpdesk.ticket", "res_id": ticket.id}
            )

        # Link the recording attachment on the ticket for direct access
        if recording_copy and "rc_recording_attachment_id" in self.env["helpdesk.ticket"]._fields:
            ticket.write({"rc_recording_attachment_id": recording_copy.id})

    def _create_ticket(self, config):
        """Create a new helpdesk ticket from a negative call."""
        self.ensure_one()

        partner_name = self.partner_id.name if self.partner_id else self.caller_number
        ticket_vals = {
            "name": f"Negative Call — {partner_name} ({self.name})",
            "partner_id": self.partner_id.id if self.partner_id else False,
            "priority": config.escalation_priority,
            "description": self._build_ticket_description(),
        }
        if config.helpdesk_team_id:
            ticket_vals["team_id"] = config.helpdesk_team_id

        if "rc_escalated" in self.env["helpdesk.ticket"]._fields:
            ticket_vals["rc_escalated"] = True

        # Add call detail fields
        ticket_vals.update(self._get_ticket_call_vals())

        ticket = self.env["helpdesk.ticket"].create(ticket_vals)
        self.write({"ticket_id": ticket.id, "ticket_ref": ticket.name})

        # Attach recording and transcript files
        self._attach_call_files_to_ticket(ticket)

        _logger.info("Auto-created ticket %s for negative call %s", ticket.name, self.name)

    def _escalate_ticket(self, ticket, config):
        """Escalate an existing ticket due to negative call."""
        self.ensure_one()

        current_priority = int(ticket.priority or "0")
        new_priority = min(current_priority + 1, 3)

        write_vals = {"priority": str(new_priority)}
        if "rc_escalated" in self.env["helpdesk.ticket"]._fields:
            write_vals["rc_escalated"] = True

        # Update call details on ticket (latest call overwrites)
        write_vals.update(self._get_ticket_call_vals())
        ticket.write(write_vals)

        # Attach files
        self._attach_call_files_to_ticket(ticket)

        ticket.message_post(
            body=(
                f"<b>Priority escalated</b> due to negative call sentiment.<br/>"
                f"Call: {self.name}<br/>"
                f"Sentiment Score: {self.sentiment_score:.2f}<br/>"
                f"<br/><b>Transcript excerpt:</b><br/>"
                f"<pre>{(self.transcript or '')[:500]}</pre>"
            ),
            subtype_xmlid="mail.mt_note",
        )

        self.write({"ticket_id": ticket.id, "ticket_ref": ticket.name})
        _logger.info("Escalated ticket %s due to negative call %s", ticket.name, self.name)

    def _build_ticket_description(self):
        """Build ticket description from call data."""
        self.ensure_one()
        sentiment_display = self.agent_sentiment or self.sentiment_label or "not analysed"
        source = "Agent flagged" if self.agent_sentiment else "AI detected"

        lines = [
            f"<b>Created from call — {source}</b>",
            f"<br/><br/>",
            f"<b>Call Reference:</b> {self.name}<br/>",
            f"<b>Direction:</b> {self.direction}<br/>",
            f"<b>Caller:</b> {self.caller_number}<br/>",
            f"<b>Callee:</b> {self.callee_number}<br/>",
            f"<b>Duration:</b> {self.duration_display}<br/>",
            f"<b>Contact:</b> {self.partner_id.name if self.partner_id else 'Unknown'}<br/>",
            f"<br/>",
            f"<b>Sentiment:</b> {sentiment_display}<br/>",
        ]
        if self.sentiment_score:
            lines.append(f"<b>AI Score:</b> {self.sentiment_score:.2f}<br/>")
        if self.sentiment_reason:
            lines.append(f"<b>AI Reason:</b> {self.sentiment_reason}<br/>")
        if self.agent_sentiment:
            lines.append(f"<b>Agent Override:</b> {self.agent_sentiment}<br/>")
        if self.notes:
            lines.append(f"<br/><b>Agent Notes:</b><br/>{self.notes}<br/>")
        if self.transcript:
            lines.append(f"<br/><b>Transcript:</b><br/><pre>{self.transcript[:2000]}</pre>")
        return "".join(lines)

    # ── Polling (Cron Fallback) ──────────────────────────────────
    @api.model
    def _cron_poll_calls(self):
        """Cron job: Poll RingCentral for recent calls."""
        configs = self.env["rc.config"].search([("polling_enabled", "=", True)])
        for config in configs:
            try:
                self._poll_ringcentral(config)
            except Exception as e:
                _logger.error("Polling failed for company %s: %s", config.company_id.name, e)

    def _poll_ringcentral(self, config):
        """Poll RingCentral Call Log API for recent calls."""
        # Default: last poll time or last 24 hours
        date_from = config.last_poll_time or (
            fields.Datetime.now() - timedelta(hours=24)
        )
        date_from_str = date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        params = {
            "dateFrom": date_from_str,
            "view": "Detailed",
            "perPage": 100,
            "withRecording": "True",
        }
        try:
            result = config._rc_api_request(
                "GET",
                "/account/~/extension/~/call-log",
                params=params,
            )
            records = result.get("records", [])
            _logger.info("Polled %d call records from RingCentral", len(records))

            for record in records:
                self._process_call_event(record, config)

            config.sudo().write({"last_poll_time": fields.Datetime.now()})

        except Exception as e:
            _logger.error("RingCentral polling error: %s", e)
            raise

    # ── Retry Failed ─────────────────────────────────────────────
    @api.model
    def _cron_retry_failed(self):
        """Retry processing for failed call logs."""
        failed = self.search([("state", "=", "failed")], limit=50)
        config_model = self.env["rc.config"]
        for call in failed:
            config = config_model._get_config()
            call._process_post_call(config.id)
