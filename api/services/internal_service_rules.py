"""Hold a programme whose GOVERNING case the eligibility assessment does not support.

Runs from ``reconcile_client_eligibility``, so it applies on BOTH the extension save
and the CSV import without new plumbing.

THE RULES
---------

**Rule 1 — the governing assessment.** Only the member's MOST RECENT eligibility
assessment that lists services counts. Every earlier one is invalid: a new assessment
is a re-determination, not an addition. A member who qualified for food under a
previous case and does not under the newest one is held.

**Rule 2 — Enhanced Care Management.** The latest assessment must include
``Enhanced Care Management (Level 2)``. Without it the member is not entitled to
internal services at all, so the programme is held as *Not an Enhanced Member*. This
is the gateway and is checked first; there is no point judging which food case is
right for a member who should have none.

**Rule 3 — boxes-only eligibility cannot hold a MEALS case.** When the assessment
names ``Food Prescriptions (Voucher / Boxes) (Food)`` and no meals service, a
Medically Tailored Meals governing case is the wrong case. Held as *Wrong Case Type
Open*.

⚠️ **Rule 4 is NOT here, deliberately.** The reverse — meals-only eligibility holding
a BOXES case — is a Service Tracker warning only, and does not hold the programme.
The authorisation data is why:

    meals-eligible members holding a BOXES case    219    218 APPROVED, 0 denied
    ... every one on a "Food Prescriptions: Boxes" programme, none on Voucher
    ... 106 previously held a meals case -- consistent with a deliberate switch

Meals eligibility permits stepping DOWN to boxes; boxes-only does not permit stepping
UP to meals. Holding the 196 governing boxes cases would stop deliveries for members
whose case the payer has approved.

SCOPE, and why each limit matters
---------------------------------

**The GOVERNING case only.** A member may hold several internal-service cases; only
the one in force says what they are being served under. Judging the others would hold
a household over a case nobody is delivering against.

**No assessment means NO VERDICT.** 6,002 of 17,028 members with a live governing
case -- 35% -- have no eligibility assessment at all. Reading silence as "not
qualified" would hold a third of the book on the next import.

**It only ever HOLDS.** No auto-resume yet: a held programme stays held until an agent
resumes it. That is deliberate -- releasing members automatically is a separate,
riskier piece.

Blast radius measured on the clone before this was written:

    rule 2, no ECM                       60 households    55 approved
    rule 3, boxes-only + a meals case    94 households    94 approved
    rule 4, FLAG ONLY                   196 households    (not held)
"""
import logging

logger = logging.getLogger(__name__)

ECM_RESULT = "Enhanced Care Management (Level 2)"

# Both spellings of every result. The bare forms appear on a few dozen older records
# and mean the same thing.
MEALS_RESULTS = {
    "Medically Tailored Meals (MTM) (Food)",
    "Medically Tailored Meals (MTM)",
    "Clinically Appropriate Meals (Food)",
    "Clinically Appropriate Meals",
}
BOXES_RESULTS = {
    "Food Prescriptions (Voucher / Boxes) (Food)",
    "Food Prescriptions (Voucher / Boxes)",
}

MEALS_CASE_SERVICE = "Medically Tailored Meals"
BOXES_CASE_SERVICE = "Produce Prescription/Voucher"

NOT_ENHANCED_REASON = (
    "Member is ineligible for company Internal services. Eligibility Assessment "
    "results show the member is not an Enhanced Care Management (Level 2)."
)
WRONG_CASE_MEALS_REASON = (
    "Only a Food Prescriptions (Voucher / Boxes) (Food) case is allowed, agent "
    "opened the wrong case."
)


def evaluate_governing_case(client):
    """``(hold_reason_code, reason_text)`` when the programme must be held, else None.

    Pure: reads the client's assessments and governing case and decides. No writes,
    so it can back a dry-run command and the tests without side effects.
    """
    from api.portal.serializers import internal_service_case
    from api.services.service_tracker import latest_eligible_services
    from api.services import hold_reasons as hr

    if client is None:
        return None

    governing = internal_service_case(client)
    if governing is None:
        return None

    # Rule 1: the MOST RECENT assessment date, and only that date. Earlier
    # assessments remain invalid.
    #
    # ⚠ THE UNION ACROSS EVERY ASSESSMENT SHARING THAT DATE, not one row of it.
    # screen_created_at is date-only for 77% of assessments (the source supplies a
    # date, stored at local midnight), so two records from the same day cannot be
    # ordered -- and taking "the last" took whatever order Postgres returned.
    #
    # It held 21 members on a coin flip: 13 as Wrong Case Type, 8 as Not an Enhanced
    # Member. BRYSON BRENTTURNER had two assessments dated 2026-09-14, one naming
    # meals and one naming boxes; row order decided whether his food stopped.
    eligible = latest_eligible_services(list(client.assessments.all()))
    if not eligible:
        return None                      # no assessment -> no verdict

    # Rule 2 first: the gateway. There is no point judging WHICH food case is right
    # for a member who is not entitled to internal services at all.
    if ECM_RESULT not in eligible:
        return hr.NOT_ENHANCED_MEMBER, NOT_ENHANCED_REASON

    # Rule 3: boxes-only eligibility cannot hold a meals case.
    if (
        eligible & BOXES_RESULTS
        and not (eligible & MEALS_RESULTS)
        and governing.service_type == MEALS_CASE_SERVICE
    ):
        return hr.WRONG_CASE_TYPE, WRONG_CASE_MEALS_REASON

    # ⚠️ Rule 4 -- meals-only eligibility holding a BOXES case -- is NOT a hold. See
    # the module docstring: 218 of 219 such cases are approved by the payer. It is
    # surfaced as a Service Tracker warning instead.
    return None


def apply_internal_service_rules(client, *, actor=None, actor_label="", source=None):
    """Hold the governing programme when the rules say so. Returns the held
    enrollments.

    Idempotent in the way that matters: an enrollment already On Hold is left alone,
    reason and all. Re-importing must not relabel a hold an agent has explained, and
    must not re-write the same StageEvent on every run.
    """
    from api.models import EnrollmentStage, NoteSource, Note
    from api.services.lifecycle import (
        ENROLLMENT_TRANSITIONS, InvalidTransition, _governing_enrollments,
        advance_enrollment,
    )
    from api.services.orders import truncate_future_deliveries

    verdict = evaluate_governing_case(client)
    if verdict is None:
        return []
    code, reason_text = verdict

    held = []
    for enr in _governing_enrollments(client):
        if EnrollmentStage(enr.stage) == EnrollmentStage.ON_HOLD:
            continue                     # already held -- leave it, reason and all
        if EnrollmentStage.ON_HOLD not in ENROLLMENT_TRANSITIONS.get(
            EnrollmentStage(enr.stage), set()
        ):
            continue
        # Truncate BEFORE the hold, matching the other off-ramps: an ON_HOLD
        # enrollment is skipped by the delivery writer, so the order matters.
        try:
            truncate_future_deliveries(enr)
        except Exception:  # pragma: no cover - defensive
            logger.warning("could not truncate deliveries for %s", enr.pk)
        try:
            advance_enrollment(
                enr, EnrollmentStage.ON_HOLD, actor=actor,
                actor_label=actor_label or "system: internal-service rules",
                note=f"Auto-hold: {reason_text}",
                hold_reason=code,
                trigger="internal_service_rules",
            )
        except InvalidTransition:
            continue
        held.append(enr)

    if held:
        # One note on the client, so an agent reading the profile sees the same
        # sentence the hold carries rather than having to open the history.
        Note.objects.create(
            client=client, source=NoteSource.SYSTEM,
            author_name="System",
            body=f"Program placed on hold. {reason_text}",
        )
    return held
