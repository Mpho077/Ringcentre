from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class RCManualSync(models.TransientModel):
    _name = "rc.manual.sync"
    _description = "RingCentral Manual Sync Wizard"

    date_from = fields.Datetime(
        string="From Date",
        required=True,
        default=lambda self: fields.Datetime.now() - timedelta(days=7),
    )
    date_to = fields.Datetime(
        string="To Date",
        required=True,
        default=lambda self: fields.Datetime.now(),
    )
    sync_recordings = fields.Boolean(
        string="Download Recordings",
        default=True,
    )
    sync_transcripts = fields.Boolean(
        string="Fetch Transcripts",
        default=True,
    )
    run_sentiment = fields.Boolean(
        string="Run Sentiment Analysis",
        default=True,
    )

    def action_sync(self):
        """Execute the manual sync."""
        self.ensure_one()
        config = self.env["rc.config"]._get_config()

        date_from_str = self.date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        date_to_str = self.date_to.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        params = {
            "dateFrom": date_from_str,
            "dateTo": date_to_str,
            "view": "Detailed",
            "perPage": 250,
            "withRecording": "True",
        }

        result = config._rc_api_request(
            "GET",
            "/account/~/extension/~/call-log",
            params=params,
        )
        records = result.get("records", [])
        count = 0

        for record in records:
            call = self.env["rc.call.log"]._process_call_event(record, config)
            if call:
                count += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync Complete"),
                "message": _("%d calls synced.") % count,
                "type": "success",
                "sticky": False,
            },
        }
