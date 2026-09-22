"""What has been done for a member, and what still needs doing.

A rule engine over the screening, the eligibility assessment and the member's
cases. It answers the question an agent opens a profile to ask -- "what is
outstanding here?" -- which otherwise means reading four tabs and knowing the rules
by heart.

IT ONLY REPORTS. Nothing here creates a case: cases are opened in Unite Us and we
learn about them on the next import, so a track that says "open this" names the
programme and stops there.

THE CASE SERVICE TYPES ARE THE STRINGS UNITE US SENDS, verified against all 23,630
internal-service cases rather than taken from our own enum, because the two do not
agree. Notably there is no "Clinically Appropriate Meals" case service type: all
3,893 cases on a CAM programme are filed as "Medically Tailored Meals".
"""
import logging
import re

# A LIVE case. A closed or cancelled case means the work was done once and is not
# in force now, which for a tracker of outstanding work reads as To-Do again.
# `draft` is excluded deliberately: an unfinished case is not a case.
logger = logging.getLogger(__name__)

# ── what counts as done ──────────────────────────────────────────────────────
LIVE_STATUSES = ("open", "managed", "pending_authorization", "off_platform")

# ── the eligibility results the rules turn on ────────────────────────────────
ECM = "Enhanced Care Management (Level 2)"

# Both spellings, everywhere. The suffixed form is what current exports send; the
# bare form appears on a few dozen older records and means the same thing. Matching
# only one would quietly drop them.
FOOD_PRESCRIPTION = (
    "Food Prescriptions (Voucher / Boxes) (Food)",
    "Food Prescriptions (Voucher / Boxes)",
)
MEALS = (
    "Medically Tailored Meals (MTM) (Food)",
    "Medically Tailored Meals (MTM)",
    "Clinically Appropriate Meals (Food)",
    "Clinically Appropriate Meals",
)

# ── domains ──────────────────────────────────────────────────────────────────
# Screening and assessment records store FULL SERVICE NAMES, not domain words, so
# "screened for Housing" has to be derived: the name's own "(Housing)" suffix where
# it has one, and a keyword otherwise. Two of the 25 screening values carry no
# suffix at all -- "Pre-tenancy Services" and "Cooking Supplies".
_SUFFIX = re.compile(r'\((Food|Housing|Transportation|Utilities)\)\s*$', re.I)
_KEYWORDS = (
    ("Housing", ("tenancy", "housing", "remediation", "asthma", "accessibility",
                 "respite", "rent")),
    ("Food", ("meal", "food", "produce", "grocer", "pantry", "nutrition",
              "cooking")),
    ("Transportation", ("transportation",)),
    ("Utilities", ("utility", "utilities")),
)


def domain_of(service):
    """The domain a service name belongs to, or ""."""
    match = _SUFFIX.search(service or "")
    if match:
        return match.group(1).title()
    low = (service or "").lower()
    for domain, words in _KEYWORDS:
        if any(word in low for word in words):
            return domain
    return ""


def domains_of(services):
    return {d for d in (domain_of(s) for s in services or []) if d}


# ── the records the rules read ───────────────────────────────────────────────

def _latest_with_services(rows):
    """The most recent record that actually lists services, plus whether an older
    one listed something different.

    "Most recent" rather than every record ever: the latest reflects what the
    member is eligible for NOW. It costs a little -- 229 of 53,678 members have an
    older screening naming a domain the newest does not -- so the difference is
    REPORTED rather than hidden.
    """
    dated = [r for r in rows if r.eligible_services]
    if not dated:
        return None, set()
    dated.sort(key=lambda r: (r.screen_created_at is None, r.screen_created_at))
    latest = dated[-1]
    earlier = set()
    for row in dated[:-1]:
        earlier |= set(row.eligible_services or [])
    return latest, earlier - set(latest.eligible_services or [])


def gather(client):
    """Everything the rules need, read once."""
    screening, screening_dropped = _latest_with_services(
        list(client.screenings.all())
    )
    assessment, assessment_dropped = _latest_with_services(
        list(client.assessments.all())
    )
    screened = list((screening.eligible_services if screening else []) or [])
    eligible = list((assessment.eligible_services if assessment else []) or [])

    cases = [
        c for c in client.cases.all()
        if (c.case_status or "") in LIVE_STATUSES
    ]
    return {
        "screening": screening,
        "assessment": assessment,
        "screened": screened,
        "eligible": eligible,
        "screened_domains": domains_of(screened),
        "dropped_from_older_screening": sorted(domains_of(screening_dropped)),
        "dropped_from_older_assessment": sorted(assessment_dropped),
        "live_cases": cases,
        "ecm": ECM in eligible,
    }


def _find_case(cases, service_type, case_type=None):
    for case in cases:
        if (case.service_type or "") != service_type:
            continue
        if case_type and (case.case_type or "") != case_type:
            continue
        return case
    return None


def _item(label, done, *, case=None, program="", detail="", state=None):
    """One line in a track. ``state`` overrides the done/todo pair for the cases
    that are neither -- a recommendation waiting on an assessment, say."""
    return {
        "label": label,
        "state": state or ("done" if done else "todo"),
        "detail": detail,
        "program_name": program,
        "case_id": str(case.case_id) if case is not None else "",
        "case_status": (case.case_status or "") if case is not None else "",
        "case_program": (case.program_name or "") if case is not None else "",
    }


# ── the rules ────────────────────────────────────────────────────────────────

def rule_0_core_case(ctx):
    """ECM -> a Social Service Case Management case in the ELIGIBILITY category.

    case_type matters here and is the whole rule. 132,777 cases carry this service
    type, but 108,080 are case_type "navigation" and only 24,697 are "eligibility".
    A navigation case does NOT satisfy this.
    """
    if not ctx["ecm"]:
        return None
    case = _find_case(
        ctx["live_cases"], "Social Service Case Management", case_type="eligibility",
    )
    return {
        "code": "core",
        "label": "Core Social Work Case",
        "rule": "Rule 0",
        "items": [_item(
            "Social Service Case Management",
            case is not None,
            case=case,
            detail="Category: Eligibility",
        )],
    }


def rule_1_housing(ctx, client):
    """Screened for Housing + ECM -> the dwelling assessment, then its
    recommendations.

    ECM ALONE is the trigger. The specific housing eligibility results (mould,
    ventilation, asthma, rent) are not preconditions -- they are what the dwelling
    assessment goes on to recommend.
    """
    if "Housing" not in ctx["screened_domains"] or not ctx["ecm"]:
        return None

    dwelling = _find_case(ctx["live_cases"], "Environmental Exposure Assessment")
    items = [_item(
        "Environmental Exposure Assessment",
        dwelling is not None,
        case=dwelling,
        detail="The dwelling case",
    )]

    # Rule 1b: only once the assessment is actually submitted. Listing
    # recommendations before then would be asking an agent to open cases for work
    # nobody has assessed.
    #
    # Resolved the same way MemberCaseRecommendationsView does -- the order, then
    # its questionnaire -- so the tracker and the Cases to Open tab cannot form
    # different opinions.
    from ..models import DispatchQuestionnaire
    from . import dispatch as dispatch_svc
    from .case_recommendations import recommended_cases

    recs = []
    submitted = False
    order = dispatch_svc.assessment_order_for(client)
    form = (
        DispatchQuestionnaire.objects.filter(dispatch_order=order).first()
        if order is not None else None
    )
    if form is not None and form.is_submitted:
        submitted = True
        try:
            recs = recommended_cases(form)
        except Exception:  # noqa: BLE001 - the track must still render
            logger.warning("service_tracker: recommendations failed for %s", client.pk)

    if dwelling is None:
        pass
    elif not submitted:
        items.append(_item(
            "Recommended cases",
            False,
            state="waiting",
            detail="Awaiting the vendor's assessment",
        ))
    else:
        for rec in recs:
            # exists / is_internal / already_open all come from the recommendation
            # engine, so the tracker agrees with the Cases to Open tab instead of
            # forming a second opinion.
            #
            # The three failures are DISTINCT because they need different fixes, and
            # collapsing them into "blocked" would tell an agent nothing actionable.
            already = rec.get("already_open", False)
            program = rec.get("program_name", "")
            if not rec.get("program_item"):
                state, detail = "blocked", "Recommended product has no programme"
            elif not rec.get("exists"):
                state, detail = "blocked", "No such programme — it must be created"
            elif not rec.get("is_internal"):
                state, detail = "blocked", "External Services — needs reclassifying"
            else:
                state, detail = (None, "")
            items.append(_item(
                rec.get("program_item") or "Unpriced recommendation",
                already,
                program=program,
                state=None if already else state,
                detail="" if already else detail,
            ))
        if not recs:
            items.append(_item(
                "Recommended cases", True,
                detail="The assessment recommended none",
            ))
    return {
        "code": "housing",
        "label": "Housing Services",
        "rule": "Rule 1",
        "items": items,
    }


def rule_2_and_3_food(ctx):
    """Screened for Food + ECM -> a voucher case and/or a meals case.

    One track, two rules: they share a trigger and an agent thinks of them as
    "the food side".
    """
    if "Food" not in ctx["screened_domains"] or not ctx["ecm"]:
        return None

    eligible = set(ctx["eligible"])
    items = []

    if eligible & set(FOOD_PRESCRIPTION):
        case = _find_case(ctx["live_cases"], "Produce Prescription/Voucher")
        items.append({
            **_item("Produce Prescription / Voucher", case is not None, case=case),
            "rule": "Rule 2",
        })

    if eligible & set(MEALS):
        case = _find_case(ctx["live_cases"], "Medically Tailored Meals")
        items.append({
            # Said explicitly because it looks wrong otherwise: a member eligible
            # for Clinically Appropriate Meals gets an MTM case, since Unite Us has
            # no CAM case service type.
            **_item(
                "Medically Tailored Meals (MTM)", case is not None, case=case,
                detail="Covers Clinically Appropriate Meals too",
            ),
            "rule": "Rule 3",
        })

    if not items:
        return None
    return {
        "code": "food",
        "label": "Food Services",
        "rule": "Rules 2 & 3",
        "items": items,
    }


def tracker_for(client):
    """The whole tracker: the gateway phase, then a track per applicable rule."""
    ctx = gather(client)

    screening = ctx["screening"]
    assessment = ctx["assessment"]
    tracks = [
        t for t in (
            rule_0_core_case(ctx),
            rule_1_housing(ctx, client),
            rule_2_and_3_food(ctx),
        ) if t
    ]
    return {
        "phase1": {
            "screening": {
                "done": screening is not None,
                "at": screening.screen_created_at if screening else None,
                # The domains we act on, in the order an agent reads them. Only
                # Food and Housing: we screen for four and take two, and listing
                # the other two as though we might act on them would mislead.
                "domains": [
                    d for d in ("Food", "Housing") if d in ctx["screened_domains"]
                ],
                "other_domains": sorted(
                    ctx["screened_domains"] - {"Food", "Housing"}
                ),
            },
            "assessment": {
                "done": assessment is not None,
                "at": assessment.screen_created_at if assessment else None,
                "services": ctx["eligible"],
            },
            "determination": {
                "done": ctx["ecm"],
                "label": "Qualified: ECM Level 2" if ctx["ecm"]
                         else "Not qualified for ECM Level 2",
            },
        },
        # Surfaced rather than dropped: the rules read the LATEST record, and for
        # 229 of 53,678 members an older screening named a domain the newest does
        # not. Silence would make the tracker look wrong to whoever remembers.
        "superseded": {
            "screening_domains": ctx["dropped_from_older_screening"],
            "assessment_services": ctx["dropped_from_older_assessment"],
        },
        "tracks": tracks,
    }
