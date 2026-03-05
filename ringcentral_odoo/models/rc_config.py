import json
import logging
import requests
from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class RingCentralConfig(models.Model):
    _name = "rc.config"
    _description = "RingCentral Configuration"
    _rec_name = "company_id"

    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )
    active = fields.Boolean(default=True)

    # ── OAuth / Auth ─────────────────────────────────────────────
    rc_server_url = fields.Char(
        string="RingCentral Server URL",
        default="https://platform.ringcentral.com",
        help="Use https://platform.devtest.ringcentral.com for sandbox",
    )
    rc_client_id = fields.Char(string="Client ID")
    rc_client_secret = fields.Char(string="Client Secret")
    rc_jwt_token = fields.Char(
        string="JWT Token",
        help="JWT credential for server-to-server auth (recommended for Odoo.sh)",
    )
    rc_access_token = fields.Char(string="Access Token", readonly=True)
    rc_token_expiry = fields.Datetime(string="Token Expiry", readonly=True)

    # ── Webhook ──────────────────────────────────────────────────
    rc_webhook_id = fields.Char(
        string="Webhook Subscription ID",
        readonly=True,
        help="RingCentral subscription ID for webhook events",
    )
    rc_webhook_verification_token = fields.Char(
        string="Webhook Verification Token",
        help="Token sent by RingCentral in webhook validation requests",
    )
    webhook_base_url = fields.Char(
        string="Odoo Base URL",
        help="Public URL of your Odoo instance (e.g. https://mycompany.odoo.com)",
    )

    # ── Sentiment & Tickets ──────────────────────────────────────
    sentiment_provider = fields.Selection(
        [
            ("ringsense", "RingSense AI (RingCentral)"),
            ("anthropic", "Anthropic Claude"),
            ("disabled", "Disabled"),
        ],
        string="Sentiment Provider",
        default="disabled",
    )
    anthropic_api_key = fields.Char(string="Anthropic API Key")
    sentiment_threshold = fields.Float(
        string="Negative Sentiment Threshold",
        default=-0.3,
        help="Sentiment score below this triggers ticket creation (-1.0 to 1.0)",
    )
    auto_create_ticket = fields.Boolean(
        string="Auto-Create Ticket on Negative Calls",
        default=True,
    )
    auto_escalate_ticket = fields.Boolean(
        string="Auto-Escalate Existing Tickets",
        default=True,
        help="If contact already has an open ticket, escalate its priority",
    )
    escalation_priority = fields.Selection(
        [("0", "Low"), ("1", "Medium"), ("2", "High"), ("3", "Urgent")],
        string="Auto-Ticket Priority",
        default="2",
    )
    helpdesk_team_id = fields.Integer(
        string="Helpdesk Team ID",
        help="ID of the helpdesk team for auto-created tickets (Enterprise only)",
    )

    # ── Sync Settings ────────────────────────────────────────────
    sync_recordings = fields.Boolean(
        string="Download & Attach Recordings",
        default=True,
    )
    sync_transcripts = fields.Boolean(
        string="Fetch & Attach Transcripts",
        default=True,
    )
    polling_enabled = fields.Boolean(
        string="Enable Polling Fallback",
        default=True,
        help="Poll RingCentral API as fallback when webhooks miss events",
    )
    polling_interval = fields.Integer(
        string="Polling Interval (minutes)",
        default=5,
    )
    last_poll_time = fields.Datetime(
        string="Last Poll Time",
        readonly=True,
    )

    # ── Constraints ──────────────────────────────────────────────
    _sql_constraints = [
        (
            "company_uniq",
            "unique(company_id)",
            "Only one RingCentral configuration per company is allowed.",
        ),
    ]

    # ── Auth Methods ─────────────────────────────────────────────
    def _get_config(self):
        """Get the RC config for current company, raise if missing."""
        config = self.search([("company_id", "=", self.env.company.id)], limit=1)
        if not config:
            raise UserError(
                _("RingCentral is not configured. Go to RingCentral → Configuration.")
            )
        return config

    def _get_access_token(self):
        """Get a valid access token, refreshing if expired."""
        self.ensure_one()
        if self.rc_access_token and self.rc_token_expiry:
            if fields.Datetime.now() < self.rc_token_expiry:
                return self.rc_access_token

        return self._refresh_access_token()

    def _refresh_access_token(self):
        """Authenticate with RingCentral using JWT and get access token."""
        self.ensure_one()
        if not self.rc_jwt_token:
            raise UserError(_("JWT Token is required for authentication."))

        url = f"{self.rc_server_url}/restapi/oauth/token"
        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self.rc_jwt_token,
        }
        try:
            resp = requests.post(
                url,
                data=payload,
                auth=(self.rc_client_id, self.rc_client_secret),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            expiry = fields.Datetime.now() + timedelta(
                seconds=data.get("expires_in", 3600) - 60  # 60s buffer
            )
            self.sudo().write(
                {
                    "rc_access_token": data["access_token"],
                    "rc_token_expiry": expiry,
                }
            )
            _logger.info("RingCentral access token refreshed successfully.")
            return data["access_token"]
        except requests.exceptions.RequestException as e:
            _logger.error("RingCentral auth failed: %s", e)
            raise UserError(_("RingCentral authentication failed: %s") % str(e))

    def _rc_api_request(self, method, endpoint, **kwargs):
        """Make an authenticated request to RingCentral API."""
        self.ensure_one()
        token = self._get_access_token()
        url = f"{self.rc_server_url}/restapi/v1.0{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.request(
                method, url, headers=headers, timeout=60, **kwargs
            )
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return {}
        except requests.exceptions.RequestException as e:
            _logger.error("RingCentral API error [%s %s]: %s", method, endpoint, e)
            raise UserError(_("RingCentral API error: %s") % str(e))

    # ── Webhook Management ───────────────────────────────────────
    def action_subscribe_webhooks(self):
        """Create webhook subscription on RingCentral."""
        self.ensure_one()
        if not self.webhook_base_url:
            raise UserError(_("Set the Odoo Base URL first."))

        callback_url = f"{self.webhook_base_url}/ringcentral/webhook/call"
        payload = {
            "eventFilters": [
                "/restapi/v1.0/account/~/extension/~/telephony/sessions",
                "/restapi/v1.0/account/~/extension/~/voicemail",
            ],
            "deliveryMode": {
                "transportType": "WebHook",
                "address": callback_url,
            },
            "expiresIn": 630720000,  # max: 20 years
        }
        result = self._rc_api_request(
            "POST", "/subscription", json=payload
        )
        self.sudo().write({
            "rc_webhook_id": result.get("id"),
            "rc_webhook_verification_token": result.get("deliveryMode", {}).get(
                "verificationToken", ""
            ),
        })
        _logger.info("RingCentral webhook subscribed: %s", result.get("id"))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Success"),
                "message": _("Webhook subscription created."),
                "type": "success",
            },
        }

    def action_unsubscribe_webhooks(self):
        """Remove webhook subscription."""
        self.ensure_one()
        if self.rc_webhook_id:
            try:
                self._rc_api_request(
                    "DELETE", f"/subscription/{self.rc_webhook_id}"
                )
            except Exception:
                pass
            self.sudo().write({"rc_webhook_id": False})

    def action_test_connection(self):
        """Test RingCentral API connection."""
        self.ensure_one()
        try:
            result = self._rc_api_request("GET", "/account/~/extension/~")
            ext_name = result.get("name", "Unknown")
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Connection Successful"),
                    "message": _("Connected as: %s") % ext_name,
                    "type": "success",
                },
            }
        except Exception as e:
            raise UserError(_("Connection failed: %s") % str(e))

    def action_manual_sync(self):
        """Trigger a manual call log sync."""
        self.ensure_one()
        self.env["rc.call.log"]._poll_ringcentral(self)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync Complete"),
                "message": _("Call log sync finished."),
                "type": "success",
            },
        }
