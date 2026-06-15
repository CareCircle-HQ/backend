"""Custom authentication for agent JWTs.

Kept in its own module (no APIView imports) so it can be referenced from
DEFAULT_AUTHENTICATION_CLASSES without triggering a circular import during
DRF settings initialization.
"""

from rest_framework_simplejwt.authentication import JWTAuthentication


class AgentUser:
    """Lightweight authenticated principal representing a logged-in Agent.

    Not a Django auth User, but exposes the attributes DRF needs so the agent
    JWT can authenticate requests without a corresponding auth_user row.
    """

    def __init__(self, agent_id=None, agent_code=None, name=None, group=None):
        self.id = agent_id
        self.pk = agent_id
        self.agent_id = agent_id
        self.agent_code = agent_code
        self.name = name
        self.group = group

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_staff(self):
        return False

    @property
    def is_superuser(self):
        return False

    def __str__(self):
        return f"Agent {self.agent_code} ({self.name})"


class AgentJWTAuthentication(JWTAuthentication):
    """Authenticate agent JWTs that carry agent claims instead of user_id."""

    def get_user(self, validated_token):
        agent_id = validated_token.get("agent_id")
        if agent_id is None:
            # Fall back to the default behavior (user_id-based tokens).
            return super().get_user(validated_token)
        return AgentUser(
            agent_id=agent_id,
            agent_code=validated_token.get("agent_code"),
            name=validated_token.get("agent_name"),
            group=validated_token.get("agent_group"),
        )
