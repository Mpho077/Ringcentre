from . import models
from . import controllers
from . import wizards


def _post_init_hook(env):
    """Load helpdesk views if the helpdesk module is installed."""
    import importlib
    if importlib.util.find_spec("odoo.addons.helpdesk"):
        from odoo.tools import convert_file
        convert_file(
            env, "ringcentral_odoo",
            "views/helpdesk_ticket_views.xml",
            {}, mode="init", noupdate=False,
        )
        # Also load helpdesk tags
        convert_file(
            env, "ringcentral_odoo",
            "data/rc_helpdesk_tags.xml",
            {}, mode="init", noupdate=True,
        )
