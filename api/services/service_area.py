"""Delivery Coverage Eligibility Check (service-area WHITELIST).

Second eligibility process (alongside the Service Fulfillment / meal rules):
verify a member's addresses are inside the service coverage area. A member whose
DELIVERY address OR PRIMARY (Current/Home) address ZIP is NOT in the editable
:class:`ServiceZipCode` whitelist is set Out of Range (reason "Delivery Address
Outside Coverage Area") and excluded from all delivery schedules / Purchase
Orders. Out of Range additionally opens a Case Closure ticket and holds the
whole household (see api.portal.views_members._enforce_delivery_coverage).

The whitelist is admin-editable from Settings (Service ZIP Codes), so this reads
it from the DB. Matching is on the first 5 digits of the ZIP. An UNCONFIGURED
(empty) whitelist is inert (everyone in range) so the feature stays off until
seeded. Once configured, an address with a blank or malformed ZIP is treated as
OUT of range (fail-closed) -- a member with NO address at all is still skipped.
"""

# Standardized reason label for this process (shown in the note body + timeline
# metadata). A ZIP outside the coverage area now sets members Out of Range (a
# dedicated status), distinct from the dietary/kitchen "Out of Orbit" block.
SERVICE_AREA_REASON = "Delivery Address Outside Coverage Area"


def _zip5(value):
    """First 5 digits of a raw ZIP cell (handles '11209-1234', ' 11209 ')."""
    return (value or "").strip()[:5]


def service_zips():
    """The set of ACTIVE service-area 5-digit ZIP codes (the whitelist). Empty
    when none are configured."""
    from api.models import ServiceZipCode

    return {z.zip for z in ServiceZipCode.objects.filter(is_active=True)}


def is_zip_out_of_range(zip_value, *, service=None):
    """True when ``zip_value`` is NOT in the active service-area whitelist.

    An empty whitelist (unconfigured) is inert (returns False). Otherwise a blank
    or malformed ZIP is OUT of range (returns True) -- only the empty-whitelist
    case fails open.
    """
    if service is None:
        service = service_zips()
    if not service:
        return False
    return _zip5(zip_value) not in service


def _addr_zip_out_of_range(addr, service):
    """The offending ZIP label if ``addr`` is outside coverage, else "". A present
    address with a blank/malformed ZIP is out of range (labelled "(blank)"); a
    missing address (``None``) is skipped."""
    if addr is None or not service:
        return ""
    if not is_zip_out_of_range(addr.zip, service=service):
        return ""
    return _zip5(addr.zip) or "(blank)"


def enrollment_out_of_range_zip(enrollment, *, service=None):
    """The offending 5-digit ZIP if the enrollment's DELIVERY address is outside
    the coverage area, else "". Only the delivery address is checked."""
    if enrollment is None:
        return ""
    addr = getattr(enrollment, "delivery_address", None)
    return _addr_zip_out_of_range(addr, service if service is not None else service_zips())


def primary_address(client):
    """A client's PRIMARY residential address: their Current address, else Home.
    Delivery / temporary / other types are NOT treated as the primary. None when
    the client has no Current/Home address."""
    if client is None:
        return None
    from api.models import AddressType

    by_type = {}
    for a in client.addresses.all():
        by_type.setdefault(a.type, a)
    for t in (AddressType.CURRENT, AddressType.HOME):
        if t in by_type:
            return by_type[t]
    return None


def member_out_of_range_info(profile, *, service=None):
    """Delivery Coverage check for a member. Returns ``(zip, source)`` where the
    member's DELIVERY address or PRIMARY (Current/Home) address ZIP is outside the
    service-area whitelist, else ``("", "")``. The delivery address takes
    precedence."""
    if profile is None:
        return "", ""
    if service is None:
        service = service_zips()
    enr = getattr(profile, "enrollment", None)
    delivery = getattr(enr, "delivery_address", None) if enr is not None else None
    z = _addr_zip_out_of_range(delivery, service)
    if z:
        return z, "delivery address"
    z = _addr_zip_out_of_range(primary_address(getattr(profile, "client", None)), service)
    if z:
        return z, "primary address"
    return "", ""


def profile_out_of_range_zip(profile, *, service=None):
    """The offending delivery-or-primary ZIP for a member, or "". Used by the
    meal rules to force Out of Orbit durably."""
    return member_out_of_range_info(profile, service=service)[0]


def service_area_note_body(zip_code, source="delivery address"):
    """System-note body explaining an out-of-coverage exclusion. ``source`` names
    which address triggered it ("delivery address" / "primary address")."""
    return (
        f"Automatically set Out of Range — the {source} ZIP {zip_code} is "
        f"outside the current delivery coverage area."
    )


def out_of_range_ticket_reason(zip_code, source="delivery address", member_names=None):
    """Pre-filled Case Closure ticket description for an out-of-range household.

    Explains that the household's ``source`` ZIP is outside the delivery
    coverage area, so service can't be provided and the case should be reviewed
    for closure. ``member_names`` (optional) lists the affected members.
    """
    who = ""
    if member_names:
        who = f" Affected member(s): {', '.join(member_names)}."
    return (
        f"Out-of-range ZIP code: the {source} ZIP {zip_code} is outside our "
        f"delivery coverage area, so this household cannot be served. The "
        f"household has been placed on hold and every member set Out of Range. "
        f"Please review this case for closure.{who}"
    )
