"""Setting the reason a member is not being served.

One helper, called at every site that pauses a member, so "why is this member not
being served?" is answerable from a single field rather than by reading a note.

WHY A HELPER RATHER THAN ASSIGNING THE FK DIRECTLY: there are eleven places that
move a member into a non-serving status, across views, serializers, services and
management commands. A helper makes them greppable as a set -- `set_pause_reason(`
finds every one -- and means a missing code fails loudly in tests rather than
silently leaving the reason blank on one path.
"""
import logging

logger = logging.getLogger(__name__)

# The system-owned codes, as constants so a typo is an ImportError rather than a
# silently-missing reason. These MUST match api/migrations/0299_seed_pause_reasons.
INSURANCE_INVALID = "insurance_invalid"
CASE_TYPE_SWITCH = "case_type_switch"
NUTRITIONIST_PAUSED = "nutritionist_paused"
OUT_OF_ORBIT = "out_of_orbit"
OUT_OF_RANGE = "out_of_range"
UNCATEGORIZED = "uncategorized"


def reason_for(code):
    """The PauseReason row for ``code``, or None.

    Returns None rather than raising: a missing catalogue row must never break a
    pause. Stopping someone's deliveries is the important half of the operation and
    the reason is the label on it -- a database without the seed migration applied
    should still be able to pause a member.
    """
    from api.models import PauseReason

    reason = PauseReason.objects.filter(code=code).first()
    if reason is None:
        logger.warning("pause reason %r is not in the catalogue", code)
    return reason


def set_pause_reason(profile, code, *, save=True, overwrite=True):
    """Record WHY ``profile`` is paused. Returns the PauseReason, or None.

    ``overwrite=False`` keeps an existing reason -- for the paths that re-run over
    an already-paused member (the eligibility reconcile runs on every import) so a
    re-import cannot relabel a pause an agent has already explained.
    """
    if profile is None:
        return None
    if not overwrite and profile.pause_reason_id:
        return profile.pause_reason
    reason = reason_for(code)
    if reason is None:
        return None
    if profile.pause_reason_id == reason.pk:
        return reason
    profile.pause_reason = reason
    if save:
        profile.save(update_fields=["pause_reason"])
    return reason


def clear_pause_reason(profile, *, save=True):
    """Drop the reason when a member returns to service.

    A stale reason on an ACTIVE member is worse than none: it reads as a current
    problem, and the Members page filters on this field.
    """
    if profile is None or not profile.pause_reason_id:
        return
    profile.pause_reason = None
    if save:
        profile.save(update_fields=["pause_reason"])
