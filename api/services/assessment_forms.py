"""The Dwelling Assessment form: questions and intervention catalogue.

Transcribed from the three real forms (Mobility, Ventilation, Mobility +
Ventilation) exactly as the vendor's tool presents them.

The form is two MODULES plus a shared wrapper:

    Mobility (2.1)      17 questions,  7 intervention groups, 18 options
    Ventilation (2.2b)  16 questions,  5 intervention groups,  8 options

A combined assessment renders both modules in order. The module sections repeat --
"Reason for Assessment" genuinely appears twice on the combined form -- while the
wrapper (interventions, justification, notes, photos, signatures) appears once.
That is not a simplification; it is what the real PDFs do.

CODES ARE PERMANENT. Answers are stored keyed by them, so renaming one orphans
data. Labels may be corrected freely; a change of MEANING needs a new version and a
new code.

``TEMPLATE_VERSION`` is bumped whenever questions are added, removed or reworded in
a way that changes meaning. A submitted questionnaire freezes the schema it was
answered against, so a signed form renders years later exactly as it was signed
rather than acquiring blank questions from a later revision.
"""

TEMPLATE_VERSION = 1

# Module codes.
#
# "2.1" and "2.2b" are NYS MEDICAID 1115 WAIVER (NYHER) HRSN SERVICE CODES, not
# form labels -- they identify the billable service under the Social Care Network
# programme, which is why the form prints them beside each module
# ("Programs Approved: [X] Mobility (2.1)  [ ] Ventilation (2.2b)").
#
#   2.1   Home Accessibility and Safety Modifications
#         ramps, handrails, grab bars, electric door openers, widening of
#         doorways and pathways, door and cabinet handles, bathroom facilities,
#         kitchen cabinet or sinks, non-skid surfaces
#   2.2b  Home Remediation: Ventilation
#         air conditioners, heaters, humidifiers, dehumidifiers
#
# Source: NYS DOH HRSN Services protocol and the SCN eligibility summary
# (health.ny.gov/health_care/medicaid/redesign). The codes appear NOWHERE in our
# own data -- they arrive only on the vendor's form -- so this comment is the only
# place the mapping is written down.
#
# Two things follow, and both explain earlier puzzles:
#
#   * our programme families ARE these waiver services. "Home Accessibility and
#     Safety Modification - <item> - <borough>" is 2.1; "Home Remediation -
#     <device> - <borough>" is 2.2b. The item lists match the waiver's almost
#     exactly.
#   * the 2.1 service also covers ACCESSIBILITY RAMPS, PATHWAYS/doorway widening
#     and ELECTRIC DOOR OPENERS, for which we have no programme at all. That is
#     why the form offers Ramps and Pathways: they are funded services, not stray
#     options. Those programmes are pending.
#
# Other waiver services in the same family that we do NOT model yet:
#   2.2a  Home Remediation: Mold and Pest Remediation
#         (our referral-only "Home Remediation Assistance: Ventilation Improving
#          Systems" programme belongs here)
#   2.2c  Home Remediation: Equipment Provision
#   2.3a  Asthma Remediation: Self-Management
# And on the care-management side: 1.1 Navigation, 1.2 Enhanced HRSN Care
# Management -- the latter being what the wizard's "ECM case billed?" refers to.
MOBILITY = "mobility"
VENTILATION = "ventilation"

MODULE_LABELS = {
    MOBILITY: "Mobility",
    VENTILATION: "Ventilation",
}
MODULE_SERVICE_CODES = {
    MOBILITY: "2.1",
    VENTILATION: "2.2b",
}

# A referral type maps to the modules that get performed. "Combined" is both, which
# is the whole reason the form has modules rather than one flat question list.
REFERRAL_MODULES = {
    "mobility": [MOBILITY],
    "ventilation": [VENTILATION],
    "combined": [MOBILITY, VENTILATION],
}


# ── questions ────────────────────────────────────────────────────────────────
# Only "Reason for Assessment" carries an "Other..." box on the real forms; the
# observation sections do not. Modelled per-section so that can change without
# touching the renderer.

QUESTIONS = {
    MOBILITY: [
        {
            "code": "mob.reason",
            "title": "Reason for Assessment",
            "allows_other": True,
            "groups": [
                {
                    "code": "mob.reason",
                    "label": "",
                    "questions": [
                        {"code": "mob.reason.mobility_limitation",
                         "label": "Mobility limitation affecting safe movement within the home"},
                        {"code": "mob.reason.fall_risk",
                         "label": "Fall risk identified — history of falls or balance impairment"},
                        {"code": "mob.reason.bathroom_safety",
                         "label": "Bathroom safety concerns — difficulty bathing or transferring"},
                        {"code": "mob.reason.stair_safety",
                         "label": "Stair safety concerns — difficulty using stairs safely"},
                    ],
                },
            ],
        },
        {
            "code": "mob.functional",
            "title": "Functional Limitations Observed",
            "allows_other": False,
            "groups": [
                {
                    "code": "mob.functional.aids",
                    "label": "Mobility Aids",
                    "questions": [
                        {"code": "mob.aids.uses_device",
                         "label": "Member uses cane, walker, or wheelchair"},
                    ],
                },
                {
                    "code": "mob.functional.physical",
                    "label": "Physical Limitations",
                    "questions": [
                        {"code": "mob.physical.bathing_transfer",
                         "label": "Difficulty bathing or transferring observed"},
                        {"code": "mob.physical.stairs",
                         "label": "Difficulty using stairs safely observed"},
                        {"code": "mob.physical.balance",
                         "label": "Balance impairment noted"},
                        {"code": "mob.physical.falls",
                         "label": "History of falls reported or observed"},
                    ],
                },
            ],
        },
        {
            "code": "mob.risks",
            "title": "Observed Environmental Risks",
            "allows_other": False,
            "groups": [
                {
                    "code": "mob.risks.bathroom",
                    "label": "Bathroom",
                    "questions": [
                        {"code": "mob.risk.slippery_tub",
                         "label": "Slippery tub or shower surface present"},
                        {"code": "mob.risk.grab_bars_absent",
                         "label": "Grab bars absent — required for safe transfer and fall prevention"},
                        {"code": "mob.risk.wet_floor",
                         "label": "Wet or slippery floor surface present"},
                    ],
                },
                {
                    "code": "mob.risks.hallway",
                    "label": "Hallway / Stairs",
                    "questions": [
                        {"code": "mob.risk.handrail_absent",
                         "label": "Handrail absent or unstable — required for safe stair use"},
                        {"code": "mob.risk.poor_lighting",
                         "label": "Poor lighting present — increases fall risk"},
                        {"code": "mob.risk.uneven_flooring",
                         "label": "Uneven flooring present — increases fall and mobility risk"},
                    ],
                },
                {
                    "code": "mob.risks.general",
                    "label": "General Mobility",
                    "questions": [
                        {"code": "mob.risk.cluttered_pathways",
                         "label": "Cluttered pathways present — clearance required for safe mobility"},
                        {"code": "mob.risk.unsafe_transfers",
                         "label": "Unsafe transfers observed"},
                    ],
                },
            ],
        },
    ],
    VENTILATION: [
        {
            "code": "vent.reason",
            "title": "Reason for Assessment",
            "allows_other": True,
            "groups": [
                {
                    "code": "vent.reason",
                    "label": "",
                    "questions": [
                        {"code": "vent.reason.poor_air_quality",
                         "label": "Poor indoor air quality observed or reported"},
                        {"code": "vent.reason.inadequate_ventilation",
                         "label": "Inadequate ventilation throughout the home"},
                        {"code": "vent.reason.extreme_temperature",
                         "label": "Extreme indoor temperature — too hot or too cold"},
                        {"code": "vent.reason.humidity_issues",
                         "label": "Humidity issues — excessive or insufficient"},
                    ],
                },
            ],
        },
        {
            "code": "vent.conditions",
            "title": "Observed Environmental Conditions",
            "allows_other": False,
            "groups": [
                {
                    "code": "vent.conditions.air",
                    "label": "Air Quality / Ventilation",
                    "questions": [
                        {"code": "vent.air.poor_ventilation",
                         "label": "Poor ventilation observed — limited airflow throughout home"},
                        {"code": "vent.air.excessive_dust",
                         "label": "Excessive dust present — contributing to poor air quality"},
                        {"code": "vent.air.smoke_odors",
                         "label": "Smoke or strong odors present — affecting indoor air quality"},
                        {"code": "vent.air.mold_odor",
                         "label": "Mold or mildew odor present — ventilation inadequate"},
                    ],
                },
                {
                    "code": "vent.conditions.temp",
                    "label": "Temperature / Humidity",
                    "questions": [
                        {"code": "vent.temp.excessive_heat",
                         "label": "Excessive indoor heat present — health risk to member"},
                        {"code": "vent.temp.excessive_cold",
                         "label": "Excessive indoor cold present — health risk to member"},
                        {"code": "vent.temp.high_humidity",
                         "label": "High indoor humidity present — contributing to moisture and air quality issues"},
                        {"code": "vent.temp.low_humidity",
                         "label": "Low indoor humidity present — affecting member comfort and health"},
                    ],
                },
            ],
        },
        {
            "code": "vent.reported",
            "title": "Member-Reported Concerns",
            "allows_other": False,
            "groups": [
                {
                    "code": "vent.reported",
                    "label": "",
                    "questions": [
                        {"code": "vent.reported.breathing",
                         "label": "Difficulty breathing indoors reported"},
                        {"code": "vent.reported.allergies",
                         "label": "Allergy symptoms worsening at home reported"},
                        {"code": "vent.reported.extreme_temp",
                         "label": "Extreme indoor temperature reported"},
                        {"code": "vent.reported.no_ac_heat",
                         "label": "No working air conditioning or heat reported"},
                    ],
                },
            ],
        },
    ],
}


# ── recommended interventions ────────────────────────────────────────────────
# Two levels, exactly as the form presents them: a GROUP heading and the specific
# options under it. Each option is QUANTIFIED on the real form ("Window air
# conditioner  x2" with a -/+ stepper), not merely ticked.
#
# ``program_item`` records the housing programme item the group corresponds to, so
# the screener later knows which case to open. Captured here because the form's own
# grouping says it -- reconstructing it from labels afterwards would go wrong on
# "Handrails" vs our "Hand Rails", a single space apart.
#
# Three groups have NO programme yet (Accessibility Ramps, Pathways) or only an
# EXTERNAL one (Doors & Cabinet Handles). They are kept because the form offers
# them; ``program_item`` is None/external and those programmes come later.

INTERVENTIONS = {
    MOBILITY: [
        {
            "code": "ramps", "label": "Accessibility Ramps",
            "program_item": None,  # no programme yet
            "options": [
                {"code": "modular_portable_ramp", "label": "Modular/portable ramp"},
                {"code": "threshold_ramp", "label": "Threshold ramp"},
            ],
        },
        {
            "code": "bathroom", "label": "Bathroom Facilities",
            "program_item": "Bathroom Facilities",
            "options": [
                {"code": "bath_bench", "label": "Bath bench"},
                {"code": "raised_toilet_seat", "label": "Raised toilet seat"},
                {"code": "shower_chair", "label": "Shower chair"},
            ],
        },
        {
            "code": "doors", "label": "Doors & Cabinet Handles",
            # The programme exists but is EXTERNAL, so a case opened for it would be
            # refused on import. Kept on the form deliberately.
            "program_item": "Doors and Cabinet Handles",
            "options": [
                {"code": "d_ring_cabinet_pull", "label": "D-ring cabinet pull"},
                {"code": "lever_door_handle", "label": "Lever door handle"},
                {"code": "loop_cabinet_handle", "label": "Loop cabinet handle"},
            ],
        },
        {
            "code": "grab_bars", "label": "Grab Bars",
            "program_item": "Grab Bars",
            "options": [
                {"code": "safety_pole", "label": "Floor-to-ceiling safety pole"},
                {"code": "grab_bar_shower", "label": "Grab bar at shower"},
                {"code": "grab_bar_toilet", "label": "Grab bar at toilet"},
                {"code": "grab_bar_tub", "label": "Grab bar at tub"},
            ],
        },
        {
            "code": "handrails", "label": "Handrails",
            # Our programme spells it "Hand Rails".
            "program_item": "Hand Rails",
            "options": [
                {"code": "hallway_handrail", "label": "Hallway handrail"},
                {"code": "staircase_handrail", "label": "Interior staircase handrail"},
            ],
        },
        {
            "code": "non_skid", "label": "Non-skid Surfaces",
            "program_item": "Non-skid Surfaces",
            "options": [
                {"code": "non_skid_bath_mat", "label": "Non-skid bath mat"},
                {"code": "non_slip_adhesive_strips", "label": "Non-slip adhesive strips"},
                {"code": "non_slip_tape", "label": "Non-slip tape"},
            ],
        },
        {
            "code": "pathways", "label": "Pathways",
            "program_item": None,  # no programme yet
            "options": [
                {"code": "threshold_reducer", "label": "Threshold reducer"},
            ],
        },
    ],
    VENTILATION: [
        {
            "code": "air_conditioner", "label": "Air Conditioner",
            "program_item": "Air Conditioner",
            "options": [
                {"code": "portable_ac", "label": "Portable air conditioner"},
                {"code": "window_ac", "label": "Window air conditioner"},
            ],
        },
        {
            "code": "air_filtration", "label": "Air Filtration Devices",
            "program_item": "Air Filtration Device",
            "options": [
                {"code": "hepa_purifier", "label": "HEPA air purifier"},
                {"code": "portable_filtration_unit", "label": "Portable air filtration unit"},
            ],
        },
        {
            "code": "dehumidifier", "label": "De-humidifier",
            "program_item": "De-humidifier",
            "options": [
                {"code": "dehumidifier_portable", "label": "Dehumidifier (portable)"},
            ],
        },
        {
            "code": "heater", "label": "Heater",
            "program_item": "Heater",
            "options": [
                {"code": "portable_space_heater", "label": "Portable space heater"},
            ],
        },
        {
            "code": "humidifier", "label": "Humidifier",
            "program_item": "Humidifier",
            "options": [
                {"code": "cool_mist_humidifier", "label": "Cool mist humidifier"},
                {"code": "portable_humidifier", "label": "Portable humidifier"},
            ],
        },
    ],
}


# ── which questions indicate which interventions ─────────────────────────────
# A ticked question SUGGESTS the intervention categories that could answer it, so
# the vendor picks quantities from a short list instead of scrolling 26 options.
#
# Keyed by question code and listing intervention GROUP codes. Deliberately
# generous where a finding has more than one reasonable remedy -- a balance
# impairment can be met with grab bars, a handrail or a non-skid surface, and
# narrowing that to one would be us making a clinical choice from a data file.
#
# SOME QUESTIONS SUGGEST NOTHING, and that is correct rather than missing:
# "Poor lighting present" is a real fall risk with no product in the catalogue to
# answer it. It still belongs on the form -- it is evidence for the justification
# and for a later programme -- but it must not conjure a category.
QUESTION_SUGGESTS = {
    # Mobility -- reason for assessment
    "mob.reason.mobility_limitation": ["ramps", "pathways", "doors"],
    "mob.reason.fall_risk": ["grab_bars", "non_skid", "handrails"],
    "mob.reason.bathroom_safety": ["bathroom", "grab_bars", "non_skid"],
    "mob.reason.stair_safety": ["handrails"],
    # Mobility -- functional limitations
    "mob.aids.uses_device": ["ramps", "pathways", "doors"],
    "mob.physical.bathing_transfer": ["bathroom", "grab_bars"],
    "mob.physical.stairs": ["handrails"],
    "mob.physical.balance": ["grab_bars", "handrails", "non_skid"],
    "mob.physical.falls": ["grab_bars", "non_skid", "handrails"],
    # Mobility -- observed risks
    "mob.risk.slippery_tub": ["non_skid", "bathroom"],
    "mob.risk.grab_bars_absent": ["grab_bars"],
    "mob.risk.wet_floor": ["non_skid"],
    "mob.risk.handrail_absent": ["handrails"],
    "mob.risk.poor_lighting": [],          # no lighting product exists
    "mob.risk.uneven_flooring": ["pathways", "ramps"],
    "mob.risk.cluttered_pathways": ["pathways"],
    "mob.risk.unsafe_transfers": ["grab_bars", "bathroom"],

    # Ventilation -- reason for assessment
    "vent.reason.poor_air_quality": ["air_filtration"],
    "vent.reason.inadequate_ventilation": ["air_filtration"],
    "vent.reason.extreme_temperature": ["air_conditioner", "heater"],
    "vent.reason.humidity_issues": ["dehumidifier", "humidifier"],
    # Ventilation -- observed conditions
    "vent.air.poor_ventilation": ["air_filtration"],
    "vent.air.excessive_dust": ["air_filtration"],
    "vent.air.smoke_odors": ["air_filtration"],
    "vent.air.mold_odor": ["air_filtration", "dehumidifier"],
    "vent.temp.excessive_heat": ["air_conditioner"],
    "vent.temp.excessive_cold": ["heater"],
    "vent.temp.high_humidity": ["dehumidifier"],
    "vent.temp.low_humidity": ["humidifier"],
    # Ventilation -- member-reported
    "vent.reported.breathing": ["air_filtration"],
    "vent.reported.allergies": ["air_filtration"],
    "vent.reported.extreme_temp": ["air_conditioner", "heater"],
    "vent.reported.no_ac_heat": ["air_conditioner", "heater"],
}


def suggested_groups(answers, modules=None):
    """Intervention group codes indicated by the TICKED answers.

    Unknown or unticked codes contribute nothing. Returns a set, so a category
    reached by three different findings appears once.
    """
    out = set()
    for code, ticked in (answers or {}).items():
        if ticked:
            out.update(QUESTION_SUGGESTS.get(code, ()))
    if modules is not None:
        allowed = {
            g["code"] for m in modules for g in INTERVENTIONS.get(m, [])
        }
        out &= allowed
    return out


def modules_for_referral(referral_type):
    """Which modules a referral type performs. Unknown -> none.

    Returning nothing for an unrecognised type is deliberate: rendering a Mobility
    assessment for a referral nobody classified would put questions in front of a
    vendor that no authorization covers.
    """
    return list(REFERRAL_MODULES.get((referral_type or "").lower(), []))


def build_schema(modules):
    """The form definition for these modules, in order.

    Returned as plain data so it can be frozen onto a submitted questionnaire --
    that snapshot is what lets a signed form render unchanged after the template
    moves on.
    """
    mods = [m for m in (MOBILITY, VENTILATION) if m in (modules or [])]
    return {
        "version": TEMPLATE_VERSION,
        "modules": [
            {
                "code": m,
                "label": MODULE_LABELS[m],
                "service_code": MODULE_SERVICE_CODES[m],
                # Each question carries the intervention groups it SUGGESTS, so the
                # app can narrow the catalogue without a second copy of the mapping.
                # Shipping it in the schema also means a SUBMITTED form records the
                # relationships that were in force when it was signed.
                "sections": _sections_with_suggestions(m),
                "intervention_groups": INTERVENTIONS[m],
            }
            for m in mods
        ],
    }


def _sections_with_suggestions(module):
    """QUESTIONS[module] with a ``suggests`` list on every question.

    Copies rather than mutating the module-level constant -- QUESTIONS is shared
    across requests, and annotating it in place would leak into every later schema
    and into the snapshots frozen onto submitted forms.
    """
    out = []
    for section in QUESTIONS[module]:
        groups = []
        for group in section["groups"]:
            groups.append({
                **group,
                "questions": [
                    {**q, "suggests": list(QUESTION_SUGGESTS.get(q["code"], ()))}
                    for q in group["questions"]
                ],
            })
        out.append({**section, "groups": groups})
    return out


def all_question_codes(modules=None):
    """Every question code in these modules. Used to validate incoming answers."""
    mods = modules or [MOBILITY, VENTILATION]
    out = []
    for m in mods:
        for section in QUESTIONS.get(m, []):
            for group in section["groups"]:
                out.extend(q["code"] for q in group["questions"])
    return out


def all_option_codes(modules=None):
    """Every intervention option code in these modules."""
    mods = modules or [MOBILITY, VENTILATION]
    out = []
    for m in mods:
        for group in INTERVENTIONS.get(m, []):
            out.extend(o["code"] for o in group["options"])
    return out


def option_program_item(option_code):
    """The housing programme item an intervention option belongs to, or None.

    None means one of two different things, and the caller has to care: the group
    has no programme at all (Ramps, Pathways), or the option code is unknown.
    """
    for groups in INTERVENTIONS.values():
        for group in groups:
            for option in group["options"]:
                if option["code"] == option_code:
                    return group["program_item"]
    return None
