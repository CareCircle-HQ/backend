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

from django.utils import timezone
from django.utils.dateparse import parse_datetime

import json

from api.history import ChangeSource, change_context
from api.models import (
    AddressType,
    Assessment,
    Case,
    CaseStatus,
    Client,
    IdentifiedSocialNeed,
    ImportRun,
    ImportRunStatus,
    Insurance,
    InsurancePlanType,
    OutcomeResolutionType,
    Screening,
    ServiceAuthorizationStatus,
    SocialCareCoverage,
    SocialCareCoverageStatus,
    VerifiedSocialNeed,
)
from api.serializers import (
    AssessmentSerializer,
    CaseSerializer,
    ClientSerializer,
    ScreeningSerializer,
)
from api.services import timeline

logger = logging.getLogger(__name__)

CSV_SOURCE = "csv_uniteus"
TIMELINE_ACTOR = "system:csv-import"

# Export types this importer understands. Mapped in follow-up slices: notes.
SUPPORTED_EXPORT_TYPES = ("clients", "screening", "assessments", "cases")


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


# Basic email shape check — the export occasionally contains junk (e.g. a phone
# number or "n/a") in the email column, which the EmailField would reject and
# fail the whole client. We drop such values rather than block the import.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(value):
    return bool(_EMAIL_RE.match((value or "").strip()))


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
        "zip": _s(row, "client_address_postal_code"),
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
        "client_phone_number": _s(profile, "client_phone_number"),
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
# mappers._AUTH_STATE_MAP in the daily import).
_AUTH_STATE_MAP = {"accepted": "approved"}


def map_case_row(row):
    """A single cases-export row -> CaseSerializer payload (one row per case).

    Mirrors the daily import's ``map_case`` semantics:
    - CSV ``service_subtype`` -> model ``service_type`` (the daily import stores
      the service name there, e.g. "Social Service Case Management", which is
      what ``derive_case_type`` keys on). CSV ``service_type`` (the broad
      category, e.g. "Individual & Family Support") has no model field.
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
    set_("date_opened", _dt(row, "user_entered_opened_date") or _dt(row, "case_created_at"))
    set_("updated_at", _dt(row, "case_updated_at"))
    set_("ar_submitted_on", _dt(row, "ar_submitted_on"))
    set_("case_processed_at", _dt(row, "case_processed_at"))
    set_("case_managed_at", _dt(row, "case_managed_at"))
    set_("case_off_platform_at", _dt(row, "case_off_platform_at"))
    set_("case_closed_at", _dt(row, "case_closed_at") or _dt(row, "user_entered_closed_date"))
    set_("closed_note", _s(row, "closed_note"))
    set_("case_description", _s(row, "case_description"))

    status = _s(row, "case_status").lower()
    out["case_status"] = status if status in CaseStatus.values else CaseStatus.OPEN
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
    set_("service_authorization_request_starts_at", _dt(row, "service_authorization_request_starts_at"))
    set_("service_authorization_request_ends_at", _dt(row, "service_authorization_request_ends_at"))
    set_("service_authorization_approval_starts_at", _dt(row, "service_authorization_approval_starts_at"))
    set_("service_authorization_approval_ends_at", _dt(row, "service_authorization_approval_ends_at"))
    return out


# --- importer --------------------------------------------------------------
class CsvImporter:
    def __init__(self, run):
        self.run = run
        self.errors = []
        self.stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        self.dataset = "clients"  # stats label; set per import_* method

    def _count(self, kind):
        self.stats[kind] += 1

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
        """Derive the funnel stage + emit timeline events (no tickets).

        Mirrors the daily API import's per-client side effects so a CSV load
        leaves the same trail: the clients export only carries consent, so the
        stage lands at Consent (accepted) or Inactive. Consent / insurance /
        coverage timeline events are deduped, so re-imports won't duplicate
        them. Each side effect is isolated — it must never fail the import row.
        """
        self._recompute_stage(client.pk, client)

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

        for cid, rows in groups.items():
            existed = Client.objects.filter(pk=cid).exists()
            try:
                payload = map_client_group(cid, rows)
                ser = ClientSerializer(data=payload)
                ser.is_valid(raise_exception=True)
                client = ser.save()
                self._count("updated" if existed else "created")
                self._post_save(client)
            except Exception as exc:  # isolate one bad client from the run
                self._count("errors")
                self.errors.append(f"client {cid}: {exc}")
                logger.warning("csv_import client %s failed: %s", cid, exc)

    def import_screenings(self, reader):
        self.dataset = "screenings"
        # Stream the denormalized (one-row-per-answer) export grouped by screen,
        # holding only one screen's rows in memory at a time -- this file can be
        # several GB. Rows for a screen are contiguous in the export.
        for sid, rows in _iter_contiguous_groups(reader, "enhanced_screen_id"):
            if sid is None:
                self._count("skipped")
                continue
            # Append-only + idempotent: screenings are immutable once complete,
            # so skip any enhanced_screen_id we already store (checked per
            # group). This keeps re-imports cheap and non-destructive, and also
            # guards the rare case of a screen split across non-contiguous
            # groups -- the second group is skipped once the first creates it.
            if Screening.objects.filter(pk=sid).exists():
                self._count("skipped")
                continue
            try:
                payload = map_screening_group(sid, rows)
                ser = ScreeningSerializer(data=payload)
                ser.is_valid(raise_exception=True)
                screening = ser.save()
                self._save_screening_needs(screening, rows)
                self._count("created")
                self._emit_screening_timeline(screening)
                self._recompute_stage(screening.client_id, screening.client)
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

    def import_assessments(self, reader):
        self.dataset = "assessments"
        # Group the denormalized (one-row-per-question) rows by submission.
        groups = OrderedDict()
        for row in reader:
            sid = (row.get("submission_id") or "").strip()
            if not sid:
                self._count("skipped")
                continue
            groups.setdefault(sid, []).append(row)

        for sid, rows in groups.items():
            existed = Assessment.objects.filter(pk=sid).exists()
            try:
                payload = map_assessment_group(sid, rows)
                ser = AssessmentSerializer(data=payload)
                ser.is_valid(raise_exception=True)
                assessment = ser.save()
                self._count("updated" if existed else "created")
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

    def import_cases(self, reader):
        self.dataset = "cases"
        # One row per case — stream directly, no grouping needed.
        for row in reader:
            cid = (row.get("case_id") or "").strip()
            if not cid:
                self._count("skipped")
                continue
            existed = Case.objects.filter(pk=cid).exists()
            try:
                payload = map_case_row(row)
                ser = CaseSerializer(data=payload)
                ser.is_valid(raise_exception=True)
                case = ser.save()
                self._count("updated" if existed else "created")
                self._post_save_case(case)
            except Exception as exc:  # isolate one bad case from the run
                self._count("errors")
                self.errors.append(f"case {cid}: {exc}")
                logger.warning("csv_import case %s failed: %s", cid, exc)

    def _post_save_case(self, case):
        """Emit the case timeline + recompute the funnel stage (cases drive the
        Navigation stage). No tickets and no enrollment reconciliation / order
        generation — this is a historical bulk load."""
        try:
            timeline.event_for_case(
                case, source=ChangeSource.IMPORT, actor=TIMELINE_ACTOR,
            )
        except Exception:  # noqa: BLE001
            logger.warning("csv_import case timeline failed", exc_info=True)
        self._recompute_stage(case.client_id, case.client)

    def finalize(self):
        self.run.stats = {self.dataset: dict(self.stats)}
        self.run.created_count = self.stats["created"]
        self.run.updated_count = self.stats["updated"]
        self.run.skipped_count = self.stats["skipped"]
        self.run.error_count = self.stats["errors"]
        self.run.processed_count = sum(self.stats.values())


def run_csv_import(*, export_type, file_obj, triggered_by="manual"):
    """Import an uploaded Unite Us CSV ``file_obj`` of ``export_type``.

    Returns the persisted :class:`ImportRun`. ``file_obj`` may be any
    binary/text file-like object (e.g. a Django ``UploadedFile``).
    """
    if export_type not in SUPPORTED_EXPORT_TYPES:
        raise ValueError(
            f"Unsupported export_type '{export_type}'. "
            f"Supported: {', '.join(SUPPORTED_EXPORT_TYPES)}."
        )

    run = ImportRun.objects.create(
        source=CSV_SOURCE, status=ImportRunStatus.RUNNING, triggered_by=triggered_by,
    )
    importer = CsvImporter(run)
    try:
        # Stream the file as text (tolerating a UTF-8 BOM from spreadsheet
        # exports) rather than reading it all into memory -- the screening
        # export runs to several GB.
        reader = csv.DictReader(_text_stream(file_obj))
        with change_context(ChangeSource.IMPORT, f"csv:{triggered_by}"):
            if export_type == "clients":
                importer.import_clients(reader)
            elif export_type == "screening":
                importer.import_screenings(reader)
            elif export_type == "assessments":
                importer.import_assessments(reader)
            elif export_type == "cases":
                importer.import_cases(reader)
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
