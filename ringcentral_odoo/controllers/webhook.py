import json
import logging
from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


class RingCentralWebhook(http.Controller):

    @http.route("/ringcentral/webhook/call", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def webhook_call(self, **kwargs):
        """Handle RingCentral webhook - validation and call events."""
        headers = request.httprequest.headers

        # Step 1: Validation handshake — RC sends Validation-Token header
        validation_token = headers.get("Validation-Token")
        if validation_token:
            _logger.info("RingCentral webhook validation received")
            return Response(
                validation_token,
                status=200,
                headers={"Validation-Token": validation_token, "Content-Type": "text/plain"},
            )

        # Step 2: Parse the event body
        try:
            raw = request.httprequest.get_data(as_text=True)
            body = json.loads(raw) if raw else {}
        except Exception as e:
            _logger.error("Failed to parse webhook body: %s", e)
            return Response(json.dumps({"status": "error"}), status=200, content_type="application/json")

        if not body:
            return Response(json.dumps({"status": "empty"}), status=200, content_type="application/json")

        _logger.info("RingCentral webhook event: %s", body.get("event", "unknown"))

        # Step 3: Verify token if configured
        env = request.env(su=True)
        config = env["rc.config"].search([], limit=1)
        if config and config.rc_webhook_verification_token:
            token = headers.get("Verification-Token", "")
            if token != config.rc_webhook_verification_token:
                _logger.warning("Webhook verification failed")
                return Response(json.dumps({"status": "auth_failed"}), status=200, content_type="application/json")

        # Step 4: Process the event
        try:
            event_type = body.get("event", "")
            event_body = body.get("body", {})

            if "telephony/sessions" in event_type and event_body:
                session_id = event_body.get("sessionId") or event_body.get("telephonySessionId")
                if session_id:
                    for party in event_body.get("parties", []):
                        if party.get("status", {}).get("code") == "Disconnected":
                            call_data = {
                                "sessionId": session_id,
                                "direction": party.get("direction", "Inbound"),
                                "from": party.get("from", {}),
                                "to": party.get("to", {}),
                                "duration": party.get("duration", 0),
                                "result": party.get("status", {}).get("reason", ""),
                                "recording": party.get("recording", {}),
                                "startTime": event_body.get("creationTime", ""),
                            }
                            env["rc.call.log"]._process_call_event(call_data, config)

            elif "voicemail" in event_type and event_body:
                call_data = {
                    "sessionId": event_body.get("id", ""),
                    "direction": "Inbound",
                    "from": event_body.get("from", {}),
                    "to": event_body.get("to", [{}])[0] if event_body.get("to") else {},
                    "duration": 0,
                    "result": "Voicemail",
                }
                for att in event_body.get("attachments", []):
                    if att.get("type") == "AudioRecording":
                        call_data["recording"] = {"contentUri": att.get("uri", "")}
                        break
                env["rc.call.log"]._process_call_event(call_data, config)

        except Exception as e:
            _logger.error("Webhook processing error: %s", e, exc_info=True)

        return Response(json.dumps({"status": "ok"}), status=200, content_type="application/json")

    @http.route("/ringcentral/webhook/validate", type="http", auth="none", methods=["GET", "POST"], csrf=False, save_session=False)
    def webhook_validate(self, **kwargs):
        """Fallback validation endpoint."""
        token = request.httprequest.headers.get("Validation-Token", "")
        if token:
            return Response(token, status=200, headers={"Validation-Token": token, "Content-Type": "text/plain"})
        return Response("OK", status=200)
