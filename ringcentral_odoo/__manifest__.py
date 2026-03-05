{
    "name": "RingCentral Integration",
    "version": "19.0.1.0.0",
    "category": "Phone",
    "summary": "RingCentral call logging, recordings, transcripts and sentiment-based ticketing",
    "author": "Corvex Consult",
    "license": "LGPL-3",
    "depends": [
        "base",
        "contacts",
        "mail",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/rc_data.xml",
        "views/rc_call_log_views.xml",
        "views/rc_config_views.xml",
        "views/res_partner_views.xml",
    ],
    "installable": True,
    "application": True,
}
