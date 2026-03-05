{
    "name": "RingCentral Integration",
    "version": "19.0.1.0.0",
    "category": "Phone",
    "summary": "RingCentral call logging, recordings, transcripts & sentiment-based ticketing",
    "description": """
        Integrates RingCentral phone system with Odoo:
        - Automatic call logging against contacts
        - Call recording & transcript attachments
        - Sentiment analysis with auto-ticket creation
        - Real-time webhooks + polling fallback
    """,
    "author": "Custom",
    "website": "",
    "license": "LGPL-3",
    "depends": [
        "base",
        "contacts",
        "mail",
    ],
    # helpdesk is Enterprise-only — loaded dynamically if available
    "external_dependencies": {},
    "data": [
        # Security
        "security/rc_security.xml",
        "security/ir.model.access.csv",
        # Data
        "data/rc_mail_templates.xml",
        "data/rc_cron.xml",
        # Views
        "views/rc_config_views.xml",
        "views/rc_sentiment_provider_views.xml",
        "views/rc_call_log_views.xml",
        "views/res_partner_views.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
    "post_init_hook": "_post_init_hook",
}
