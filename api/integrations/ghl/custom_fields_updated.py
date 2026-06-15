"""Mapping of GoHighLevel contact custom fields to ``Client`` data.

Each entry maps a GHL custom field id to a resolver that returns the value for a
given client (or ``None``/"" to skip). Add fields here incrementally -- the ids
come from ``python manage.py ghl_fields`` (raw dump in ghl_custom_fields.json).

Kept separate from contacts.py so the field catalog is easy to read, extend, and
remove when the external CRM is retired.

Field IDs are from: tmp/fields_external_crm.csv
"""

# =============================================================================
# Contact Custom Field IDs (GHL Location: HwYldKwhYiZywGFkXr0y)
# =============================================================================

# Core Identifiers
FIELD_ENROLLMENT_CLIENT_ID = "iWw4cIFFBCFcKGlUeffm"  # contact.enrollment_client_id
FIELD_TOTAL_HOUSEHOLD_MEMBERS = "7Y8aipXfNywpogNFsEk0"  # contact.total_household_members

# Agent/Assignment Tracking
FIELD_AGENT_CODE_ATTESTATION_COMPLETED = "Y6zgE4FdPysieN3jJ55C"  # contact.agent_code__attestation_completed
FIELD_AGENT_CODE_EXTERNAL_SERVICES = "xtnpC8QzUPakgeMXldMA"  # contact.agent_code__external_services
FIELD_ASSIGNED_AGENT_ATTESTATION_REQUESTED = "wuXdpSPdvglOX2XaMdOT"  # contact._assigned_agent__attestation_requested
FIELD_ASSIGNED_AGENT_ATTESTATION_COMPLETE = "v02vvzMuoOMcD3wqS5nD"  # contact._assigned_agent__attestation_complete

# Delivery/Verification
FIELD_DATE_OF_FIRST_DELIVERY = "wW6QENM8RU9fRzQPEU2u"  # contact.delivery_start
FIELD_FINAL_VERIFICATION_STATUS = "WjuizZewAHUp2W3nXZL2"  # contact.final_verification_status
FIELD_FINAL_VERIFICATION_FORM_URL = "xavLL0N59Ct2sG8QzdzO"  # contact.final_ver_form_url

# HM (Holocaust Meals) Screening Tracking (on Contact)
FIELD_HM3_SCREENING = "ykUXk9fRXjVxGtQOIwIO"  # contact.hm_3__screening
FIELD_HM5_SCREENING = "X9COcyRjnHTqMbl73WXT"  # contact.hm_5__screening
FIELD_HM7_ELIGIBILITY = "xTiHii7zbB9YwPHYxvPJ"  # contact.hm_7__eligibility
FIELD_HM9_SCREENING = "wGMFGBUjQy9FiMUVOX7Y"  # contact.hm_9__screening

# HM Member Enhanced Status
FIELD_HM2_MEMBER_ENHANCED = "w1MgWEq2umOoKWKPaFkQ"  # contact.hm_2__member_enhanced
FIELD_HM4_MEMBER_ENHANCED = "xwSFdzVSVervYfSnbsYa"  # contact.hm_4__member_enhanced
FIELD_HM7_MEMBER_ENHANCED = "xStwW0W0f0Qq6jvibH9D"  # contact.hm_7__member_enhanced

# HM Confirmations (No case created yet)
FIELD_HM4_CONFIRM_NO_CASE = "xwSFdzVSVervYfSnbsYa"  # contact.hm_4__confirm_no_case
FIELD_HM6_CONFIRM_NO_CASE = "Vj38uh3d910lY9yDCN4o"  # contact.hm_6__confirm_no_case
FIELD_HM9_CONFIRM_NO_CASE = "VfWg4y3Ym5jtNy2oK2QY"  # contact.hm_9__confirm_no_case

# HM Enrollment Platform IDs (Contact-level snapshots)
FIELD_HM2_ENROLLMENT_CLIENT_ID_CONTACT = "v3PN3UCC4uPJDbwHc83c"  # contact.hm_2__enrollment_platform_id
FIELD_HM3_ENROLLMENT_CLIENT_ID_CONTACT = "xUrnyFjlqiaK4iI7hI0U"  # contact.hm_3__enrollment_platform_id
FIELD_HM7_ENROLLMENT_CLIENT_ID_CONTACT = "YIA99adbI63iyFfaJcPl"  # contact.hm_7__enrollment_platform_id

# System/Internal Fields (🚫 prefix in GHL - may not need to sync)
FIELD_ELIGIBLE_SERVICES = "zneDTRPytLOixqQLAqU9"  # contact._eligible_services
FIELD_MEMBER_STATUS = "Z4tA1fpY3gt4OjLP1v9v"  # contact.member_status
FIELD_VERIFICATION_RESULT = "WFmbE1EhsFcexEi1wOoZ"  # contact._verification_result
FIELD_MEDICAID_TYPE_VERIFIED = "VeuV354LT2elBhJAuKIv"  # contact._medicaid_type_verified

# TODO: Add after API migration for Doctor fields
# FIELD_DOCTORS_NAME = "VDp9dccvMPl8Yood6e9O"
# FIELD_DOCTORS_STREET_ADDRESS = "Yvfn5jNSITA7oDZ9qc0G"
# FIELD_DOCTORS_PHONE = "XtwIwYRKfgaTe88T92wB"
# FIELD_DOCTORS_FAX = "XJyU9CjxrH5dID7vWtcC"
# FIELD_DOCTORS_EMAIL = "ViDnbjtmh5VhDJHby2hW"

# TODO: Add after API migration for Meal Category
# FIELD_HM7_MEAL_CATEGORY = "YjqeQEbuyZZGr0YApljP"
# FIELD_HM9_MEAL_CATEGORY = "x8571pZEntk6pNnQ92Qs"


# =============================================================================
# Resolvers - Functions to extract values from Client model
# =============================================================================

def _enrollment_client_id(client):
    """Primary enrollment platform identifier."""
    return str(client.pk)


def _total_household_members(client):
    """Total family/household size."""
    return client.total_family_members or client.household_size


def _agent_code_attestation_completed(client):
    """Agent code when attestation was completed."""
    # If we track attestation completion separately, use that
    # Otherwise, use current agent_code if consent is accepted
    if client.consent_status == "accepted":
        return client.agent_code
    return None


def _agent_code_external_services(client):
    """Agent code for external services coordination."""
    return client.agent_code


def _assigned_agent_attestation_requested(client):
    """Agent assigned when attestation was requested."""
    return client.agent_code


def _assigned_agent_attestation_complete(client):
    """Agent who completed attestation."""
    # Same as agent_code if consent accepted
    if client.consent_status == "accepted":
        return client.agent_code
    return None


def _date_of_first_delivery(client):
    """Calculated or stored delivery start date."""
    # Check for stored delivery date first
    # This might come from a Case or Service record
    return None  # TODO: Implement based on actual data source


def _hm_screening_status(client, hm_number):
    """Generic HM screening status checker.
    
    Returns "Yes" if client has a screening of the specified HM type.
    """
    # Query client's screenings for specific HM type
    screenings = client.screenings.filter(
        screen_type__icontains=f"HM #{hm_number}"
    ).exists()
    return "Yes" if screenings else None


def _hm3_screening(client):
    return _hm_screening_status(client, 3)


def _hm5_screening(client):
    return _hm_screening_status(client, 5)


def _hm9_screening(client):
    return _hm_screening_status(client, 9)


def _hm7_eligibility(client):
    """Check if client has HM #7 eligibility assessment."""
    eligibilities = client.assessments.filter(
        screen_type__icontains="HM #7"
    ).exists()
    return "Yes" if eligibilities else None


def _hm_member_enhanced(client, hm_number):
    """Check if member is 'enhanced' for specific HM program."""
    # This likely comes from screening data or flags
    # Return "Yes" or "No" based on enhanced status
    return None  # TODO: Implement based on actual data source


def _hm2_member_enhanced(client):
    return _hm_member_enhanced(client, 2)


def _hm4_member_enhanced(client):
    return _hm_member_enhanced(client, 4)


def _hm7_member_enhanced(client):
    return _hm_member_enhanced(client, 7)


def _hm_confirm_no_case(client, hm_number):
    """Confirm no meals/boxes case exists for household member."""
    # Check if there's a related case for this HM type
    # Return confirmation status
    return None  # TODO: Implement logic


def _hm4_confirm_no_case(client):
    return _hm_confirm_no_case(client, 4)


def _hm6_confirm_no_case(client):
    return _hm_confirm_no_case(client, 6)


def _hm9_confirm_no_case(client):
    return _hm_confirm_no_case(client, 9)


def _eligible_services(client):
    """List of services client is eligible for."""
    # Return as list for MULTIPLE_OPTIONS field
    services = client.eligible_for or []
    # Transform service codes to display names
    return services if services else None


def _member_status(client):
    """Client lifecycle stage for member status."""
    # Map lifecycle_stage to GHL status values
    status_map = {
        "lead": "Lead",
        "prospect": "Prospect",
        "screened": "Screened",
        "eligible": "Eligible",
        "ineligible": "Not Eligible",
        "client": "Active Client",
    }
    return status_map.get(client.lifecycle_stage)


def _final_verification_status(client):
    """Overall verification status based on screenings."""
    # Aggregate from related screenings (verified_at was removed; use status)
    verified_screenings = client.screenings.filter(
        screen_status__iexact="complete"
    ).exists()
    return "Complete" if verified_screenings else "Pending"


# =============================================================================
# Field Resolver Registry
# (field_id, resolver) pairs. Only non-empty resolved values are sent.
# =============================================================================

CONTACT_FIELD_RESOLVERS = [
    # Core identifiers
    (FIELD_ENROLLMENT_CLIENT_ID, _enrollment_client_id),
    (FIELD_TOTAL_HOUSEHOLD_MEMBERS, _total_household_members),
    
    # Agent/Assignment tracking
    (FIELD_AGENT_CODE_ATTESTATION_COMPLETED, _agent_code_attestation_completed),
    (FIELD_AGENT_CODE_EXTERNAL_SERVICES, _agent_code_external_services),
    (FIELD_ASSIGNED_AGENT_ATTESTATION_REQUESTED, _assigned_agent_attestation_requested),
    (FIELD_ASSIGNED_AGENT_ATTESTATION_COMPLETE, _assigned_agent_attestation_complete),
    
    # HM Screening tracking
    (FIELD_HM3_SCREENING, _hm3_screening),
    (FIELD_HM5_SCREENING, _hm5_screening),
    (FIELD_HM9_SCREENING, _hm9_screening),
    (FIELD_HM7_ELIGIBILITY, _hm7_eligibility),
    
    # HM Member Enhanced status
    (FIELD_HM2_MEMBER_ENHANCED, _hm2_member_enhanced),
    (FIELD_HM4_MEMBER_ENHANCED, _hm4_member_enhanced),
    (FIELD_HM7_MEMBER_ENHANCED, _hm7_member_enhanced),
    
    # System/Status fields
    (FIELD_ELIGIBLE_SERVICES, _eligible_services),
    (FIELD_MEMBER_STATUS, _member_status),
    (FIELD_FINAL_VERIFICATION_STATUS, _final_verification_status),
]


def build_custom_fields(client):
    """Return the GHL ``customFields`` array for a client (skips empties)."""
    out = []
    for field_id, resolver in CONTACT_FIELD_RESOLVERS:
        try:
            value = resolver(client)
        except Exception:
            value = None
        if value not in (None, ""):
            # GHL expects different formats based on field type
            out.append({"id": field_id, "field_value": value})
    return out
