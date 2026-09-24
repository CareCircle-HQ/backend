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

def latest_eligible_services(rows):
    """The UNION of eligible_services across every record sharing the latest date.

    ⚠ NOT the services of one "latest" row, and this is the important part.
    ``screen_created_at`` is a DATE for most assessments -- 6,148 of 8,000 sampled sit
    at local midnight, because the source supplies a date and it is stored as a
    datetime. So the only ordering field cannot separate two records from the same
    day, and picking "the last one" picks whatever order Postgres returned.

    That was not theoretical. 581 members have several assessments tied on the latest
    date, 345 of them DISAGREEING about the food service, and it held 21 members
    arbitrarily -- 13 as "Wrong Case Type", 8 as "Not an Enhanced Member". BRYSON
    BRENTTURNER had two assessments dated 2026-09-14, one naming meals and one naming
    boxes; row order decided whether his food stopped.

    A tie is not a re-determination. Two assessments recorded on the same day are two
    records of that day's assessment, so the honest reading is BOTH: union them. It is
    also the only resolution that cannot stop a member's food on a coin flip.

    Records from EARLIER dates are still invalid -- rule 1 is unchanged. Only the tie
    is resolved differently.
    """
    dated = [r for r in rows if r.eligible_services]
    if not dated:
        return set()
    latest = max(
        (r.screen_created_at for r in dated if r.screen_created_at is not None),
        default=None,
    )
    if latest is None:
        # Nothing is dated: union them all rather than trusting row order.
        tied = dated
    else:
        tied = [r for r in dated if r.screen_created_at == latest]
    services = set()
    for row in tied:
        services |= set(row.eligible_services or [])
    return services


def _latest_with_services(rows):
    """The most recent record that actually lists services, or None.

    "Most recent" rather than every record ever: the latest reflects what the member
    is eligible for NOW. It has a cost -- 229 of 53,678 members have an OLDER
    screening naming a domain the newest does not, so those members show one fewer
    track than the union of their history would give. That is the intended reading;
    the tracker works from the most recent record only.
    """
    dated = [r for r in rows if r.eligible_services]
    if not dated:
        return None
    dated.sort(key=lambda r: (r.screen_created_at is None, r.screen_created_at))
    return dated[-1]


def gather(client):
    """Everything the rules need, read once."""
    screening = _latest_with_services(list(client.screenings.all()))
    assessment = _latest_with_services(list(client.assessments.all()))
    screened = list((screening.eligible_services if screening else []) or [])
    # The UNION across every assessment tied on the latest DATE -- see
    # latest_eligible_services. Reading one arbitrarily-chosen row made the food rule
    # and the rule-4 warning depend on Postgres row order, because screen_created_at
    # is date-only for 77% of assessments.
    eligible = sorted(latest_eligible_services(list(client.assessments.all())))

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
        "live_cases": cases,
        # ALL of them, not just the live ones: a scheduled reauthorization is not
        # yet serving, which is the whole point of it.
        "all_cases": list(client.cases.all()),
        "ecm": ECM in eligible,
        # Where the member LIVES, for the borough check on every row.
        "home_borough": member_home_borough(client),
        # The member, so detect_alerts can call evaluate_governing_case rather than
        # restating its conditions -- see _internal_service_rule_alerts.
        "client": client,
    }


def _find_case(cases, service_type, case_type=None):
    for case in cases:
        if (case.service_type or "") != service_type:
            continue
        if case_type and (case.case_type or "") != case_type:
            continue
        return case
    return None


# ── the borough check ────────────────────────────────────────────────────────
# WHERE A MEMBER BELONGS comes from their SOCIAL CARE COVERAGE, not their address.
#
# The coverage names the borough that is paying:
#
#     Public Health Solutions - Brooklyn NY1115 Enhanced HRSN Services
#                               ^^^^^^^^
#
# and that is the borough a case must be opened in, whatever address the member
# happens to live at. An address can be stale, a mailing address, or simply
# somewhere else -- the coverage is the contractual answer.
#
# ⚠ ENROLLED, AND "Enhanced HRSN Services" SPECIFICALLY. A member may also hold
# "MCO Screening and Navigation" or "FFS Screening and Navigation" coverage naming
# the same borough, and those are NOT accepted. The consequence is large and worth
# knowing: of 3,000 sampled members with any social care coverage, 1,799 have no
# ENROLLED Enhanced HRSN row, so their borough is now UNKNOWN and the check is
# silent for them. Almost all of those hold a Screening and Navigation plan that
# names a borough -- 1,491 of them enrolled -- so widening the rule would answer
# most of them if that is ever wanted.
_HRSN_PLAN = re.compile(
    r"^Public Health Solutions - (.+?) NY1115 Enhanced HRSN Services$", re.I,
)


def member_home_borough(client):
    """The borough from the member's ENROLLED Enhanced HRSN coverage, or "".

    Not the address. The plan names the borough that is paying for the service, and
    that is the one a case has to be opened in.

    Only a borough the ZIP table knows is accepted, so the three non-NYC regions
    that use the same plan shape -- Hudson Valley, Long Island, Southern Tier, 57
    rows between them -- answer "" rather than being reported as a borough we
    could compare a programme against.
    """
    from .service_area import service_boroughs

    known = service_boroughs()
    for coverage in client.social_care_coverages.all():
        if (coverage.status or "").strip().lower() != "enrolled":
            continue
        match = _HRSN_PLAN.match((coverage.plan_name or "").strip())
        if not match:
            continue
        borough = match.group(1).strip()
        if borough in known:
            return borough
    return ""


def _programme_borough(program_name):
    """The borough a programme name names, or "".

    Reads ActiveProgram.borough first -- decoded once by migration 0282 against the
    ServiceZipCode list -- and falls back to the same name-tail parse for a case
    whose programme is not in our table, which is most external ones.
    """
    from ..models import ActiveProgram

    name = (program_name or "").strip()
    if not name:
        return ""
    program = ActiveProgram.objects.filter(program_name=name).only("borough").first()
    if program is not None:
        return program.borough or ""
    parts = [p.strip() for p in name.split(" - ")]
    candidate = parts[-1] if len(parts) >= 2 else ""
    from .service_area import service_boroughs

    return candidate if candidate in service_boroughs() else ""


def _item(label, done, *, case=None, program="", detail="", state=None,
          home_borough=""):
    """One line in a track. ``state`` overrides the done/todo pair for the cases
    that are neither -- a recommendation waiting on an assessment, say."""
    # THE BOROUGH THIS ROW IS IN, and whether it matches where the member lives.
    # A case opened in the wrong borough is billed against the wrong programme, and
    # nothing else on the profile compares the two.
    #
    # `match` is deliberately THREE-VALUED: None means we could not tell -- no
    # address ZIP, or a programme with no borough in its name -- and painting that
    # red would accuse good data of being wrong.
    row_borough = _programme_borough(
        (case.program_name if case is not None else "") or program
    )
    match = None
    if row_borough and home_borough:
        match = row_borough == home_borough
    return {
        "label": label,
        "state": state or ("done" if done else "todo"),
        "detail": detail,
        "program_name": program,
        "case_id": str(case.case_id) if case is not None else "",
        "case_status": (case.case_status or "") if case is not None else "",
        "case_program": (case.program_name or "") if case is not None else "",
        "borough": row_borough,
        "borough_match": match,
    }


# ── the rules ────────────────────────────────────────────────────────────────

def rule_0_care_management(ctx):
    """ECM -> BOTH a care management case and an eligibility case, each live.

    They are two different pieces of work and a member needs both. Both carry
    ``service_type = "Social Service Case Management"``, so the only thing telling
    them apart is ``case_type``:

        ELIGIBILITY category     9 programmes   57,388 cases   case_type eligibility
        Care Management category 5 programmes  108,520 cases   case_type navigation

    ⚠ THE CARE MANAGEMENT CASE HAS case_type "navigation". The programme category
    says Care Management and the case type says navigation, which do not sound like
    the same thing -- but the split is exact on every one of the 165,908 cases, so
    case_type is the reliable discriminator and this is what it looks like.
    """
    if not ctx["ecm"]:
        return None

    care = _find_case(
        ctx["live_cases"], "Social Service Case Management", case_type="navigation",
    )
    eligibility = _find_case(
        ctx["live_cases"], "Social Service Case Management", case_type="eligibility",
    )
    return {
        "code": "core",
        "label": "Care Management Case",
        "rule": "Rule 0",
        "items": [
            _item(
                "Care Management Case", care is not None, case=care,
                detail="Category: Care Management",
                home_borough=ctx["home_borough"],
            ),
            _item(
                "Eligibility Case", eligibility is not None, case=eligibility,
                detail="Category: Eligibility",
                home_borough=ctx["home_borough"],
            ),
        ],
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

    # WHERE THE ASSESSMENT ACTUALLY IS. The Unite Us CASE existing is what Rule 1a
    # asks for, but it is not what an agent wants to know -- the work happens on our
    # DISPATCH ORDER, and "the case exists" says nothing about whether a vendor has
    # been, or can even be sent.
    #
    # Out of range OUTRANKS the case: an order we are withholding cannot progress
    # however many cases exist, and that is the one state with an action attached.
    from . import dispatch as dispatch_svc

    order = dispatch_svc.assessment_order_for(client)
    state, detail = _assessment_progress(order, dwelling, dispatch_svc)
    items = [_item(
        "Environmental Exposure Assessment",
        dwelling is not None,
        case=dwelling,
        state=state,
        detail=detail,
        home_borough=ctx["home_borough"],
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

    # No order means nothing to wait FOR -- "awaiting the vendor's assessment" beside
    # an assessment nobody has ordered points at the wrong party.
    if dwelling is None or order is None:
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
            # A ticked row with no reason is less use than it looks. "Opened" and
            # not "open": the check is whether a case for this product EXISTS, in
            # any status -- a closed remediation case means the work was done, and
            # recommending it again would have an agent open a duplicate.
            items.append(_item(
                rec.get("program_item") or "Unpriced recommendation",
                already,
                program=program,
                state=None if already else state,
                detail="Case already opened" if already else detail,
                home_borough=ctx["home_borough"],
            ))
        if not recs:
            items.append(_item(
                "Recommended cases", True,
                detail="The assessment recommended none",
            ))
    return {
        "code": "housing",
        "label": "Housing Program",
        "rule": "Rule 1",
        "items": items,
    }


def _last_ended_food_case(ctx):
    """The most recently CLOSED food case, when none is live.

    Read from ``all_cases`` rather than ``live_cases``: the point is to show that a
    programme existed and has finished, which no live-only view can say. Ordered by
    close date so a member with several closed cases reports the last one -- that is
    the programme that "ended", not their first ever.
    """
    candidates = [
        case for case in ctx["all_cases"]
        if (case.service_type or "") in (
            "Medically Tailored Meals", "Produce Prescription/Voucher",
        )
        and (case.case_status or "") not in LIVE_STATUSES
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda c: (
            c.case_closed_at or c.case_created_at or timezone.now().replace(
                year=1970,
            ),
        ),
    )


def rule_2_and_3_food(ctx):
    """Screened for Food + ECM -> a FOOD CASE. Meals or boxes; either satisfies it.

    ⚠ REBUILT. This used to expect a SPECIFIC service -- a voucher case when the
    assessment named Food Prescriptions, a meals case when it named MTM or CAM --
    and flagged the other as the wrong case. The authorisation data says that is
    simply not how the programme works:

        meals-eligible, holding a BOX case      219    218 approved
        boxes-eligible, holding a MEALS case    103    102 approved
        NO food eligibility, holding either     217    212 approved

    320 of 322 "wrong service" cases are APPROVED BY THE PAYER, and so are 212 of
    the 217 whose assessment names no food service at all. Nor is it a timing
    artefact: 198 of those cases were opened AFTER the assessment that supposedly
    forbids them.

    So the assessment's service list does not gate which food case may be opened.
    A food-eligible member may hold meals or boxes, and may move between them --
    106 of the 219 had a meals case at some point, though 113 never did and 25 hold
    both at once, so "they switch" only describes part of it.

    What the tracker can honestly say is whether a food case EXISTS.
    """
    if "Food" not in ctx["screened_domains"] or not ctx["ecm"]:
        return None

    eligible = set(ctx["eligible"])
    meals = _find_case(ctx["live_cases"], "Medically Tailored Meals")
    boxes = _find_case(ctx["live_cases"], "Produce Prescription/Voucher")
    case = meals or boxes
    ended = _last_ended_food_case(ctx) if case is None else None

    if not (eligible & (set(MEALS) | set(FOOD_PRESCRIPTION))):
        # No food service named at all. Normally the track is absent rather than
        # showing a to-do: 116 such members hold an APPROVED food case, so demanding
        # one would be inventing a requirement the programme does not have.
        #
        # ⚠ BUT NOT WHEN THERE IS A FOOD CASE TO SHOW. A member who was served and
        # whose programme has since closed had the track disappear entirely, so the
        # profile gave no sign a food programme had ever existed. 6 members on the
        # clone, and the one that surfaced it was screened for Food with an APPROVED
        # meals case closed in September.
        if case is None and ended is None:
            return None

    # Which one they actually have, because "a food case" is the rule but an agent
    # still wants to know whether it is meals or boxes.
    if meals and boxes:
        detail = "Meals and voucher cases are both open"
    elif meals:
        detail = "Medically Tailored Meals"
    elif boxes:
        detail = "Produce Prescription / Voucher"
    elif ended is not None:
        # ⚠ ENDED, NOT OUTSTANDING. Before this, 1,461 members whose food programme
        # had closed showed "Food service case — to-do", which reads as "nobody ever
        # opened one". They did; it ran and finished. Saying "open this case" on all
        # of them would be both wrong and the loudest thing on the tracker.
        closed_on = getattr(ended, "case_closed_at", None)
        detail = (
            f"Service ended — {ended.service_type}"
            + (f", closed {closed_on.date().isoformat()}" if closed_on else "")
        )
    else:
        detail = "Meals or voucher — either satisfies this"

    # "ended" is its own state so the UI can render it as history rather than as
    # either a success or a task, and the header badge counts only todo + blocked.
    row_state = "ended" if (case is None and ended is not None) else None
    items = [{
        **_item(
            "Food service case", case is not None,
            case=case or ended, detail=detail, state=row_state,
            home_borough=ctx["home_borough"],
        ),
        "rule": "Rules 2 & 3",
    }]
    items += _scheduled_reauth_rows(ctx, "Produce Prescription/Voucher")
    items += _scheduled_reauth_rows(ctx, "Medically Tailored Meals")

    return {
        "code": "food",
        "label": "Food Program",
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
            rule_0_care_management(ctx),
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
        # So the UI can name the borough it is comparing against, rather than
        # showing a red chip with nothing to compare it to.
        "home_borough": ctx["home_borough"],
        "alerts": detect_alerts(ctx),
        "tracks": tracks,
    }


def _assessment_progress(order, dwelling_case, dispatch_svc):
    """``(state, detail)`` for the dwelling-assessment row.

    Reports the ORDER's progress rather than only whether a case exists, because
    "Pending Schedule" and "the vendor has been and submitted" are the same thing to
    a rule that only checks for a case.
    """
    from ..models import DispatchStatus

    if order is None:
        # NOT "done", even when the Unite Us case exists. Rule 1a asks for the case
        # and the case may well be open -- but nobody has raised the work order, so
        # no vendor is going, and a green tick beside "no assessment order raised
        # yet" is a contradiction an agent would have to reason past.
        #
        # Both facts are stated, because they are genuinely different: the case being
        # open is progress, and the missing order is the next action.
        if dwelling_case is not None:
            return ("todo", "Case open — no assessment order raised yet")
        return ("todo", "No assessment order raised yet")

    # Checked FIRST: a withheld order cannot move, whatever its status says, and it
    # is the only one of these an agent can fix. Same helper the vendor API uses, so
    # the tracker cannot claim an order was sent when it was not.
    withheld = dispatch_svc.not_dispatchable_reason(order)
    if withheld:
        return ("blocked", f"Out of range — {withheld}")

    if order.status == DispatchStatus.CANCELLED:
        return ("blocked", "The assessment order was cancelled")

    # A visit that is booked is waiting on someone else, not on us.
    if order.status == DispatchStatus.PENDING_SCHEDULE:
        return ("waiting", "Sent to the vendor — awaiting scheduling")
    if order.status == DispatchStatus.CONFIRMED:
        visit = next(
            (v for v in order.visits.all() if v.scheduled_for), None,
        )
        when = f" for {visit.scheduled_for:%d %b}" if visit else ""
        return ("waiting", f"Visit confirmed{when}")
    if order.status == DispatchStatus.PENDING_SUBMISSION:
        return ("waiting", "Visited — awaiting the vendor's submission")

    # SUBMITTED / UPLOADED: the vendor's work is done. The row's done/to-do still
    # comes from the CASE, because that is what Rule 1a requires -- so a submitted
    # assessment with no case reads as outstanding, which it is.
    label = order.get_status_display()
    if dwelling_case is None:
        return (None, f"Assessment {label.lower()} — the case still needs opening")
    return (None, f"Assessment {label.lower()}")


def _scheduled_reauth_rows(ctx, service_type):
    """A row for a REAUTHORIZATION that is approved but has not started yet.

    Shown BELOW the governing case it extends, because that is what it is -- the
    same service continuing, not a second service. Without it an agent sees a case
    whose window is about to end and no sign that the next one is already approved,
    which is exactly when somebody opens a duplicate.

    Defers to ``lifecycle.deferred_extension_case_ids``, the same helper that stops
    a future-dated extension supplanting the serving case. Recomputing "is it
    scheduled?" here would be a second opinion on a rule with a whole design
    document behind it.
    """
    from .lifecycle import deferred_extension_case_ids

    cases = ctx["all_cases"]
    try:
        deferred = deferred_extension_case_ids(cases)
    except Exception:  # noqa: BLE001 - the track must still render
        logger.warning("service_tracker: deferred extensions failed")
        return []

    rows = []
    for case in cases:
        if case.case_id not in deferred:
            continue
        if (case.service_type or "") != service_type:
            continue
        start = case.service_authorization_approval_starts_at
        when = f"starts {start:%d %b %Y}" if start else "start date not set"
        rows.append(_item(
            "Reauthorization scheduled",
            False,
            # "waiting", not to-do: it is approved and activates on its own date.
            # Marking it outstanding would have an agent chasing work that is done.
            state="waiting",
            case=case,
            home_borough=ctx["home_borough"],
            # Only the FIRST letter: str.capitalize() lower-cases the rest and
            # turned "starts 25 Oct 2026" into "Starts 25 oct 2026".
            detail=when[:1].upper() + when[1:],
        ))
    return rows


# ── bad scenarios ────────────────────────────────────────────────────────────
# Things that are WRONG rather than merely unfinished. A to-do row says "this still
# needs doing"; an alert says "what is already recorded does not add up".
#
# All four are rare on real data -- 0, 1, 1 and 6 in a 600-member sample -- which is
# what makes a red banner the right weight for them. A warning that fires on a third
# of members gets scrolled past, and then it is worse than nothing.
#
# NOT INCLUDED: "screened for Housing, qualified, but no dwelling case". That is 6%
# of members and already the first row of the housing track, in amber, saying which
# half is missing. Repeating it as an alert would double-count the commonest state in
# the tracker and teach an agent to ignore the banner.

def _alert(code, title, detail, *, severity="error", program=""):
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "program_name": program,
    }


# What the internal-service rules decide, restated for the panel.
#
# ⚠ DRIVEN BY evaluate_governing_case ITSELF, not by a second copy of its
# conditions. If the panel restated them it could drift from the rule, and the
# failure mode is the worst one available here: the tracker saying a member is fine
# while their deliveries are stopped. That is exactly the state 96 held households
# were in.
_RULE_ALERTS = {
    "not_enhanced_member": (
        "Not qualified for Enhanced Care Management (Level 2)",
        "The latest eligibility assessment does not include ECM Level 2, so the "
        "member is not entitled to internal services.",
    ),
    "wrong_case_type": (
        "The open case is not the service the assessment permits",
        "The latest eligibility assessment names a produce prescription and not "
        "medically tailored meals, so a meals case cannot be served.",
    ),
}


def _internal_service_rule_alerts(ctx):
    """Rules 2 and 3, as panel alerts."""
    client = ctx.get("client")
    if client is None:
        return []
    # Imported here, not at module scope: internal_service_rules imports
    # latest_eligible_services from THIS module.
    from api.services.internal_service_rules import evaluate_governing_case

    try:
        verdict = evaluate_governing_case(client)
    except Exception:  # noqa: BLE001 - the panel must render even if a rule fails
        logger.exception("internal-service rule alert failed for %s", client.pk)
        return []
    if verdict is None:
        return []
    code, _reason = verdict
    titles = _RULE_ALERTS.get(code)
    if titles is None:
        return []
    title, detail = titles

    # WHAT ACTUALLY HAPPENED to this member, because the rule's verdict alone no
    # longer says: a member in service is HELD, one earlier in the funnel gets a
    # TICKET, and a terminal enrollment gets neither. Saying "the programme is On
    # Hold" to someone whose programme is not held would be its own wrong answer.
    from api.models import EnrollmentStage
    from api.services.internal_service_rules import TICKETABLE_STAGES

    # ⚠ ASK "IS IT HELD?" FIRST, before looking at anything else. Reading only the
    # non-held enrollments made a member with one ON_HOLD and one CLOSED enrollment
    # look terminal, so the alert dropped the outcome sentence entirely while their
    # deliveries were stopped. Which enrollments exist BESIDES the hold says nothing
    # about whether the hold is there.
    stages = {e.stage for e in client.enrollments.all()}
    if EnrollmentStage.ON_HOLD in stages:
        outcome = " The programme is On Hold."
    elif EnrollmentStage.SERVICE_ACTIVE in stages:
        # Found by the rule but not yet applied -- the next case write holds it.
        outcome = " The programme will be held on the next case save."
    elif stages & TICKETABLE_STAGES:
        outcome = " A ticket has been raised; the programme is not held."
    else:
        # Closed, cancelled, disregarded or superseded: neither held nor ticketed.
        outcome = " No programme is in service, so nothing was held."
    return [_alert(code, title, detail + outcome, severity="error")]


def detect_alerts(ctx):
    """What does not add up about this member's screening, eligibility and cases."""
    alerts = []
    domains = ctx["screened_domains"]
    eligible = set(ctx["eligible"])

    # A NEED WITH NO QUALIFICATION. The member was screened as needing something we
    # provide, but the assessment did not qualify them for ECM Level 2 -- so no case
    # can be opened at all, and every track is absent. Without this the tracker
    # would simply show nothing, which reads as "nothing to do here".
    if not ctx["ecm"]:
        # ⚠ RULE 2 FIRST, and NOT gated on the screened domains. A member held as
        # "Not an Enhanced Member" who was never screened for Housing or Food got
        # neither an alert nor a single track -- an entirely EMPTY panel beside a
        # stopped programme. 50 held households read that way.
        alerts.extend(_internal_service_rule_alerts(ctx))
        for domain in ("Housing", "Food"):
            if domain in domains:
                alerts.append(_alert(
                    f"no_ecm_{domain.lower()}",
                    f"Screened for {domain}, but not qualified for ECM Level 2",
                    f"No {domain.lower()} case can be opened without it. Either the "
                    f"eligibility assessment is missing or incomplete, or the "
                    f"screened need cannot be served.",
                    severity="warning",
                ))
        return alerts

    # RULE 4 -- meals-only eligibility holding a BOXES case. A WARNING, never a hold.
    #
    # ⚠ The distinction is the whole point. Rule 3 (boxes-only eligibility + a MEALS
    # case) HOLDS the programme in internal_service_rules.py, because stepping UP to
    # the expensive service without eligibility is a real problem. This one does not,
    # because the payer plainly disagrees that it is one:
    #
    #     meals-eligible members holding a BOXES case   219   218 APPROVED, 0 denied
    #     ... every one on a "Food Prescriptions: Boxes" programme, none on Voucher
    #     ... 106 previously held a meals case -- a deliberate switch
    #
    # So it is surfaced for review and nobody's deliveries stop. 196 governing cases
    # on the clone.
    if "Food" in domains:
        meals_only = bool(eligible & set(MEALS)) and not (
            eligible & set(FOOD_PRESCRIPTION)
        )
        boxes_case = _find_case(ctx["live_cases"], "Produce Prescription/Voucher")
        if meals_only and boxes_case is not None:
            alerts.append(_alert(
                "boxes_case_meals_only",
                "Boxes case open, but the assessment names meals only",
                "The latest eligibility assessment permits medically tailored meals "
                "and not a produce prescription. Worth checking — though this is "
                "common and usually approved, so the programme is NOT held.",
                program=boxes_case.program_name or "",
                severity="warning",
            ))

    # RULE 3 -- boxes-only eligibility holding a MEALS case. This one DOES hold, and
    # until now the panel said nothing about it: a member whose deliveries were
    # stopped showed "[done] Food service case" and no alert at all. 96 held
    # households.
    alerts.extend(_internal_service_rule_alerts(ctx))

    # ⚠ THE OPPOSITE ALERT WAS REMOVED, and deliberately so.
    #
    # They fired when a member held a meals case with voucher eligibility, or the
    # reverse. The authorisation data shows both are normal:
    #
    #     meals-eligible, holding a BOX case      219    218 approved
    #     boxes-eligible, holding a MEALS case    103    102 approved
    #
    # 320 of 322 approved by the payer. An alert that calls 320 approved cases an
    # error is not a check, it is noise -- and it would have been the loudest thing
    # on the tracker.
    #
    # I built them from the eligibility strings without ever asking whether the
    # payer agreed. Approval was one query away and it answers the question
    # outright. Before adding another rule of this shape, check the authorisation
    # status first.
    return alerts
