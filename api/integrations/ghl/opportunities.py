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

# Sandbox and production share the same opportunity pipeline IDs, so these are
# hard-coded (from tmp/pipelines_id.csv). They must belong to GHL_LOCATION_ID.

# B: Screening - for HM screening opportunities
PIPELINE_SCREENING = "ENJvUOcoV0fQWX36V8Rq"

# C: Eligibility Assessment - for eligibility opportunities
PIPELINE_ELIGIBILITY = "F6cAYzGyB9H1Tsb88QZO"

# G1: Internal Services - Food Delivery - for food delivery cases
PIPELINE_CASE_FOOD = "05nsZFCbcujvqSJIdlbN"

# D: Navigation - for navigation cases
PIPELINE_CASE_NAVIGATION = "2GToxmnm3MrMsotZ1kgn"

# E: External Services - for external service cases
PIPELINE_CASE_EXTERNAL = "vVnLwzTO1nkVxUt0zmdF"

# Default case pipeline (use food delivery as default for now)
PIPELINE_CASE = PIPELINE_CASE_FOOD

# Canonical case pipelines by Case Category, keyed on the case's inferred
# category (keys are normalized lowercase). IDs come from tmp/pipelines_id.csv.
CASE_CATEGORY_PIPELINES = {
    "eligibility": PIPELINE_ELIGIBILITY,         # C: Eligibility Assessment
    "navigation": PIPELINE_CASE_NAVIGATION,      # D: Navigation
    "internal services": PIPELINE_CASE_FOOD,     # G1: Internal Services
    "external services": PIPELINE_CASE_EXTERNAL,  # E: External Services
}

# F: Attestation - for attestation tracking
PIPELINE_ATTESTATION = "ld0HoLxCzj8ooiuOm8hX"

# Screening pipeline stage to use for synced (already-completed) screenings.
# Resolved by NAME at runtime, so it works across locations. The B: Screening
# stages are: Created, Pending, !ISSUE!, Completed.
SCREENING_STAGE_COMPLETED = "Completed"

# Value for the "Originating Team" dropdown on screening opportunities.
# TODO: confirm the exact option label configured in GHL.
ORIGINATING_TEAM = "Benefully"

# Opportunity custom-field IDs for the B: Screening pipeline.
# IMPORTANT: these MUST be the CURRENT per-location ids -- dump them with
# `python manage.py ghl_fields --model opportunity` and fill in below.
# Leave a value as "" to skip sending that field.
OP_SCREENING_FIELDS = {
    "case_create_date": "GyamIQS6sK6DDaFv7xZK",      # DATE  opportunity.case_created
    "enrollment_case_id": "Rc5rlPesgeb4NsFixu1k",    # TEXT  opportunity.enrollment_id
    "enrollment_client_id": "3nu3OIVRqAYySA0RuHWB",  # TEXT  opportunity.enrollment_platform_client_id_ca
    "agent_code": "zmVYjtdwYlVKnlzbAeAX",            # NUMERICAL  opportunity.agent_code
    "assigned_agent": "X5mtvTCL91TvaOcoKy7B",        # TEXT  opportunity.agent
    "program_name": "",          # opportunity.program_name (KhagGTE91tkOsuXcrXzR) -- skipped
    "originating_team": "",      # SINGLE_OPTIONS opportunity.originating_team -- skipped
    "source": "",                # no opportunity Source field in GHL -- skipped
}

# C: Eligibility pipeline stage for synced (already-completed) assessments.
# TODO: confirm the actual stage name in the C: Eligibility pipeline.
ELIGIBILITY_STAGE_COMPLETED = "Completed"

# Opportunity custom-field IDs for the C: Eligibility pipeline (fill from the
# live dump, same as OP_SCREENING_FIELDS).
OP_ELIGIBILITY_FIELDS = {
    "case_create_date": "GyamIQS6sK6DDaFv7xZK",      # DATE  opportunity.case_created
    "enrollment_case_id": "Rc5rlPesgeb4NsFixu1k",    # TEXT  opportunity.enrollment_id
    "enrollment_client_id": "3nu3OIVRqAYySA0RuHWB",  # TEXT  opportunity.enrollment_platform_client_id_ca
    "agent_code": "zmVYjtdwYlVKnlzbAeAX",            # NUMERICAL  opportunity.agent_code
    "assigned_agent": "X5mtvTCL91TvaOcoKy7B",        # TEXT  opportunity.agent
    "source": "",                # no opportunity Source field in GHL -- skipped
}

# Opportunity custom-field IDs for the Case pipelines (same per-location ids as
# screening/eligibility where shared). program_name carries the case service.
OP_CASE_FIELDS = {
    "case_create_date": "GyamIQS6sK6DDaFv7xZK",      # DATE  opportunity.case_created
    "enrollment_case_id": "Rc5rlPesgeb4NsFixu1k",    # TEXT  opportunity.enrollment_id
    "enrollment_client_id": "3nu3OIVRqAYySA0RuHWB",  # TEXT  opportunity.enrollment_platform_client_id_ca
    "agent_code": "zmVYjtdwYlVKnlzbAeAX",            # NUMERICAL  opportunity.agent_code
    "assigned_agent": "X5mtvTCL91TvaOcoKy7B",        # TEXT  opportunity.agent
    "program_name": "KhagGTE91tkOsuXcrXzR",          # TEXT  opportunity.program_name (Service)
}

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
# Field ID -> human label (DEBUG ONLY)
# =============================================================================
# Used purely to annotate console output so we can recognize each GHL field by
# name. Labels mirror the GHL field names (from `manage.py ghl_fields
# --model opportunity`). NOT sent in the request body.
OP_FIELD_LABELS = {
    "GyamIQS6sK6DDaFv7xZK": "Case Create Date",
    "Rc5rlPesgeb4NsFixu1k": "Enrollment Platform Case ID",
    "3nu3OIVRqAYySA0RuHWB": "Enrollment Platform Client ID (Case Level)",
    "zmVYjtdwYlVKnlzbAeAX": "Agent Code",
    "X5mtvTCL91TvaOcoKy7B": "Assigned Agent",
    "KhagGTE91tkOsuXcrXzR": "Program Name (Service)",
    OP_FIELD_FINAL_VERIFICATION_STATUS: "Final Verification Status",
    OP_FIELD_GENERAL_VERIFICATION_NOTE: "General Verification Note",
}


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


def _client_full_name(obj):
    """Build a client name from the related Client (denormalized name fields
    were removed from Screening/Case/Eligibility)."""
    client = getattr(obj, "client", None)
    if client is None:
        return ""
    return f"{client.first_name or ''} {client.last_name or ''}".strip()


def _screening_to_opportunity_name(screening):
    """Generate opportunity name from screening."""
    client_name = _client_full_name(screening)
    screen_type = screening.screen_type or "Screening"
    return f"{screen_type} - {client_name}"


def _case_to_opportunity_name(case):
    """Generate a distinct opportunity name from a case.

    A client can have several cases, so the name must be unique per case or GHL
    collapses them into one opportunity. We use ``{service} - {client name}``
    plus a short case-id fragment to guarantee uniqueness across same-service
    cases.
    """
    client_name = _client_full_name(case)
    service = case.service_type or "Case"
    base = f"{service} - {client_name}".strip(" -")
    suffix = str(getattr(case, "case_id", "") or "")[:8]
    return f"{base} ({suffix})" if suffix else base


def _eligibility_to_opportunity_name(eligibility):
    """Generate opportunity name from eligibility."""
    client_name = _client_full_name(eligibility)
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
# Pipeline ID resolution
# =============================================================================
# The hard-coded PIPELINE_* constants are GHL "originId"s (the template ids
# shared across locations / sandbox+prod). The opportunity API requires the
# real per-location pipeline "id", which differs per location. We resolve
# originId -> id at runtime via the pipelines endpoint and cache the map.

# {originId|id: {"id": real_id, "stages": {stage_name_lower: stage_id}}}
_PIPELINE_MAP = None


def _load_pipeline_map():
    """Fetch and cache pipeline + stage ids for the configured location."""
    global _PIPELINE_MAP
    if _PIPELINE_MAP:
        return _PIPELINE_MAP
    mapping = {}
    try:
        url = f"{config.API_BASE}/opportunities/pipelines"
        resp = requests.get(
            url, headers=config.headers(),
            params={"locationId": config.LOCATION_ID}, timeout=config.TIMEOUT,
        )
        resp.raise_for_status()
        for p in resp.json().get("pipelines", []):
            real_id = p.get("id")
            if not real_id:
                continue
            stages = {
                (s.get("name") or "").strip().lower(): s.get("id")
                for s in p.get("stages", []) if s.get("id")
            }
            info = {"id": real_id, "stages": stages}
            mapping[real_id] = info              # already a real id
            origin = p.get("originId")
            if origin:
                mapping[origin] = info           # template id -> real id
    except Exception:
        logger.exception("Failed to load GHL pipelines for id resolution")
    # Only cache a populated map so a transient failure can be retried.
    if mapping:
        _PIPELINE_MAP = mapping
    return mapping


def _resolve_pipeline_id(origin_or_id):
    """Translate a template originId to this location's real pipeline id."""
    info = _load_pipeline_map().get(origin_or_id)
    return info["id"] if info else origin_or_id


def _resolve_stage_id(origin_or_id, stage_name):
    """Return this location's real stage id for a stage name within a pipeline."""
    info = _load_pipeline_map().get(origin_or_id)
    if not info or not stage_name:
        return None
    return info["stages"].get(stage_name.strip().lower())


def _agent_name(agent_code):
    """Resolve an agent's full name from their agent_code (Agent model)."""
    if not agent_code:
        return None
    from api.models import Agent  # local import to avoid circular import
    return (
        Agent.objects.filter(agent_code=agent_code)
        .values_list("name", flat=True)
        .first()
    )


def _to_number(value):
    """Coerce a value to int/float for GHL NUMERICAL fields, else None."""
    if value in (None, ""):
        return None
    try:
        s = str(value).strip()
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except (TypeError, ValueError):
        return None


# =============================================================================
# Payload Builders
# =============================================================================

def build_screening_payload(screening):
    """Build GHL opportunity payload from Screening model."""
    client = screening.client
    if not client or not client.crm_contact_id:
        return None
    
    payload = {
        "name": _client_full_name(screening),
        "pipelineId": _resolve_pipeline_id(PIPELINE_SCREENING),
        "status": "open",  # open/won/lost/abandoned
        "contactId": client.crm_contact_id,
    }

    # Screenings synced from Unite US are already done -> "Completed" stage.
    stage_id = _resolve_stage_id(PIPELINE_SCREENING, SCREENING_STAGE_COMPLETED)
    if stage_id:
        payload["pipelineStageId"] = stage_id

    # Opportunity custom fields. IDs live in OP_SCREENING_FIELDS and MUST be the
    # current per-location ids (dump via `manage.py ghl_fields --model opportunity`).
    custom = []

    def _add(field_id, value):
        if field_id and value not in (None, "", []):
            custom.append({"id": field_id, "field_value": value})

    _add(OP_SCREENING_FIELDS.get("case_create_date"),
         screening.screen_created_at.isoformat() if screening.screen_created_at else None)
    _add(OP_SCREENING_FIELDS.get("enrollment_case_id"), str(screening.enhanced_screen_id))
    _add(OP_SCREENING_FIELDS.get("enrollment_client_id"),
         str(client.client_id) if client.client_id else None)
    _add(OP_SCREENING_FIELDS.get("agent_code"), _to_number(client.agent_code))
    _add(OP_SCREENING_FIELDS.get("assigned_agent"),
         client.agent_name or _agent_name(client.agent_code))
    # program_name + originating_team intentionally skipped for now.
    _add(OP_SCREENING_FIELDS.get("source"), client.lead_source)

    if custom:
        payload["customFields"] = custom

    return payload


def _infer_case_category(case):
    """Best-effort Case Category from the case text, used to route the case to
    a GHL pipeline. Mirrors the source data's category buckets."""
    text = (
        f"{getattr(case, 'service_type', '') or ''} "
        f"{getattr(case, 'program_name', '') or ''}"
    ).lower()
    if "eligibility" in text:
        return "eligibility"
    if "navigation" in text or "care management" in text:
        return "navigation"
    if any(k in text for k in (
        "medically tailored meals", "(mtm)", "clinically appropriate meals",
        "food prescriptions: boxes",
    )):
        return "internal services"
    return "external services"


def _resolve_case_pipeline(case):
    """Return the GHL pipeline id for a case.

    Infer the Case Category from the case text and route by category
    (CASE_CATEGORY_PIPELINES), defaulting to External Services.
    """
    program = (getattr(case, "program_name", "") or "").strip()
    category = _infer_case_category(case)
    origin_id = CASE_CATEGORY_PIPELINES.get(category, PIPELINE_CASE_EXTERNAL)
    resolved = _resolve_pipeline_id(origin_id)
    print(f"[GHL]   pipeline: no program match for '{program}' -> category "
          f"fallback '{category}' ({origin_id} => {resolved})", flush=True)
    return resolved


def build_case_payload(case):
    """Build GHL opportunity payload from Case model."""
    client = case.client
    if not client or not client.crm_contact_id:
        return None
    
    payload = {
        "name": _case_to_opportunity_name(case),
        "pipelineId": _resolve_case_pipeline(case),
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

    def _add(field_id, value):
        if field_id and value not in (None, "", []):
            custom.append({"id": field_id, "field_value": value})

    # Requested case mappings.
    _add(OP_CASE_FIELDS.get("case_create_date"),
         case.date_opened.isoformat() if case.date_opened else None)
    _add(OP_CASE_FIELDS.get("enrollment_case_id"),
         str(case.case_id) if case.case_id else None)
    _add(OP_CASE_FIELDS.get("enrollment_client_id"),
         str(client.client_id) if client.client_id else None)
    _add(OP_CASE_FIELDS.get("agent_code"), _to_number(client.agent_code))
    _add(OP_CASE_FIELDS.get("assigned_agent"),
         client.agent_name or _agent_name(client.agent_code))
    _add(OP_CASE_FIELDS.get("program_name"), case.service_type)

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
    
    # Determine status based on eligible_status. The model stores values like
    # "Eligible" / "Not Eligible", so compare case-insensitively.
    opp_status = (
        "won"
        if (eligibility.eligible_status or "").strip().lower() == "eligible"
        else "lost"
    )

    payload = {
        "name": _client_full_name(eligibility),
        "pipelineId": _resolve_pipeline_id(PIPELINE_ELIGIBILITY),
        "status": opp_status,
        "contactId": client.crm_contact_id,
    }

    # Eligibility synced from Unite US is already done -> "Completed" stage
    # (skipped automatically if that stage name doesn't exist in the pipeline).
    stage_id = _resolve_stage_id(PIPELINE_ELIGIBILITY, ELIGIBILITY_STAGE_COMPLETED)
    if stage_id:
        payload["pipelineStageId"] = stage_id

    # Opportunity custom fields -- same shape as screening. IDs MUST be the
    # current per-location ids (dump via `manage.py ghl_fields --model opportunity`).
    custom = []

    def _add(field_id, value):
        if field_id and value not in (None, "", []):
            custom.append({"id": field_id, "field_value": value})

    _add(OP_ELIGIBILITY_FIELDS.get("case_create_date"),
         eligibility.screen_created_at.isoformat() if eligibility.screen_created_at else None)
    _add(OP_ELIGIBILITY_FIELDS.get("enrollment_case_id"), str(eligibility.assessment_id))
    _add(OP_ELIGIBILITY_FIELDS.get("enrollment_client_id"),
         str(client.client_id) if client.client_id else None)
    _add(OP_ELIGIBILITY_FIELDS.get("agent_code"), _to_number(client.agent_code))
    _add(OP_ELIGIBILITY_FIELDS.get("assigned_agent"),
         client.agent_name or _agent_name(client.agent_code))
    _add(OP_ELIGIBILITY_FIELDS.get("source"), client.lead_source)

    if custom:
        payload["customFields"] = custom

    return payload


# =============================================================================
# Sync Functions
# =============================================================================

def _annotate_payload(payload):
    """Return a display copy of the payload where every custom field also
    carries its human ``label`` (from OP_FIELD_LABELS). The label is for console
    readability only and is NOT part of what we send to GHL."""
    display = dict(payload)
    fields = payload.get("customFields")
    if isinstance(fields, list):
        display["customFields"] = [
            {
                "id": f.get("id"),
                "label": OP_FIELD_LABELS.get(f.get("id"), "(unknown field)"),
                "field_value": f.get("field_value"),
            }
            for f in fields
        ]
    return display


def _print_payload(label, payload):
    """Write a labeled view of the JSON we're about to send to GHL.

    Custom fields are annotated with their GHL field label so we can recognize
    each one; the real request body (sent in _sync_opportunity) stays clean.
    """
    print(f"[GHL]   {label} JSON to send (labels are debug-only) ->", flush=True)
    print(json.dumps(_annotate_payload(payload), default=str, indent=2), flush=True)


def _extract_opportunity_id(data):
    """Extract opportunity ID from GHL response."""
    if not isinstance(data, dict):
        return None
    return data.get("id") or data.get("opportunity", {}).get("id")


def _payload_hash(payload):
    """Generate hash for deduplication."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Fields accepted on create/upsert but rejected by the update (PUT) endpoint
# (the contact is fixed once the opportunity exists). Stripped on update.
_OPP_UPDATE_EXCLUDED_FIELDS = {"contactId"}


def _opportunity_is_gone(resp):
    """True when a PUT update failed because the opportunity no longer exists
    in GHL (e.g. it was deleted there but we still hold its id). GHL returns
    400/404 with a "doesn't exist or is deleted" message in that case."""
    if resp.status_code not in (400, 404):
        return False
    text = (resp.text or "").lower()
    return "doesn't exist" in text or "does not exist" in text or "deleted" in text


def _duplicate_existing_id(resp):
    """Return the existing opportunity id when GHL refused a create because the
    contact already has an opportunity in this pipeline (duplicates disabled).
    GHL returns 400 with ``meta.existingId`` in that case; else None."""
    if resp.status_code != 400:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if "duplicate opportunity" not in (data.get("message") or "").lower():
        return None
    return (data.get("meta") or {}).get("existingId")


def _create_opportunity(payload):
    """POST a new opportunity. Returns the requests.Response."""
    body = dict(payload, locationId=config.LOCATION_ID)
    url = f"{config.API_BASE}/opportunities/"
    print(f"[GHL]   POST {url}", flush=True)
    print(f"[GHL]   body -> {json.dumps(body, default=str, indent=2)}", flush=True)
    resp = requests.post(
        url, headers=config.headers(), json=body, timeout=config.TIMEOUT
    )
    print(f"[GHL]   <- {resp.status_code} {resp.text[:500]}", flush=True)
    return resp


def _update_opportunity(opp_id, payload):
    """PUT an update to an existing opportunity. Returns the requests.Response."""
    update_body = {
        k: v for k, v in payload.items() if k not in _OPP_UPDATE_EXCLUDED_FIELDS
    }
    url = f"{config.API_BASE}/opportunities/{opp_id}"
    print(f"[GHL]   PUT {url}", flush=True)
    print(f"[GHL]   body -> {json.dumps(update_body, default=str, indent=2)}",
          flush=True)
    resp = requests.put(
        url, headers=config.headers(), json=update_body, timeout=config.TIMEOUT
    )
    print(f"[GHL]   <- {resp.status_code} {resp.text[:500]}", flush=True)
    return resp


def _sync_opportunity(obj, payload, label):
    """Create or update a GHL opportunity for ``obj`` and persist the id back.

    Shared by screenings, cases, and eligibility. Creates when the object has
    no ``crm_opportunity_id`` yet (POST ``/opportunities/``), otherwise updates
    the existing opportunity (PUT ``/opportunities/{id}``). Self-heals in both
    directions:
    - deleted in GHL (update 400/404 "doesn't exist") -> recreate.
    - deleted locally but still in GHL (create 400 "duplicate" with an
      existingId) -> adopt that id and update instead.
    Errors are logged, never raised, so a CRM hiccup can't break the local save.
    """
    new_hash = _payload_hash(payload)
    opp_id = getattr(obj, "crm_opportunity_id", "")

    # Nothing changed since the last successful push -> skip the round trip.
    if opp_id and getattr(obj, "crm_sync_hash", "") == new_hash:
        return opp_id

    try:
        if opp_id:
            resp = _update_opportunity(opp_id, payload)
            # Self-heal: opportunity was deleted in GHL -> recreate from scratch.
            if _opportunity_is_gone(resp):
                print(f"[GHL]   opportunity {opp_id} gone in GHL -> recreating",
                      flush=True)
                obj.crm_opportunity_id = ""
                resp = _create_opportunity(payload)
        else:
            resp = _create_opportunity(payload)

        # Self-heal: GHL already has an opportunity for this contact (local id
        # was lost / duplicates disabled). Adopt the existing one and update it.
        existing_id = _duplicate_existing_id(resp)
        if existing_id:
            print(f"[GHL]   contact already has opportunity {existing_id} -> "
                  "adopting + updating", flush=True)
            obj.crm_opportunity_id = existing_id
            resp = _update_opportunity(existing_id, payload)

        resp.raise_for_status()
        new_id = _extract_opportunity_id(resp.json())
        if not new_id:
            print(f"[GHL]   WARNING: {label} synced but no opportunity id returned.",
                  flush=True)
            logger.warning(
                "GHL %s sync succeeded but returned no opportunity id: %s",
                label, resp.text[:300],
            )
            return None

        obj.crm_opportunity_id = new_id
        obj.crm_sync_hash = new_hash
        obj.crm_synced_at = timezone.now()
        obj.save(
            update_fields=["crm_opportunity_id", "crm_sync_hash", "crm_synced_at"]
        )
        print(f"[GHL]   OK {label} -> opportunity {new_id}", flush=True)
        return new_id

    except requests.RequestException as exc:
        body = getattr(exc.response, "text", "")[:300] if exc.response else ""
        print(f"[GHL]   ERROR {label} sync failed: {exc} {body}", flush=True)
        logger.warning("GHL %s sync failed: %s %s", label, exc, body)
        return None
    except Exception:  # never let CRM issues break the local save
        import traceback
        print(f"[GHL]   EXCEPTION syncing {label}:\n{traceback.format_exc()}",
              flush=True)
        logger.exception("Unexpected error syncing %s to GHL", label)
        return None


def sync_screening(screening):
    """Sync a Screening to GHL as an Opportunity (create or update)."""
    sid = getattr(screening, "enhanced_screen_id", "?")
    print(f"[GHL] sync_screening({sid}) enabled={config.is_enabled()}", flush=True)
    if not config.is_enabled():
        print("[GHL]   SKIPPED: CRM sync disabled (CRM_SYNC_ENABLED / token / "
              "location not set).", flush=True)
        return None
    payload = build_screening_payload(screening)
    if not payload:
        client = getattr(screening, "client", None)
        cid = getattr(client, "crm_contact_id", "") if client else ""
        print(f"[GHL]   SKIPPED: no payload (client={getattr(client, 'pk', None)}, "
              f"crm_contact_id={cid or '(none)'}). Save the Profile first so the "
              "contact exists.", flush=True)
        return None
    _print_payload("screening", payload)
    return _sync_opportunity(screening, payload, "screening")


def sync_case(case):
    """Sync a Case to GHL as an Opportunity (create or update)."""
    cid = getattr(case, "case_id", "?")
    print(f"[GHL] sync_case({cid}) enabled={config.is_enabled()}", flush=True)
    if not config.is_enabled():
        print("[GHL]   SKIPPED: CRM sync disabled (CRM_SYNC_ENABLED / token / "
              "location not set).", flush=True)
        return None
    payload = build_case_payload(case)
    if not payload:
        client = getattr(case, "client", None)
        ccid = getattr(client, "crm_contact_id", "") if client else ""
        print(f"[GHL]   SKIPPED: no payload (client={getattr(client, 'pk', None)}, "
              f"crm_contact_id={ccid or '(none)'}). Save the Profile first so the "
              "contact exists.", flush=True)
        return None
    _print_payload("case", payload)
    return _sync_opportunity(case, payload, "case")


def sync_eligibility(eligibility):
    """Sync an Eligibility to GHL as an Opportunity (create or update)."""
    eid = getattr(eligibility, "assessment_id", "?")
    print(f"[GHL] sync_eligibility({eid}) enabled={config.is_enabled()}", flush=True)
    if not config.is_enabled():
        print("[GHL]   SKIPPED: CRM sync disabled (CRM_SYNC_ENABLED / token / "
              "location not set).", flush=True)
        return None
    payload = build_eligibility_payload(eligibility)
    if not payload:
        client = getattr(eligibility, "client", None)
        cid = getattr(client, "crm_contact_id", "") if client else ""
        print(f"[GHL]   SKIPPED: no payload (client={getattr(client, 'pk', None)}, "
              f"crm_contact_id={cid or '(none)'}). Save the Profile first so the "
              "contact exists.", flush=True)
        return None
    _print_payload("eligibility", payload)
    return _sync_opportunity(eligibility, payload, "eligibility")
