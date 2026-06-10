"""Sync Screenings, Cases, and Eligibility assessments to GoHighLevel Opportunities.

Each record type maps to an opportunity in a specific pipeline:
- Screening → Screening Pipeline
- Case → Case Management Pipeline  
- Eligibility → Eligibility Pipeline

Opportunities are linked to the Contact via crm_contact_id.
"""

import hashlib
import json
import logging
from decimal import Decimal, InvalidOperation

import requests
from django.utils import timezone

from . import config

logger = logging.getLogger(__name__)

# =============================================================================
# Pipeline & Stage Configuration (from tmp/pipelines_id.csv)
# =============================================================================

# B: Screening - for HM screening opportunities
PIPELINE_SCREENING = "ENJvUOcoV0fQWX36V8Rq"

# C: Eligibility Assessment - for eligibility opportunities  
PIPELINE_ELIGIBILITY = "F6cAYzGyB9H1Tsb88QZO"

# G1: Internal Services - Food Delivery - for food delivery cases
# Other service types may map to different G-pipelines
PIPELINE_CASE_FOOD = "05nsZFCbcujvqSJIdlbN"

# D: Navigation - for navigation cases
PIPELINE_CASE_NAVIGATION = "2GToxmnm3MrMsotZ1kgn"

# E: External Services - for external service cases
PIPELINE_CASE_EXTERNAL = "vVnLwzTO1nkVxUt0zmdF"

# Default case pipeline (use food delivery as default for now)
PIPELINE_CASE = PIPELINE_CASE_FOOD

# F: Attestation - for attestation tracking
PIPELINE_ATTESTATION = "ld0HoLxCzj8ooiuOm8hX"

# Stage mappings - TODO: Get actual stage IDs from GHL for each pipeline
# Stages vary by pipeline and need to be configured per pipeline
STAGES = {
    "screening": {
        "new": "new",  # TODO: Replace with actual GHL stage ID
        "in_progress": "in_progress",
        "completed": "completed",
    },
    "eligibility": {
        "assigned": "assigned",
        "in_review": "in_review",
        "eligible": "eligible",
        "ineligible": "ineligible",
    },
    "case": {
        "open": "open",
        "in_progress": "in_progress",
        "authorized": "authorized",
        "closed": "closed",
    },
}


# =============================================================================
# Opportunity Custom Field IDs
# =============================================================================

# Core Identifiers
OP_FIELD_MEDICAID_ACTIVE = "ZtubJmMvebNIoZDU4ZQ1"
OP_FIELD_HM7_ENROLLMENT_CLIENT_ID = "YIA99adbI63iyFfaJcPl"
OP_FIELD_HM6_ENROLLMENT_CLIENT_ID = "Yc2fe2qhIBslTcQtublT"
OP_FIELD_HM3_ENROLLMENT_CLIENT_ID = "xUrnyFjlqiaK4iI7hI0U"
OP_FIELD_HM2_ENROLLMENT_CLIENT_ID = "WENdRf2mogcWav03WEDP"
OP_FIELD_HM5_ENROLLMENT_CLIENT_ID = "vVXgJXK0quzmm69l8aox"
OP_FIELD_HM6_EP_CLIENT_ID = "VsYvy4968SkjD2R3kefD"

# Status/Verification
OP_FIELD_FINAL_VERIFICATION_COMPLETE = "Wb6Y82h4yC9ieCsTHGmQ"
OP_FIELD_FINAL_VERIFICATION_STATUS = "WjuizZewAHUp2W3nXZL2"
OP_FIELD_FINAL_VERIFICATION_NOTE = "vPtYBPakJTQyhnQBW NW"
OP_FIELD_GENERAL_VERIFICATION_NOTE = "V7YKGHEonzDbf89qSkQA"
OP_FIELD_UNABLE_TO_REACH_MEMBER = "x8ech2HdWGslyzLFXC9C"

# Attestation
OP_FIELD_ATTESTATION_REQUESTED_DATE = "yA55yUbzmqQrt9YodIh1"
OP_FIELD_ATTESTATION_REQUESTED_METHOD = "YzhsUM1nL5vl4CKzT0Ke"
OP_FIELD_ATTESTATION_COMPLETED_METHOD = "ZLvhmJXqjMx3YUTWwAhl"

# Restrictions/Allergies
OP_FIELD_HM9_FOOD_ALLERGIES = "zgJWaql1i7kQ8A7ibYS8"
OP_FIELD_HM8_FOOD_ALLERGIES = "wzt3nlUv7yrdZY8Qy5Bv"
OP_FIELD_HM2_OTHER_ALLERGIES = "v5a7PN4WIQtf5CyxUYaL"
OP_FIELD_HM2_OTHER_RESTRICTIONS = "Yk0zLEy87tXyNzZtwbQn"
OP_FIELD_HM5_OTHER_RESTRICTIONS = "yFWKxbGlWr2tvgf6ksLL"
OP_FIELD_HM5_OTHER_RESTRICTIONS_2 = "W6YZwMQfaM4hGHLEjJqf"

# Confirmations
OP_FIELD_HM2_CONFIRM_NO_CASE = "Y9TLbdPcC1jldlKiKrJF"
OP_FIELD_HM4_CONFIRM_NO_CASE = "VA0OoK0KAobBzCxZLaJ"
OP_FIELD_HM6_CONFIRM_NO_CASE = "XjLns3DZE7bekTvc0qmu"

# Status Flags
OP_FIELD_HM6_ACTIVE_INSURANCE = "XjLns3DZE7bekTvc0qmu"
OP_FIELD_HM5_ACTIVE_INSURANCE = "VTuVaLmNZa6fPsyntaSY"
OP_FIELD_HM9_ELIGIBILITY = "VGtEgT8uMSbVqzIppA5k"
OP_FIELD_HM4_MEMBER_ENHANCED = "VA0OoK0KAobBzCxZLaJ"
OP_FIELD_HM3_ELIGIBILITY = "W4y4hQHzyTyMWTu6JBZP"
OP_FIELD_HM7_MEMBER_ENHANCED = "xStwW0W0f0Qq6jvibH9D"
OP_FIELD_HM2_MEMBER_ENHANCED = "w1MgWEq2umOoKWKPaFkQ"

# Screening Types
OP_FIELD_HM2_SCREENING = "v3PN3UCC4uPJDbwHc83c"
OP_FIELD_HM5_SCREENING = "x8571pZEntk6pNnQ92Qs"

# Form URLs
OP_FIELD_FINAL_VER_FORM_URL = "wU71kYX2bmN0xr8a3pPf"


# =============================================================================
# Transformers
# =============================================================================

def _parse_currency(value):
    """Parse currency string like '$8,736.00' to Decimal."""
    if not value:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    # Remove $ and commas
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _format_date(dt):
    """Format datetime/date to ISO string."""
    if not dt:
        return None
    if hasattr(dt, 'isoformat'):
        return dt.isoformat()
    return str(dt)


def _boolean_from_presence(value):
    """Convert presence check to Yes/No for GHL options."""
    return "Yes" if value else "No"


def _screening_to_opportunity_name(screening):
    """Generate opportunity name from screening."""
    client_name = f"{screening.client_first_name} {screening.client_last_name}".strip()
    screen_type = screening.screen_type or "Screening"
    return f"{screen_type} - {client_name}"


def _case_to_opportunity_name(case):
    """Generate opportunity name from case."""
    client_name = f"{case.client_first_name} {case.client_last_name}".strip()
    service = case.service_type or "Case"
    return f"{service} - {client_name}"


def _eligibility_to_opportunity_name(eligibility):
    """Generate opportunity name from eligibility."""
    client_name = f"{eligibility.client_first_name} {eligibility.client_last_name}".strip()
    status = eligibility.eligible_status or "Eligibility"
    return f"{status} - {client_name}"


def _map_status_to_stage(status, pipeline_type):
    """Map our status to GHL pipeline stage."""
    # TODO: Implement based on actual GHL pipeline stages
    mappings = {
        "screening": {
            "assigned": "new",
            "in_progress": "in_progress",
            "completed": "completed",
            "declined": "lost",
        },
        "case": {
            "open": "open",
            "authorized": "authorized",
            "closed": "closed",
        },
        "eligibility": {
            "eligible": "eligible",
            "ineligible": "ineligible",
            "pending": "in_review",
        },
    }
    return mappings.get(pipeline_type, {}).get(status, "new")


# =============================================================================
# Payload Builders
# =============================================================================

def build_screening_payload(screening):
    """Build GHL opportunity payload from Screening model."""
    client = screening.client
    if not client or not client.crm_contact_id:
        return None
    
    payload = {
        "name": _screening_to_opportunity_name(screening),
        "pipelineId": PIPELINE_SCREENING,
        "stageId": STAGES["screening"].get(
            _map_status_to_stage(screening.screen_status, "screening"), "new"
        ),
        "status": "open",  # open/won/lost
        "contactId": client.crm_contact_id,
    }
    
    # Monetary value if available
    # Screenings typically don't have monetary value
    
    # Custom fields
    custom = []
    
    # Enrollment platform ID
    if screening.enhanced_screen_id:
        custom.append({
            "id": OP_FIELD_HM3_ENROLLMENT_CLIENT_ID,
            "field_value": str(screening.enhanced_screen_id)
        })
    
    # Verification status
    if screening.verified_at:
        custom.append({
            "id": OP_FIELD_FINAL_VERIFICATION_COMPLETE,
            "field_value": "Yes"
        })
    
    if screening.screen_status:
        custom.append({
            "id": OP_FIELD_FINAL_VERIFICATION_STATUS,
            "field_value": screening.screen_status
        })
    
    # Decline/verification notes
    if screening.decline_note:
        note = screening.decline_note[:500]  # Truncate for LARGE_TEXT
        custom.append({
            "id": OP_FIELD_FINAL_VERIFICATION_NOTE,
            "field_value": note
        })
    
    # Outreach status
    if screening.outreach_status:
        unable_to_reach = screening.outreach_status.lower() in ["unreachable", "no_response"]
        custom.append({
            "id": OP_FIELD_UNABLE_TO_REACH_MEMBER,
            "field_value": _boolean_from_presence(unable_to_reach)
        })
    
    # Eligible services (as restrictions)
    if screening.eligible_services:
        services = screening.eligible_services
        if isinstance(services, list):
            custom.append({
                "id": OP_FIELD_HM5_OTHER_RESTRICTIONS,
                "field_value": services
            })
    
    # Duration (converted from seconds to minutes if needed)
    if screening.duration:
        # Store or transform as needed
        pass
    
    if custom:
        payload["customFields"] = custom
    
    return payload


def build_case_payload(case):
    """Build GHL opportunity payload from Case model."""
    client = case.client
    if not client or not client.crm_contact_id:
        return None
    
    payload = {
        "name": _case_to_opportunity_name(case),
        "pipelineId": PIPELINE_CASE,
        "stageId": STAGES["case"].get(
            _map_status_to_stage(case.case_status, "case"), "open"
        ),
        "status": "won" if case.case_status == "closed" else "open",
        "contactId": client.crm_contact_id,
    }
    
    # Monetary value from authorization
    if case.authorized_amount:
        value = _parse_currency(case.authorized_amount)
        if value:
            payload["monetaryValue"] = float(value)
    
    # Expected close date
    if case.service_authorization_request_ends_at:
        payload["expectedCloseDate"] = _format_date(
            case.service_authorization_request_ends_at
        )
    
    # Custom fields
    custom = []
    
    # Case ID reference
    if case.case_id:
        custom.append({
            "id": OP_FIELD_HM2_ENROLLMENT_CLIENT_ID,
            "field_value": str(case.case_id)
        })
    
    # Authorization status
    if case.service_authorization_status:
        custom.append({
            "id": OP_FIELD_FINAL_VERIFICATION_STATUS,
            "field_value": case.service_authorization_status
        })
    
    # Outcome
    if case.outcome_description:
        custom.append({
            "id": OP_FIELD_GENERAL_VERIFICATION_NOTE,
            "field_value": case.outcome_description[:500]
        })
    
    if custom:
        payload["customFields"] = custom
    
    return payload


def build_eligibility_payload(eligibility):
    """Build GHL opportunity payload from Eligibility model."""
    client = eligibility.client
    if not client or not client.crm_contact_id:
        return None
    
    # Determine status based on eligible_status
    opp_status = "won" if eligibility.eligible_status == "eligible" else "lost"
    
    payload = {
        "name": _eligibility_to_opportunity_name(eligibility),
        "pipelineId": PIPELINE_ELIGIBILITY,
        "stageId": STAGES["eligibility"].get(
            _map_status_to_stage(eligibility.eligible_status, "eligibility"), "in_review"
        ),
        "status": opp_status,
        "contactId": client.crm_contact_id,
    }
    
    # Custom fields
    custom = []
    
    # Eligibility ID
    if eligibility.eligibility_id:
        custom.append({
            "id": OP_FIELD_HM7_ENROLLMENT_CLIENT_ID,
            "field_value": str(eligibility.eligibility_id)
        })
    
    # Eligible status
    if eligibility.eligible_status:
        custom.append({
            "id": OP_FIELD_HM9_ELIGIBILITY,
            "field_value": eligibility.eligible_status
        })
    
    # Eligible services
    if eligibility.eligible_services:
        services = eligibility.eligible_services
        if isinstance(services, list):
            custom.append({
                "id": OP_FIELD_HM5_OTHER_RESTRICTIONS,
                "field_value": services
            })
    
    # Verification
    if eligibility.verified_at:
        custom.append({
            "id": OP_FIELD_FINAL_VERIFICATION_COMPLETE,
            "field_value": "Yes"
        })
    
    if custom:
        payload["customFields"] = custom
    
    return payload


# =============================================================================
# Sync Functions
# =============================================================================

def _extract_opportunity_id(data):
    """Extract opportunity ID from GHL response."""
    if not isinstance(data, dict):
        return None
    return data.get("id") or data.get("opportunity", {}).get("id")


def _payload_hash(payload):
    """Generate hash for deduplication."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def sync_screening(screening):
    """Sync a Screening to GHL as an Opportunity.
    
    Returns opportunity ID on success, None on failure.
    """
    if not config.is_enabled():
        return None
    
    payload = build_screening_payload(screening)
    if not payload:
        return None
    
    # Check if already synced and unchanged
    new_hash = _payload_hash(payload)
    if hasattr(screening, 'crm_opportunity_id') and screening.crm_opportunity_id:
        if hasattr(screening, 'crm_sync_hash') and screening.crm_sync_hash == new_hash:
            return screening.crm_opportunity_id
    
    try:
        if hasattr(screening, 'crm_opportunity_id') and screening.crm_opportunity_id:
            # Update existing
            url = f"{config.API_BASE}/opportunities/{screening.crm_opportunity_id}"
            resp = requests.put(
                url, headers=config.headers(), json=payload, timeout=config.TIMEOUT
            )
        else:
            # Create new
            url = f"{config.API_BASE}/opportunities"
            body = dict(payload, locationId=config.LOCATION_ID)
            resp = requests.post(
                url, headers=config.headers(), json=body, timeout=config.TIMEOUT
            )
        
        resp.raise_for_status()
        opp_id = _extract_opportunity_id(resp.json())
        
        if not opp_id:
            logger.warning("GHL screening sync returned no opportunity ID")
            return None
        
        # Store sync info on screening
        screening.crm_opportunity_id = opp_id
        screening.crm_sync_hash = new_hash
        screening.crm_synced_at = timezone.now()
        screening.save(update_fields=['crm_opportunity_id', 'crm_sync_hash', 'crm_synced_at'])
        
        return opp_id
        
    except requests.RequestException as exc:
        body = getattr(exc.response, 'text', '')[:300] if exc.response else ''
        logger.warning("GHL screening sync failed: %s %s", exc, body)
        return None
    except Exception:
        logger.exception("Unexpected error syncing screening to GHL")
        return None


def sync_case(case):
    """Sync a Case to GHL as an Opportunity."""
    if not config.is_enabled():
        return None
    
    payload = build_case_payload(case)
    if not payload:
        return None
    
    new_hash = _payload_hash(payload)
    if hasattr(case, 'crm_opportunity_id') and case.crm_opportunity_id:
        if hasattr(case, 'crm_sync_hash') and case.crm_sync_hash == new_hash:
            return case.crm_opportunity_id
    
    try:
        if hasattr(case, 'crm_opportunity_id') and case.crm_opportunity_id:
            url = f"{config.API_BASE}/opportunities/{case.crm_opportunity_id}"
            resp = requests.put(
                url, headers=config.headers(), json=payload, timeout=config.TIMEOUT
            )
        else:
            url = f"{config.API_BASE}/opportunities"
            body = dict(payload, locationId=config.LOCATION_ID)
            resp = requests.post(
                url, headers=config.headers(), json=body, timeout=config.TIMEOUT
            )
        
        resp.raise_for_status()
        opp_id = _extract_opportunity_id(resp.json())
        
        if opp_id:
            case.crm_opportunity_id = opp_id
            case.crm_sync_hash = new_hash
            case.crm_synced_at = timezone.now()
            case.save(update_fields=['crm_opportunity_id', 'crm_sync_hash', 'crm_synced_at'])
        
        return opp_id
        
    except requests.RequestException as exc:
        body = getattr(exc.response, 'text', '')[:300] if exc.response else ''
        logger.warning("GHL case sync failed: %s %s", exc, body)
        return None
    except Exception:
        logger.exception("Unexpected error syncing case to GHL")
        return None


def sync_eligibility(eligibility):
    """Sync an Eligibility to GHL as an Opportunity."""
    if not config.is_enabled():
        return None
    
    payload = build_eligibility_payload(eligibility)
    if not payload:
        return None
    
    new_hash = _payload_hash(payload)
    if hasattr(eligibility, 'crm_opportunity_id') and eligibility.crm_opportunity_id:
        if hasattr(eligibility, 'crm_sync_hash') and eligibility.crm_sync_hash == new_hash:
            return eligibility.crm_opportunity_id
    
    try:
        if hasattr(eligibility, 'crm_opportunity_id') and eligibility.crm_opportunity_id:
            url = f"{config.API_BASE}/opportunities/{eligibility.crm_opportunity_id}"
            resp = requests.put(
                url, headers=config.headers(), json=payload, timeout=config.TIMEOUT
            )
        else:
            url = f"{config.API_BASE}/opportunities"
            body = dict(payload, locationId=config.LOCATION_ID)
            resp = requests.post(
                url, headers=config.headers(), json=body, timeout=config.TIMEOUT
            )
        
        resp.raise_for_status()
        opp_id = _extract_opportunity_id(resp.json())
        
        if opp_id:
            eligibility.crm_opportunity_id = opp_id
            eligibility.crm_sync_hash = new_hash
            eligibility.crm_synced_at = timezone.now()
            eligibility.save(update_fields=['crm_opportunity_id', 'crm_sync_hash', 'crm_synced_at'])
        
        return opp_id
        
    except requests.RequestException as exc:
        body = getattr(exc.response, 'text', '')[:300] if exc.response else ''
        logger.warning("GHL eligibility sync failed: %s %s", exc, body)
        return None
    except Exception:
        logger.exception("Unexpected error syncing eligibility to GHL")
        return None
