"""GoHighLevel (GHL) CRM integration.

TEMPORARY integration: we mirror our records into an external GHL CRM until we
build our own back office. It is deliberately self-contained so it can be
removed cleanly later:

    1. Delete the ``api/integrations`` package.
    2. Remove the ``sync_client`` calls in ``api/views.py``.
    3. Drop the ``crm_*`` columns via a migration.

Nothing here may raise into the request path -- a CRM failure must never break a
local save. The public entry points are :func:`sync_client`, :func:`sync_case`,
:func:`sync_screening`, and :func:`sync_eligibility`.
"""

from .contacts import sync_client
from .opportunities import sync_case, sync_eligibility, sync_screening

__all__ = ["sync_client", "sync_case", "sync_eligibility", "sync_screening"]
