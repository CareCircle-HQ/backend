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

    def __init__(self, credential, allow_refresh=True):
        self.cred = credential
        # allow_refresh=False uses the stored access token as-is and never does a
        # server-side refresh-token rotation. Unite Us refresh tokens are
        # single-use and shared with the live browser session, so a refresh from
        # here can log the agent out; probes/read-only tools pass False.
        self.allow_refresh = allow_refresh
        self.host = config.api_base().rstrip("/")  # e.g. https://core.uniteus.io
        self.base = f"{self.host}/v1"
        self.timeout = config.timeout()
        self._session = requests.Session()

    # -- low level ---------------------------------------------------------
    def _headers(self):
        h = creds_client.auth_headers(self.cred)
        h["accept"] = "application/json"
        return h

    def core_get(self, path, params=None):
        """GET ``<base><path>`` and return parsed JSON. Refreshes the token
        first (unless ``allow_refresh`` is False); raises UniteUsAuthExpired on
        401/403."""
        if self.allow_refresh:
            if not creds_client.ensure_fresh(self.cred):
                raise UniteUsAuthExpired(
                    f"credential {self.cred.pk} is not usable (provider={self.cred.provider_id})"
                )
        elif not self.cred.access_token:
            raise UniteUsAuthExpired(
                f"credential {self.cred.pk} has no access token (provider={self.cred.provider_id})"
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

    def core_post(self, path, json_body):
        """POST ``<base><path>`` with a JSON body and return parsed JSON.
        Same auth/refresh semantics as :meth:`core_get`."""
        if self.allow_refresh:
            if not creds_client.ensure_fresh(self.cred):
                raise UniteUsAuthExpired(
                    f"credential {self.cred.pk} is not usable (provider={self.cred.provider_id})"
                )
        elif not self.cred.access_token:
            raise UniteUsAuthExpired(
                f"credential {self.cred.pk} has no access token (provider={self.cred.provider_id})"
            )
        headers = self._headers()
        headers["content-type"] = "application/json"
        url = f"{self.base}{path}"
        try:
            resp = self._session.post(
                url, headers=headers, json=json_body, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise UniteUsApiError(f"POST {path} failed: {exc}")
        if resp.status_code in (401, 403):
            raise UniteUsAuthExpired(f"POST {path} -> {resp.status_code}")
        if resp.status_code >= 400:
            raise UniteUsApiError(f"POST {path} -> {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise UniteUsApiError(f"POST {path} returned non-JSON: {exc}")

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
    def list_notes(self, subject_id, subject_type):
        """Notes whose subject is a person or a case.

        The core API requires the polymorphic subject filtered by both the id
        (``filter[subject]``) and a lowercase type (``filter[subject.type]``,
        one of ``person``, ``case``, ``referral``); omitting either returns 400.
        """
        return list(
            self._paginate(
                "/notes",
                {
                    "filter[subject]": subject_id,
                    "filter[subject.type]": (subject_type or "").lower(),
                },
                page_size=100,
            )
        )

    # -- exports (bulk report files) --------------------------------------
    # The "Exports" page (app.uniteus.io/exports) is backed by these core-API
    # endpoints. An export is requested (POST /exports), generated async by
    # Unite Us, then its downloadable file is exposed as a ``file_uploads``
    # record tied to the export (record.type=export).
    EXPORT_TYPES = (
        "assessments", "screenings", "screeningsv2", "cases", "clients",
        "referrals", "users", "notes", "assistance_requests",
        "assistance_requests_supplemental_responses", "invoices",
        "resource_list_shares",
    )

    def list_exports(self, export_types=None, provider_id=None, page_size=100):
        """List the provider's exports (most-recent first if the API sorts).

        Mirrors the results-table poll:
        ``GET /exports?filter[requester.provider]=<pid>&filter[export_type]=…``
        Returns the raw JSON:API ``data`` list (each has attributes incl. the
        generation state + a link/relationship to the file once ready)."""
        pid = provider_id or self.cred.provider_id
        types = ",".join(export_types or self.EXPORT_TYPES)
        return list(
            self._paginate(
                "/exports",
                {
                    "filter[requester.provider]": pid,
                    "filter[export_type]": types,
                },
                page_size=page_size,
            )
        )

    def request_export(self, export_type, start_date, end_date, requester_id=None):
        """Request a new export (the "Request Export" button). ``start_date`` /
        ``end_date`` are ``YYYY-MM-DD`` strings. Returns the created export
        record (``data``) -- poll its ``attributes.state`` until ``completed``.

        ``requester_id`` defaults to this credential's employee id (the API
        requires a requester employee)."""
        requester = requester_id or self.cred.employee_id
        body = {
            "jsonapi": {"version": "1.0"},
            "data": {
                "type": "export",
                "attributes": {
                    "export_type": export_type,
                    "state": "requested",
                    "details": {"start_date": start_date, "end_date": end_date},
                },
                "relationships": {
                    "requester": {"data": {"type": "employee", "id": requester}},
                },
            },
        }
        return (self.core_post("/exports", body) or {}).get("data") or {}

    def get_export(self, export_id):
        """Fetch a single export record (to poll its state)."""
        return (self.core_get(f"/exports/{export_id}") or {}).get("data") or {}

    def list_export_file_uploads(self, export_id):
        """The file_uploads record(s) for one export -- holds the download URL
        and upload state once Unite Us finishes generating the file.
        ``GET /file_uploads?filter[record]=<export_id>&filter[record.type]=export``"""
        return self.core_get(
            "/file_uploads",
            params={
                "filter[record]": export_id,
                "filter[record.type]": "export",
            },
        )

    def download_export_file(self, path, dest_fileobj):
        """Stream a completed export's CSV to ``dest_fileobj``.

        ``path`` is the ``file_uploads`` ``attributes.path`` (a Rails
        ActiveStorage signed redirect under the core host, e.g.
        ``/rails/active_storage/blobs/redirect/...``). The signed link expires
        ~30 min after generation, so resolve file_uploads then call this
        promptly. Returns the number of bytes written.

        requests drops the Authorization header on the cross-host redirect to
        the signed blob store, so no credentials leak to S3/GCS."""
        url = path if path.startswith("http") else f"{self.host}{path}"
        try:
            resp = self._session.get(
                url, headers=self._headers(), timeout=self.timeout,
                stream=True, allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise UniteUsApiError(f"download export file failed: {exc}")
        if resp.status_code in (401, 403):
            raise UniteUsAuthExpired(f"download -> {resp.status_code}")
        if resp.status_code >= 400:
            raise UniteUsApiError(f"download -> {resp.status_code}: {resp.text[:300]}")
        written = 0
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                dest_fileobj.write(chunk)
                written += len(chunk)
        return written

    # -- generic related lookups ------------------------------------------
    def get_resource(self, resource_path, resource_id):
        """e.g. get_resource('/programs', id) -> single record body."""
        return self.core_get(f"{resource_path}/{resource_id}")
