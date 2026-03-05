from . import rc_config
from . import rc_sentiment_provider
from . import rc_call_log
from . import res_partner

# Helpdesk integration (Enterprise only)
# Only import if the helpdesk module is actually installed
import importlib
if importlib.util.find_spec("odoo.addons.helpdesk"):
    from . import helpdesk_ticket
