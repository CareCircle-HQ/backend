"""Unite Us screenings-ingestion API client (server-side).

Assessment/screening RESULTS (``eligible_services`` / ``eligible_status``) live on
a DIFFERENT host than the core JSON:API the daily pull uses -- the same
``screenings-ingestion.uniteus.io`` host the browser extension reads (see
extension/content/uniteus.js: ``apiFetchScreeningList`` / ``apiFetchScreeningDetail``
/ ``parseApiAssessmentDetail``). Auth is the SAME per-provider session bearer +
``x-employee-id`` / ``x-provider-id`` headers as the core client, so this reuses
``api.integrations.uniteus.client`` for headers + refresh.

The endpoints:

    GET /v2/screenings?person_id=<id>&type=assessment&offset=&limit=20
    GET /v2/screenings/<id>?template_format=surveyjs

The list returns ``{screens: [...], total: N}``; the detail returns
``{screen: {...}}``. Fetchers are deliberately thin: they return raw dicts and
let ``api.integrations.uniteus.mappers.map_assessment_api`` translate them into
the shape ``AssessmentSerializer`` accepts.
"""

import logging

import requests

from . import client as creds_client
from . import config
from .api import UniteUsApiError, UniteUsAuthExpired

logger = logging.getLogger(__name__)

# The list endpoint 400s for limits larger than 20; match the page exactly.
PAGE_LIMIT = 20


class ScreeningsIngestionClient:
    """Thin screenings-ingestion client bound to a single ``UniteUsCredential``."""

    def __init__(self, credential, allow_refresh=False):
        self.cred = credential
        # allow_refresh=False (the default here) uses the stored access token
        # as-is and never rotates the shared, single-use refresh token -- a
        # server-side refresh can log the live agent out. The nightly pull runs
        # off-hours and may set True; read-only tools keep it False.
        self.allow_refresh = allow_refresh
        self.host = config.screenings_ingestion_base().rstrip("/")
        self.base = f"{self.host}/v2"
        self.timeout = config.timeout()
        self._session = requests.Session()

    # -- low level ---------------------------------------------------------
    def _headers(self):
        h = creds_client.auth_headers(self.cred)
        h["accept"] = "application/json"
        return h

    def _ensure_usable(self):
        if self.allow_refresh:
            if not creds_client.ensure_fresh(self.cred):
                raise UniteUsAuthExpired(
                    f"credential {self.cred.pk} is not usable "
                    f"(provider={self.cred.provider_id})"
                )
        elif not self.cred.access_token:
            raise UniteUsAuthExpired(
                f"credential {self.cred.pk} has no access token "
                f"(provider={self.cred.provider_id})"
            )

    def _get(self, path, params=None):
        self._ensure_usable()
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

    # -- list --------------------------------------------------------------
    def _list_screens(self, person_id, screen_type, provider_id=None):
        """Enumerate every screen of ``screen_type`` for a person, following
        offset/limit pagination (mirrors the extension's ``apiFetchScreeningList``).

        When ``provider_id`` is given, keep only that provider's own records
        (``organization_id == provider_id``) -- the Met Council scoping the
        extension applies, since the org NAME is unreliable/null in the API."""
        out = []
        offset = 0
        for _guard in range(50):  # hard stop to avoid runaway loops
            body = self._get(
                "/screenings",
                params={
                    "person_id": person_id,
                    "type": screen_type,
                    "offset": offset,
                    "limit": PAGE_LIMIT,
                },
            )
            screens = body.get("screens") if isinstance(body, dict) else None
            screens = screens if isinstance(screens, list) else []
            out.extend(screens)
            total = body.get("total") if isinstance(body, dict) else None
            offset += PAGE_LIMIT
            if not screens or (total is not None and len(out) >= total):
                break
        if provider_id:
            want = str(provider_id).lower()
            out = [s for s in out if str(s.get("organization_id") or "").lower() == want]
        return out

    def list_assessments(self, person_id, provider_id=None):
        """List a person's eligibility assessments (``type=assessment``)."""
        return self._list_screens(person_id, "assessment", provider_id=provider_id)

    def list_screenings(self, person_id, provider_id=None):
        """List a person's screenings (``type=screening``)."""
        return self._list_screens(person_id, "screening", provider_id=provider_id)

    # -- detail ------------------------------------------------------------
    def get_screen_detail(self, screen_id):
        """Fetch one screen's SurveyJS detail. Returns the inner ``screen`` dict
        (the questions + eligible_services live there), matching the extension's
        ``apiFetchScreeningDetail`` unwrap of ``body.screen``."""
        body = self._get(f"/screenings/{screen_id}", params={"template_format": "surveyjs"})
        if isinstance(body, dict):
            return body.get("screen") or body
        return {}
