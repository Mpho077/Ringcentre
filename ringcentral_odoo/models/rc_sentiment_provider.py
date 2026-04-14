import json
import logging
import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class RCSentimentProvider(models.Model):
    _name = "rc.sentiment.provider"
    _description = "Sentiment Analysis Provider"
    _order = "sequence, name"

    name = fields.Char(string="Provider Name", required=True)
    code = fields.Char(
        string="Code",
        required=True,
        help="Unique identifier: e.g. 'anthropic', 'openai', 'kopa', 'custom_webhook'",
    )
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    # ── Provider Type ────────────────────────────────────────────
    provider_type = fields.Selection(
        [
            ("anthropic", "Anthropic Claude"),
            ("openai", "OpenAI / ChatGPT"),
            ("ringsense", "RingSense AI"),
            ("webhook", "Custom Webhook (any API)"),
        ],
        string="Provider Type",
        required=True,
        default="webhook",
        help="'Custom Webhook' works with ANY AI — just point it at your endpoint",
    )

    # ── Authentication ───────────────────────────────────────────
    api_key = fields.Char(string="API Key", groups="ringcentral_odoo.group_rc_manager")
    api_url = fields.Char(
        string="API Endpoint URL",
        help="For webhook type: the full URL to POST transcript to",
    )
    api_model = fields.Char(
        string="Model Name",
        help="e.g. 'claude-sonnet-4-20250514', 'gpt-4o', 'gemini-pro'",
    )

    # ── Request Configuration ────────────────────────────────────
    auth_header_name = fields.Char(
        string="Auth Header Name",
        default="Authorization",
        help="Header name for authentication. e.g. 'Authorization', 'x-api-key', 'X-Auth-Token'",
    )
    auth_header_prefix = fields.Char(
        string="Auth Header Prefix",
        default="Bearer",
        help="Prefix before API key in header. e.g. 'Bearer', '' (empty for raw key)",
    )
    custom_headers = fields.Text(
        string="Additional Headers (JSON)",
        help='Extra headers as JSON: {"X-Custom": "value"}',
    )

    # ── Request Body Template ────────────────────────────────────
    request_template = fields.Text(
        string="Request Body Template",
        help=(
            "JSON template for the API request body. Use these placeholders:\n"
            "  {{transcript}} — the call transcript text\n"
            "  {{model}} — the model name from above\n\n"
            "Leave empty to use the default template for the provider type."
        ),
    )

    # ── Response Parsing ─────────────────────────────────────────
    response_score_path = fields.Char(
        string="Score JSON Path",
        default="score",
        help=(
            "Dot-notation path to the sentiment score in the response JSON.\n"
            "Examples: 'score', 'data.sentiment.score', 'results[0].score'\n"
            "The value should be a float from -1.0 to 1.0"
        ),
    )
    response_reason_path = fields.Char(
        string="Reason JSON Path",
        default="reason",
        help="Dot-notation path to the explanation text. e.g. 'reason', 'data.explanation'",
    )

    # ── Settings ─────────────────────────────────────────────────
    timeout = fields.Integer(string="Timeout (seconds)", default=60)
    is_default = fields.Boolean(
        string="Default Provider",
        help="Use this provider for automatic sentiment analysis",
    )

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Provider code must be unique."),
    ]

    # ── Core: Analyze Sentiment ──────────────────────────────────
    def analyze_sentiment(self, transcript):
        """
        Analyze sentiment of a transcript.
        Returns: dict {'score': float, 'reason': str}
        """
        self.ensure_one()

        if self.provider_type == "anthropic":
            return self._analyze_anthropic(transcript)
        elif self.provider_type == "openai":
            return self._analyze_openai(transcript)
        elif self.provider_type == "ringsense":
            return self._analyze_ringsense(transcript)
        elif self.provider_type == "webhook":
            return self._analyze_webhook(transcript)
        else:
            raise UserError(_("Unknown provider type: %s") % self.provider_type)

    # ── Anthropic ────────────────────────────────────────────────
    def _analyze_anthropic(self, transcript):
        self.ensure_one()
        prompt = self._get_sentiment_prompt(transcript)
        model = self.api_model or "claude-sonnet-4-20250514"

        resp = requests.post(
            self.api_url or "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        return self._parse_json_response(text)

    # ── OpenAI ───────────────────────────────────────────────────
    def _analyze_openai(self, transcript):
        self.ensure_one()
        prompt = self._get_sentiment_prompt(transcript)
        model = self.api_model or "gpt-4o"

        resp = requests.post(
            self.api_url or "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return self._parse_json_response(text)

    # ── RingSense ────────────────────────────────────────────────
    def _analyze_ringsense(self, transcript):
        """RingSense is handled differently — via RC API, not standalone."""
        self.ensure_one()
        # This would be called from rc_call_log with the RC config
        # Placeholder — actual implementation uses rc.config._rc_api_request
        return {"score": 0.0, "reason": "RingSense analysis via RingCentral API"}

    # ── Custom Webhook (ANY AI) ──────────────────────────────────
    def _analyze_webhook(self, transcript):
        """
        Send transcript to any custom endpoint.
        This is the universal adapter — works with kopa.ai, Hugging Face,
        local models, custom Flask/FastAPI services, anything.
        """
        self.ensure_one()

        if not self.api_url:
            raise UserError(_("API Endpoint URL is required for webhook providers."))

        # Build headers
        headers = {"Content-Type": "application/json"}

        if self.api_key:
            prefix = self.auth_header_prefix or ""
            header_name = self.auth_header_name or "Authorization"
            if prefix:
                headers[header_name] = f"{prefix} {self.api_key}"
            else:
                headers[header_name] = self.api_key

        if self.custom_headers:
            try:
                extra = json.loads(self.custom_headers)
                headers.update(extra)
            except json.JSONDecodeError:
                pass

        # Build request body
        if self.request_template:
            body_str = self.request_template.replace(
                "{{transcript}}", transcript[:4000]
            ).replace(
                "{{model}}", self.api_model or ""
            )
            try:
                body = json.loads(body_str)
            except json.JSONDecodeError:
                body = {"transcript": transcript[:4000]}
        else:
            # Default: just send the transcript
            body = {
                "transcript": transcript[:4000],
                "task": "sentiment_analysis",
                "response_format": {
                    "score": "float from -1.0 to 1.0",
                    "reason": "brief explanation",
                },
            }

        # Make the request
        resp = requests.post(
            self.api_url,
            headers=headers,
            json=body,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        # Parse response using configured paths
        score = self._extract_json_path(data, self.response_score_path or "score")
        reason = self._extract_json_path(data, self.response_reason_path or "reason")

        return {
            "score": float(score) if score is not None else 0.0,
            "reason": str(reason) if reason else "",
        }

    # ── Helpers ──────────────────────────────────────────────────
    @staticmethod
    def _get_sentiment_prompt(transcript):
        """Standard sentiment analysis prompt for LLM-based providers."""
        return (
            "Analyze the sentiment of this customer phone call transcript. "
            "Respond ONLY with a JSON object: "
            '{"score": <float from -1.0 to 1.0>, "reason": "<brief explanation>"}\n\n'
            "Scoring guide:\n"
            "-1.0 = extremely negative (angry, threatening)\n"
            "-0.5 = clearly negative (frustrated, upset)\n"
            "0.0 = neutral\n"
            "0.5 = positive (satisfied, happy)\n"
            "1.0 = extremely positive (delighted, praising)\n\n"
            f"Transcript:\n{transcript[:3000]}"
        )

    @staticmethod
    def _parse_json_response(text):
        """Parse a JSON response from an LLM."""
        text = text.strip().strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
        try:
            result = json.loads(text)
            return {
                "score": float(result.get("score", 0.0)),
                "reason": result.get("reason", ""),
            }
        except (json.JSONDecodeError, ValueError):
            _logger.warning("Failed to parse sentiment JSON: %s", text[:200])
            return {"score": 0.0, "reason": "Parse error"}

    @staticmethod
    def _extract_json_path(data, path):
        """
        Extract value from nested dict using dot notation.
        e.g. 'data.sentiment.score' → data['data']['sentiment']['score']
        Also supports simple array indexing: 'results[0].score'
        """
        if not path:
            return None

        current = data
        for key in path.split("."):
            if current is None:
                return None
            # Handle array indexing like results[0]
            if "[" in key:
                field, idx = key.split("[")
                idx = int(idx.rstrip("]"))
                current = current.get(field, [])
                if isinstance(current, list) and len(current) > idx:
                    current = current[idx]
                else:
                    return None
            else:
                if isinstance(current, dict):
                    current = current.get(key)
                else:
                    return None
        return current

    # ── Get default provider ─────────────────────────────────────
    @api.model
    def get_default_provider(self):
        """Get the default sentiment provider."""
        provider = self.search([("is_default", "=", True)], limit=1)
        if not provider:
            provider = self.search([], limit=1)
        return provider

    # ── Test button ──────────────────────────────────────────────
    def action_test_provider(self):
        """Test the provider with a sample transcript."""
        self.ensure_one()
        sample = (
            "Agent: Thank you for calling, how can I help?\n"
            "Customer: I've been waiting 3 weeks for my order and nobody "
            "has responded to my emails. This is absolutely unacceptable.\n"
            "Agent: I'm very sorry about that, let me look into it.\n"
            "Customer: You better, because I'm about to cancel everything."
        )
        try:
            result = self.analyze_sentiment(sample)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Test Result"),
                    "message": _(
                        "Score: %(score).2f\nReason: %(reason)s"
                    ) % result,
                    "type": "success" if result["score"] != 0.0 else "warning",
                    "sticky": True,
                },
            }
        except Exception as e:
            raise UserError(_("Provider test failed: %s") % str(e))
