"""Offline simulation harness for the daily Unite Us pull.

Lets us exercise the FULL pipeline (mappers -> serializers -> history tagging ->
tickets -> ImportRun) with zero external dependencies: no live token, no network,
no extension. ``FakeUniteUsClient`` returns canned JSON:API bodies shaped exactly
like the live core API (the shapes confirmed in extension/content/uniteus.js), so
swapping it in for the real client is the only difference between this and a live
run.

Used by ``manage.py daily_pull --simulate`` (see commands/daily_pull.py).
"""

import uuid

from django.utils import timezone

from api.models import (
    Client,
    UniteUsCredential,
    UniteUsCredentialStatus,
)

# Stable ids for one provider/person/case scenario, regenerated per build_scenario.
SCENARIO = {}


def build_scenario(*, case_closed=False, with_coverage=True, with_insurance=True):
    """Construct a single-provider, single-person scenario and stash it in the
    module-level SCENARIO dict that FakeUniteUsClient reads from.

    Flags let you flip conditions to exercise different ticket rules:
      case_closed     -> CASE_CLOSED ticket
      with_insurance  -> if False, NO_ACTIVE_INSURANCE ticket
      with_coverage   -> if False, NO_ACTIVE_COVERAGE ticket
    """
    now = timezone.now()
    iso = now.isoformat()
    future = "9999-12-31T00:00:00Z"  # 9999 sentinel => still in force

    # Deterministic ids (uuid5) so re-running the simulation hits the SAME
    # records — lets us verify idempotency and look rows up in the admin.
    ns = uuid.UUID("11111111-1111-1111-1111-111111111111")
    ids = {k: str(uuid.uuid5(ns, k)) for k in (
        "provider", "employee", "person", "case", "service", "program",
        "network", "worker", "auth", "provided_service", "invoice", "plan",
        "insurance", "coverage", "consent", "note_person", "note_case",
    )}

    person = {
        "data": {
            "id": ids["person"], "type": "person",
            "attributes": {
                "first_name": "Maria", "last_name": "Sample",
                "date_of_birth": "1962-03-14T00:00:00Z",
                "gender": "female", "marital_status": "widowed",
                "race": "black_african_american", "ethnicity": "not_hispanic_latino",
                "phone_numbers": [
                    {"phone_number": "7185551234", "phone_type": "mobile", "is_primary": True}
                ],
                "email_addresses": [
                    {"email_address": "maria.sample@example.com", "is_primary": True}
                ],
            },
            "relationships": {
                "consent": {"data": {"id": ids["consent"], "type": "consent"}},
            },
        },
        "included": [
            {
                "id": str(uuid.uuid4()), "type": "address",
                "attributes": {
                    "address_type": "home", "is_primary": True,
                    "line_1": "123 Grand Concourse", "line_2": "Apt 4B",
                    "city": "Bronx", "county": "Bronx", "state": "NY",
                    "postal_code": "10451",
                },
            }
        ],
    }

    consent = {"data": {"id": ids["consent"], "attributes": {
        "state": "accepted", "consented_at": iso,
    }}}

    plan_info = {ids["plan"]: {"name": "Healthfirst Medicaid", "plan_type": "medicaid"}}

    insurance_rec = {
        "id": ids["insurance"], "type": "insurance",
        "attributes": {
            "external_member_id": "M123456789", "external_group_id": "G42",
            "enrolled_at": "2023-01-01T00:00:00Z",
            "expired_at": future if with_insurance else "2020-01-01T00:00:00Z",
            "insurance_status": "active", "state": "active",
        },
        "relationships": {"plan": {"data": {"id": ids["plan"], "type": "plan"}}},
    }
    coverage_rec = {
        "id": ids["coverage"], "type": "insurance",
        "attributes": {
            "external_member_id": "SCC987", "external_group_id": "",
            "enrolled_at": "2023-06-01T00:00:00Z",
            "expired_at": future if with_coverage else "2021-01-01T00:00:00Z",
            "insurance_status": "enrolled" if with_coverage else "expired",
            "state": "active",
        },
        "relationships": {"plan": {"data": {"id": ids["plan"], "type": "plan"}}},
    }

    case = {
        "id": ids["case"], "type": "case",
        "attributes": {
            "state": "closed" if case_closed else "managed",
            "description": "Medically tailored meals for post-discharge nutrition.",
            "opened_date": "2024-02-01T00:00:00Z",
            "closed_date": iso if case_closed else None,
            "updated_at": iso,
        },
        "relationships": {
            "person": {"data": {"id": ids["person"], "type": "person"}},
            "service": {"data": {"id": ids["service"], "type": "service"}},
            "program": {"data": {"id": ids["program"], "type": "program"}},
            "network": {"data": {"id": ids["network"], "type": "network"}},
            "primary_worker": {"data": {"id": ids["worker"], "type": "employee"}},
            "service_authorization": {"data": {"id": ids["auth"], "type": "service_authorization"}},
        },
    }

    auth = {
        "id": ids["auth"], "type": "service_authorization",
        "attributes": {
            "state": "approved", "short_id": "AUTH-77001",
            "approved_cents": 873600, "approved_unit_amount": 20,
            "approved_starts_at": "2024-02-05T00:00:00Z",
            "approved_ends_at": "2024-08-05T00:00:00Z",
        },
        "relationships": {},
    }

    provided_service = {
        "id": ids["provided_service"], "type": "provided_service",
        "attributes": {
            "state": "active", "unit_amount": 20,
            "starts_at": "2024-02-05T00:00:00Z", "ends_at": "2024-08-05T00:00:00Z",
            "service_duration": 30,
            "metadata": [
                {"field": "specific_support_provided", "value": "Home-delivered medically tailored meals"}
            ],
            "created_at": iso, "updated_at": iso,
        },
        "relationships": {
            "program": {"data": {"id": ids["program"], "type": "program"}},
            "invoices": {"data": [{"id": ids["invoice"], "type": "invoice"}]},
        },
    }

    invoice = {"data": {
        "id": ids["invoice"], "type": "invoice",
        "attributes": {
            "short_id": "INV-30021", "invoice_status": "accepted_by_payer",
            "total_amount_invoiced": 175000, "created_at": iso,
            "fee_schedule_program_name": "Clinically Appropriate Meals",
            "fee_schedule_program_unit": "meal",
        },
    }}

    notes = {
        ids["person"]: [{"id": ids["note_person"], "type": "note", "attributes": {
            "text": "Intake call completed; member consented to services.",
            "author_name": "Jane Coordinator", "created_at": "2024-02-01T15:04:00Z",
        }}],
        ids["case"]: [{"id": ids["note_case"], "type": "note", "attributes": {
            "text": "Authorization approved for 20 meals.",
            "author_name": "Jane Coordinator", "created_at": "2024-02-06T10:00:00Z",
        }}],
    }

    resources = {
        ("/services", ids["service"]): {"data": {"attributes": {"name": "Medically Tailored Meals"}}},
        ("/programs", ids["program"]): {"data": {"attributes": {"name": "Clinically Appropriate Meals"}}},
        ("/networks", ids["network"]): {"data": {"attributes": {"name": "NY Health Network"}}},
        ("/employees", ids["worker"]): {"data": {"attributes": {"full_name": "Jane Coordinator"}}},
    }

    SCENARIO.clear()
    SCENARIO.update({
        "ids": ids,
        "person": person,
        "consent": consent,
        "plan_info": plan_info,
        "insurance": [insurance_rec] if with_insurance else [insurance_rec],  # always returned; expiry drives status
        "coverage": [coverage_rec] if with_coverage else [coverage_rec],
        "case": case,
        "auth": auth,
        "provided_service": provided_service,
        "invoice": invoice,
        "notes": notes,
        "resources": resources,
    })
    return SCENARIO


class FakeUniteUsClient:
    """Drop-in for api.integrations.uniteus.api.UniteUsClient that serves the
    module-level SCENARIO instead of hitting the network."""

    def __init__(self, credential):
        self.cred = credential

    # -- people / consent --------------------------------------------------
    def get_person(self, person_id, include="addresses"):
        if person_id == SCENARIO["ids"]["person"]:
            return SCENARIO["person"]
        return {"data": {}}  # unknown member -> MEMBER_NOT_FOUND ticket

    def get_consent(self, consent_id):
        return SCENARIO["consent"]

    # -- coverage ----------------------------------------------------------
    def list_insurances(self, person_id, plan_types):
        if "social" in plan_types:
            return list(SCENARIO["coverage"])
        # both the medical and the dedicated medicaid query return the medical rec
        return list(SCENARIO["insurance"])

    def get_plans(self, plan_ids):
        return {pid: SCENARIO["plan_info"][pid] for pid in plan_ids if pid in SCENARIO["plan_info"]}

    # -- cases -------------------------------------------------------------
    def list_cases(self, person_id):
        return [SCENARIO["case"]] if person_id == SCENARIO["ids"]["person"] else []

    def get_service_authorization(self, auth_id):
        return {"data": SCENARIO["auth"]}

    def list_service_authorizations(self, case_id):
        return [SCENARIO["auth"]]

    def list_provided_services(self, case_id):
        return [SCENARIO["provided_service"]]

    def get_invoice(self, invoice_id):
        return SCENARIO["invoice"]

    # -- notes / resources -------------------------------------------------
    def list_notes(self, subject_id, subject_type=None):
        return SCENARIO["notes"].get(subject_id, [])

    def get_resource(self, resource_path, resource_id):
        return SCENARIO["resources"].get((resource_path, resource_id), {"data": {"attributes": {}}})


def seed(*, ensure_key=True):
    """Persist the fixtures needed for discovery: an ACTIVE credential and a stub
    Client (the pull refreshes Clients already in our DB). Returns the person id."""
    ids = SCENARIO["ids"]
    UniteUsCredential.objects.update_or_create(
        provider_id=ids["provider"], employee_id=ids["employee"],
        defaults={
            "access_token": "sim-access-token",
            "refresh_token": "sim-refresh-token",
            "access_expires_at": timezone.now() + timezone.timedelta(hours=1),
            "status": UniteUsCredentialStatus.ACTIVE,
            "token_type": "Bearer",
        },
    )
    Client.objects.update_or_create(
        client_id=ids["person"],
        defaults={"first_name": "Maria", "last_name": "Sample"},
    )
    return ids["person"]


def teardown():
    """Remove everything the simulation created (credential, client + cascade,
    and the ImportRuns/tickets it produced)."""
    from api.models import ImportRun, Note, Ticket

    ids = SCENARIO.get("ids") or {}
    if ids:
        UniteUsCredential.objects.filter(provider_id=ids.get("provider")).delete()
        # Notes FK to Client/Case is SET_NULL, so deleting the client orphans
        # them rather than removing them — delete by source id explicitly.
        Note.objects.filter(
            source_note_id__in=[ids.get("note_person"), ids.get("note_case")]
        ).delete()
        Client.objects.filter(client_id=ids.get("person")).delete()  # cascades cases/etc.
    Ticket.objects.filter(import_run__triggered_by="simulate").delete()
    ImportRun.objects.filter(triggered_by="simulate").delete()
