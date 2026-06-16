"""Translate Unite Us core JSON:API records into the dict shapes our existing
DRF serializers (ClientSerializer / CaseSerializer / ContractedServiceSerializer)
already accept. Keeping these as pure functions means the daily pull reuses all
of the serializers' idempotent upsert + reconcile logic rather than duplicating
it.

Field mappings mirror the browser extension's confirmed usage
(extension/content/uniteus.js: mapPersonToClient, mapInsuranceRecords,
buildCaseDetailFromApi, apiFetchContractedServices).
"""

from api.models import (
    AddressType,
    CaseStatus,
    InsurancePlanType,
    ServiceAuthorizationStatus,
)


# --- small helpers ---------------------------------------------------------
def _date(s):
    """ISO datetime/date string -> 'YYYY-MM-DD' (DRF DateField input)."""
    return str(s)[:10] if s else None


def _dt(s):
    """Pass through an ISO datetime string (DRF DateTimeField parses it)."""
    return s or None


def _attrs(record):
    return (record or {}).get("attributes") or {}


def _rel_id(record, key):
    rel = (record or {}).get("relationships") or {}
    data = (rel.get(key) or {}).get("data")
    if isinstance(data, dict):
        return data.get("id")
    return None


def _rel_ids(record, key):
    rel = (record or {}).get("relationships") or {}
    data = (rel.get(key) or {}).get("data")
    if isinstance(data, list):
        return [d.get("id") for d in data if isinstance(d, dict) and d.get("id")]
    return []


def cents_to_usd(cents):
    if cents in (None, ""):
        return ""
    try:
        return f"${int(cents) / 100:,.2f}"
    except (TypeError, ValueError):
        return ""


def _enum_or_blank(value, choices_values, mapping=None):
    if not value:
        return ""
    v = str(value).lower()
    if mapping and v in mapping:
        v = mapping[v]
    return v if v in choices_values else ""


# --- person -> client ------------------------------------------------------
def map_person_to_client(person_body, *, consent=None, languages=None):
    """``GET /people/{id}?include=addresses`` body -> ClientSerializer dict.

    ``consent`` / ``languages`` are optional already-mapped dicts the caller can
    merge in (they require separate API calls).
    """
    data = (person_body or {}).get("data") or {}
    a = data.get("attributes") or {}
    out = {"client_id": data.get("id")}

    def set_(k, v):
        if v:
            out[k] = v

    set_("first_name", a.get("first_name"))
    set_("last_name", a.get("last_name"))
    set_("date_of_birth", _date(a.get("date_of_birth")))
    set_("gender", a.get("gender"))
    set_("marital_status", a.get("marital_status"))
    set_("race", a.get("race"))
    set_("ethnicity", a.get("ethnicity"))
    if isinstance(a.get("sexuality"), list):
        set_("sexuality", ", ".join(str(s) for s in a["sexuality"] if s))

    phones = a.get("phone_numbers") or []
    phone = next((p for p in phones if p.get("is_primary")), phones[0] if phones else None)
    if phone:
        set_("client_phone_number", phone.get("phone_number"))
        set_("phone_type", phone.get("phone_type"))

    emails = a.get("email_addresses") or []
    email = next((e for e in emails if e.get("is_primary")), emails[0] if emails else None)
    if email:
        set_("client_email_address", email.get("email_address"))

    addresses = map_addresses(person_body.get("included"))
    if addresses:
        out["addresses"] = addresses

    if consent:
        out.update(consent)
    if languages:
        out.update(languages)
    return out


def map_addresses(included):
    addrs = [x for x in (included or []) if x.get("type") == "address"]
    out = []
    for rec in addrs:
        a = rec.get("attributes") or {}
        atype = str(a.get("address_type") or "").lower()
        if atype not in AddressType.values:
            atype = AddressType.CURRENT
        street = " ".join(p for p in [a.get("line_1"), a.get("line_2")] if p).strip()
        out.append(
            {
                "type": atype,
                "street": street,
                "city": a.get("city") or "",
                "state": (a.get("state") or "")[:2],
                "zip": a.get("postal_code") or "",
            }
        )
    return out


def map_consent(consent_body):
    a = ((consent_body or {}).get("data") or {}).get("attributes") or {}
    out = {}
    if a.get("state"):
        status = str(a["state"]).lower()
        out["consent_status"] = status
        out["consent_accepted"] = status == "accepted"
    if a.get("consented_at"):
        out["consented_at"] = _dt(a["consented_at"])
    return out


# --- insurance / social care coverage --------------------------------------
def _plan_type(plan_info, plan_id, medicaid_ids):
    if medicaid_ids and plan_id in medicaid_ids:
        return InsurancePlanType.MEDICAID
    pt = str((plan_info.get(plan_id) or {}).get("plan_type") or "").lower()
    return pt if pt in InsurancePlanType.values else ""


def map_insurance_record(rec, plan_info, medicaid_ids):
    """A medical insurance JSON:API record -> InsuranceSerializer dict."""
    a = _attrs(rec)
    plan_id = _rel_id(rec, "plan")
    plan = plan_info.get(plan_id) or {}
    return {
        "insurance_id": rec.get("id") or "",
        "plan_name": plan.get("name") or "",
        "plan_type": _plan_type(plan_info, plan_id, medicaid_ids),
        "external_member_id": a.get("external_member_id") or "",
        "external_group_id": a.get("external_group_id") or "",
        "enrolled_at": _dt(a.get("enrolled_at")),
        "expired_at": _dt(a.get("expired_at")),
    }


def map_coverage_record(rec, plan_info, medicaid_ids):
    """A social-care-coverage JSON:API record -> SocialCareCoverageSerializer dict."""
    a = _attrs(rec)
    plan_id = _rel_id(rec, "plan")
    plan = plan_info.get(plan_id) or {}
    return {
        "coverage_id": rec.get("id") or "",
        "plan_name": plan.get("name") or "",
        "plan_type": _plan_type(plan_info, plan_id, medicaid_ids),
        "external_member_id": a.get("external_member_id") or "",
        "external_group_id": a.get("external_group_id") or "",
        "enrolled_at": _dt(a.get("enrolled_at")),
        "expired_at": _dt(a.get("expired_at")),
    }


# --- case ------------------------------------------------------------------
# Unite Us authorization `state` -> our ServiceAuthorizationStatus.
_AUTH_STATE_MAP = {"accepted": "approved"}


def map_case(case_body_data, *, names=None, auth=None):
    """A single case JSON:API record (``body['data'][i]``) -> CaseSerializer dict.

    ``names`` is a resolved {service, program, network, primary_worker,
    program_id, network_id, primary_worker_id} dict; ``auth`` is the case's
    service_authorization attributes dict (or None).
    """
    names = names or {}
    a = _attrs(case_body_data)
    person_id = _rel_id(case_body_data, "person")
    out = {
        "case_id": case_body_data.get("id"),
        "client_id": person_id,
        "subject_id": person_id,
    }

    def set_(k, v):
        if v not in (None, ""):
            out[k] = v

    state = str(a.get("state") or "").lower()
    out["case_status"] = state if state in CaseStatus.values else CaseStatus.OPEN
    set_("case_description", a.get("description"))
    set_("date_opened", _dt(a.get("opened_date")))
    set_("case_closed_at", _dt(a.get("closed_date")))
    set_("updated_at", _dt(a.get("updated_at")))

    set_("service_type", names.get("service"))
    set_("program_name", names.get("program"))
    set_("program_id", names.get("program_id"))
    set_("network_name", names.get("network"))
    set_("network_id", names.get("network_id"))
    set_("primary_worker_name", names.get("primary_worker"))
    set_("primary_worker_id", names.get("primary_worker_id"))

    if auth:
        raw = str(auth.get("state") or "")
        set_(
            "service_authorization_status",
            _enum_or_blank(raw, ServiceAuthorizationStatus.values, _AUTH_STATE_MAP),
        )
        set_("service_authorization_status_label", raw.replace("_", " ").title())
        set_("unite_us_authorization_id", auth.get("short_id"))
        set_("authorized_amount", cents_to_usd(auth.get("approved_cents")))
        set_("service_authorization_approval_starts_at", _dt(auth.get("approved_starts_at")))
        set_("service_authorization_approval_ends_at", _dt(auth.get("approved_ends_at")))
        set_("service_authorization_request_starts_at", _dt(auth.get("requested_starts_at")))
        set_("service_authorization_request_ends_at", _dt(auth.get("requested_ends_at")))
    return out


# --- contracted service (provided_service) ---------------------------------
def map_provided_service(ps, *, case_id, sole_auth=None, invoice=None, program_name=""):
    """A provided_service record (+ optional sole authorization + latest invoice)
    -> ContractedServiceSerializer dict. Mirrors apiFetchContractedServices."""
    a = _attrs(ps)
    out = {
        "contracted_service_id": ps.get("id"),
        "case_id": case_id,
        "name": _provided_description(a),
        "service_type": program_name or "",
        "status": str(a.get("state") or ""),
        "authorized_units": str(a["unit_amount"]) if a.get("unit_amount") is not None else "",
        "service_starts_at": _date(a.get("starts_at")),
        "service_ends_at": _date(a.get("ends_at")),
        "created_at": _dt(a.get("created_at")),
        "updated_at": _dt(a.get("updated_at")),
    }

    if sole_auth:
        aa = sole_auth
        if aa.get("state"):
            out["authorization_status"] = str(aa["state"]).upper()
        if aa.get("short_id"):
            out["unite_us_authorization_id"] = aa["short_id"]
        amount = cents_to_usd(
            aa.get("approved_cents") if aa.get("approved_cents") is not None
            else aa.get("requested_cents")
        )
        if amount:
            out["authorized_amount"] = amount
        units = aa.get("approved_unit_amount")
        if units is None:
            units = aa.get("requested_unit_amount")
        if units is not None:
            out["authorized_units"] = str(units)
        starts = _date(aa.get("approved_starts_at") or aa.get("requested_starts_at"))
        ends = _date(aa.get("approved_ends_at") or aa.get("requested_ends_at"))
        if starts:
            out["service_starts_at"] = starts
        if ends:
            out["service_ends_at"] = ends

    if invoice:
        ia = invoice
        out["invoice_number"] = ia.get("short_id") or ia.get("invoice_number") or ""
        if ia.get("invoice_status"):
            out["invoice_status"] = str(ia["invoice_status"]).replace("_", " ").upper()
        elif ia.get("state"):
            out["invoice_status"] = str(ia["state"]).upper()
        amt = cents_to_usd(
            ia.get("total_amount_invoiced") if ia.get("total_amount_invoiced") is not None
            else ia.get("amount_paid")
        )
        if amt:
            out["invoice_amount"] = amt
        out["invoiced_at"] = _dt(ia.get("created_at") or ia.get("approved_at"))
        if ia.get("fee_schedule_program_name"):
            out["fee_schedule_program_name"] = ia["fee_schedule_program_name"]
        if ia.get("fee_schedule_program_unit"):
            out["unit_type"] = ia["fee_schedule_program_unit"]
    return out


def _provided_description(a):
    md = a.get("metadata")
    if isinstance(md, list):
        pref = next(
            (m.get("value") for m in md
             if m.get("field") == "specific_support_provided" and m.get("value")),
            None,
        )
        if pref:
            return pref
        any_v = next((m.get("value") for m in md if m.get("value")), None)
        if any_v:
            return any_v
    return ""


# --- notes -----------------------------------------------------------------
def map_note(note_rec, *, client_id=None, case_id=None):
    """A ``/notes`` record -> kwargs for api.models.Note (created directly)."""
    a = _attrs(note_rec)
    return {
        "source": "unite_us",
        "source_note_id": note_rec.get("id") or "",
        "author_name": a.get("author_name") or a.get("created_by_name") or "",
        "body": a.get("text") or "",
        "source_created_at": _dt(a.get("created_at")),
        "client_id": client_id,
        "case_id": case_id,
    }
