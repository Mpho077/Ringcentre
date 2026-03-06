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

    name = fields.Char(required=True)
    code = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    is_default = fields.Boolean(string="Default Provider")

    provider_type = fields.Selection(
        [("anthropic", "Anthropic Claude"), ("openai", "OpenAI"), ("webhook", "Custom Webhook")],
        required=True, default="webhook",
    )

    api_key = fields.Char(string="API Key")
    api_url = fields.Char(string="API Endpoint URL")
    api_model = fields.Char(string="Model Name")
    timeout = fields.Integer(default=60)

    # Webhook config
    auth_header_name = fields.Char(default="Authorization")
    auth_header_prefix = fields.Char(default="Bearer")
    custom_headers = fields.Text(string="Extra Headers (JSON)")
    request_template = fields.Text(string="Request Body Template")
    response_score_path = fields.Char(default="score")
    response_reason_path = fields.Char(default="reason")

    _sql_constraints = [("code_uniq", "unique(code)", "Provider code must be unique.")]

    def analyze_sentiment(self, transcript):
        self.ensure_one()
        if self.provider_type == "anthropic":
            return self._analyze_anthropic(transcript)
        elif self.provider_type == "openai":
            return self._analyze_openai(transcript)
        else:
            return self._analyze_webhook(transcript)

    def _get_prompt(self, transcript):
        return (
            "Analyze the sentiment of this customer phone call transcript. "
            "Respond ONLY with JSON: "
            '{"score": <float -1.0 to 1.0>, "reason": "<brief>"}\n\n'
            "-1.0=very negative, 0.0=neutral, 1.0=very positive\n\n"
            f"Transcript:\n{transcript[:3000]}"
        )

    def _parse_json(self, text):
        text = text.strip().strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
        try:
            r = json.loads(text)
            return {"score": float(r.get("score", 0.0)), "reason": r.get("reason", "")}
        except Exception:
            return {"score": 0.0, "reason": "Parse error"}

    def _analyze_anthropic(self, transcript):
        self.ensure_one()
        resp = requests.post(
            self.api_url or "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": self.api_model or "claude-sonnet-4-20250514", "max_tokens": 256,
                  "messages": [{"role": "user", "content": self._get_prompt(transcript)}]},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        text = "".join(b["text"] for b in resp.json().get("content", []) if b.get("type") == "text")
        return self._parse_json(text)

    def _analyze_openai(self, transcript):
        self.ensure_one()
        resp = requests.post(
            self.api_url or "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.api_model or "gpt-4o", "max_tokens": 256,
                  "messages": [{"role": "user", "content": self._get_prompt(transcript)}],
                  "response_format": {"type": "json_object"}},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return self._parse_json(text)

    def _analyze_webhook(self, transcript):
        self.ensure_one()
        if not self.api_url:
            raise UserError(_("API URL required for webhook providers."))

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            prefix = self.auth_header_prefix or ""
            hname = self.auth_header_name or "Authorization"
            headers[hname] = f"{prefix} {self.api_key}".strip()

        if self.custom_headers:
            try:
                headers.update(json.loads(self.custom_headers))
            except Exception:
                pass

        if self.request_template:
            try:
                body = json.loads(
                    self.request_template.replace("{{transcript}}", transcript[:4000]).replace("{{model}}", self.api_model or "")
                )
            except Exception:
                body = {"transcript": transcript[:4000]}
        else:
            body = {"transcript": transcript[:4000], "task": "sentiment_analysis"}

        resp = requests.post(self.api_url, headers=headers, json=body, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        score = self._extract_path(data, self.response_score_path or "score")
        reason = self._extract_path(data, self.response_reason_path or "reason")
        return {"score": float(score) if score is not None else 0.0, "reason": str(reason or "")}

    @staticmethod
    def _extract_path(data, path):
        current = data
        for key in (path or "").split("."):
            if current is None:
                return None
            if "[" in key:
                field, idx = key.split("[")
                idx = int(idx.rstrip("]"))
                current = current.get(field, [])
                current = current[idx] if isinstance(current, list) and len(current) > idx else None
            elif isinstance(current, dict):
                current = current.get(key)
            else:
                return None
        return current

    @api.model
    def get_default_provider(self):
        return self.search([("is_default", "=", True)], limit=1) or self.search([], limit=1)

    def action_test_provider(self):
        self.ensure_one()
        sample = (
            "Agent: How can I help?\n"
            "Customer: I've waited 3 weeks and nobody responds. This is unacceptable.\n"
            "Agent: I'm sorry, let me look into it.\n"
            "Customer: You better, I'm about to cancel everything."
        )
        try:
            result = self.analyze_sentiment(sample)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Test Result",
                    "message": f"Score: {result['score']:.2f} — {result['reason']}",
                    "type": "success" if result["score"] != 0.0 else "warning",
                    "sticky": True,
                },
            }
        except Exception as e:
            raise UserError(_("Test failed: %s") % str(e))
