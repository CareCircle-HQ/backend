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
COMBINED = "combined"

FORM_LABELS = {
    MOBILITY: "Mobility",
    VENTILATION: "Ventilation",
    COMBINED: "Mobility + Ventilation",
}
FORM_SERVICE_CODES = {
    MOBILITY: "2.1",
    VENTILATION: "2.2b",
    COMBINED: "2.1 + 2.2b",
}

# A referral type selects ONE form. Combined is its own document, not the two
# others concatenated: it merges the two Reason sections into one and, per the
# supplied questionnaire, carries no Member-Reported Concerns section.
REFERRAL_FORM = {
    "mobility": MOBILITY,
    "ventilation": VENTILATION,
    "combined": COMBINED,
}


# ── product categories ───────────────────────────────────────────────────────
# The five main categories from the billable-items table. A question GROUP names
# one of these, and ticking anything in that group offers every product underneath
# it -- the whole category, not a per-question subset.
BATHROOM = "bathroom"
DOORS = "doors"
MOBILITY_ACCESS = "mobility_access"
AIR_QUALITY = "air_quality"
TEMPERATURE = "temperature"

CATEGORY_LABELS = {
    BATHROOM: "Bathroom",
    DOORS: "Doors, Handles & Access",
    MOBILITY_ACCESS: "Mobility & Access",
    AIR_QUALITY: "Air Quality",
    TEMPERATURE: "Temperature Control",
}

# Which programme family a category's cases belong to. The three mobility-side
# categories are 2.1 Home Accessibility and Safety Modification; the two
# ventilation-side ones are Home Remediation. This is what turns a recommended
# product into the name of a case an agent has to open.
CATEGORY_FAMILY = {
    BATHROOM: "Home Accessibility and Safety Modification",
    DOORS: "Home Accessibility and Safety Modification",
    MOBILITY_ACCESS: "Home Accessibility and Safety Modification",
    AIR_QUALITY: "Home Remediation",
    TEMPERATURE: "Home Remediation",
}


def category_of_option(option_code):
    """The main category an intervention option belongs to, or ""."""
    for category, groups in INTERVENTIONS.items():
        for group in groups:
            if any(o["code"] == option_code for o in group["options"]):
                return category
    return ""


def group_of_option(option_code):
    """The intervention GROUP an option belongs to, or None."""
    for groups in INTERVENTIONS.values():
        for group in groups:
            if any(o["code"] == option_code for o in group["options"]):
                return group
    return None


# ── questions ────────────────────────────────────────────────────────────────
# Transcribed from tmp/import/{Mobility,Ventilation,Combined}Questionnaire.txt.
#
# Each GROUP carries two rules straight from those documents:
#
#   category        ticking anything here offers that category's products.
#                   None for the sections marked "No recommend any product".
#   requires_photo  the sections marked "Required photo if any is selected".
#
# Question CODES are unchanged from the previous version wherever the text is the
# same, so answers already saved against a draft survive the restructure. What
# changed is the grouping and what each group points at.

_MOB_REASON = {
    "code": "mob.reason",
    "label": "",
    "category": None,        # "No recommend any product"
    "requires_photo": False,  # "Doesn't required photo"
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
}

_VENT_REASON = {
    "code": "vent.reason",
    "label": "",
    "category": None,
    "requires_photo": False,
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
}

_FUNCTIONAL_MOBILITY = {
    "code": "mob.functional",
    "label": "Mobility & Access",
    "category": MOBILITY_ACCESS,
    "requires_photo": True,
    "questions": [
        # The source reads "Member uses cane, walker, or wheelchair Physical
        # Limitations" -- the trailing words are the next sub-heading, left behind
        # when the document was flattened to text, not part of the question.
        {"code": "mob.aids.uses_device",
         "label": "Member uses cane, walker, or wheelchair"},
        {"code": "mob.physical.bathing_transfer",
         "label": "Difficulty bathing or transferring observed"},
        {"code": "mob.physical.stairs",
         "label": "Difficulty using stairs safely observed"},
        {"code": "mob.physical.balance", "label": "Balance impairment noted"},
        {"code": "mob.physical.falls",
         "label": "History of falls reported or observed"},
    ],
}

# ⚠ The source documents say these two groups "relate to Temperature Control".
# That is a copy-paste error in all four places it appears: every other note names
# the group's own heading, and a slippery bath or an absent handrail cannot be
# answered with an air conditioner. Mapped to the categories the headings name.
_RISK_BATHROOM = {
    "code": "mob.risk.bathroom",
    "label": "Bathroom",
    "category": BATHROOM,
    "requires_photo": True,
    "questions": [
        {"code": "mob.risk.slippery_tub",
         "label": "Slippery tub or shower surface present"},
        {"code": "mob.risk.grab_bars_absent",
         "label": "Grab bars absent — required for safe transfer and fall prevention"},
        {"code": "mob.risk.wet_floor",
         "label": "Wet or slippery floor surface present"},
    ],
}

_RISK_MOBILITY = {
    "code": "mob.risk.mobility",
    "label": "Mobility & Access",
    "category": MOBILITY_ACCESS,
    "requires_photo": True,
    "questions": [
        {"code": "mob.risk.handrail_absent",
         "label": "Handrail absent or unstable — required for safe stair use"},
        {"code": "mob.risk.poor_lighting",
         "label": "Poor lighting present — increases fall risk"},
        {"code": "mob.risk.uneven_flooring",
         "label": "Uneven flooring present — increases fall and mobility risk"},
        {"code": "mob.risk.cluttered_pathways",
         "label": "Cluttered pathways present — clearance required for safe mobility"},
        {"code": "mob.risk.unsafe_transfers", "label": "Unsafe transfers observed"},
    ],
}

_CONDITIONS_AIR = {
    "code": "vent.air",
    "label": "Air Quality",
    "category": AIR_QUALITY,
    "requires_photo": True,
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
}

_CONDITIONS_TEMP = {
    "code": "vent.temp",
    "label": "Temperature Control",
    "category": TEMPERATURE,
    "requires_photo": True,
    "questions": [
        {"code": "vent.temp.excessive_heat",
         "label": "Excessive indoor heat present — health risk to member"},
        {"code": "vent.temp.excessive_cold",
         "label": "Excessive indoor cold present — health risk to member"},
        {"code": "vent.temp.high_humidity",
         "label": "High indoor humidity present — contributing to moisture and air "
                  "quality issues"},
        {"code": "vent.temp.low_humidity",
         "label": "Low indoor humidity present — affecting member comfort and health"},
    ],
}

_MEMBER_REPORTED = {
    "code": "vent.reported",
    "label": "",
    "category": None,
    "requires_photo": False,
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
}

# ── interventions ────────────────────────────────────────────────────────────
# Keyed by the five MAIN CATEGORIES, matching BillableItem.main_category, because
# that is the unit a question group points at: tick anything under Temperature
# Control and the vendor is offered every product in Temperature Control.
INTERVENTIONS = {
    BATHROOM: [
        {
            "code": 'bathroom',
            "label": 'Bathroom Facilities',
            "program_item": 'Bathroom Facilities',
            "options": [
                {"code": 'bath_bench', "label": 'Bath bench'},
                {"code": 'raised_toilet_seat', "label": 'Raised toilet seat'},
                {"code": 'shower_chair', "label": 'Shower chair'},
            ],
        },
        {
            "code": 'non_skid',
            "label": 'Non-skid Surfaces',
            "program_item": 'Non-skid Surfaces',
            "options": [
                {"code": 'non_skid_bath_mat', "label": 'Non-skid bath mat'},
                {"code": 'non_slip_adhesive_strips', "label": 'Non-slip adhesive strips'},
                {"code": 'non_slip_tape', "label": 'Non-slip tape'},
            ],
        },
        {
            "code": 'grab_bars',
            "label": 'Grab Bars',
            "program_item": 'Grab Bars',
            "options": [
                {"code": 'safety_pole', "label": 'Floor-to-ceiling safety pole'},
                {"code": 'grab_bar_shower', "label": 'Grab bar at shower'},
                {"code": 'grab_bar_toilet', "label": 'Grab bar at toilet'},
                {"code": 'grab_bar_tub', "label": 'Grab bar at tub'},
            ],
        },
    ],
    DOORS: [
        {
            "code": 'doors',
            "label": 'Doors & Cabinet Handles',
            "program_item": 'Doors and Cabinet Handles',
            "options": [
                {"code": 'd_ring_cabinet_pull', "label": 'D-ring cabinet pull'},
                {"code": 'lever_door_handle', "label": 'Lever door handle'},
                {"code": 'loop_cabinet_handle', "label": 'Loop cabinet handle'},
            ],
        },
    ],
    MOBILITY_ACCESS: [
        {
            "code": 'ramps',
            "label": 'Accessibility Ramps',
            "program_item": None,
            "options": [
                {"code": 'modular_portable_ramp', "label": 'Modular/portable ramp'},
                {"code": 'threshold_ramp', "label": 'Threshold ramp'},
            ],
        },
        {
            "code": 'handrails',
            "label": 'Handrails',
            "program_item": 'Hand Rails',
            "options": [
                {"code": 'hallway_handrail', "label": 'Hallway handrail'},
                {"code": 'staircase_handrail', "label": 'Interior staircase handrail'},
            ],
        },
        {
            "code": 'pathways',
            "label": 'Pathways',
            "program_item": None,
            "options": [
                {"code": 'threshold_reducer', "label": 'Threshold reducer'},
            ],
        },
    ],
    AIR_QUALITY: [
        {
            "code": 'air_filtration',
            "label": 'Air Filtration Devices',
            "program_item": 'Air Filtration Device',
            "options": [
                {"code": 'hepa_purifier', "label": 'HEPA air purifier'},
                {"code": 'portable_filtration_unit', "label": 'Portable air filtration unit'},
            ],
        },
        {
            "code": 'dehumidifier',
            "label": 'De-humidifier',
            "program_item": 'De-humidifier',
            "options": [
                {"code": 'dehumidifier_portable', "label": 'Dehumidifier (portable)'},
            ],
        },
        {
            "code": 'humidifier',
            "label": 'Humidifier',
            "program_item": 'Humidifier',
            "options": [
                {"code": 'cool_mist_humidifier', "label": 'Cool mist humidifier'},
                {"code": 'portable_humidifier', "label": 'Portable humidifier'},
            ],
        },
    ],
    TEMPERATURE: [
        {
            "code": 'air_conditioner',
            "label": 'Air Conditioner',
            "program_item": 'Air Conditioner',
            "options": [
                {"code": 'portable_ac', "label": 'Portable air conditioner'},
                {"code": 'window_ac', "label": 'Window air conditioner'},
            ],
        },
        {
            "code": 'heater',
            "label": 'Heater',
            "program_item": 'Heater',
            "options": [
                {"code": 'portable_space_heater', "label": 'Portable space heater'},
            ],
        },
    ],
}

FORMS = {
    MOBILITY: [
        {"code": "reason", "title": "Reason for Assessment",
         "allows_other": True, "groups": [_MOB_REASON]},
        {"code": "functional", "title": "Functional Limitations Observed",
         "allows_other": False, "groups": [_FUNCTIONAL_MOBILITY]},
        {"code": "risks", "title": "Observed Environmental Risks",
         "allows_other": False, "groups": [_RISK_BATHROOM, _RISK_MOBILITY]},
    ],
    VENTILATION: [
        {"code": "reason", "title": "Reason for Assessment",
         "allows_other": True, "groups": [_VENT_REASON]},
        {"code": "conditions", "title": "Observed Environmental Conditions",
         "allows_other": False, "groups": [_CONDITIONS_AIR, _CONDITIONS_TEMP]},
        {"code": "reported", "title": "Member-Reported Concerns",
         "allows_other": False, "groups": [_MEMBER_REPORTED]},
    ],
    COMBINED: [
        # One Reason section holding both sets, per the combined document.
        {"code": "reason", "title": "Reason for Assessment", "allows_other": True,
         "groups": [
             {**_MOB_REASON, "code": "comb.reason.mob", "label": "Mobility"},
             {**_VENT_REASON, "code": "comb.reason.vent", "label": "Ventilation"},
         ]},
        {"code": "functional", "title": "Functional Limitations Observed",
         "allows_other": False, "groups": [_FUNCTIONAL_MOBILITY]},
        {"code": "risks", "title": "Observed Environmental Risks",
         "allows_other": False, "groups": [_RISK_BATHROOM, _RISK_MOBILITY]},
        {"code": "conditions", "title": "Observed Environmental Conditions",
         "allows_other": False, "groups": [_CONDITIONS_AIR, _CONDITIONS_TEMP]},
        # NOTE: the combined document has no Member-Reported Concerns section,
        # which the ventilation-only one does. Transcribed as supplied.
    ],
}

def form_for_referral(referral_type):
    """Which form a referral type performs. Unknown -> None."""
    return REFERRAL_FORM.get((referral_type or "").strip().lower())


def modules_for_referral(referral_type):
    """Backwards-compatible: the stored ``modules`` list for a referral type.

    A single-element list holding the FORM code. It used to hold one entry per
    module, with combined meaning both -- but combined is now its own document
    rather than the two concatenated, so the list names the form. Existing rows
    holding ["mobility", "ventilation"] still resolve, via _form_code.
    """
    form = form_for_referral(referral_type)
    return [form] if form else []


def _form_code(modules):
    """The form these stored ``modules`` mean.

    Tolerates the OLD shape: ["mobility", "ventilation"] was how combined used to
    be stored, and those rows are already in the database.
    """
    codes = [m for m in (modules or []) if m]
    if not codes:
        return None
    if len(codes) > 1:
        return COMBINED
    code = codes[0]
    return code if code in FORMS else None


def sections_for(modules):
    """The sections of the form these modules identify, or []."""
    form = _form_code(modules)
    return FORMS.get(form, [])


def category_for_group(group_code, modules=None):
    """The product category a question group points at, or ""."""
    for section in sections_for(modules) or [
        sec for secs in FORMS.values() for sec in secs
    ]:
        for group in section["groups"]:
            if group["code"] == group_code:
                return group.get("category") or ""
    return ""


def suggested_categories(answers, modules=None):
    """Main category codes indicated by the TICKED answers.

    Per GROUP, which is what the questionnaires specify: "if any question is
    selected then we show the whole list of products under that category". A group
    with no category -- Reason for Assessment, Member-Reported Concerns -- suggests
    nothing however many boxes are ticked, because those sections are marked "No
    recommend any product".
    """
    out = set()
    ticked = {code for code, value in (answers or {}).items() if value}
    if not ticked:
        return out
    for section in sections_for(modules):
        for group in section["groups"]:
            category = group.get("category")
            if not category:
                continue
            if any(q["code"] in ticked for q in group["questions"]):
                out.add(category)
    return out


def suggested_groups(answers, modules=None):
    """Intervention GROUP codes indicated by the ticked answers.

    Every product group under every suggested category -- the questionnaires ask
    for the whole category, not a subset.
    """
    out = set()
    for category in suggested_categories(answers, modules):
        for group in INTERVENTIONS.get(category, []):
            out.add(group["code"])
    return out


def photo_groups_required(answers, modules=None):
    """Question-group codes that need a photo because something in them is ticked.

    Returns ``[(group_code, label)]``. The label is what the vendor is asked for,
    so it is the group's own heading ("Bathroom") rather than a product category.
    """
    out = []
    ticked = {code for code, value in (answers or {}).items() if value}
    if not ticked:
        return out
    for section in sections_for(modules):
        for group in section["groups"]:
            if not group.get("requires_photo"):
                continue
            if any(q["code"] in ticked for q in group["questions"]):
                label = group["label"] or section["title"]
                out.append((group["code"], label))
    return out


def build_schema(modules):
    """The form definition for these modules, as plain data.

    Frozen onto a submitted questionnaire: that snapshot is what lets a signed form
    render unchanged after the template moves on.
    """
    form = _form_code(modules)
    if form is None:
        return {"version": TEMPLATE_VERSION, "form": None, "sections": [],
                "categories": []}
    return {
        "version": TEMPLATE_VERSION,
        "form": form,
        "label": FORM_LABELS[form],
        "service_code": FORM_SERVICE_CODES[form],
        "sections": FORMS[form],
        # Every category the form can reach, each with its products. The app shows
        # a category when the answers point at it, so it needs them all up front.
        "categories": [
            {
                "code": code,
                "label": CATEGORY_LABELS[code],
                "groups": INTERVENTIONS.get(code, []),
            }
            for code in _categories_in(form)
        ],
    }


def _categories_in(form):
    """The categories this form's groups point at, in the order they appear."""
    seen = []
    for section in FORMS[form]:
        for group in section["groups"]:
            category = group.get("category")
            if category and category not in seen:
                seen.append(category)
    return seen


def all_question_codes(modules=None):
    """Every question code in these modules. Used to validate incoming answers."""
    sections = sections_for(modules) if modules else [
        sec for secs in FORMS.values() for sec in secs
    ]
    return {
        q["code"]
        for section in sections
        for group in section["groups"]
        for q in group["questions"]
    }


def all_option_codes(modules=None):
    """Every intervention option code available to these modules."""
    if modules:
        categories = _categories_in(_form_code(modules)) if _form_code(modules) else []
    else:
        categories = list(INTERVENTIONS)
    return {
        o["code"]
        for category in categories
        for group in INTERVENTIONS.get(category, [])
        for o in group["options"]
    }


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
