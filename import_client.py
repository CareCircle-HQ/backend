#!/usr/bin/env python
"""Import a single client (plus their cases and screenings) from the CSV exports.

Usage:
    python import_client.py                       # imports the default client id
    python import_client.py <client_id>           # imports a specific client

It reads the latest matching files in ./data:
    clients_export_*.csv      (keyed by client_id)
    cases_export_*.csv        (keyed by client_id)
    screeningsv2_export_*.csv (keyed by subject_id == client_id)

Records are upserted via the Django ORM directly (not the DRF serializers) so the
raw Unite Us values that don't match our choice enums (e.g. marital_status
"undisclosed") are imported as-is rather than rejected.
"""
import csv
import glob
import json
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from api.models import (  # noqa: E402
    Address,
    Case,
    Client,
    Eligibility,
    Insurance,
    Program,
    Provider,
    Screening,
)

DEFAULT_CLIENT_ID = "eed58b75-5418-4282-bb84-2d53223815f0"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# CSV field-size limit can be small on Windows; raise it for the wide screening file.
csv.field_size_limit(2_000_000)


# ---------------------------------------------------------------------------
# Value parsing helpers
# ---------------------------------------------------------------------------
def s(v):
    return (v or "").strip()


def u(v):
    """UUID string or None (empty -> None)."""
    return s(v) or None


def num_int(v):
    v = s(v)
    try:
        return int(float(v)) if v else None
    except ValueError:
        return None


def dec(v):
    v = s(v)
    try:
        return Decimal(v) if v else None
    except InvalidOperation:
        return None


def flt(v):
    v = s(v)
    try:
        return float(v) if v else None
    except ValueError:
        return None


def boolean(v):
    v = s(v).lower()
    if v in ("true", "1", "yes", "t"):
        return True
    if v in ("false", "0", "no", "f"):
        return False
    return None


def consent_bool(v):
    v = s(v).lower()
    if v in ("accepted", "true", "yes"):
        return True
    if v in ("declined", "false", "no"):
        return False
    return None


def d(v):
    """Parse a date, tolerating a trailing time component."""
    v = s(v)
    if not v:
        return None
    v = v.split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def dt(v):
    """Parse a datetime and make it timezone-aware if the project uses TZ."""
    v = s(v)
    if not v:
        return None
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(v, fmt)
            break
        except ValueError:
            continue
    if parsed and getattr(settings, "USE_TZ", False) and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed


def jlist(v):
    v = s(v)
    if not v:
        return []
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def latest(pattern):
    files = sorted(glob.glob(os.path.join(DATA_DIR, pattern)))
    if not files:
        raise FileNotFoundError(f"No file matching {pattern!r} in {DATA_DIR}")
    return files[-1]


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


# ---------------------------------------------------------------------------
# FK resolution
# ---------------------------------------------------------------------------
def resolve_provider(provider_id, name=""):
    pid = u(provider_id)
    if not pid:
        return None
    obj, _ = Provider.objects.update_or_create(
        provider_id=pid, defaults={"name": s(name)}
    )
    return obj


def resolve_program(program_id, name="", provider=None):
    pid = u(program_id)
    if not pid:
        return None
    obj, _ = Program.objects.update_or_create(
        program_id=pid, defaults={"name": s(name), "provider": provider}
    )
    return obj


# ---------------------------------------------------------------------------
# Importers
# ---------------------------------------------------------------------------
@transaction.atomic
def import_client(client_id):
    rows = [r for r in read_rows(latest("clients_export_*.csv"))
            if s(r.get("client_id")) == client_id]
    if not rows:
        print(f"[!] Client {client_id} not found in clients CSV.")
        return None

    row = rows[0]
    defaults = {
        "created_by_id": u(row.get("client_created_by_id")),
        "created_by_name": s(row.get("client_created_by_name")),
        "created_at": dt(row.get("client_created_at")),
        "updated_at": dt(row.get("client_updated_at")),
        "first_name": s(row.get("first_name")),
        "middle_name": s(row.get("middle_name")),
        "last_name": s(row.get("last_name")),
        "suffix": s(row.get("suffix")),
        "title": s(row.get("title")),
        "consent_status": s(row.get("client_consent_status")) or "pending",
        "consented_at": dt(row.get("client_consented_at")),
        "date_of_birth": d(row.get("date_of_birth")),
        "gender": s(row.get("gender")),
        "sexuality": s(row.get("sexuality")),
        "sexuality_other": s(row.get("sexuality_other")),
        "race": s(row.get("race")),
        "ethnicity": s(row.get("ethnicity")),
        "marital_status": s(row.get("marital_status")),
        "citizenship": s(row.get("citizenship")),
        "gross_monthly_income": dec(row.get("gross_monthly_income")),
        "household_size": num_int(row.get("household_size")),
        "adults_in_household": num_int(row.get("adults_in_household")),
        "children_in_household": num_int(row.get("children_in_household")),
        "preferred_communication_method": s(row.get("preferred_communication_method")),
        "preferred_spoken_language": s(row.get("preferred_spoken_language")),
        "preferred_written_language": s(row.get("preferred_written_language")),
        "phone_type": s(row.get("phone_type")) or "mobile",
        "client_phone_number": s(row.get("client_phone_number")),
        "client_email_address": s(row.get("client_email_address")),
        "care_coordinator": s(row.get("care_coordinator")),
        "care_coordinator_status": s(row.get("care_coordinator_status")),
    }
    client, created = Client.objects.update_or_create(
        client_id=client_id, defaults=defaults
    )
    print(f"[+] Client {'created' if created else 'updated'}: "
          f"{client.first_name} {client.last_name} ({client_id})")

    # Current address (use the first row that carries an address line).
    addr_row = next(
        (r for r in rows if s(r.get("current_client_address_line1"))), None
    )
    if addr_row:
        Address.objects.update_or_create(
            client=client,
            address_type="current",
            defaults={
                "is_mailing_address": bool(
                    boolean(addr_row.get("current_client_address_is_mailing_address"))
                ),
                "line1": s(addr_row.get("current_client_address_line1")),
                "line2": s(addr_row.get("current_client_address_line2")),
                "city": s(addr_row.get("current_client_address_city")),
                "county": s(addr_row.get("current_client_address_county")),
                "postal_code": s(addr_row.get("current_client_address_postal_code")),
                "state": s(addr_row.get("current_client_address_state")),
                "created_at": dt(addr_row.get("current_client_address_created_at")),
                "updated_at": dt(addr_row.get("current_client_address_updated_at")),
            },
        )
        print("    - address imported")

    # Insurances (dedupe by insurance_id across the client's rows).
    seen = set()
    ins_count = 0
    for r in rows:
        key = s(r.get("insurance_id"))
        if not key or key in seen:
            continue
        seen.add(key)
        primary = s(r.get("primary_health_insurance_id"))
        Insurance.objects.update_or_create(
            client=client,
            insurance_id=key,
            defaults={
                "plan_external_id": s(r.get("insurance_plan_external_id")),
                "plan_type": s(r.get("insurance_plan_type")),
                "plan_name": s(r.get("insurance_plan_name")),
                "status": s(r.get("insurance_status")),
                "is_primary": bool(primary) and primary == key,
                "external_group_id": s(r.get("external_group_id")),
                "external_member_id": s(r.get("external_member_id")),
                "ingested": bool(boolean(r.get("insurance_ingested"))),
                "enrolled_at": dt(r.get("insurance_enrolled_at")),
                "expired_at": dt(r.get("insurance_expired_at")),
                "record_status": s(r.get("insurance_record_status")),
                "verified": bool(boolean(r.get("insurance_verified"))),
                "verified_at": dt(r.get("insurance_verified_at")),
                "created_at": dt(r.get("insurance_created_at")),
                "updated_at": dt(r.get("insurance_updated_at")),
            },
        )
        ins_count += 1
    if ins_count:
        print(f"    - {ins_count} insurance record(s) imported")

    return client


@transaction.atomic
def import_cases(client):
    rows = [r for r in read_rows(latest("cases_export_*.csv"))
            if s(r.get("client_id")) == str(client.client_id)]
    count = 0
    for row in rows:
        provider = resolve_provider(row.get("provider_id"), row.get("provider_name"))
        originating = resolve_provider(
            row.get("originating_provider_id"), row.get("originating_provider_name")
        )
        program = resolve_program(
            row.get("program_id"), row.get("program_name"), provider
        )
        Case.objects.update_or_create(
            case_id=u(row.get("case_id")),
            defaults={
                "client": client,
                "client_first_name": s(row.get("client_first_name")),
                "client_last_name": s(row.get("client_last_name")),
                "client_dob": d(row.get("client_dob")),
                "created_by_id": u(row.get("case_created_by_id")),
                "created_by_name": s(row.get("case_created_by_name")),
                "created_at": dt(row.get("case_created_at")),
                "updated_at": dt(row.get("case_updated_at")),
                "user_entered_opened_date": d(row.get("user_entered_opened_date")),
                "user_entered_closed_date": d(row.get("user_entered_closed_date")),
                "ar_submitted_on": dt(row.get("ar_submitted_on")),
                "case_processed_at": dt(row.get("case_processed_at")),
                "case_managed_at": dt(row.get("case_managed_at")),
                "case_off_platform_at": dt(row.get("case_off_platform_at")),
                "case_closed_at": dt(row.get("case_closed_at")),
                "closed_note": s(row.get("closed_note")),
                "network_id": u(row.get("network_id")),
                "network_name": s(row.get("network_name")),
                "originating_provider": originating,
                "originating_provider_name": s(row.get("originating_provider_name")),
                "provider": provider,
                "provider_name": s(row.get("provider_name")),
                "out_of_network_provider_name": s(row.get("out_of_network_provider_name")),
                "program": program,
                "program_name": s(row.get("program_name")),
                "primary_worker_id": u(row.get("primary_worker_id")),
                "primary_worker_name": s(row.get("primary_worker_name")),
                "care_coordinator": s(row.get("care_coordinator")),
                "care_coordinator_status": s(row.get("care_coordinator_status")),
                "case_description": s(row.get("case_description")),
                "case_status": s(row.get("case_status")) or "open",
                "started_as_assistance_request": bool(
                    boolean(row.get("started_as_assistance_request"))
                ),
                "case_is_referred": bool(boolean(row.get("case_is_referred"))),
                "service_type": s(row.get("service_type")),
                "service_subtype": s(row.get("service_subtype")),
                "outcome_id": u(row.get("outcome_id")),
                "outcome_description": s(row.get("outcome_description")),
                "outcome_resolution_type": s(row.get("outcome_resolution_type")),
                "service_authorization_status": s(row.get("service_authorization_status")),
                "service_authorization_request_starts_at": dt(
                    row.get("service_authorization_request_starts_at")
                ),
                "service_authorization_request_ends_at": dt(
                    row.get("service_authorization_request_ends_at")
                ),
                "service_authorization_approval_starts_at": dt(
                    row.get("service_authorization_approval_starts_at")
                ),
                "service_authorization_approval_ends_at": dt(
                    row.get("service_authorization_approval_ends_at")
                ),
                "export_provider_role": s(row.get("export_provider_role")),
            },
        )
        count += 1
        print(f"    - case {row.get('case_id')} "
              f"({s(row.get('service_type'))} / {s(row.get('case_status'))})")
    print(f"[+] {count} case(s) imported")
    return count


def _screen_values(row, client, case):
    """Field mapping shared by the Screening and Eligibility models (same shape)."""
    return {
        "subject_id": u(row.get("subject_id")),
        "subject_type": s(row.get("subject_type")),
        "client": client,
        "case": case,
        "active_screen": bool(boolean(row.get("active_screen"))),
        "assigned_at": dt(row.get("assigned_at")),
        "assigned_to_id": u(row.get("assigned_to_id")),
        "screen_created_at": dt(row.get("screen_created_at")),
        "screen_updated_at": dt(row.get("screen_updated_at")),
        "screen_status": s(row.get("screen_status")),
        "screen_status_at": dt(row.get("screen_status_at")),
        "screen_type": s(row.get("screen_type")),
        "screen_source": s(row.get("screen_source")),
        "client_first_name": s(row.get("client_first_name")),
        "client_last_name": s(row.get("client_last_name")),
        "client_dob": d(row.get("client_dob")),
        "duration": num_int(row.get("duration")),
        "facilitator_id": u(row.get("facilitator_id")),
        "facilitator_type": s(row.get("facilitator_type")),
        "provider_id": u(row.get("provider_id")),
        "provider_name": s(row.get("provider_name")),
        "performing_organization_name": s(row.get("performing_organization_name")),
        "outreach_count": num_int(row.get("outreach_count")) or 0,
        "outreach_status": s(row.get("outreach_status")),
        "decline_note": s(row.get("decline_note")),
        "decline_reason_id": u(row.get("decline_reason_id")),
        "decline_primary_text": s(row.get("decline_primary_text")),
        "decline_secondary_text": s(row.get("decline_secondary_text")),
        "decline_reason_key": s(row.get("decline_reason_key")),
        "interpreter_id": u(row.get("interpreter_id")),
        "interpreter_type": s(row.get("interpreter_type")),
        "language": s(row.get("language")),
        "consent": consent_bool(row.get("consent")),
        "consent_code": s(row.get("consent_code")),
        "interpersonal_safety_riskscore": flt(
            row.get("interpersonal_safety_riskscore")
        ),
        "interpersonal_safety_interpretation": s(
            row.get("interpersonal_safety_interpretation")
        ),
        "screen_snomed_codes": jlist(row.get("screen_snomed_codes")),
        "screen_icd10_codes": jlist(row.get("screen_icd10_codes")),
        "clinical_code_classification": s(row.get("clinical_code_classification")),
        "verified_clinical_code": s(row.get("verified_clinical_code")),
        "verified_clinical_code_description": s(
            row.get("verified_clinical_code_description")
        ),
        "eligible_status": s(row.get("eligible_status")),
        "eligible_services": jlist(row.get("eligible_services")),
        "verified_at": dt(row.get("verified_at")),
        "verified_by_id": u(row.get("verified_by_id")),
        "verified_by_type": s(row.get("verified_by_type")),
        "is_case_sensitive": bool(boolean(row.get("is_case_sensitive"))),
        "filter_date": d(row.get("filter_date")),
    }


def _is_eligibility(screen_type):
    t = (screen_type or "").lower()
    return "assess" in t or "eligib" in t


@transaction.atomic
def import_screenings(client):
    """Import screenings & eligibility assessments, routing by screen_type."""
    # The screening CSV has one row per answer; dedupe to the first row per screen.
    by_screen = {}
    for r in read_rows(latest("screeningsv2_export_*.csv")):
        if s(r.get("subject_id")) != str(client.client_id):
            continue
        sid = s(r.get("enhanced_screen_id"))
        if sid and sid not in by_screen:
            by_screen[sid] = r

    scr_count = elig_count = 0
    for sid, row in by_screen.items():
        case = None
        case_id = u(row.get("case_id"))
        if case_id:
            case = Case.objects.filter(pk=case_id).first()
        vals = _screen_values(row, client, case)

        if _is_eligibility(row.get("screen_type")):
            Eligibility.objects.update_or_create(eligibility_id=sid, defaults=vals)
            elig_count += 1
            kind = "eligibility"
        else:
            Screening.objects.update_or_create(enhanced_screen_id=sid, defaults=vals)
            scr_count += 1
            kind = "screening"
        print(f"    - {kind} {sid} ({s(row.get('screen_type'))} / "
              f"{s(row.get('screen_status'))})")

    print(f"[+] {scr_count} screening(s) and {elig_count} eligibility "
          f"assessment(s) imported (answers/questions not imported by this script)")
    return scr_count, elig_count


def main():
    client_id = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_CLIENT_ID
    print(f"=== Importing client {client_id} ===")
    client = import_client(client_id)
    if client is None:
        sys.exit(1)
    import_cases(client)
    import_screenings(client)
    print("=== Done ===")


if __name__ == "__main__":
    main()
