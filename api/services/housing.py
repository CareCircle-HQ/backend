"""Housing service resolution.

Internal Services is not food-only: housing programs (Dwelling Assessment / SOW
Development, Home Remediation) are internal services of a different TYPE. The rule
is ONE GOVERNING INTERNAL-SERVICE CASE PER TYPE, so a member may hold a Food case
and a Housing case at once without them competing.

The Unite Us flow this models::

    screener creates  Environmental Exposure Assessment (EEA)   the governing case
          |  verification (a new kind -- not built yet)
          |  creates an ASSESSMENT ORDER
          |  a vendor attends the home and inspects
          |  inspection results
    creates  Home Expense Assistance/Repairs (HEAR) cases       = WORK ORDERS

Three rules from the operator, and how each is enforced here:

1. **Only an Environmental Exposure Assessment may govern.** Expressed as an
   ALLOWLIST rather than "HEAR is excluded", so a service type added to housing
   later cannot accidentally start governing by default -- it has to be admitted
   deliberately.
2. **Individual only, tied to the PRIMARY.** Food resolution deliberately falls
   back to the HOUSEHOLD's case for a dependent, so "every household member shows
   the same authorization instead of a blank"
   (`governing_service_case_for_display`). Housing must NOT do that: a dependent
   has no housing case of their own and must not appear to. A dwelling is one
   property, so the case belongs to the primary alone.
3. **Work orders hang off the governing assessment.** They are looked up through
   it rather than standing alone, so a HEAR case can never be mistaken for the
   thing that authorises housing service.

Ranking is the EXISTING `governing_case_key`, unchanged -- "the same rules we did
for food". Known consequence, accepted rather than engineered around: that key
ranks authorization favour first, so a lapsed-but-still-APPROVED assessment would
outrank a new PENDING one. The operator states re-assessment cases never occur, so
a member never holds two live EEAs and the situation does not arise.
"""
from api.models import ActiveProgram, CaseType
from api.services.catalog import case_service_domain, case_service_type_code

HOUSING_DOMAIN = "housing"

# Service types allowed to GOVERN housing service. An allowlist: everything else
# housing-typed is a work order.
GOVERNING_SERVICE_TYPES = frozenset({
    ActiveProgram.ServiceType.ENVIRONMENTAL_EXPOSURE_ASSESSMENT.value,
})

# Service types that are WORK ORDERS -- the output of an inspection, never the
# authority for service.
WORK_ORDER_SERVICE_TYPES = frozenset({
    ActiveProgram.ServiceType.HOME_EXPENSE_ASSISTANCE_REPAIRS.value,
})


def is_housing_case(case):
    """True when the case's service TYPE is housing (derived from its program)."""
    return (
        getattr(case, "case_type", "") == CaseType.INTERNAL_SERVICE
        and case_service_domain(case) == HOUSING_DOMAIN
    )


def is_assessment_case(case):
    """True for an Environmental Exposure Assessment -- the only housing case that
    may govern."""
    return (
        is_housing_case(case)
        and case_service_type_code(case) in GOVERNING_SERVICE_TYPES
    )


def is_work_order_case(case):
    """True for a Home Expense Assistance/Repairs case -- a work order.

    Kept as an explicit predicate rather than "housing and not assessment": a
    housing case whose service type resolves to NEITHER is a classification gap
    worth seeing, not something to silently treat as a work order.
    """
    return (
        is_housing_case(case)
        and case_service_type_code(case) in WORK_ORDER_SERVICE_TYPES
    )


def housing_assessment_cases(client):
    """The member's OWN Environmental Exposure Assessment cases, most-governing
    first by `governing_case_key`.

    No household fallback -- see rule 2 in the module docstring.
    """
    from api.services.lifecycle import governing_case_key

    cases = [c for c in client.cases.all() if is_assessment_case(c)]
    return sorted(cases, key=governing_case_key, reverse=True)


def housing_service_case(client):
    """The member's GOVERNING housing case, or None.

    The housing counterpart of `internal_service_case`. None when the member holds
    no assessment -- including when they hold only work orders, which cannot
    authorise anything on their own.
    """
    cases = housing_assessment_cases(client)
    return cases[0] if cases else None


def housing_work_orders(client):
    """The member's housing work orders (Home Expense Assistance/Repairs).

    Ordered by `governing_case_key` for a stable, meaningful sequence, matching
    how food orders its cases.

    Returns them for the member rather than strictly "those linked to the
    governing assessment": Unite Us gives us no parent reference on the case, so
    the link is by member, and the operator's rule is that every work order sits
    under the one governing assessment anyway. If a member ever holds two
    assessments, this cannot attribute orders between them -- recorded here
    because that is the point at which a real parent link would be needed.
    """
    from api.services.lifecycle import governing_case_key

    orders = [c for c in client.cases.all() if is_work_order_case(c)]
    return sorted(orders, key=governing_case_key, reverse=True)


def has_open_housing_case(client):
    """True when the member has an OPEN governing housing assessment."""
    from api.models import CaseStatus

    closed = {CaseStatus.CLOSED, CaseStatus.CANCELLED}
    return any(
        c.case_status not in closed for c in housing_assessment_cases(client)
    )
