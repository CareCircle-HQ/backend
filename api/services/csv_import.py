"""Manual Unite Us **CSV** import (initial setup + backup recovery).

Mirrors the daily API pull (``api.services.uniteus_import``) but the source is an
uploaded CSV export instead of the live API: each row is mapped into the dict
shape our existing DRF serializers accept, then upserted through them — reusing
all of their idempotent ``update_or_create`` + reconcile logic.

The Clients export is **denormalized**: one row per (client x address x
insurance/coverage), so a client appears on many rows. We therefore group rows by
``client_id`` (the Unite Us UUID = ``Client`` PK — the only dedupe key) and build
one nested ClientSerializer payload per client, collecting the distinct
addresses / insurances / social-care coverages.

Unite Us is the system of record for clients/insurance/coverage, so the import is
authoritative: it passes ``reconcile_insurances`` / ``reconcile_social_care_coverages``
so records absent from the export are deactivated (verified rows are left alone).
Runs are silent (no tickets/timeline spam) — this is a bulk load — and roll up
into an :class:`~api.models.ImportRun`.
"""
import csv
import io
import logging
import re
from collections import OrderedDict

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone
from django.utils.dateparse import parse_datetime

import json

from api.history import ChangeSource, change_context
from api.models import (
    AddressType,
    Assessment,
    Case,
    CaseStatus,
    CaseType,
    Client,
    IdentifiedSocialNeed,
    ImportRun,
    ImportRunStatus,
    Insurance,
    InsurancePlanType,
    Note,
    NoteSource,
    OutcomeResolutionType,
    Screening,
    ServiceAuthorizationStatus,
    SocialCareCoverage,
    SocialCareCoverageStatus,
    UniteUsAgent,
    VerifiedSocialNeed,
)
from api.serializers import (
    AssessmentSerializer,
    CaseSerializer,
    ClientSerializer,
    ScreeningSerializer,
    derive_case_type,
)
from api.services import timeline, tickets

logger = logging.getLogger(__name__)

CSV_SOURCE = "csv_uniteus"
TIMELINE_ACTOR = "system:csv-import"

# Unite Us creator/author allowlist scope: only cases/assessments/screenings/
# notes authored by a UniteUsAgent on one of these CareCircle teams are imported
# (matched on originating_team, sourced from the CareCircle roster). Everyone
# else -- notably Met Council Team -- is excluded. An EMPTY match set means no
# gate (accept all), so imports keep working until the roster is populated.
CARECIRCLE_ALLOWLIST_TEAMS = ("CareCircle Call Center", "CareCircle Street Team")

# Export types exposed in the Settings > Import web UI.
SUPPORTED_EXPORT_TYPES = ("clients", "screening", "assessments", "cases", "notes")

# Every export type the engine understands. Now identical to the web-UI set --
# notes is offered in Settings too (uploaded to S3 + processed by Celery like
# the rest), not just via ``manage.py import_csv``.
CLI_EXPORT_TYPES = SUPPORTED_EXPORT_TYPES


# --- streaming helpers -----------------------------------------------------
def _text_stream(file_obj):
    """Wrap a binary file-like in a streaming UTF-8 (BOM-tolerant) text reader.

    Avoids loading the whole upload into memory -- the denormalized screening
    export runs to several GB. Handles plain binary files (CLI
    ``open(path, "rb")``), ``io.BytesIO``, and Django ``UploadedFile`` objects
    (whose raw bytes live on ``.file``). Falls back to decoding an
    already-read payload for objects that aren't TextIOWrapper-compatible.
    """
    raw = getattr(file_obj, "file", file_obj)
    try:
        raw.seek(0)
    except (AttributeError, OSError, ValueError):
        pass
    try:
        return io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    except (TypeError, AttributeError):
        data = file_obj.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8-sig")
        return io.StringIO(data)


def _iter_contiguous_groups(reader, key_field):
    """Yield ``(key, rows)`` for runs of consecutive rows sharing ``key_field``.

    Memory-safe for huge exports: only one entity's rows are held at a time
    (the Unite Us denormalized exports emit all rows for an entity together).
    Rows with a blank key are yielded individually as ``(None, [row])`` so the
    caller can count them as skipped without disturbing the current group.
    """
    current_key = None
    bucket = []
    for row in reader:
        k = (row.get(key_field) or "").strip()
        if not k:
            yield None, [row]
            continue
        if k != current_key:
            if bucket:
                yield current_key, bucket
            current_key, bucket = k, [row]
        else:
            bucket.append(row)
    if bucket:
        yield current_key, bucket


# --- value parsing helpers -------------------------------------------------
def _s(row, key):
    """Trimmed string for a column (missing column -> '')."""
    return (row.get(key) or "").strip()


def _bool(row, key):
    return _s(row, key).lower() in ("true", "t", "1", "yes", "y")


def _list_field(value):
    """Parse a CSV cell into a list of strings: JSON array, or pipe/semicolon
    delimited, else a single-element list. Mirrors the extension's array shape."""
    v = (value or "").strip()
    if not v:
        return []
    if v.startswith("["):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, TypeError):
            pass
    for sep in ("|", ";"):
        if sep in v:
            return [p.strip() for p in v.split(sep) if p.strip()]
    return [v]


def _int(row, key):
    v = _s(row, key)
    if not v:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _date(row, key):
    """ISO date/datetime string -> 'YYYY-MM-DD' for DRF DateField."""
    v = _s(row, key)
    return v[:10] if v else None


def _dt(row, key):
    """Pass an ISO datetime string through (DRF DateTimeField parses it)."""
    return _s(row, key) or None


def _aware_dt(row, key):
    """Parse an ISO datetime to a timezone-aware value for direct ORM writes
    (those that bypass DRF, which would otherwise warn on naive datetimes)."""
    raw = _s(row, key)
    if not raw:
        return None
    dt = parse_datetime(raw)
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _enum(value, allowed, default=""):
    v = (value or "").strip().lower()
    return v if v in allowed else default


def _zip5(value):
    """A clean 5-digit US ZIP, or "" when the source can't yield one.

    Business rule: never import a ZIP with more than 5 digits. A ZIP+4
    ("12345-6789" / 9 straight digits) is reduced to its 5-digit base; anything
    that isn't a 5- or 9-digit code (garbage, partial, foreign) is dropped."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) in (5, 9):
        return digits[:5]
    return ""


def _format_phone(value):
    """Auto-format a US phone number to '(XXX) XXX-XXXX'.

    Strips a leading country code ('1') and any punctuation; a clean 10-digit
    number is formatted, otherwise the trimmed original is kept (so we never
    lose an unparseable number, e.g. an extension or a foreign format)."""
    raw = (value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return raw


# Email validity check — the export occasionally contains junk (e.g. a phone
# number, "n/a", or a malformed address like "foo@bar..com") in the email
# column, which the EmailField would reject and fail the whole client. We drop
# such values rather than block the import. Use Django's own validator so this
# check matches the serializer's EmailField exactly (a looser regex would let
# values through that the serializer then rejects, re-introducing the failure).
def _valid_email(value):
    value = (value or "").strip()
    if not value:
        return False
    try:
        validate_email(value)
    except ValidationError:
        return False
    return True


# --- per-client mapping ----------------------------------------------------
def _address_from_row(row):
    """One ``Address`` dict from a row, or None when there is no street."""
    line1 = _s(row, "client_address_line1")
    line2 = _s(row, "client_address_line2")
    street = " ".join(p for p in (line1, line2) if p).strip()
    if not street:
        return None
    return {
        "type": _enum(
            _s(row, "client_address_type"), set(AddressType.values), AddressType.CURRENT
        ),
        "street": street,
        "city": _s(row, "client_address_city"),
        "county": _s(row, "client_address_county"),
        "state": _s(row, "client_address_state")[:2],
        "zip": _zip5(_s(row, "client_address_postal_code")),
        "created_at": _dt(row, "client_address_created_at"),
        "updated_at": _dt(row, "client_address_updated_at"),
        "_is_current": _bool(row, "client_address_is_current"),
    }


def _insurance_from_row(row):
    """A medical-insurance dict (``insurance_plan_type`` != 'social')."""
    return {
        "insurance_id": _s(row, "insurance_id"),
        "plan_external_id": _s(row, "insurance_plan_external_id"),
        "plan_type": _enum(_s(row, "insurance_plan_type"), set(InsurancePlanType.values)),
        "plan_name": _s(row, "insurance_plan_name"),
        "external_group_id": _s(row, "external_group_id"),
        "external_member_id": _s(row, "external_member_id"),
        "status": _enum(_s(row, "insurance_record_status"), {"active", "pending", "inactive", "expired"}),
        "record_status": _enum(_s(row, "insurance_record_status"), {"active", "pending", "inactive", "expired"}),
        "enrolled_at": _dt(row, "insurance_enrolled_at"),
        "expired_at": _dt(row, "insurance_expired_at"),
        "verified": _bool(row, "insurance_verified"),
        "verified_at": _dt(row, "insurance_verified_at"),
        "ingested": _bool(row, "insurance_ingested"),
        "is_primary": bool(
            _s(row, "insurance_id")
            and _s(row, "insurance_id") == _s(row, "primary_health_insurance_id")
        ),
        "created_at": _dt(row, "insurance_created_at"),
        "updated_at": _dt(row, "insurance_updated_at"),
    }


def _coverage_from_row(row):
    """A social-care-coverage dict (``insurance_plan_type`` == 'social'). Note:
    the export reuses the insurance_* columns; 'social' is only the discriminator,
    not the plan type, so we don't set plan_type here."""
    return {
        "coverage_id": _s(row, "insurance_id"),
        "plan_name": _s(row, "insurance_plan_name"),
        "external_group_id": _s(row, "external_group_id"),
        "external_member_id": _s(row, "external_member_id"),
        "status": _enum(_s(row, "insurance_status"), set(SocialCareCoverageStatus.values)),
        "enrolled_at": _dt(row, "insurance_enrolled_at"),
        "expired_at": _dt(row, "insurance_expired_at"),
        "verified": _bool(row, "insurance_verified"),
        "verified_at": _dt(row, "insurance_verified_at"),
        "ingested": _bool(row, "insurance_ingested"),
        "created_at": _dt(row, "insurance_created_at"),
        "updated_at": _dt(row, "insurance_updated_at"),
    }


def map_client_group(client_id, rows):
    """Build one ClientSerializer payload from all CSV rows for ``client_id``.

    Profile scalars are taken from the most-recently-updated row; addresses,
    insurances, and social-care coverages are collected as de-duplicated child
    lists from across the group.
    """
    # Most recently updated row wins for the scalar profile fields.
    profile = max(rows, key=lambda r: _s(r, "client_updated_at"))

    out = {
        "client_id": client_id,
        "first_name": _s(profile, "first_name"),
        "middle_name": _s(profile, "middle_name"),
        "last_name": _s(profile, "last_name"),
        "date_of_birth": _date(profile, "date_of_birth"),
        "gender": _s(profile, "gender"),
        "marital_status": _s(profile, "marital_status"),
        "race": _s(profile, "race"),
        "ethnicity": _s(profile, "ethnicity"),
        "sexuality": _s(profile, "sexuality"),
        "citizenship": _s(profile, "citizenship"),
        "client_phone_number": _format_phone(_s(profile, "client_phone_number")),
        "phone_type": _s(profile, "phone_type"),
        "client_email_address": _s(profile, "client_email_address"),
        "consent_status": _s(profile, "client_consent_status").lower(),
        "consent_accepted": _s(profile, "client_consent_status").lower() == "accepted",
        "consented_at": _dt(profile, "client_consented_at"),
        "household_size": _int(profile, "household_size"),
        "adults_in_household": _int(profile, "adults_in_household"),
        "children_in_household": _int(profile, "children_in_household"),
        "gross_monthly_income": _s(profile, "gross_monthly_income"),
        "preferred_spoken_language": _s(profile, "preferred_spoken_language"),
        "preferred_written_language": _s(profile, "preferred_written_language"),
        "care_coordinator": _s(profile, "care_coordinator"),
        "care_coordinator_status": _s(profile, "care_coordinator_status"),
        "created_at": _dt(profile, "client_created_at"),
        "updated_at": _dt(profile, "client_updated_at"),
        # Authoritative: deactivate stored records absent from this export.
        "reconcile_insurances": True,
        "reconcile_social_care_coverages": True,
    }

    # Drop the email unless it is well-formed (blank or junk values would
    # otherwise fail the client's EmailField and skip the whole record).
    if not _valid_email(out["client_email_address"]):
        out.pop("client_email_address")

    # Addresses: keep the latest per address type, preferring the current one.
    addrs = {}
    for r in rows:
        a = _address_from_row(r)
        if a is None:
            continue
        key = a["type"]
        if key not in addrs or a.pop("_is_current", False):
            a.pop("_is_current", None)
            addrs[key] = a
        else:
            a.pop("_is_current", None)
    if addrs:
        out["addresses"] = list(addrs.values())

    # Insurance + coverage, discriminated by insurance_plan_type, de-duped by id.
    ins, scc = OrderedDict(), OrderedDict()
    for r in rows:
        plan_type = _s(r, "insurance_plan_type").lower()
        if plan_type == "social":
            cid = _s(r, "insurance_id") or _s(r, "insurance_plan_name")
            if _s(r, "insurance_plan_name"):
                scc[cid] = _coverage_from_row(r)
        elif plan_type:  # any non-social, non-blank plan type = medical insurance
            iid = _s(r, "insurance_id") or _s(r, "insurance_plan_name")
            if _s(r, "insurance_plan_name"):
                ins[iid] = _insurance_from_row(r)
    if ins:
        out["insurances"] = list(ins.values())
    if scc:
        out["social_care_coverages"] = list(scc.values())

    return out


# --- per-screening mapping -------------------------------------------------
def _answer_value(row):
    """Human-readable answer for a screening row, matching what the extension
    captures (``it.a``): the selected option's label, else the free-text /
    typed value."""
    for key in ("question_option_text", "value_string", "answer_value"):
        v = _s(row, key)
        if v:
            return v
    for key in (
        "answer_value_bool", "answer_value_int",
        "answer_value_float", "answer_value_datetime",
    ):
        v = _s(row, key)
        if v:
            return v
    return ""


def map_screening_group(screen_id, rows):
    """Collapse the denormalized (one-row-per-answer) screening rows for a single
    ``enhanced_screen_id`` into the ScreeningSerializer payload.

    ``questions_answers`` is ``[{question, answer}]`` and ``identified_social_needs``
    is an array of name strings — the SAME JSON shapes the extension POSTs (see
    ``buildScreeningPayloads`` in sidepanel.js), so imported and extension-captured
    screenings are interchangeable.
    """
    head = rows[0]
    out = {
        "enhanced_screen_id": screen_id,
        "subject_id": _s(head, "subject_id"),
        "screen_created_at": _dt(head, "screen_created_at"),
        "screen_status": _s(head, "screen_status"),
        "screen_type": _s(head, "screen_type"),
        "screen_source": _s(head, "screen_source"),
        "provider_name": _s(head, "provider_name"),
        "performing_organization_name": _s(head, "performing_organization_name"),
        "eligible_status": _s(head, "eligible_status"),
    }
    duration = _int(head, "duration")
    if duration is not None:
        out["duration"] = duration
    services = _list_field(_s(head, "eligible_services"))
    if services:
        out["eligible_services"] = services

    # questions_answers: dedupe by answer_id (the cross-join with needs repeats
    # each answer once per identified/verified need on the screen).
    qa, seen = [], set()
    for r in rows:
        question = _s(r, "question_primary_text")
        if not question:
            continue
        answer = _answer_value(r)
        key = _s(r, "answer_id") or (question, answer)
        if key in seen:
            continue
        seen.add(key)
        qa.append({"question": question, "answer": answer})
    if qa:
        out["questions_answers"] = qa

    # identified_social_needs: array of distinct name strings (extension shape).
    names, seen_names = [], set()
    for r in rows:
        name = _s(r, "identified_social_need_name")
        if name and name not in seen_names:
            seen_names.add(name)
            names.append(name)
    if names:
        out["identified_social_needs"] = names

    return out


def _screening_need_rows(rows):
    """Distinct IdentifiedSocialNeed / VerifiedSocialNeed dicts for a screen,
    keyed on their source UUIDs (deduped across the denormalized rows)."""
    identified, verified = OrderedDict(), OrderedDict()
    for r in rows:
        iid = _s(r, "identified_social_need_id")
        if iid and iid not in identified:
            identified[iid] = {
                "identified_social_need_id": iid,
                "identified_social_need_code": _s(r, "identified_social_need_code"),
                "identified_social_need_name": _s(r, "identified_social_need_name"),
                "identified_created_at": _aware_dt(r, "identified_created_at"),
                "identified_updated_at": _aware_dt(r, "identified_updated_at"),
                "is_need_sensitive": _bool(r, "is_need_sensitive"),
            }
        vid = _s(r, "verified_social_need_id")
        if vid and vid not in verified:
            verified[vid] = {
                "verified_social_need_id": vid,
                "verified_social_need_code": _s(r, "verified_social_need_code"),
                "verified_social_need_name": _s(r, "verified_social_need_name"),
                "verified_created_at": _aware_dt(r, "verified_created_at"),
                "verified_updated_at": _aware_dt(r, "verified_updated_at"),
            }
    return list(identified.values()), list(verified.values())


# --- per-assessment mapping ------------------------------------------------
def map_assessment_group(submission_id, rows):
    """Collapse the denormalized (one-row-per-question) assessment rows for a
    single ``submission_id`` into the AssessmentSerializer payload.

    ``questions_answers`` is ``[{question, answer}]`` — the SAME shape the
    extension POSTs (see ``buildEligibilityPayloads`` in sidepanel.js). The
    assessments export carries no eligibility results, so ``eligible_status`` /
    ``eligible_services`` are left empty.
    """
    head = rows[0]
    out = {
        "assessment_id": submission_id,
        "subject_id": _s(head, "client_id"),
        "screen_created_at": _dt(head, "submission_created_at"),
        "form_name": _s(head, "form_name"),
        # The submitter person -> provider_name (matches the extension); the
        # network/org string -> performing_organization_name.
        "provider_name": _s(head, "submission_created_by_name"),
        "performing_organization_name": _s(head, "provider_name"),
    }
    qa = []
    for r in rows:
        question = _s(r, "question")
        if not question:
            continue
        qa.append({"question": question, "answer": _s(r, "responses")})
    if qa:
        out["questions_answers"] = qa
    return out


# --- per-case mapping ------------------------------------------------------
# Unite Us authorization state -> our ServiceAuthorizationStatus (mirrors
# mappers._AUTH_STATE_MAP in the daily import). The cases export carries states
# our enum doesn't model 1:1: "requested"/"deferred" are pre-decision states ->
# Pending; "draft" stays unmapped (normalized status blank, raw label kept).
_AUTH_STATE_MAP = {
    "accepted": "approved",
    "requested": "pending",
    "deferred": "pending",
    # A rejected authorization is a denial (drives the case to Closed).
    "rejected": "denied",
}


def map_case_row(row):
    """A single cases-export row -> CaseSerializer payload (one row per case).

    Mirrors the daily import's ``map_case`` semantics:
    - CSV ``service_subtype`` -> model ``service_type`` (the daily import stores
      the service name there, e.g. "Social Service Case Management", which is
      what ``derive_case_type`` keys on). CSV ``service_type`` (the broad
      category, e.g. "Food Assistance") -> model ``service_category``.
    - Unknown ``case_status`` values fall back to Open; auth status is
      normalized but the raw label is preserved.
    """
    out = {
        "case_id": _s(row, "case_id"),
        "client_id": _s(row, "client_id"),
        "subject_id": _s(row, "client_id"),
    }

    def set_(key, value):
        if value not in (None, ""):
            out[key] = value

    set_("previous_case_id", _s(row, "previous_case_id"))
    set_("created_by_id", _s(row, "case_created_by_id"))
    set_("created_by_name", _s(row, "case_created_by_name"))
    # Prefer the Unite Us case-created timestamp; fall back to the user-entered
    # opened date only when the source carries no creation timestamp.
    set_("date_opened", _dt(row, "case_created_at") or _dt(row, "user_entered_opened_date"))
    set_("updated_at", _dt(row, "case_updated_at"))
    set_("ar_submitted_on", _dt(row, "ar_submitted_on"))
    set_("case_processed_at", _dt(row, "case_processed_at"))
    set_("case_managed_at", _dt(row, "case_managed_at"))
    set_("case_off_platform_at", _dt(row, "case_off_platform_at"))
    closed_at = _dt(row, "case_closed_at") or _dt(row, "user_entered_closed_date")
    set_("case_closed_at", closed_at)
    set_("closed_note", _s(row, "closed_note"))
    set_("case_description", _s(row, "case_description"))

    # Case status is Open/Closed ONLY, driven by the closed date. Unite Us keeps
    # the exported state as "managed" even after closing, so a populated closed
    # date is the only reliable "closed" signal (mirrors the daily API import's
    # map_case + the browser extension); everything else is Open. Authorization
    # status is tracked separately and NEVER drives the case status.
    out["case_status"] = CaseStatus.CLOSED if closed_at else CaseStatus.OPEN
    set_("started_as_assistance_request", _bool(row, "started_as_assistance_request"))
    set_("case_is_referred", _bool(row, "case_is_referred"))

    set_("network_id", _s(row, "network_id"))
    set_("network_name", _s(row, "network_name"))
    set_("originating_provider_id", _s(row, "originating_provider_id"))
    set_("originating_provider_name", _s(row, "originating_provider_name"))
    set_("provider_id", _s(row, "provider_id"))
    set_("provider_name", _s(row, "provider_name"))
    set_("out_of_network_provider_name", _s(row, "out_of_network_provider_name"))
    set_("program_id", _s(row, "program_id"))
    set_("program_name", _s(row, "program_name"))
    set_("primary_worker_id", _s(row, "primary_worker_id"))
    set_("primary_worker_name", _s(row, "primary_worker_name"))
    # CSV service_subtype carries the service name used for classification.
    set_("service_type", _s(row, "service_subtype"))
    # CSV service_type is the BROAD category (e.g. "Food Assistance") -- stored
    # as service_category (on the live API this is the service node's parent).
    set_("service_category", _s(row, "service_type"))

    set_("outcome_id", _s(row, "outcome_id"))
    set_("outcome_description", _s(row, "outcome_description"))
    set_(
        "outcome_resolution_type",
        _enum(_s(row, "outcome_resolution_type"), OutcomeResolutionType.values),
    )

    raw_auth = _s(row, "service_authorization_status")
    if raw_auth:
        norm = _AUTH_STATE_MAP.get(raw_auth.lower(), raw_auth.lower())
        if norm in ServiceAuthorizationStatus.values:
            out["service_authorization_status"] = norm
        out["service_authorization_status_label"] = raw_auth.replace("_", " ").title()
    elif out["case_status"] == CaseStatus.OPEN:
        # An OPEN case with a BLANK authorization request has never had an
        # authorization requested -- record that explicitly as "Never Requested"
        # instead of leaving a blank, so the authorization UI reads a real state.
        # Neutral in lifecycle logic (like blank): not favorable, not denied.
        out["service_authorization_status"] = ServiceAuthorizationStatus.NEVER_REQUESTED
        out["service_authorization_status_label"] = "Never Requested"
    set_("service_authorization_request_starts_at", _dt(row, "service_authorization_request_starts_at"))
    set_("service_authorization_request_ends_at", _dt(row, "service_authorization_request_ends_at"))
    set_("service_authorization_approval_starts_at", _dt(row, "service_authorization_approval_starts_at"))
    set_("service_authorization_approval_ends_at", _dt(row, "service_authorization_approval_ends_at"))
    return out


# --- importer --------------------------------------------------------------
class CsvImporter:
    # Case-ticket actions to detect but never auto-create from a CSV import.
    # (Currently none: the case_no_services rule was removed because only the
    # household primary holds internal-service cases, so it flooded members.)
    SKIP_TICKET_ACTIONS = frozenset()

    def __init__(self, run, emit_side_effects=True, create_tickets=False,
                 emit_timeline=True):
        self.run = run
        # Write per-record audit timeline events. Separate from create_tickets so
        # an import can keep the derived state fresh (enrollment reconcile, funnel,
        # Care Management warnings) WITHOUT the high-volume timeline writes -- Care
        # Management is the source of truth for what needs attention.
        self.emit_timeline = emit_timeline
        self.errors = []
        self.stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        self.dataset = "clients"  # stats label; set per import_* method
        # When False, import rows only -- skip timeline events + funnel-stage
        # recompute. Used for bulk historical loads where the derived trail is
        # regenerated separately (and avoids timeline dedupe collisions with the
        # daily API sync).
        self.emit_side_effects = emit_side_effects
        # Every client_id whose data this run created/updated. At the end of the
        # run we recompute each one's funnel stage ONCE, so the import always
        # self-heals lifecycle_stage even on bulk loads (emit_side_effects=False)
        # and regardless of the order the client/case/screening files arrive in.
        self.touched_client_ids = set()
        # Clients whose INTERNAL-SERVICE cases this run touched. The client-wide
        # authorization reconcile (pause/cancel/resume/advance) is deferred during
        # the row loop and run ONCE per client here, on the full case picture, so
        # a client with several cases isn't reconciled against a partial state
        # mid-stream (see reconcile_touched_cases + deferred_internal_service_reconcile).
        self.reconcile_client_ids = set()
        # --- live progress (for the async S3 + Celery flow) --------------------
        # Every _count() call == one processed work item (a row for cases/notes,
        # a grouped entity for clients/screenings/assessments). ``processed`` is
        # the numerator; ``progress_total`` the denominator (set once known via
        # _set_total). We flush both to the ImportRun row every ``flush_every``
        # items with a cheap UPDATE so the UI can poll a real percentage that
        # survives page reloads -- without touching the run object mid-stream.
        self.processed = 0
        self.progress_total = None
        self.flush_every = 200
        # --- follow-up actions (case imports only) ----------------------------
        # create_tickets=False (default) => PREVIEW: detect the follow-up tickets
        # a case import WOULD open and record them for review, WITHOUT writing any
        # Ticket rows. =True => also open them (parity with the daily sync). The
        # aggregate (stats["actions"]) + a capped detail list (planned_actions)
        # are stored on the run either way so an agent can review before/after.
        self.create_tickets = create_tickets
        self._track_actions = False  # flipped on by import_cases
        self._planned_cap = 1000
        self.actions = {
            "applied": create_tickets,   # ticket creation enabled for this run
            "tickets": 0,                # detected (all rules)
            "tickets_created": 0,        # actually opened
            "cases_closed": 0,
            "auth_changed": 0,
            "timeline_events": 0,
            "auth_changed_to": {},  # {new_status: count}
        }
        self.planned_actions = []
        # Case imports only: how many of the imported (created/updated) cases are
        # Internal Service (the ones that drive the member base). Tracked apart
        # from ``stats`` so it never inflates the processed/total counts; surfaced
        # in the cases dataset stats at ``finalize``.
        self.internal_service_count = 0

    def _flush_progress(self):
        ImportRun.objects.filter(pk=self.run.pk).update(
            processed_count=self.processed, progress_total=self.progress_total,
        )

    def _set_total(self, total):
        """Record the number of work items this run will process (denominator)."""
        self.progress_total = int(total) if total is not None else None
        self._flush_progress()

    def _count(self, kind):
        self.stats[kind] += 1
        self.processed += 1
        if self.processed % self.flush_every == 0:
            self._flush_progress()

    def _mark_touched(self, client_id):
        if client_id:
            self.touched_client_ids.add(str(client_id))

    def _recompute_stage(self, client_id, client):
        """Recompute the client's funnel stage after an import write, isolating
        failures from the run. No-op when the record isn't linked to a client."""
        if not client_id or client is None:
            return
        try:
            from api.services.lifecycle import recompute_client_stage

            # actor must be a User or None (StageEvent.actor is an FK); the
            # import is system-driven so we pass no actor.
            recompute_client_stage(client)
        except Exception:  # noqa: BLE001 - never abort the import on a funnel hiccup
            logger.warning("recompute_client_stage failed for %s", client_id, exc_info=True)

    def _post_save(self, client):
        """Emit per-client timeline events (no tickets).

        Mirrors the daily API import's per-client trail: the clients export only
        carries consent, so this records the consent + insurance + coverage
        events (deduped, so re-imports won't duplicate them). Each emit is
        isolated — it must never fail the import row.

        Gated on ``emit_timeline``: these consent/insurance/coverage writes are
        the bulk of the import's DB load, and the Settings-page upload disables
        them (Care Management is the source of truth for what needs attention).
        The funnel stage is NOT recomputed here — ``recompute_touched()`` derives
        it once at the end of the run from the full picture, so an in-loop
        recompute would just double the per-client stage-derivation queries.
        """
        if not self.emit_timeline:
            return
        for builder, obj in self._timeline_targets(client):
            try:
                builder(obj, source=ChangeSource.IMPORT, actor=TIMELINE_ACTOR)
            except Exception:  # noqa: BLE001
                logger.warning("csv_import timeline emit failed (%s)", type(obj).__name__, exc_info=True)

    @staticmethod
    def _timeline_targets(client):
        yield timeline.event_for_consent, client
        for ins in Insurance.objects.filter(client=client):
            yield timeline.event_for_insurance, ins
        for scc in SocialCareCoverage.objects.filter(client=client):
            yield timeline.event_for_social_care_coverage, scc

    def import_clients(self, reader):
        # Group denormalized rows by client_id (the only dedupe key).
        groups = OrderedDict()
        for row in reader:
            cid = (row.get("client_id") or "").strip()
            if not cid:
                self._count("skipped")
                continue
            groups.setdefault(cid, []).append(row)

        # Denominator = blank-key rows already skipped + one item per group.
        self._set_total(self.processed + len(groups))
        for cid, rows in groups.items():
            existed = Client.objects.filter(pk=cid).exists()
            try:
                payload = map_client_group(cid, rows)
                ser = ClientSerializer(data=payload)
                ser.is_valid(raise_exception=True)
                client = ser.save()
                self._mark_touched(client.pk)
                self._count("updated" if existed else "created")
                if self.emit_side_effects:
                    self._post_save(client)
                    # Client-based eligibility disposition: now that ALL of this
                    # client's rows (insurances, coverages, addresses) are
                    # persisted, evaluate the CareCircle gates ONCE and dispose
                    # (INELIGIBLE + note + timeline + stop future deliveries).
                    try:
                        from api.services.eligibility import (
                            reconcile_client_eligibility,
                        )

                        reconcile_client_eligibility(
                            client, actor_label=TIMELINE_ACTOR,
                            source=ChangeSource.IMPORT,
                        )
                    except Exception as exc:  # isolate from the client upsert
                        logger.warning(
                            "csv_import eligibility %s failed: %s", client.pk, exc
                        )
            except Exception as exc:  # isolate one bad client from the run
                self._count("errors")
                self.errors.append(f"client {cid}: {exc}")
                logger.warning("csv_import client %s failed: %s", cid, exc)

    def import_screenings(self, reader, provider_id=None, provider_name=None):
        self.dataset = "screenings"
        # Optional provider scope: import only screens performed by the given
        # provider (id OR name, case-insensitive/trimmed). Non-matching screens
        # are counted as skipped. Lets a network-wide export be narrowed to a
        # single provider (e.g. Met Council).
        want_id = (str(provider_id).strip() if provider_id else "")
        want_name = (provider_name or "").strip().casefold()
        provider_filter = bool(want_id or want_name)
        # Unite Us facilitator allowlist: when any allowlisted UniteUsAgent rows
        # exist, only import screens whose ``facilitator_id`` is one of them.
        # NB: the screening export's ``facilitator_id`` maps to
        # ``UniteUsAgent.employee_id`` (NOT ``user_id``, which is what the cases
        # export's ``case_created_by_id`` maps to). Only agents on a CareCircle
        # team (CARECIRCLE_ALLOWLIST_TEAMS) count -- Met Council Team agents are
        # excluded. An EMPTY list means no gate -- accept all -- so imports keep
        # working until the roster is populated.
        allow_facilitator_ids = {
            str(e).lower()
            for e in UniteUsAgent.objects.filter(
                originating_team__in=CARECIRCLE_ALLOWLIST_TEAMS,
                employee_id__isnull=False,
            ).values_list("employee_id", flat=True)
        }
        # Group the denormalized (one-row-per-answer) export by screen. The Unite
        # Us export does NOT guarantee a screen's rows are contiguous -- they can
        # be scattered across the file -- so collect ALL rows per screen id before
        # building the payload. (A contiguous-only pass would create each screen
        # from just the first fragment of its answers and skip the rest as
        # "already exists".) Holds the file in memory; fine for the
        # few-hundred-MB exports we import.
        groups = OrderedDict()
        for row in reader:
            sid = (row.get("enhanced_screen_id") or "").strip()
            if not sid:
                self._count("skipped")
                continue
            groups.setdefault(sid, []).append(row)

        # Denominator = blank-key rows already skipped + one item per screen.
        self._set_total(self.processed + len(groups))
        for sid, rows in groups.items():
            head = rows[0]
            # Facilitator allowlist (only enforced when the list is non-empty).
            if allow_facilitator_ids:
                facilitator = (head.get("facilitator_id") or "").strip().lower()
                if facilitator not in allow_facilitator_ids:
                    self._count("skipped")
                    continue
            if provider_filter:
                row_id = (head.get("provider_id") or "").strip()
                row_name = (head.get("provider_name") or "").strip().casefold()
                if not ((want_id and row_id == want_id)
                        or (want_name and row_name == want_name)):
                    self._count("skipped")
                    continue
            # Append-only + idempotent: screenings are immutable once complete,
            # so skip any enhanced_screen_id we already store. This keeps
            # re-imports cheap and non-destructive.
            if Screening.objects.filter(pk=sid).exists():
                self._count("skipped")
                continue
            try:
                payload = map_screening_group(sid, rows)
                ser = ScreeningSerializer(data=payload)
                ser.is_valid(raise_exception=True)
                screening = ser.save()
                self._mark_touched(screening.client_id)
                self._save_screening_needs(screening, rows)
                self._count("created")
                if self.emit_side_effects:
                    self._emit_screening_timeline(screening)
            except Exception as exc:  # isolate one bad screen from the run
                self._count("errors")
                self.errors.append(f"screening {sid}: {exc}")
                logger.warning("csv_import screening %s failed: %s", sid, exc)

    @staticmethod
    def _save_screening_needs(screening, rows):
        identified, verified = _screening_need_rows(rows)
        for d in identified:
            IdentifiedSocialNeed.objects.update_or_create(
                identified_social_need_id=d.pop("identified_social_need_id"),
                defaults={**d, "screening": screening},
            )
        for d in verified:
            VerifiedSocialNeed.objects.update_or_create(
                verified_social_need_id=d.pop("verified_social_need_id"),
                defaults={**d, "screening": screening},
            )

    def _emit_screening_timeline(self, screening):
        try:
            timeline.event_for_screening(
                screening, source=ChangeSource.IMPORT, actor=TIMELINE_ACTOR,
            )
        except Exception:  # noqa: BLE001 - never fail the import on a timeline hiccup
            logger.warning("csv_import screening timeline failed", exc_info=True)

    def import_assessments(self, reader, provider_id=None, provider_name=None):
        self.dataset = "assessments"
        # Optional provider scope: import only submissions performed by the given
        # provider (id OR name, case-insensitive/trimmed). Non-matching ones are
        # counted as skipped. Lets a network-wide export be narrowed to a single
        # provider (e.g. Met Council).
        want_id = (str(provider_id).strip() if provider_id else "")
        want_name = (provider_name or "").strip().casefold()
        provider_filter = bool(want_id or want_name)
        # Unite Us creator allowlist: when any allowlisted UniteUsAgent rows
        # exist, only import submissions whose ``submission_created_by_id`` is
        # one of them. This maps to ``UniteUsAgent.user_id`` (the SAME key the
        # cases export's ``case_created_by_id`` uses -- unlike screenings, whose
        # ``facilitator_id`` maps to ``employee_id``). Only agents on a CareCircle
        # team (CARECIRCLE_ALLOWLIST_TEAMS) count -- Met Council Team agents are
        # excluded. An EMPTY list means no gate -- accept all -- so imports keep
        # working until the roster is populated.
        allow_creator_ids = {
            str(u).lower()
            for u in UniteUsAgent.objects.filter(
                originating_team__in=CARECIRCLE_ALLOWLIST_TEAMS
            ).values_list("user_id", flat=True)
        }
        # Group the denormalized (one-row-per-question) rows by submission.
        groups = OrderedDict()
        for row in reader:
            sid = (row.get("submission_id") or "").strip()
            if not sid:
                self._count("skipped")
                continue
            groups.setdefault(sid, []).append(row)

        # Denominator = blank-key rows already skipped + one item per submission.
        self._set_total(self.processed + len(groups))
        for sid, rows in groups.items():
            head = rows[0]
            # Creator allowlist (only enforced when the list is non-empty).
            if allow_creator_ids:
                creator = (head.get("submission_created_by_id") or "").strip().lower()
                if creator not in allow_creator_ids:
                    self._count("skipped")
                    continue
            if provider_filter:
                row_id = (head.get("provider_id") or "").strip()
                row_name = (head.get("provider_name") or "").strip().casefold()
                if not ((want_id and row_id == want_id)
                        or (want_name and row_name == want_name)):
                    self._count("skipped")
                    continue
            existed = Assessment.objects.filter(pk=sid).exists()
            try:
                payload = map_assessment_group(sid, rows)
                ser = AssessmentSerializer(data=payload)
                ser.is_valid(raise_exception=True)
                assessment = ser.save()
                self._mark_touched(assessment.client_id)
                self._count("updated" if existed else "created")
                if self.emit_side_effects:
                    self._post_save_assessment(assessment)
            except Exception as exc:  # isolate one bad submission from the run
                self._count("errors")
                self.errors.append(f"assessment {sid}: {exc}")
                logger.warning("csv_import assessment %s failed: %s", sid, exc)

    def _post_save_assessment(self, assessment):
        """Emit the assessment timeline + recompute the funnel stage, mirroring
        AssessmentViewSet (no tickets)."""
        try:
            timeline.event_for_assessment(
                assessment, source=ChangeSource.IMPORT, actor=TIMELINE_ACTOR,
            )
        except Exception:  # noqa: BLE001
            logger.warning("csv_import assessment timeline failed", exc_info=True)
        self._recompute_stage(assessment.client_id, assessment.client)

    def import_notes(self, reader):
        self.dataset = "notes"
        # Unite Us author allowlist: when any US-flagged UniteUsAgent rows exist,
        # only import notes whose ``noted_by_employee_id`` is one of them. This
        # maps to ``UniteUsAgent.employee_id`` (the SAME key the screening
        # export's ``facilitator_id`` uses). We also use the matched agent to
        # translate the author into a readable name -- the notes export carries
        # no author-name column. Only agents on a CareCircle team
        # (CARECIRCLE_ALLOWLIST_TEAMS) count. An EMPTY allowlist means no gate --
        # accept all -- but author_name is then only filled when the employee id
        # happens to match an agent.
        agents_by_emp = {
            str(a.employee_id).lower(): a
            for a in UniteUsAgent.objects.exclude(employee_id__isnull=True)
        }
        allow_author_ids = {
            emp for emp, a in agents_by_emp.items()
            if a.originating_team in CARECIRCLE_ALLOWLIST_TEAMS
        }
        # Pre-load existing Unite Us note ids so re-runs are a cheap set lookup
        # (the model has no unique constraint on source_note_id).
        existing_note_ids = set(
            Note.objects.filter(source=NoteSource.UNITE_US)
            .exclude(source_note_id="")
            .values_list("source_note_id", flat=True)
        )
        # Per-run caches so shared clients/cases aren't re-queried each row.
        client_cache = {}
        case_cache = {}

        def _client(cid):
            if cid not in client_cache:
                client_cache[cid] = Client.objects.filter(pk=cid).first()
            return client_cache[cid]

        def _case(case_id):
            if case_id not in case_cache:
                case_cache[case_id] = Case.objects.filter(pk=case_id).first()
            return case_cache[case_id]

        # One row per note -- stream directly, no grouping needed.
        for row in reader:
            note_id = (row.get("note_id") or "").strip()
            if not note_id:
                self._count("skipped")
                continue
            # Author allowlist (only enforced when the list is non-empty).
            author_emp = (row.get("noted_by_employee_id") or "").strip().lower()
            if allow_author_ids and author_emp not in allow_author_ids:
                self._count("skipped")
                continue
            # Idempotent: skip note ids we already store.
            if note_id in existing_note_ids:
                self._count("skipped")
                continue
            # Track notes by client: require the client to exist so a re-run
            # after the clients import picks up any that were missing.
            cid = (row.get("client_id") or "").strip()
            client = _client(cid) if cid else None
            if client is None:
                self._count("skipped")
                continue
            # Link the case when the note's subject is a Case we already store.
            case = None
            if (row.get("subject_type") or "").strip().lower() == "case":
                sid = (row.get("subject_id") or "").strip()
                if sid:
                    case = _case(sid)
            agent = agents_by_emp.get(author_emp)
            try:
                Note.objects.create(
                    client=client,
                    case=case,
                    source=NoteSource.UNITE_US,
                    source_note_id=note_id,
                    author_name=(agent.name if agent else ""),
                    body=(row.get("text") or "").strip(),
                    source_created_at=_aware_dt(row, "note_created_at"),
                )
                existing_note_ids.add(note_id)
                self._count("created")
            except Exception as exc:  # isolate one bad note from the run
                self._count("errors")
                self.errors.append(f"note {note_id}: {exc}")
                logger.warning("csv_import note %s failed: %s", note_id, exc)

    def import_cases(self, reader, provider_id=None, provider_name=None):
        self.dataset = "cases"
        # Case imports detect/preview follow-up actions (tickets, closes, auth
        # changes) -- record them in stats["actions"] + planned_actions.
        self._track_actions = self.emit_side_effects
        # Optional provider scope: import only rows serviced by the given
        # provider (id OR name, case-insensitive/trimmed). Non-matching rows are
        # counted as skipped. Used to load a single provider (e.g. Met Council)
        # from a network-wide export.
        want_id = (str(provider_id).strip() if provider_id else "")
        want_name = (provider_name or "").strip().casefold()
        provider_filter = bool(want_id or want_name)
        # Unite Us creator allowlist: when any allowlisted UniteUsAgent rows are
        # configured, only import cases whose ``case_created_by_id`` is in that
        # list (it maps exactly to Case.created_by_id). Only agents on a
        # CareCircle team (CARECIRCLE_ALLOWLIST_TEAMS) count -- Met Council Team
        # agents are excluded. An EMPTY list means no gate -- accept all -- so
        # existing imports keep working until the roster is populated.
        allow_creator_ids = {
            str(u).lower()
            for u in UniteUsAgent.objects.filter(
                originating_team__in=CARECIRCLE_ALLOWLIST_TEAMS
            ).values_list("user_id", flat=True)
        }
        # STRICT Met Council org gate: keep a case ONLY if Met Council
        # MANAGES/services it -- i.e. provider_id == the Met Council id OR
        # provider_name == "Met Council - SCN - PHS". The originating columns
        # (originating_provider_id / originating_provider_name) are deliberately
        # IGNORED here: a case Met Council merely CREATED/referred (even a meal
        # case) is out of scope for the extraction unless Met Council also
        # manages it. Any case with no managing Met Council signal is dropped.
        from api.services.lifecycle import is_met_council_case

        # One row per case — stream directly, no grouping needed.
        for row in reader:
            cid = (row.get("case_id") or "").strip()
            if not cid:
                self._count("skipped")
                continue
            # Managing-provider gate (provider_id OR provider_name). Originating
            # is NOT considered (allow_originating=False, and we pass no
            # originating id).
            prov_id = (row.get("provider_id") or "").strip()
            prov_name = (row.get("provider_name") or "").strip()
            keep = is_met_council_case(
                provider_id=prov_id, provider_name=prov_name, allow_originating=False,
            )
            if not keep:
                self._count("skipped")
                continue
            # Creator allowlist (only enforced when the list is non-empty).
            if allow_creator_ids:
                creator = (row.get("case_created_by_id") or "").strip().lower()
                if creator not in allow_creator_ids:
                    self._count("skipped")
                    continue
            if provider_filter:
                row_id = (row.get("provider_id") or "").strip()
                row_name = (row.get("provider_name") or "").strip().casefold()
                if not ((want_id and row_id == want_id)
                        or (want_name and row_name == want_name)):
                    self._count("skipped")
                    continue
            # Referral cases (Unite Us case_status == "referred") are intake
            # referrals, not managed cases -- never import them. (The status
            # isn't a stored CaseStatus value, so it would otherwise land as
            # OPEN and pollute the case list / verification flow.)
            if (row.get("case_status") or "").strip().lower() == "referred":
                self._count("skipped")
                continue
            # A blank program_name means the case never advanced into a Met
            # Council program (overwhelmingly declined / denied / recalled): out
            # of scope, don't import.
            if not (row.get("program_name") or "").strip():
                self._count("skipped")
                continue
            # External-service cases are out of scope -- we don't track them.
            # (Classified from the program's ActiveProgram category.)
            if derive_case_type(
                row.get("service_subtype"), row.get("program_name")
            ) == CaseType.EXTERNAL_SERVICE:
                self._count("skipped")
                continue
            # Capture the pre-save status/auth so we can detect what changed
            # (case closed, authorization approved/denied/etc.) after upsert.
            prev = Case.objects.filter(pk=cid).first()
            existed = prev is not None
            prev_status = prev.case_status if prev else None
            prev_auth = prev.service_authorization_status if prev else None
            try:
                payload = map_case_row(row)
                ser = CaseSerializer(data=payload)
                ser.is_valid(raise_exception=True)
                case = ser.save()
                self._mark_touched(case.client_id)
                self._count("updated" if existed else "created")
                if case.case_type == CaseType.INTERNAL_SERVICE:
                    self.internal_service_count += 1
                    # Defer the client-wide reconcile to a single post-pass
                    # (reconcile_touched_cases) once every row is written.
                    if case.client_id:
                        self.reconcile_client_ids.add(str(case.client_id))
                if self.emit_side_effects:
                    self._post_save_case(case, prev_status, prev_auth)
            except Exception as exc:  # isolate one bad case from the run
                self._count("errors")
                self.errors.append(f"case {cid}: {exc}")
                logger.warning("csv_import case %s failed: %s", cid, exc)

    def _post_save_case(self, case, previous_status=None, previous_auth_status=None):
        """Emit the case timeline and record (and optionally open) the follow-up
        tickets a change triggers. Tickets are only WRITTEN when create_tickets
        is True; otherwise they're previewed for review.

        The client-wide authorization reconcile (pause/cancel/resume/advance) and
        the funnel-stage recompute are NOT run here: they'd fire per row against a
        partial case picture. They run ONCE per client after every row is written
        -- reconcile via ``reconcile_touched_cases``, stage via
        ``recompute_touched``.

        Runs ONLY when ``emit_side_effects`` is True (the manual Settings
        upload). Bulk historical CLI loads (``emit_side_effects=False``) skip
        this entirely, so a backfill never mass-advances stages / regenerates
        deliveries."""
        if self.emit_timeline:
            try:
                event = timeline.event_for_case(
                    case, source=ChangeSource.IMPORT, actor=TIMELINE_ACTOR,
                )
                if event is not None:
                    self.actions["timeline_events"] += 1
            except Exception:  # noqa: BLE001
                logger.warning("csv_import case timeline failed", exc_info=True)
        self._record_case_actions(case, previous_status, previous_auth_status)

    def reconcile_touched_cases(self):
        """Run the client-wide internal-service reconcile ONCE per client whose
        cases this run touched, now that every row is written -- so the household
        rules (pause / cancel / resume / advance, delivery-calendar truncation)
        evaluate the COMPLETE picture instead of firing per row against partial
        state. Applies to every case-import path (manual upload AND bulk CLI).

        Runs OUTSIDE the ``deferred_internal_service_reconcile`` context, so this
        is the reconcile the deferred per-save calls were skipped in favor of.
        Each client is isolated -- one reconcile hiccup never fails the run."""
        from api.services.lifecycle import reconcile_internal_service_authorization

        ids = list(self.reconcile_client_ids)
        chunk = 500
        for start in range(0, len(ids), chunk):
            batch = ids[start:start + chunk]
            clients = Client.objects.filter(pk__in=batch).prefetch_related(
                "cases",
                "enrollments",
                "household_membership__household__members",
                "household_membership__household__enrollment_verifications",
                "member_profiles__enrollment",
            )
            for client in clients:
                try:
                    reconcile_internal_service_authorization(client)
                except Exception:  # noqa: BLE001 - never fail the run on a reconcile hiccup
                    logger.warning(
                        "csv_import reconcile failed for %s", client.pk, exc_info=True
                    )

    def _record_case_actions(self, case, previous_status, previous_auth_status):
        """Record the case change (timeline events + follow-up tickets) via the
        shared handler, then aggregate the outcome into ``self.actions`` +
        ``self.planned_actions`` for the Import Activity review. Tickets are
        opened only when ``create_tickets`` is True and the action isn't excluded
        for CSV imports (``SKIP_TICKET_ACTIONS``); everything is attributed to
        the import and the uploading agent (``run.triggered_by``)."""
        from api.services import case_events

        res = case_events.record_case_change(
            case,
            previous_status=previous_status,
            previous_auth=previous_auth_status,
            source=ChangeSource.IMPORT,
            actor=self.run.triggered_by or TIMELINE_ACTOR,
            create_tickets=self.create_tickets,
            emit_timeline=self.emit_timeline,
            skip_actions=self.SKIP_TICKET_ACTIONS,
            import_run=self.run,
        )
        self.actions["timeline_events"] += res.timeline_events
        self.actions["tickets_created"] += res.tickets_created
        for p in res.planned:
            self.actions["tickets"] += 1
            if p["action"] in self.actions:
                self.actions[p["action"]] += 1
            if p["action"] == "auth_changed" and p["detail"]:
                self.actions["auth_changed_to"][p["detail"]] = (
                    self.actions["auth_changed_to"].get(p["detail"], 0) + 1
                )
            if len(self.planned_actions) < self._planned_cap:
                self.planned_actions.append({
                    "case_id": str(case.case_id),
                    "client_id": str(case.client_id or ""),
                    "action": p["action"],
                    "detail": p["detail"],
                    "reason": p["reason"],
                    "created": p["created"],
                })

    def recompute_touched(self):
        """Recompute the funnel stage for every client this run touched, once.

        Runs at the END of the import (after all rows are written) so each
        client's stage is derived from the full picture for this file, not the
        partial state mid-stream. Always runs -- including bulk loads where
        per-record side effects are disabled -- so a stale lifecycle_stage can
        never survive an upload. Each client is isolated from the others.
        """
        from api.services.lifecycle import recompute_client_stage
        from api.services.warnings import sync_client_warnings

        # Fetch the touched clients in bulk with the relations the stage +
        # warning derivations traverse prefetched, so each client costs a handful
        # of shared queries per chunk instead of an N+1 (one point-lookup plus
        # several relation queries PER client). Chunked to keep the prefetch
        # working set bounded on very large imports.
        ids = list(self.touched_client_ids)
        chunk = 500
        for start in range(0, len(ids), chunk):
            batch = ids[start:start + chunk]
            clients = Client.objects.filter(pk__in=batch).prefetch_related(
                "enrollments",
                "member_profiles__enrollment",
                "cases",
                "household_membership__household__members",
                "household_membership__household__enrollment_verifications",
            )
            for client in clients:
                try:
                    recompute_client_stage(client)
                except Exception:  # noqa: BLE001 - a funnel hiccup must not fail the run
                    logger.warning(
                        "recompute_client_stage failed for %s", client.pk, exc_info=True
                    )
                # Refresh the member/household warning snapshot from the imported
                # data (catches insurance/client-only rows too). Best-effort.
                sync_client_warnings(client)

    def finalize(self):
        dataset_stats = dict(self.stats)
        # Case imports also surface how many of the imported cases are Internal
        # Service (the member-base drivers), alongside created/updated/skipped/
        # errors. Kept out of ``stats`` proper so it never inflates
        # processed_count (which sums self.stats). Gated on the dataset (not
        # side effects) so bulk loads report it too.
        if self.dataset == "cases":
            dataset_stats["internal_service"] = self.internal_service_count
        self.run.stats = {self.dataset: dataset_stats}
        # Case imports also record the follow-up actions detected (previewed or
        # applied) so the UI can show what tickets/changes the run produced.
        if self._track_actions:
            self.run.stats["actions"] = dict(self.actions)
            self.run.planned_actions = self.planned_actions
        self.run.created_count = self.stats["created"]
        self.run.updated_count = self.stats["updated"]
        self.run.skipped_count = self.stats["skipped"]
        self.run.error_count = self.stats["errors"]
        self.run.processed_count = sum(self.stats.values())
        # Preserve the pre-counted denominator so the final row shows 100%
        # (guards against run.save() overwriting the value set via _flush).
        self.run.progress_total = self.progress_total


def _precount_data_rows(file_obj):
    """Count data rows (excluding the header) in a CSV, tolerant of quoted
    fields that contain embedded newlines. Rewinds ``file_obj`` afterwards so
    the real import pass reads from the top. Returns None if the stream can't
    be rewound (so we simply fall back to an indeterminate progress bar)."""
    wrapper = _text_stream(file_obj)
    reader = csv.reader(wrapper)
    try:
        next(reader, None)  # header
        total = sum(1 for _ in reader)
    except Exception:  # noqa: BLE001 - never fail the import on a pre-count hiccup
        return None
    finally:
        # Detach so the wrapper doesn't close the underlying file on GC -- the
        # real import pass reads the same file_obj again right after this.
        try:
            wrapper.detach()
        except Exception:  # noqa: BLE001 - StringIO fallback has no detach()
            pass
    raw = getattr(file_obj, "file", file_obj)
    try:
        raw.seek(0)
    except (AttributeError, OSError, ValueError):
        return None  # not rewindable -> skip the pre-count denominator
    return total


# Signature id-column that identifies each export type. Order matters: cases /
# screening / assessments / notes all ALSO carry client_id, so their specific
# key is checked first; clients (client_id only) is the fallback.
#
# Screening is checked BEFORE cases: the Unite Us "screening v2" export now
# carries a ``case_id`` column too, so a cases-first order would mis-detect a
# screening export as a cases export. Only screening carries
# ``enhanced_screen_id``, so checking it first is unambiguous.
_EXPORT_SIGNATURES = (
    ("screening", "enhanced_screen_id"),
    ("cases", "case_id"),
    ("assessments", "submission_id"),
    ("notes", "note_id"),
    ("clients", "client_id"),
)
_SIGNATURE_COLUMN = dict(_EXPORT_SIGNATURES)

# Columns each importer critically depends on. If the file is the right TYPE but
# one of these is missing, the export schema likely changed (Unite Us renamed a
# column) -- fail loudly rather than silently importing partial/blank data.
_REQUIRED_COLUMNS = {
    "clients": ("client_id", "first_name", "last_name", "client_consent_status"),
    "cases": ("case_id", "client_id", "case_status", "program_name",
              "service_subtype", "service_authorization_status"),
    # v2 screening exports identify the person via subject_id (+ subject_type),
    # NOT client_id. map_screening_group reads subject_id, so require that.
    "screening": ("enhanced_screen_id", "subject_id"),
    "assessments": ("submission_id", "client_id"),
    "notes": ("note_id", "client_id", "text"),
}


def _read_header(file_obj):
    """Read the CSV header row (normalized: BOM-stripped, lowercased) and rewind
    ``file_obj`` so the real import pass reads from the top. Returns [] on any
    hiccup (so we skip the guard rather than block a valid import)."""
    wrapper = _text_stream(file_obj)
    reader = csv.reader(wrapper)
    try:
        header = next(reader, []) or []
    except Exception:  # noqa: BLE001
        header = []
    finally:
        try:
            wrapper.detach()
        except Exception:  # noqa: BLE001 - StringIO fallback has no detach()
            pass
    raw = getattr(file_obj, "file", file_obj)
    try:
        raw.seek(0)
    except (AttributeError, OSError, ValueError):
        pass
    return [(h or "").strip().lstrip("\ufeff").lower() for h in header]


def _detect_export_type(header):
    cols = set(header)
    for export_type, key in _EXPORT_SIGNATURES:
        if key in cols:
            return export_type
    return None


def _header_mismatch(export_type, header):
    """Return a human error string if the CSV header doesn't match the selected
    export type, else None. Guards against e.g. uploading a Cases export while
    'Clients' is selected (which would reject every row)."""
    if not header:
        return None  # unreadable header -> let the import proceed / fail normally
    detected = _detect_export_type(header)
    if detected is None:
        expected = _SIGNATURE_COLUMN.get(export_type, "id")
        return (
            f"This file doesn't look like a Unite Us {export_type} export "
            f"(missing a '{expected}' column). Check the file and export type."
        )
    if detected != export_type:
        return (
            f"This looks like a {detected} export, but '{export_type}' is "
            f"selected. Choose the '{detected}' export type (or upload the "
            f"matching {export_type} file)."
        )
    # Right type: verify the columns the importer depends on are present, so a
    # Unite Us schema change (renamed/removed column) fails loudly instead of
    # silently importing blank/partial data.
    cols = set(header)
    missing = [c for c in _REQUIRED_COLUMNS.get(export_type, ()) if c not in cols]
    if missing:
        return (
            f"This {export_type} export is missing expected column(s): "
            f"{', '.join(missing)}. The Unite Us export format may have changed "
            f"-- check the column names before importing."
        )
    return None


def run_csv_import(*, export_type, file_obj, triggered_by="manual", emit_side_effects=True,
                   provider_id=None, provider_name=None, run=None, create_tickets=False,
                   emit_timeline=True):
    """Import an uploaded Unite Us CSV ``file_obj`` of ``export_type``.

    Returns the persisted :class:`ImportRun`. ``file_obj`` may be any
    binary/text file-like object (e.g. a Django ``UploadedFile``).

    Pass an existing ``run`` (created earlier, e.g. at S3-presign time) to
    reuse it instead of creating a new one -- the async Celery flow does this so
    the row the browser is already polling is the one that gets updated.

    When ``emit_side_effects`` is False, only the data rows are written --
    timeline events and funnel-stage recompute are skipped (useful for bulk
    historical loads).
    """
    if export_type not in CLI_EXPORT_TYPES:
        raise ValueError(
            f"Unsupported export_type '{export_type}'. "
            f"Supported: {', '.join(CLI_EXPORT_TYPES)}."
        )

    if run is None:
        run = ImportRun.objects.create(
            source=CSV_SOURCE, status=ImportRunStatus.RUNNING,
            triggered_by=triggered_by, export_type=export_type,
        )
    else:
        run.status = ImportRunStatus.RUNNING
        if not run.export_type:
            run.export_type = export_type
        run.save(update_fields=["status", "export_type"])

    # Guard: reject a file whose columns don't match the selected export type
    # (e.g. a Cases export uploaded as "Clients") before doing any work, with a
    # clear message instead of silently failing every row.
    mismatch = _header_mismatch(export_type, _read_header(file_obj))
    if mismatch is not None:
        run.status = ImportRunStatus.FAILED
        run.error_log = mismatch
        run.finished_at = timezone.now()
        run.save()
        return run

    importer = CsvImporter(
        run, emit_side_effects=emit_side_effects, create_tickets=create_tickets,
        emit_timeline=emit_timeline,
    )
    try:
        # Row-per-entity exports (one _count() call per row) get an exact
        # denominator up front. Grouped exports (clients/screenings/assessments)
        # instead set their denominator to the group count after grouping, since
        # rows there collapse many-to-one.
        if export_type in ("cases", "notes"):
            total = _precount_data_rows(file_obj)
            if total is not None:
                importer._set_total(total)
        # Stream the file as text (tolerating a UTF-8 BOM from spreadsheet
        # exports) rather than reading it all into memory -- the screening
        # export runs to several GB.
        reader = csv.DictReader(_text_stream(file_obj))
        with change_context(ChangeSource.IMPORT, f"csv:{triggered_by}"):
            if export_type == "clients":
                importer.import_clients(reader)
            elif export_type == "screening":
                importer.import_screenings(
                    reader, provider_id=provider_id, provider_name=provider_name,
                )
            elif export_type == "assessments":
                importer.import_assessments(
                    reader, provider_id=provider_id, provider_name=provider_name,
                )
            elif export_type == "cases":
                # Defer the per-save client-wide reconcile: with one case per row,
                # reconciling inside each save would evaluate the household rules
                # (pause/cancel/resume/advance) against a partial picture -- e.g.
                # cancelling a household before the row for its still-open case is
                # written. Reconcile ONCE per client afterwards on the full picture.
                from api.services.lifecycle import deferred_internal_service_reconcile

                with deferred_internal_service_reconcile():
                    importer.import_cases(
                        reader, provider_id=provider_id, provider_name=provider_name,
                    )
                importer.reconcile_touched_cases()
            elif export_type == "notes":
                importer.import_notes(reader)
            # Always reconcile the funnel stage for every touched client, so the
            # upload self-heals lifecycle_stage even when per-record side effects
            # are off (bulk load) or the client file was imported before cases.
            importer.recompute_touched()
        run.status = ImportRunStatus.COMPLETED
    except Exception as exc:  # fatal: bad file / decode error
        run.status = ImportRunStatus.FAILED
        importer.errors.append(f"FATAL: {exc}")
        logger.exception("csv_import aborted")
    finally:
        importer.finalize()
        run.finished_at = timezone.now()
        if importer.errors:
            run.error_log = "\n".join(importer.errors)[:10000]
        run.save()
    return run
