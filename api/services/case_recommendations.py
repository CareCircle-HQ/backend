"""Which Unite Us cases an agent should open after an assessment.

The chain this closes:

    the vendor recommends PRODUCTS  (Grab bar at tub x2, Shower chair x1)
        -> each product belongs to a CATEGORY   (Grab Bars, Bathroom Facilities)
        -> each category + borough is ONE PROGRAMME
        -> each programme is ONE CASE

So **many products collapse into one case per programme per borough**. Four grab
bars are not four cases; they are one "Grab Bars - Queens" case with four items on
it. Getting that wrong would have an agent open four cases Unite Us would treat as
duplicates.

THE BOROUGH COMES FROM THE MEMBER'S GOVERNING DWELLING ASSESSMENT CASE, not from
the address. The address on the order is the dwelling, which on the local clone is
in Florida, and ``address_city`` is a postal city rather than a borough in any
case. The governing case's programme name already ends in the borough the member
is served in, and using it guarantees the new cases match the existing one.

Nothing here CREATES a case. It tells an agent what to create, because opening a
case happens in Unite Us and the CRM only learns about it on the next import.
"""
import logging
from collections import OrderedDict

from ..models import ActiveProgram, BillableItem, Case
from .assessment_forms import INTERVENTIONS
from .housing import parse_housing_program_name

logger = logging.getLogger(__name__)

# Category -> the programme item that prices it. Recorded on the intervention
# groups rather than matched on text: "Handrails" vs "Hand Rails" and
# "Air Filtration Devices" vs "Air Filtration Device" differ, and a label match
# would report false gaps and invite duplicate programmes.
_CATEGORY_TO_PROGRAM_ITEM = {
    g["label"]: g["program_item"]
    for groups in INTERVENTIONS.values() for g in groups if g["program_item"]
}
_CATEGORY_FAMILY = {
    g["label"]: (
        "Home Remediation" if module == "ventilation"
        else "Home Accessibility and Safety Modification"
    )
    for module, groups in INTERVENTIONS.items() for g in groups
}


def member_borough(client, order=None):
    """The borough the new cases should be opened in.

    THE DWELLING'S ZIP WINS. The work happens at the assessed address, so its
    borough is the one the remediation programmes must name -- and it comes from
    ServiceZipCode, the same table that decides whether we serve the address at
    all.

    Falls back to the GOVERNING CASE's borough when the ZIP gives no answer: an
    address outside the service area, or one entered before this check existed.
    That keeps a recommendation possible rather than blank, and the two agreeing is
    the normal case.

    Returns "" when neither knows -- which the caller must handle rather than
    guess, because a case opened in the wrong borough is billed against the wrong
    programme.
    """
    from .housing import housing_service_case
    from .service_area import order_service_area

    if order is not None and (order.address_zip or order.address_formatted):
        borough = order_service_area(order)["borough"]
        if borough:
            return borough

    case = housing_service_case(client)
    if case is None:
        return ""
    _family, _item, borough = parse_housing_program_name(case.program_name)
    return borough


def borough_conflict(client, order):
    """``(zip_borough, case_borough)`` when the two DISAGREE, else None.

    Worth surfacing rather than silently preferring one: a member enrolled through
    a Queens case who has moved to Brooklyn needs new cases in Brooklyn, but an
    agent should be told the records disagree rather than discovering it on an
    invoice.
    """
    from .housing import housing_service_case
    from .service_area import order_service_area

    if order is None:
        return None
    zip_borough = order_service_area(order)["borough"]
    case = housing_service_case(client)
    case_borough = ""
    if case is not None:
        _f, _i, case_borough = parse_housing_program_name(case.program_name)
    if zip_borough and case_borough and zip_borough != case_borough:
        return (zip_borough, case_borough)
    return None


def _existing_housing_cases(client):
    """Programme items the member ALREADY has a housing case for, by borough."""
    out = set()
    for case in Case.objects.filter(
        client=client,
        program_name__iregex=r"^(Home Remediation|Home Accessibility)",
    ):
        _f, item, borough = parse_housing_program_name(case.program_name)
        out.add((item, borough))
    return out


def recommended_cases(questionnaire):
    """The cases to open for one submitted assessment.

    Returns one entry per programme, each listing the products that roll into it:

        {
          "program_name": "Home Accessibility and Safety Modification - Grab Bars - Queens",
          "program_item": "Grab Bars",
          "borough": "Queens",
          "exists": False,          # the PROGRAMME is in ActiveProgram
          "is_internal": True,      # ... and is an internal service
          "already_open": False,    # the member already has this case
          "products": [{item, qty, vendor_price, line_total}, ...],
          "total": Decimal,
        }

    ``exists`` / ``is_internal`` / ``already_open`` are reported rather than
    filtered on. An agent needs to see "you should open this, but the programme is
    External" -- silently dropping it would leave a recommended product with no
    explanation for why no case appeared.
    """
    from decimal import Decimal

    order = questionnaire.dispatch_order
    client = order.client
    vendor = order.vendor
    borough = member_borough(client, order)

    chosen = [
        i for i in (questionnaire.interventions or [])
        if i.get("option") and (i.get("qty") or 0) > 0
    ]
    if not chosen:
        return []

    # option code -> the priced item, which carries the category.
    items = {
        b.option_code: b
        for b in BillableItem.objects.exclude(option_code="")
    }
    # Programme names that exist, and whether they are internal.
    programmes = {}
    for p in ActiveProgram.objects.filter(
        program_name__iregex=r"^(Home Remediation|Home Accessibility)",
    ):
        programmes[p.program_name] = "internal" in (p.case_category or "").lower()

    already = _existing_housing_cases(client)

    from . import pricing

    percent = pricing.fee_percent_for(vendor)
    price_by_option = {
        r["option_code"]: Decimal(r["price"])
        for r in pricing.price_list_for(vendor) if r["option_code"]
    }

    grouped = OrderedDict()
    for entry in chosen:
        code = entry["option"]
        qty = int(entry.get("qty") or 0)
        item = items.get(code)
        if item is None:
            # Recommended but unpriced: surfaced as its own entry rather than
            # dropped, because an agent still has to decide what to do with it.
            grouped.setdefault(("", ""), {
                "program_name": "",
                "program_item": "",
                "borough": borough,
                "exists": False,
                "is_internal": False,
                "already_open": False,
                "products": [],
                "total": Decimal("0"),
            })["products"].append({
                "item": code, "qty": qty, "vendor_price": None,
                "line_total": None,
            })
            continue

        category = item.billing_category
        program_item = _CATEGORY_TO_PROGRAM_ITEM.get(category)
        family = _CATEGORY_FAMILY.get(category, "")
        name = (
            f"{family} - {program_item} - {borough}"
            if program_item and family and borough else ""
        )
        key = (program_item or category, borough)

        if key not in grouped:
            grouped[key] = {
                "program_name": name,
                "program_item": program_item or category,
                "family": family,
                "borough": borough,
                # A programme that is not in the table cannot be opened at all; one
                # that is External is refused on import. Different problems, so
                # they are reported separately.
                "exists": name in programmes,
                "is_internal": programmes.get(name, False),
                "already_open": (program_item, borough) in already,
                "products": [],
                "total": Decimal("0"),
            }
        price = price_by_option.get(code)
        line = (price * qty) if price is not None else None
        grouped[key]["products"].append({
            "item": item.item,
            "option_code": code,
            "qty": qty,
            "vendor_price": str(price) if price is not None else None,
            "line_total": str(line) if line is not None else None,
        })
        if line is not None:
            grouped[key]["total"] += line

    out = []
    for entry in grouped.values():
        total = entry["total"]
        entry["total"] = str(total)
        # What we would bill Unite Us for this case, which is what makes it worth
        # opening. The CRM may see this; the vendor never does.
        entry["billed_total"] = str(total + pricing.admin_fee(total, percent))
        entry["admin_fee_percent"] = str(percent)
        out.append(entry)
    return out
