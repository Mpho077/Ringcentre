import hashlib
import hmac
import json
import logging

from odoo import http, SUPERUSER_ID
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


class RingCentralWebhook(http.Controller):

    @http.route(
        "/ringcentral/webhook/call",
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def webhook_call(self, **kwargs):
        """
        Receive call events from RingCentral webhook subscription.

        RingCentral sends:
        1. Validation request (initial handshake)
        2. Call session events (start, end, missed)
        3. Voicemail notifications
        """
        headers = request.httprequest.headers

        # ── Step 1: Handle webhook validation ────────────────────
        validation_token = headers.get("Validation-Token")
        if validation_token:
            _logger.info("RingCentral webhook validation request received")
            return Response(
                status=200,
                headers={"Validation-Token": validation_token},
            )

        # ── Step 2: Parse the event body ─────────────────────────
        try:
            body = request.get_json_data()
        except Exception:
            try:
                body = json.loads(request.httprequest.get_data(as_text=True))
            except Exception as e:
                _logger.error("Failed to parse webhook body: %s", e)
                return {"status": "error", "message": "Invalid JSON"}

        if not body:
            return {"status": "error", "message": "Empty body"}

        _logger.info(
            "RingCentral webhook event received: %s",
            body.get("event", "unknown"),
        )

        # ── Step 3: Verify webhook signature (if configured) ────
        env = request.env(su=True)
        config = env["rc.config"].search([], limit=1)

        if config and config.rc_webhook_verification_token:
            token = headers.get("Verification-Token", "")
            if token != config.rc_webhook_verification_token:
                _logger.warning("Webhook verification failed — token mismatch")
                return {"status": "error", "message": "Verification failed"}

        # ── Step 4: Process the event ────────────────────────────
        try:
            event_type = body.get("event", "")
            event_body = body.get("body", {})

            if not event_body:
                return {"status": "ok", "message": "No body to process"}

            # Telephony session events
            if "telephony/sessions" in event_type:
                self._process_telephony_event(env, config, event_body)

            # Voicemail events
            elif "voicemail" in event_type:
                self._process_voicemail_event(env, config, event_body)

            else:
                _logger.info("Unhandled RC event type: %s", event_type)

            return {"status": "ok"}

        except Exception as e:
            _logger.error("Webhook processing error: %s", e, exc_info=True)
            return {"status": "error", "message": str(e)}

    def _process_telephony_event(self, env, config, event_body):
        """Process a telephony session event."""
        # The event body contains a list of parties in the session
        parties = event_body.get("parties", [])
        session_id = event_body.get("sessionId") or event_body.get("telephonySessionId")

        if not session_id:
            return

        for party in parties:
            status = party.get("status", {})
            call_status = status.get("code", "")

            # We care about these statuses
            if call_status in ("Disconnected", "Answered", "Setup"):
                call_data = {
                    "sessionId": session_id,
                    "direction": party.get("direction", "Inbound"),
                    "from": party.get("from", {}),
                    "to": party.get("to", {}),
                    "duration": party.get("duration", 0),
                    "result": party.get("status", {}).get("reason", ""),
                    "extension": party.get("extension", {}),
                    "recording": party.get("recording", {}),
                }

                # Only fully process ended calls
                if call_status == "Disconnected":
                    call_data["startTime"] = event_body.get("creationTime", "")
                    env["rc.call.log"]._process_call_event(call_data, config)
                    _logger.info("Processed ended call session: %s", session_id)

    def _process_voicemail_event(self, env, config, event_body):
        """Process a voicemail notification."""
        attachments = event_body.get("attachments", [])
        from_data = event_body.get("from", {})

        call_data = {
            "sessionId": event_body.get("id", ""),
            "direction": "Inbound",
            "from": from_data,
            "to": event_body.get("to", [{}])[0] if event_body.get("to") else {},
            "duration": event_body.get("vmTranscriptionStatus", 0),
            "result": "Voicemail",
        }

        # Get voicemail recording URL
        for att in attachments:
            if att.get("type") == "AudioRecording":
                call_data["recording"] = {"contentUri": att.get("uri", "")}
                break

        env["rc.call.log"]._process_call_event(call_data, config)
        _logger.info("Processed voicemail event")

    # ── Alternative: HTTP route for non-JSON webhooks ────────────
    @http.route(
        "/ringcentral/webhook/validate",
        type="http",
        auth="none",
        methods=["GET", "POST"],
        csrf=False,
    )
    def webhook_validate(self, **kwargs):
        """
        Fallback validation endpoint.
        Some RingCentral webhook configs send a GET for validation.
        """
        validation_token = request.httprequest.headers.get("Validation-Token", "")
        if validation_token:
            return Response(
                validation_token,
                status=200,
                headers={
                    "Validation-Token": validation_token,
                    "Content-Type": "text/plain",
                },
            )
        return Response("OK", status=200)
