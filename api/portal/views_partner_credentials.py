"""Settings > Delivery Company > POD API access (MANAGEMENT ONLY).

Issue, rotate and revoke the credential a delivery company uses to push proof of
delivery, and export the integration guide their developers need.

Management-only on purpose: the rest of Settings is ``IsPortalAgent``, which
would let CS / Logistics / Nutritionist mint third-party credentials.

The raw secret exists only in the response that creates or rotates it -- we store
a hash. A later doc download therefore renders a placeholder instead, with
instructions to rotate if it was lost.
"""

import json

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import (
    Agent,
    DeliveryCompany,
    DeliveryCompanyApiClient,
    PartnerAccessToken,
    PartnerScope,
    default_partner_scopes,
)
from ..partner import auth as partner_auth
from ..services import partner_docs
from .permissions import IsManagementAgent


def _client_payload(client, *, secret=None):
    """Credential state for the UI. ``secret`` is present only on create/rotate."""
    data = {
        "client_id": client.client_id,
        "delivery_company_id": str(client.delivery_company_id),
        "delivery_company": client.delivery_company.name,
        "scopes": list(client.scopes or []),
        "is_active": client.is_active,
        "revoked_at": client.revoked_at,
        "expires_at": client.expires_at,
        "last_used_at": client.last_used_at,
        "last_used_ip": client.last_used_ip,
        "allowed_ips": list(client.allowed_ips or []),
        "created_at": client.created_at,
        "created_by": client.created_by.name if client.created_by_id else "",
        "base_url": partner_docs.partner_base_url(),
        # Rotation overlap: the old secret keeps working until this lapses.
        "previous_secret_expires_at": (
            client.previous_secret_expires_at
            if client.previous_secret_hash and client.previous_secret_expires_at
            and client.previous_secret_expires_at > timezone.now()
            else None
        ),
        "active_tokens": client.tokens.filter(
            revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).count(),
    }
    if secret is not None:
        data["client_secret"] = secret
        data["secret_warning"] = (
            "Copy this now: it is hashed on save and cannot be shown again."
        )
    return data


def _agent_from_request(request):
    agent_id = getattr(request.user, "agent_id", None)
    return Agent.objects.filter(pk=agent_id).first() if agent_id else None


class DeliveryCompanyApiClientView(APIView):
    """GET / POST / DELETE the credential for one delivery company.

    POST creates it (or rotates the secret when ``rotate`` is set), DELETE
    revokes it and kills every live access token.
    """

    permission_classes = [IsManagementAgent]

    def get(self, request, company_id):
        company = get_object_or_404(DeliveryCompany, pk=company_id)
        client = DeliveryCompanyApiClient.objects.filter(
            delivery_company=company
        ).select_related("delivery_company").first()
        if client is None:
            return Response({"exists": False, "base_url": partner_docs.partner_base_url()})
        return Response({"exists": True, **_client_payload(client)})

    def post(self, request, company_id):
        company = get_object_or_404(DeliveryCompany, pk=company_id)
        rotate = bool(request.data.get("rotate"))
        # A LEAKED secret must die now, not after the grace window. "Rotate" is
        # the button people reach for in that situation, so it takes a flag that
        # skips the overlap and kills tokens already minted from the old secret.
        compromised = bool(request.data.get("compromised"))
        client = DeliveryCompanyApiClient.objects.filter(
            delivery_company=company
        ).select_related("delivery_company").first()

        secret = partner_auth.generate_secret()
        if client is None:
            client = DeliveryCompanyApiClient.objects.create(
                delivery_company=company,
                client_id=partner_auth.generate_client_id(company.name),
                secret_hash=partner_auth.hash_secret(secret),
                scopes=default_partner_scopes(),
                created_by=_agent_from_request(request),
            )
            created = True
        else:
            if not rotate:
                return Response(
                    {"error": "This company already has a credential. Pass rotate=true "
                              "to issue a new secret."},
                    status=http.HTTP_409_CONFLICT,
                )
            if compromised:
                # No overlap: the old secret stops working immediately, and any
                # access token already issued from it is revoked.
                client.previous_secret_hash = ""
                client.previous_secret_expires_at = None
            else:
                # Keep the old secret alive briefly so the vendor can redeploy
                # without an outage.
                client.previous_secret_hash = client.secret_hash
                client.previous_secret_expires_at = timezone.now() + timezone.timedelta(
                    seconds=partner_auth.ROTATION_GRACE_SECONDS
                )
            client.secret_hash = partner_auth.hash_secret(secret)
            client.revoked_at = None  # rotating reactivates a revoked credential
            client.save(update_fields=[
                "previous_secret_hash", "previous_secret_expires_at",
                "secret_hash", "revoked_at", "updated_at",
            ])
            if compromised:
                PartnerAccessToken.objects.filter(
                    client=client, revoked_at__isnull=True
                ).update(revoked_at=timezone.now())
            created = False

        client.refresh_from_db()
        return Response(
            {"created": created, "rotated": not created, **_client_payload(client, secret=secret)},
            status=http.HTTP_201_CREATED if created else http.HTTP_200_OK,
        )

    def patch(self, request, company_id):
        """Adjust scopes, expiry or the optional IP allowlist."""
        company = get_object_or_404(DeliveryCompany, pk=company_id)
        client = get_object_or_404(DeliveryCompanyApiClient, delivery_company=company)
        fields = []
        if "scopes" in request.data:
            valid = set(PartnerScope.values)
            scopes = [s for s in (request.data.get("scopes") or []) if s in valid]
            client.scopes = scopes
            fields.append("scopes")
        if "allowed_ips" in request.data:
            client.allowed_ips = [
                str(x).strip() for x in (request.data.get("allowed_ips") or []) if str(x).strip()
            ]
            fields.append("allowed_ips")
        if "expires_at" in request.data:
            client.expires_at = request.data.get("expires_at") or None
            fields.append("expires_at")
        if fields:
            client.save(update_fields=fields + ["updated_at"])
        client.refresh_from_db()
        return Response(_client_payload(client))

    def delete(self, request, company_id):
        """Revoke: the credential stops working immediately, and so does every
        access token already issued from it."""
        company = get_object_or_404(DeliveryCompany, pk=company_id)
        client = get_object_or_404(DeliveryCompanyApiClient, delivery_company=company)
        now = timezone.now()
        client.revoked_at = now
        client.previous_secret_hash = ""
        client.previous_secret_expires_at = None
        client.save(update_fields=[
            "revoked_at", "previous_secret_hash", "previous_secret_expires_at", "updated_at",
        ])
        killed = PartnerAccessToken.objects.filter(
            client=client, revoked_at__isnull=True
        ).update(revoked_at=now)
        return Response({"revoked": True, "tokens_revoked": killed})


class DeliveryCompanyApiDocsView(APIView):
    """Export the integration guide for a company's developers.

    ``?fmt=md`` (default) | ``pdf`` | ``openapi``. The secret is included only
    when passed straight through from a create/rotate response
    (``?secret=...``), which is how the one-time "integration pack" download
    works -- we cannot recover it later.

    NB: the parameter is ``fmt``, not ``format`` -- DRF reserves ``format`` for
    renderer negotiation and answers an unknown value with its own 404.
    """

    permission_classes = [IsManagementAgent]

    def get(self, request, company_id):
        company = get_object_or_404(DeliveryCompany, pk=company_id)
        client = DeliveryCompanyApiClient.objects.filter(
            delivery_company=company
        ).select_related("delivery_company").first()
        if client is None:
            return Response(
                {"error": "Create the API credential first."},
                status=http.HTTP_404_NOT_FOUND,
            )

        fmt = (request.query_params.get("fmt") or "md").lower()
        secret = request.query_params.get("secret") or ""
        stem = f"carecircle-pod-api-{company.name.lower().replace(' ', '-')}"

        if fmt == "openapi":
            spec = partner_docs.build_openapi(client)
            resp = HttpResponse(
                json.dumps(spec, indent=2), content_type="application/json"
            )
            resp["Content-Disposition"] = f'attachment; filename="{stem}-openapi.json"'
            return resp

        if fmt == "pdf":
            pdf = partner_docs.build_pdf(client, secret=secret)
            resp = HttpResponse(pdf, content_type="application/pdf")
            resp["Content-Disposition"] = f'attachment; filename="{stem}.pdf"'
            return resp

        md = partner_docs.build_markdown(client, secret=secret)
        resp = HttpResponse(md, content_type="text/markdown; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{stem}.md"'
        return resp
