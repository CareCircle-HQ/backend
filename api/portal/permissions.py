"""Portal access control.

The portal is for customer-support / verification staff only. Screeners (who
use the Chrome extension) must NOT be able to sign in or call portal endpoints,
even with a valid agent JWT. Access is gated on the agent's group, which the
``AgentJWTAuthentication`` puts on ``request.user.group``.
"""

from rest_framework.permissions import BasePermission

# Groups allowed into the support portal. Screeners are intentionally excluded.
PORTAL_ALLOWED_GROUPS = frozenset({"Verifiers", "Management", "CS"})


def is_portal_group(group):
    return (group or "") in PORTAL_ALLOWED_GROUPS


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
