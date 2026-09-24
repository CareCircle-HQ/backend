"""Assigning a catalogue reason to a programme hold.

One place, because a dozen call sites need identical behaviour and a code typed as a
string literal in twelve files is a typo waiting to be silently ignored.

``advance_enrollment`` is where the StageEvent is written, so it takes a
``hold_reason`` and stamps BOTH the event and the enrollment. These helpers exist for
the callers that need to set or clear a reason outside that path.
"""
import logging

logger = logging.getLogger(__name__)

# The codes, so a caller imports a name rather than retyping a string.
PENDING_CASE_CLOSURE = "pending_case_closure"
WRONG_CASE_TYPE = "wrong_case_type"
NOT_ENHANCED_MEMBER = "not_enhanced_member"
GOVERNING_CASE_DENIED = "governing_case_denied"
GOVERNING_CASE_CLOSED = "governing_case_closed"
SOCIAL_COVERAGE_INVALID = "social_coverage_invalid"
INSURANCE_INVALID = "insurance_invalid"
MEMBER_REQUESTED = "member_requested"
ZIP_OUT_OF_COVERAGE = "zip_out_of_coverage"
MEDICAID_TYPE_NOT_SERVED = "medicaid_type_not_served"
ALL_MEMBERS_PAUSED = "all_members_paused"
DERIVED_STATUS = "derived_status"
UNCATEGORIZED = "uncategorized"


def hold_reason_obj(code, *, fallback=None):
    """The HoldReason row for ``code``, or ``fallback``'s row, or None.

    NEVER raises. A missing catalogue row must not break a hold: stopping the
    household's deliveries is the operative half and the reason is the label on it.
    """
    from api.models import HoldReason

    for candidate in (code, fallback):
        if not candidate:
            continue
        obj = HoldReason.objects.filter(code=candidate).first()
        if obj is not None:
            return obj
        logger.warning("hold reason %r is not in the catalogue", candidate)
    return None


def set_hold_reason(enrollment, code, *, fallback=UNCATEGORIZED, save=True,
                    overwrite=True):
    """Record WHY this enrollment is On Hold.

    ``overwrite=False`` leaves an existing reason alone -- for the import paths,
    which re-evaluate every member on every run. A re-import must not relabel a hold
    an agent has already explained.
    """
    if enrollment is None:
        return None
    if not overwrite and enrollment.hold_reason_id:
        return enrollment.hold_reason
    reason = hold_reason_obj(code, fallback=fallback)
    enrollment.hold_reason = reason
    if save:
        enrollment.save(update_fields=["hold_reason"])
    return reason


def clear_hold_reason(enrollment, *, save=True):
    """Drop the reason on the way back to service.

    A reason left on a SERVING enrollment reads as a current problem, and any hold
    queue filters on this field.
    """
    if enrollment is None or not enrollment.hold_reason_id:
        return
    enrollment.hold_reason = None
    if save:
        enrollment.save(update_fields=["hold_reason"])


def reason_for_ineligibility(client):
    """Which hold reason an import-time ineligibility maps to.

    ⚠ ONE NOTE COVERS FOUR GATES. "Auto-hold: member marked Ineligible by import"
    is written for an expired insurance, a missing insurance, an unserved Medicaid
    plan type AND an out-of-coverage ZIP -- so the note cannot tell them apart and
    the stored reasons have to. Measured on the clone:

        16,898  Medicaid plan type not served     -> medicaid_type_not_served
         3,622  insurance expired / missing       -> insurance_invalid
         3,122  ZIP outside the coverage area     -> zip_out_of_coverage
             7  state not served                  -> zip_out_of_coverage

    Anything else stays Uncategorized rather than being guessed at.
    """
    stored = " ".join(
        getattr(client, "ineligible_reasons", None) or []
    ).lower()
    if "medicaid plan type" in stored:
        return MEDICAID_TYPE_NOT_SERVED
    if "insurance" in stored:
        return INSURANCE_INVALID
    if "outside the coverage area" in stored or "is not served" in stored:
        return ZIP_OUT_OF_COVERAGE
    return UNCATEGORIZED
