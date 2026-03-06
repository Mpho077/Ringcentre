import json
import logging
from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


class RingCentralWebhook(http.Controller):

    @http.route("/ringcentral/webhook/call", type="json", auth="none", methods=["POST"], csrf=False)
    def webhook_call(self, **kwargs):
        headers = request.httprequest.headers
        validation_token = headers.get("Validation-Token")
        if validation_token:
            return Response(status=200, headers={"Validation-Token": validation_token})

        try:
            body = request.get_json_data()
        except Exception:
            try:
                body = json.loads(request.httprequest.get_data(as_text=True))
            except Exception:
                return {"status": "error"}

        if not body:
            return {"status": "error"}

        env = request.env(su=True)
        config = env["rc.config"].search([], limit=1)
        if not config:
            return {"status": "no config"}

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

        return {"status": "ok"}

    @http.route("/ringcentral/webhook/validate", type="http", auth="none", methods=["GET", "POST"], csrf=False)
    def webhook_validate(self, **kwargs):
        token = request.httprequest.headers.get("Validation-Token", "")
        if token:
            return Response(token, status=200, headers={"Validation-Token": token})
        return Response("OK", status=200)
