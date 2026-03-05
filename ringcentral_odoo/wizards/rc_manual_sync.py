from datetime import timedelta
from odoo import fields, models, _


class RCManualSync(models.TransientModel):
    _name = "rc.manual.sync"
    _description = "RingCentral Manual Sync"

    date_from = fields.Datetime(required=True, default=lambda self: fields.Datetime.now() - timedelta(days=7))
    date_to = fields.Datetime(required=True, default=lambda self: fields.Datetime.now())
    sync_recordings = fields.Boolean(default=True)
    sync_transcripts = fields.Boolean(default=True)

    def action_sync(self):
        self.ensure_one()
        config = self.env["rc.config"].search([("company_id", "=", self.env.company.id)], limit=1)
        if not config:
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "Error", "message": "No RingCentral config found.", "type": "danger"}}

        result = config._rc_api_request("GET", "/account/~/extension/~/call-log", params={
            "dateFrom": self.date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "dateTo": self.date_to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "view": "Detailed", "perPage": 250, "withRecording": "True",
        })
        count = 0
        for record in result.get("records", []):
            call = self.env["rc.call.log"]._process_call_event(record, config)
            if call:
                count += 1

        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Sync Complete", "message": f"{count} calls synced.", "type": "success"}}
