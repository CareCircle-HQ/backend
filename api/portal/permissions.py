"""Portal access control.

The portal is for customer-support / verification staff only. Screeners (who
use the Chrome extension) must NOT be able to sign in or call portal endpoints,
even with a valid agent JWT. Access is gated on the agent's group, which the
``AgentJWTAuthentication`` puts on ``request.user.group``.
"""

from rest_framework.permissions import BasePermission

# Groups allowed into the support portal. Screeners are intentionally excluded
# (they use the Chrome extension). Logistics staff work the kitchen-assignment /
# Logistics page, so they must be able to sign in too.
PORTAL_ALLOWED_GROUPS = frozenset({"Verifiers", "Management", "CS", "Logistics"})

# The management group. High-impact, shared-household actions (e.g. changing the
# household's assigned kitchen from the program tab) are locked to this group so
# verification / CS / logistics agents can't alter them.
MANAGEMENT_GROUP = "Management"


def is_portal_group(group):
    return (group or "") in PORTAL_ALLOWED_GROUPS


def is_management_group(group):
    return (group or "") == MANAGEMENT_GROUP


class IsPortalAgent(BasePermission):
    """Authenticated agent whose group is allowed in the portal."""

    message = "Your agent group does not have access to the support portal."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(
            user
            and getattr(user, "is_authenticated", False)
            and is_portal_group(getattr(user, "group", None))
        )


class IsManagementAgent(IsPortalAgent):
    """Authenticated portal agent in the Management group. Locks high-impact
    shared-household actions (e.g. changing the assigned kitchen) to management
    staff only -- verification and other agents are read-only for these."""

    message = "Only Management users can perform this action."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return is_management_group(getattr(getattr(request, "user", None), "group", None))
