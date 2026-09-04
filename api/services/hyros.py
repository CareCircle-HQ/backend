"""Hyros lead-tracking integration.

When a member whose Lead Source is "Meta Ads" is saved and has an internal-service
case, we push them to Hyros tagged "Enrolled" (for ad-spend optimization). The push
is once-per-member (guarded by ``Client.hyros_enrolled_pushed_at``) and best-effort:
network / API failures never break the save, and the whole integration is dormant
until ``HYROS_API_KEY`` is configured.

Docs: https://api-docs.hyros.com/#tag/leads/POST/api/v1.0/leads
"""

import logging

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# The lead source that opts a member into the Hyros push (matched case-insensitively;
# the data stores it as "META ADS").
META_ADS_LEAD_SOURCE = "meta ads"
HYROS_ENROLLED_TAG = "!enrolled"
HYROS_STAGE = "Enrolled"


def is_meta_ads(lead_source):
    return (lead_source or "").strip().lower() == META_ADS_LEAD_SOURCE


def _phone_numbers(client):
    """The member's phone numbers (primary first), normalized 10-digit form."""
    nums = []
    for p in client.phones.all().order_by("-is_primary", "created_at"):
        v = (p.normalized or p.raw or "").strip()
        if v and v not in nums:
            nums.append(v)
    return nums


def _has_internal_service_case(client):
    from api.models import CaseType

    return client.cases.filter(case_type=CaseType.INTERNAL_SERVICE).exists()


def _consent(client):
    accepted = bool(
        client.consent_accepted
        or (client.consent_status or "").strip().lower() == "accepted"
    )
    return "GRANTED" if accepted else "DENIED"


def build_lead_payload(client):
    """The Hyros /leads body. Missing fields are sent blank/empty per the spec."""
    return {
        "email": client.client_email_address or "",
        "firstName": client.first_name or "",
        "lastName": client.last_name or "",
        "tags": [HYROS_ENROLLED_TAG],
        "phoneNumbers": _phone_numbers(client),
        "stage": HYROS_STAGE,
        "adOptimizationConsent": _consent(client),
    }


def _qualifies(client):
    return (
        getattr(settings, "HYROS_API_KEY", "")
        and client is not None
        and client.hyros_enrolled_pushed_at is None
        and is_meta_ads(getattr(client, "lead_source", ""))
        and _has_internal_service_case(client)
    )


def maybe_enqueue_enrollment(client):
    """If this member now qualifies (Meta Ads + internal-service case + not yet
    pushed) and the integration is configured, enqueue the async push. No-op
    otherwise. Never raises -- attribution/marketing must not break a save."""
    try:
        if not _qualifies(client):
            return
        from api.tasks import push_hyros_enrollment

        push_hyros_enrollment.delay(str(client.client_id))
    except Exception:  # pragma: no cover - defensive; never break the caller
        logger.exception("failed to enqueue Hyros enrollment push")


def push_enrollment(client_id):
    """Do the actual Hyros POST and, on success, mark the member pushed. Re-checks
    the guards so a stale/duplicate task is a no-op. Called by the Celery task."""
    from api.models import Client

    api_key = getattr(settings, "HYROS_API_KEY", "")
    if not api_key:
        return
    client = Client.objects.filter(pk=client_id).first()
    if not _qualifies(client):
        return

    url = getattr(
        settings, "HYROS_LEADS_URL", "https://api.hyros.com/v1/api/v1.0/leads"
    )
    try:
        resp = requests.post(
            url,
            json=build_lead_payload(client),
            headers={"Content-Type": "application/json", "API-Key": api_key},
            timeout=15,
        )
    except requests.RequestException:
        logger.exception("Hyros push failed (network) for client %s", client_id)
        return
    if resp.status_code >= 400:
        logger.warning(
            "Hyros push rejected for client %s: %s %s",
            client_id, resp.status_code, (resp.text or "")[:500],
        )
        return

    # Success -> stamp so we never push this member again.
    client.hyros_enrolled_pushed_at = timezone.now()
    client.save(update_fields=["hyros_enrolled_pushed_at"])
