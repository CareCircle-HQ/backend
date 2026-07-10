"""Williamsburg exception: fast-track an ext verification request straight to
Service Active.

Williamsburg clients (``Client.is_williamsburg``, derived from
``lead_source == "Williamsburg"``) skip the normal manual pipeline
(pending verification -> verified -> kitchen assignment -> service active).
When the agent requests a verification from the extension, we instead apply the
whole assignment in one shot:

  * ensure the household (primary + members),
  * give every member the same dietary profile -- ``Kosher`` menu, Pork +
    Shellfish carried as kitchen meal type / food notes, kept ACTIVE (the
    standard meal rule would push Kosher + multi-allergy Out of Orbit; that is
    intentionally bypassed here),
  * assign the Williamsburg kitchen,
  * default the delivery address to the primary's current address when none is
    set (shared by the whole household),
  * stamp the verification facts, build the delivery schedule + calendar on a
    Mon/Thu cadence, and advance the enrollment to SERVICE_ACTIVE.

Purchase Orders remain a separate manual step (same as the normal flow).
"""
import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# The exception's fixed assignment values.
WILLIAMSBURG_KITCHEN_NAME = "Williamsburg"
WILLIAMSBURG_MENU_TYPE = "Kosher"
# The verification fields are explicitly empty -- these clients have no real
# dietary restrictions or food allergies. The Kosher pork/shellfish exclusion
# is NOT an allergy; it is carried to the kitchen via the meal type + notes.
WILLIAMSBURG_DIETARY_RESTRICTIONS = ["none"]  # "No restrictions"
WILLIAMSBURG_FOOD_ALLERGIES = ["none"]  # "None"
WILLIAMSBURG_KITCHEN_MEAL_TYPE = "Kosher"
WILLIAMSBURG_KITCHEN_FOOD_NOTES = "Pork Free, Shellfish Free"
# MenuCategory has no "Kosher"; the standard/regular meal category is Fresh Meal.
WILLIAMSBURG_MEAL_CATEGORY = "fresh_meal"


def _primary_current_address(client):
    """The client's Current address (fallback: any address), or None."""
    from api.models import AddressType

    addresses = list(client.addresses.all())
    if not addresses:
        return None
    for addr in addresses:
        if addr.type == AddressType.CURRENT:
            return addr
    return addresses[0]


@transaction.atomic
def fast_track_williamsburg_enrollment(enrollment, *, actor=None, agent=None):
    """Apply the Williamsburg exception to ``enrollment`` and activate it.

    ``actor`` is the requesting ``User`` (recorded on the StageEvents); ``agent``
    is the requesting ``Agent`` (recorded as ``verified_by``). Both optional.

    Idempotent-ish: safe to call on a freshly created (Pending Verification)
    enrollment. Returns the refreshed enrollment.
    """
    from api.models import (
        DeliveryCadence,
        EnrollmentStage,
        Kitchen,
        MemberDietaryProfile,
        MemberStatus,
    )
    from api.serializers import ensure_household_with_primary
    from api.portal.serializers import primary_case
    from api.services.delivery import create_member_delivery_schedules
    from api.services.lifecycle import ENROLLMENT_TRANSITIONS, advance_enrollment
    from api.services.orders import generate_delivery_calendar

    client = enrollment.client
    if client is None:
        return enrollment

    # 1. Household (primary + members) is the source of the participant list.
    household = ensure_household_with_primary(client)
    if enrollment.household_id is None:
        enrollment.household = household

    # 2. Delivery address: keep an explicit one, else default to the primary's
    #    current address (shared by the whole household).
    if enrollment.delivery_address_id is None:
        addr = _primary_current_address(client)
        if addr is not None:
            enrollment.delivery_address = addr

    # 3. Williamsburg kitchen + verification facts + Mon/Thu cadence.
    kitchen = Kitchen.objects.filter(name__iexact=WILLIAMSBURG_KITCHEN_NAME).first()
    enrollment.kitchen = kitchen
    enrollment.is_family_verified = True
    enrollment.medicaid_type_verified = True
    enrollment.delivery_address_verified = True
    enrollment.verified_at = timezone.now()
    if agent is not None:
        enrollment.verified_by = agent
    if not enrollment.delivery_weekdays:
        enrollment.delivery_weekdays = ["mon", "thu"]
    enrollment.save()

    # 4. Per-member dietary: rebuild from the household so every member gets the
    #    same Kosher rules and stays ACTIVE. (No prior schedules reference these
    #    yet, so a clean rebuild is safe.)
    enrollment.member_profiles.all().delete()
    for hm in household.members.select_related("client").all():
        member = hm.client
        if member is None:
            continue
        MemberDietaryProfile.objects.create(
            enrollment=enrollment,
            client=member,
            member_name=f"{member.first_name} {member.last_name}".strip(),
            menu_type=WILLIAMSBURG_MENU_TYPE,
            dietary_restrictions=list(WILLIAMSBURG_DIETARY_RESTRICTIONS),
            food_allergies=list(WILLIAMSBURG_FOOD_ALLERGIES),
            meal_category=WILLIAMSBURG_MEAL_CATEGORY,
            status=MemberStatus.ACTIVE,
            kitchen_meal_type=WILLIAMSBURG_KITCHEN_MEAL_TYPE,
            kitchen_food_notes=WILLIAMSBURG_KITCHEN_FOOD_NOTES,
        )

    # 5. Verified -> (skip the waiting) -> Service Active. Build the per-member
    #    delivery plan + dated calendar in between (same work the manual
    #    kitchen-assignment step does); PO generation stays separate.
    case = enrollment.case or primary_case(client)
    # Advance to VERIFIED only if the enrollment hasn't already passed it. A
    # freshly-created (Pending Verification) enrollment moves up; an enrollment
    # already at/after Kitchen Assignment can't move BACK to Verified (the
    # transition map is forward-only), so we skip straight to the activation
    # steps below. The verification facts were already stamped above regardless.
    if EnrollmentStage.VERIFIED in ENROLLMENT_TRANSITIONS.get(enrollment.stage, set()):
        advance_enrollment(
            enrollment, EnrollmentStage.VERIFIED, actor=actor, force=True,
            note="Williamsburg exception: auto-verified on extension request.",
        )
    create_member_delivery_schedules(
        enrollment, case=case, cadence=DeliveryCadence.MON_THU, kitchen=kitchen,
    )
    generate_delivery_calendar(enrollment)
    # Activate unless already Service Active (avoid an illegal self-transition on
    # an enrollment that somehow reached Service Active without a kitchen).
    if enrollment.stage != EnrollmentStage.SERVICE_ACTIVE:
        advance_enrollment(
            enrollment, EnrollmentStage.SERVICE_ACTIVE, actor=actor, force=True,
            note=f"Williamsburg exception: activated directly ({WILLIAMSBURG_KITCHEN_NAME}).",
        )
    enrollment.refresh_from_db()
    return enrollment
