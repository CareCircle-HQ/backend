"""Delivery Coverage Eligibility Check.

Second eligibility process (alongside the Service Fulfillment / meal rules):
verify a member's addresses are inside the service coverage area. A member
whose DELIVERY address OR PRIMARY (Current/Home) address ZIP is in the editable
:class:`ExcludedZipCode` list is set Out of Orbit (reason "Delivery Address
Outside Coverage Area") and excluded from all delivery schedules / Purchase
Orders.

The excluded-ZIP list is admin-editable from Settings (no code change), so this
reads it from the DB. Matching is on the first 5 digits of the ZIP.
"""

# Standardized Out-of-Orbit reason label for this process (shown in the note
# body + timeline metadata).
SERVICE_AREA_REASON = "Delivery Address Outside Coverage Area"


def _zip5(value):
    """First 5 digits of a raw ZIP cell (handles '11209-1234', ' 11209 ')."""
    return (value or "").strip()[:5]


def excluded_zips():
    """The set of excluded 5-digit ZIP codes (empty when none configured)."""
    from api.models import ExcludedZipCode

    return {z.zip for z in ExcludedZipCode.objects.all()}


def is_zip_excluded(zip_value, *, excluded=None):
    """True when ``zip_value`` (raw) is in the excluded list."""
    z = _zip5(zip_value)
    if not z:
        return False
    if excluded is None:
        excluded = excluded_zips()
    return z in excluded


def enrollment_excluded_zip(enrollment, *, excluded=None):
    """The offending 5-digit ZIP if the enrollment's DELIVERY address is outside
    the coverage area, else "". Only the delivery address is checked."""
    if enrollment is None:
        return ""
    addr = getattr(enrollment, "delivery_address", None)
    if addr is None:
        return ""
    z = _zip5(addr.zip)
    if z and is_zip_excluded(z, excluded=excluded):
        return z
    return ""


def _addr_zip_excluded(addr, excluded):
    """The offending 5-digit ZIP of ``addr`` if excluded, else ""."""
    if addr is None:
        return ""
    z = _zip5(addr.zip)
    return z if (z and is_zip_excluded(z, excluded=excluded)) else ""


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


def member_excluded_info(profile, *, excluded=None):
    """Delivery Coverage check for a member. Returns ``(zip, source)`` where the
    member's DELIVERY address or PRIMARY (Current/Home) address ZIP is in the
    excluded list, else ``("", "")``. The delivery address takes precedence."""
    if profile is None:
        return "", ""
    if excluded is None:
        excluded = excluded_zips()
    enr = getattr(profile, "enrollment", None)
    delivery = getattr(enr, "delivery_address", None) if enr is not None else None
    z = _addr_zip_excluded(delivery, excluded)
    if z:
        return z, "delivery address"
    z = _addr_zip_excluded(primary_address(getattr(profile, "client", None)), excluded)
    if z:
        return z, "primary address"
    return "", ""


def profile_excluded_zip(profile, *, excluded=None):
    """The offending delivery-or-primary ZIP for a member, or "". Used by the
    meal rules to force Out of Orbit durably."""
    return member_excluded_info(profile, excluded=excluded)[0]


def service_area_note_body(zip_code, source="delivery address"):
    """System-note body explaining an out-of-coverage exclusion. ``source`` names
    which address triggered it ("delivery address" / "primary address")."""
    return (
        f"Automatically set Out of Orbit — the {source} ZIP {zip_code} is "
        f"outside the current delivery coverage area."
    )
