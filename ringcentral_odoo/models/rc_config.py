import logging
import requests
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class RCConfig(models.Model):
    _name = "rc.config"
    _description = "RingCentral Configuration"

    name = fields.Char(default="RingCentral Config", required=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True)
    active = fields.Boolean(default=True)

    rc_server_url = fields.Char(string="Server URL", default="https://platform.ringcentral.com")
    rc_client_id = fields.Char(string="Client ID")
    rc_client_secret = fields.Char(string="Client Secret")
    rc_jwt_token = fields.Char(string="JWT Token")
    rc_access_token = fields.Char(string="Access Token", readonly=True)
    rc_token_expiry = fields.Datetime(string="Token Expiry", readonly=True)

    webhook_base_url = fields.Char(string="Odoo Base URL")
    rc_webhook_id = fields.Char(string="Webhook Subscription ID", readonly=True)
    rc_webhook_verification_token = fields.Char(string="Webhook Verification Token")

    sentiment_threshold = fields.Float(string="Negative Threshold", default=-0.3)
    auto_create_ticket = fields.Boolean(string="Auto-Create Tickets", default=True)
    auto_escalate_ticket = fields.Boolean(string="Auto-Escalate Tickets", default=True)
    escalation_priority = fields.Selection(
        [("0", "Low"), ("1", "Medium"), ("2", "High"), ("3", "Urgent")],
        default="2",
    )
    helpdesk_team_id = fields.Many2one("helpdesk.team", string="Helpdesk Team",
        help="Team for auto-created tickets")

    sync_recordings = fields.Boolean(string="Download Recordings", default=True)
    sync_transcripts = fields.Boolean(string="Fetch Transcripts", default=True)
    polling_enabled = fields.Boolean(string="Enable Polling", default=True)
    last_poll_time = fields.Datetime(string="Last Poll", readonly=True)

    _sql_constraints = [("company_uniq", "unique(company_id)", "One config per company.")]

    def _get_access_token(self):
        self.ensure_one()
        if self.rc_access_token and self.rc_token_expiry and fields.Datetime.now() < self.rc_token_expiry:
            return self.rc_access_token
        return self._refresh_access_token()

    def _refresh_access_token(self):
        self.ensure_one()
        if not self.rc_jwt_token:
            raise UserError(_("JWT Token is required."))
        resp = requests.post(
            f"{self.rc_server_url}/restapi/oauth/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self.rc_jwt_token},
            auth=(self.rc_client_id, self.rc_client_secret),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        expiry = fields.Datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 60)
        self.sudo().write({"rc_access_token": data["access_token"], "rc_token_expiry": expiry})
        return data["access_token"]

    def _rc_api_request(self, method, endpoint, **kwargs):
        self.ensure_one()
        token = self._get_access_token()
        resp = requests.request(
            method, f"{self.rc_server_url}/restapi/v1.0{endpoint}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=60, **kwargs,
        )
        if not resp.ok:
            try:
                error_detail = resp.json()
            except Exception:
                error_detail = resp.text
            _logger.error("RingCentral API error [%s %s]: %s - %s", method, endpoint, resp.status_code, error_detail)
            raise UserError(_("RingCentral API error (%s): %s") % (resp.status_code, error_detail))
        return resp.json() if resp.content else {}

    def action_test_connection(self):
        self.ensure_one()
        result = self._rc_api_request("GET", "/account/~/extension/~")
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": "Success", "message": f"Connected as: {result.get('name', 'OK')}", "type": "success"},
        }

    def action_subscribe_webhooks(self):
        self.ensure_one()
        if not self.webhook_base_url:
            raise UserError(_("Set Odoo Base URL first."))
        result = self._rc_api_request("POST", "/subscription", json={
            "eventFilters": [
                "/restapi/v1.0/account/~/extension/~/telephony/sessions",
                "/restapi/v1.0/account/~/extension/~/voicemail",
            ],
            "deliveryMode": {"transportType": "WebHook", "address": f"{self.webhook_base_url}/ringcentral/webhook/call"},
            "expiresIn": 630720000,
        })
        self.sudo().write({
            "rc_webhook_id": result.get("id"),
            "rc_webhook_verification_token": result.get("deliveryMode", {}).get("verificationToken", ""),
        })

    def action_manual_sync(self):
        self.ensure_one()
        self.env["rc.call.log"]._poll_ringcentral(self)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": "Done", "message": "Call log synced.", "type": "success"},
        }
