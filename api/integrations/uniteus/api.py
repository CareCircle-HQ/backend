"""Unite Us core API client (server-side).

Mirrors the live endpoints the extension already calls from the browser (see
extension/content/uniteus.js). The core API is JSON:API shaped
(``{data, included, meta, relationships, attributes}``) served from
``<UNITEUS_API_BASE>/v1``. Auth is the per-provider Bearer + x-employee-id /
x-provider-id, refreshed via api.integrations.uniteus.client.ensure_fresh.

All fetchers are deliberately thin: they return raw JSON:API dicts/lists and let
api.integrations.uniteus.mappers translate them into the shapes our existing
DRF serializers accept.
"""

import logging

import requests

from . import client as creds_client
from . import config

logger = logging.getLogger(__name__)

# Medical plan_type classifications used by the UI's insurance filter.
MEDICAL_PLAN_TYPES = "commercial,medicare,medicaid,tricare"
# Case list filters the org dashboard uses (managed + off-platform, active).
CASE_STATE_FILTER = "managed,off_platform"
CASE_INTERNAL_STATE_FILTER = "managed,pending_authorization"


class UniteUsApiError(Exception):
    """Transport/HTTP error talking to the Unite Us core API."""


class UniteUsAuthExpired(UniteUsApiError):
    """The credential is no longer usable (401/403); agent must re-login."""


class UniteUsClient:
    """Thin core-API client bound to a single ``UniteUsCredential``."""

    def __init__(self, credential):
        self.cred = credential
        self.base = f"{config.api_base().rstrip('/')}/v1"
        self.timeout = config.timeout()
        self._session = requests.Session()

    # -- low level ---------------------------------------------------------
    def _headers(self):
        h = creds_client.auth_headers(self.cred)
        h["accept"] = "application/json"
        return h

    def core_get(self, path, params=None):
        """GET ``<base><path>`` and return parsed JSON. Refreshes the token
        first; raises UniteUsAuthExpired on 401/403."""
        if not creds_client.ensure_fresh(self.cred):
            raise UniteUsAuthExpired(
                f"credential {self.cred.pk} is not usable (provider={self.cred.provider_id})"
            )
        url = f"{self.base}{path}"
        try:
            resp = self._session.get(
                url, headers=self._headers(), params=params, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise UniteUsApiError(f"GET {path} failed: {exc}")
        if resp.status_code in (401, 403):
            raise UniteUsAuthExpired(f"GET {path} -> {resp.status_code}")
        if resp.status_code >= 400:
            raise UniteUsApiError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise UniteUsApiError(f"GET {path} returned non-JSON: {exc}")

    def _paginate(self, path, base_params=None, page_size=50):
        """Yield every ``data`` record across JSON:API ``page[number]`` pages."""
        params = dict(base_params or {})
        params["page[size]"] = page_size
        number = 1
        for _guard in range(200):  # hard stop to avoid runaway loops
            params["page[number]"] = number
            body = self.core_get(path, params=params)
            for rec in body.get("data") or []:
                yield rec
            meta_page = (body.get("meta") or {}).get("page") or {}
            total_pages = meta_page.get("total_pages")
            if not total_pages or number >= total_pages:
                break
            number += 1

    # -- people / profile --------------------------------------------------
    def get_person(self, person_id, include="addresses"):
        params = {"include": include} if include else None
        return self.core_get(f"/people/{person_id}", params=params)

    def get_consent(self, consent_id):
        return self.core_get(f"/consents/{consent_id}")

    def list_record_languages(self, person_id):
        return self.core_get(
            "/record_languages",
            params={"filter[record_id]": person_id, "filter[record_type]": "Person"},
        )

    # -- insurance / coverage ---------------------------------------------
    def list_insurances(self, person_id, plan_types):
        """Insurances for a person filtered by plan_type classification."""
        return list(
            self._paginate(
                "/insurances",
                {
                    "filter[person]": person_id,
                    "filter[state]": "active,pending,inactive",
                    "filter[plan.plan_type]": plan_types,
                },
                page_size=50,
            )
        )

    def get_plans(self, plan_ids):
        ids = [p for p in dict.fromkeys(plan_ids) if p]
        if not ids:
            return {}
        body = self.core_get(
            "/plans",
            params={"filter[id]": ",".join(ids), "page[number]": 1, "page[size]": len(ids)},
        )
        out = {}
        for p in body.get("data") or []:
            if p and p.get("id"):
                a = p.get("attributes") or {}
                out[p["id"]] = {"name": a.get("name", ""), "plan_type": a.get("plan_type", "")}
        return out

    # -- cases -------------------------------------------------------------
    def list_cases(self, person_id):
        return list(
            self._paginate(
                "/cases",
                {
                    "filter[person]": person_id,
                    "filter[state]": CASE_STATE_FILTER,
                    "filter[include_pathways]": "false",
                    "filter[internal_state]": CASE_INTERNAL_STATE_FILTER,
                    "sort": "updated_at",
                    "sort_direction": "desc",
                },
                page_size=50,
            )
        )

    def get_service_authorization(self, auth_id):
        return self.core_get(f"/service_authorizations/{auth_id}")

    def list_service_authorizations(self, case_id):
        return list(
            self._paginate(
                "/service_authorizations", {"filter[case]": case_id}, page_size=100
            )
        )

    def list_provided_services(self, case_id):
        return list(
            self._paginate(
                "/provided_services", {"filter[case]": case_id}, page_size=100
            )
        )

    def get_invoice(self, invoice_id):
        return self.core_get(f"/invoices/{invoice_id}")

    # -- notes -------------------------------------------------------------
    def list_notes(self, subject_id):
        """Notes whose subject is a person or a case (subject_id)."""
        return list(
            self._paginate("/notes", {"filter[subject]": subject_id}, page_size=100)
        )

    # -- generic related lookups ------------------------------------------
    def get_resource(self, resource_path, resource_id):
        """e.g. get_resource('/programs', id) -> single record body."""
        return self.core_get(f"{resource_path}/{resource_id}")
