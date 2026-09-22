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
        # ALL of them, not just the live ones: a scheduled reauthorization is not
        # yet serving, which is the whole point of it.
        "all_cases": list(client.cases.all()),
        "ecm": ECM in eligible,
        # Where the member LIVES, for the borough check on every row.
        "home_borough": member_home_borough(client),
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
# Which address decides where a member LIVES. The same set the eligibility gate
# judges, minus "mailing": a PO box in another borough says nothing about where the
# member is, and "current" first because that is the one the profile header shows --
# so the tracker and the profile cannot disagree about where somebody lives.
_HOME_ADDRESS_TYPES = ("current", "home", "delivery")


def member_home_borough(client):
    """The borough the member lives in, from their primary address ZIP, or "".

    Derived from ServiceZipCode -- the same table the service-area check and the
    programme borough decoder use, so all three agree about what a borough is.
    """
    from .service_area import housing_area_check

    addresses = list(client.addresses.all())
    for wanted in _HOME_ADDRESS_TYPES:
        for address in addresses:
            if (address.type or "").lower() == wanted and address.zip:
                borough = housing_area_check(address.zip)["borough"]
                if borough:
                    return borough
    # Any address with a usable ZIP, rather than nothing: a member with only a
    # mailing address still lives somewhere, and "unknown" disables the check.
    for address in addresses:
        if address.zip:
            borough = housing_area_check(address.zip)["borough"]
            if borough:
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
        "label": "Housing Program Progress",
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
            **_item(
                "Produce Prescription / Voucher", case is not None, case=case,
                home_borough=ctx["home_borough"],
            ),
            "rule": "Rule 2",
        })
        items += _scheduled_reauth_rows(ctx, "Produce Prescription/Voucher")

    if eligible & set(MEALS):
        case = _find_case(ctx["live_cases"], "Medically Tailored Meals")
        items.append({
            # Said explicitly because it looks wrong otherwise: a member eligible
            # for Clinically Appropriate Meals gets an MTM case, since Unite Us has
            # no CAM case service type.
            **_item(
                "Medically Tailored Meals (MTM)", case is not None, case=case,
                detail="Covers Clinically Appropriate Meals too",
                home_borough=ctx["home_borough"],
            ),
            "rule": "Rule 3",
        })
        items += _scheduled_reauth_rows(ctx, "Medically Tailored Meals")

    if not items:
        return None
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
        # Surfaced rather than dropped: the rules read the LATEST record, and for
        # 229 of 53,678 members an older screening named a domain the newest does
        # not. Silence would make the tracker look wrong to whoever remembers.
        "superseded": {
            "screening_domains": ctx["dropped_from_older_screening"],
            "assessment_services": ctx["dropped_from_older_assessment"],
        },
        # So the UI can name the borough it is comparing against, rather than
        # showing a red chip with nothing to compare it to.
        "home_borough": ctx["home_borough"],
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
