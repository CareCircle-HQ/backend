"""Portal endpoints for housing dispatch — the CRM side only.

The CRM CREATES an assessment order (the wizard) and READS everything the vendor
produces. It never authors evidence: no signature, finding photo or questionnaire
answer is writable here, by anyone, including a manager. That is the locking rule
the whole feature rests on, and it is enforced by there being no endpoint for it
rather than by a permission check someone can widen later.

See docs/housing-assessment-order-plan.md.
"""
import logging
import re

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.response import Response

from ..models import (
    Client, DispatchAvailabilityWindow, DispatchKind, DispatchOrder,
    DispatchReferralType, DispatchStatus, StageEventSource, Vendor,
)
from ..services import dispatch as dispatch_svc
from .base import PortalAPIView, current_agent

logger = logging.getLogger(__name__)


def _serialize_order(order):
    """Everything the CRM shows about an order. Read-only by construction."""
    active = dispatch_svc.active_submission(order)
    return {
        "id": str(order.dispatch_order_id),
        "kind": order.kind,
        "kind_label": order.get_kind_display(),
        "status": order.status,
        "status_label": order.get_status_display(),
        "parent_id": str(order.parent_id) if order.parent_id else None,
        "case_id": str(order.case_id) if order.case_id else None,
        "vendor": (
            {"id": str(order.vendor_id), "name": order.vendor.name}
            if order.vendor_id else None
        ),
        "referral_type": order.referral_type,
        # WHAT gets installed, WHERE, and whether it is APPROVED. The three
        # together are the vendor's actual instruction; any one alone is not
        # actionable -- an approved case with no item says nothing, and an item on
        # an unapproved case must not be fitted.
        "item": order.item,
        "location": order.location,
        "program_name": order.case.program_name if order.case_id else "",
        "authorization": (
            {
                "status": order.case.service_authorization_status or "",
                "approved": (order.case.service_authorization_status or "").lower()
                == "approved",
            }
            if order.case_id else {"status": "", "approved": False}
        ),
        "service_address": order.service_address,
        "address_notes": order.address_notes,
        "contact_phone": order.contact_phone,
        "contact_phone_type": order.contact_phone_type,
        "contact_email": order.contact_email,
        "consent": {
            "call": order.consent_to_call,
            "text": order.consent_to_text,
            "method": order.consent_method,
            "captured_at": order.consent_captured_at,
        },
        "ecm_billed_confirmed": order.ecm_billed_confirmed,
        "notes": order.notes,
        "availability": [
            {
                "date": w.date,
                "start_time": w.start_time,
                "end_time": w.end_time,
            }
            for w in order.availability_windows.all()
        ],
        "visits": [
            {
                "scheduled_for": v.scheduled_for,
                "confirmed_at": v.confirmed_at,
                "started_at": v.started_at,
                "completed_at": v.completed_at,
            }
            for v in order.visits.all()
        ],
        "findings": [
            {
                "id": f.pk,
                "title": f.title,
                "description": f.description,
                "recommendation": f.recommendation,
                "photo_count": f.proofs.count(),
            }
            for f in order.findings.all()
        ],
        "documents": [
            {
                "id": d.pk, "filename": d.filename, "doc_type": d.doc_type,
                "url": d.file_url, "created_at": d.created_at,
            }
            for d in order.documents.all()
        ],
        "submissions": [
            {
                "id": str(s.dispatch_submission_id),
                "sequence": s.sequence,
                "state": s.state,
                "submitted_at": s.submitted_at,
                "voided_at": s.voided_at,
                # Shown deliberately: a void with no visible reason looks like a
                # system glitch to whoever finds it later.
                "void_reason": s.void_reason,
                "signatures": [
                    {"role": sig.signer_role, "signed_at": sig.signed_at}
                    for sig in s.signatures.all()
                ],
            }
            for s in order.submissions.all()
        ],
        "uniteus_uploads": [
            {
                "uploaded_at": u.uploaded_at,
                "recorded_at": u.recorded_at,
                "uploaded_by": u.uploaded_by.name if u.uploaded_by else "",
                "uniteus_ref": u.uniteus_ref,
                # Set when the submission it covered was voided: Unite Us holds
                # evidence the CRM no longer accepts, and someone must re-upload.
                "superseded_at": u.superseded_at,
            }
            for u in order.uniteus_uploads.all()
        ],
        # What still blocks submission. Surfaced so the CRM can show progress
        # without duplicating the rule -- the gate lives in one place.
        "missing_for_submission": dispatch_svc.missing_for_submission(order),
        "active_submission_id": (
            str(active.dispatch_submission_id) if active else None
        ),
        "created_at": order.created_at,
    }


class MemberDispatchOrdersView(PortalAPIView):
    """GET: every dispatch order for a member, assessment first."""

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        orders = (
            DispatchOrder.objects
            .filter(client=client)
            .select_related("vendor", "parent", "case")
            .prefetch_related(
                "availability_windows", "visits", "findings__proofs", "documents",
                "submissions__signatures", "uniteus_uploads",
            )
            # Assessment first, then its remediation orders newest-first.
            .order_by("kind", "-created_at")
        )
        return Response([_serialize_order(o) for o in orders])


class MemberAssessmentOrderCreateView(PortalAPIView):
    """POST: the assessment-order wizard. Once per member.

    On success the order exists at PENDING SCHEDULE and the vendor can see it.
    """

    @transaction.atomic
    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        data = request.data or {}

        # The member must actually have a governing housing case -- otherwise this
        # is an assessment of nothing. Mirrors how the food wizard requires an open
        # internal-service case before it will start.
        from ..services.housing import housing_service_case

        governing = housing_service_case(
            Client.objects.prefetch_related("cases").get(pk=client.pk)
        )
        if governing is None:
            return Response(
                {"detail": "This member has no governing Dwelling Assessment case."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        # REQUIRED fields, enforced here and not only in the wizard. The UI
        # disables its buttons, but a disabled button is not a rule: the food
        # verification's picker excluded housing cases while its ENDPOINT still
        # accepted one, and only an API-level test found it. An order missing a
        # phone or an address cannot be executed by a vendor, so it should not
        # exist.
        missing = []
        if len(re.sub(r"\D", "", data.get("contact_phone") or "")) < 10:
            missing.append("contact_phone")
        if not (data.get("address_line1") or "").strip():
            missing.append("address_line1")
        if not (data.get("referral_type") or "").strip():
            missing.append("referral_type")
        if not (data.get("vendor_id") or "").strip():
            missing.append("vendor_id")
        # Consent is the vendor's authority to contact the member at all, and the
        # ECM confirmation is the agent's sign-off. Neither is optional.
        if not data.get("consent_to_call") and not data.get("consent_to_text"):
            missing.append("consent_to_call/consent_to_text")
        if not data.get("ecm_billed_confirmed"):
            missing.append("ecm_billed_confirmed")
        if missing:
            return Response(
                {"detail": f"Missing required field(s): {', '.join(missing)}"},
                status=http.HTTP_400_BAD_REQUEST,
            )

        vendor_id = (data.get("vendor_id") or "").strip()
        vendor = Vendor.objects.filter(pk=vendor_id, is_active=True).first()
        if vendor is None:
            return Response(
                {"detail": "Unknown or inactive vendor."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        referral_type = (data.get("referral_type") or "").strip()
        if referral_type not in DispatchReferralType.values:
            return Response(
                {"detail": f"Unknown referral type: {referral_type}"},
                status=http.HTTP_400_BAD_REQUEST,
            )

        # Availability: at least THREE DISTINCT DATES, each with a window. Counted
        # as dates, not rows -- three windows on one Tuesday does not satisfy the
        # rule, and a len(windows) >= 3 check would wrongly accept it.
        windows = data.get("availability") or []
        dates = {
            (w.get("date") or "").strip() for w in windows if (w.get("date") or "").strip()
        }
        if len(dates) < dispatch_svc.MIN_AVAILABILITY_DATES:
            return Response(
                {
                    "detail": (
                        f"At least {dispatch_svc.MIN_AVAILABILITY_DATES} different "
                        f"dates of availability are required (got {len(dates)})."
                    )
                },
                status=http.HTTP_400_BAD_REQUEST,
            )

        consent_call = bool(data.get("consent_to_call"))
        consent_text = bool(data.get("consent_to_text"))
        agent = current_agent(request)

        # A resubmitted wizard is the common case (double click, back button), so
        # check BEFORE inserting. The constraint below is the backstop for a true
        # race, and needs its own savepoint: catching IntegrityError inside the
        # outer atomic() would leave the transaction unusable for the follow-up
        # query.
        existing = dispatch_svc.assessment_order_for(client)
        if existing is not None:
            return Response(_serialize_order(existing), status=http.HTTP_200_OK)

        try:
            with transaction.atomic():  # savepoint
                order = DispatchOrder.objects.create(
                    # Honour a client-supplied id when given: an offline or retried
                    # submission must not create a second order.
                    **(
                        {"dispatch_order_id": data["id"]} if data.get("id") else {}
                    ),
                    kind=DispatchKind.ASSESSMENT,
                    client=client,
                    case=governing,
                    dwellings={"primary": str(governing.case_id)},
                    vendor=vendor,
                    created_by=agent,
                    status=DispatchStatus.PENDING_SCHEDULE,
                    contact_phone=(data.get("contact_phone") or "").strip(),
                    contact_phone_type=(data.get("contact_phone_type") or "").strip(),
                    contact_email=(data.get("contact_email") or "").strip(),
                    address_line1=(data.get("address_line1") or "").strip(),
                    address_line2=(data.get("address_line2") or "").strip(),
                    address_city=(data.get("address_city") or "").strip(),
                    address_state=(data.get("address_state") or "").strip(),
                    address_zip=(data.get("address_zip") or "").strip(),
                    address_formatted=(data.get("address_formatted") or "").strip(),
                    address_place_id=(data.get("address_place_id") or "").strip(),
                    address_lat=data.get("address_lat") or None,
                    address_lng=data.get("address_lng") or None,
                    address_notes=(data.get("address_notes") or "").strip(),
                    referral_type=referral_type,
                    consent_to_call=consent_call,
                    consent_to_text=consent_text,
                    consent_method="verbal",
                    consent_captured_at=(
                        timezone.now() if (consent_call or consent_text) else None
                    ),
                    consent_captured_by=agent if (consent_call or consent_text) else None,
                    ecm_billed_confirmed=bool(data.get("ecm_billed_confirmed")),
                    notes=(data.get("notes") or "").strip(),
                )
        except IntegrityError:
            # The one-assessment-per-client constraint. Returning the EXISTING order
            # rather than an error makes a double-submitted wizard harmless.
            existing = dispatch_svc.assessment_order_for(client)
            if existing is not None:
                return Response(
                    _serialize_order(existing), status=http.HTTP_200_OK,
                )
            raise

        for w in windows:
            if not (w.get("date") and w.get("start_time") and w.get("end_time")):
                continue
            DispatchAvailabilityWindow.objects.create(
                dispatch_order=order,
                date=w["date"], start_time=w["start_time"], end_time=w["end_time"],
            )

        dispatch_svc.record_transition(
            order, "", order.status,
            actor=getattr(request, "user", None), source=StageEventSource.MANUAL,
            note=(
                f"assessment order created by {agent.name}" if agent
                else "assessment order created"
            ),
            metadata={
                "case_id": str(governing.case_id),
                # The FK cannot hold an AgentUser, so the acting agent is preserved
                # here and in the note -- the pattern stage_event_actor documents.
                "agent_code": getattr(agent, "agent_code", ""),
            },
        )

        # Home Remediation cases that arrived BEFORE this order have been waiting
        # with nowhere to hang. MIRIAM ISRAEL holds nine.
        adopted = dispatch_svc.adopt_unlinked_remediation_cases(order)

        payload = _serialize_order(order)
        payload["adopted_remediation_orders"] = len(adopted)
        return Response(payload, status=http.HTTP_201_CREATED)


# Fields an agent may still change, and the ONLY statuses in which they may. Once
# the appointment is CONFIRMED the vendor has been told when and where to go, and
# the member has been promised a slot -- editing the address or the phone at that
# point silently desynchronises the two. A confirmed order is corrected by
# rescheduling, not by editing underneath it.
EDITABLE_BEFORE_CONFIRMED = {
    "contact_phone", "contact_phone_type", "contact_email",
    "address_line1", "address_line2", "address_city", "address_state",
    "address_zip", "address_formatted", "address_place_id", "address_notes",
    "referral_type", "ecm_billed_confirmed", "notes",
}


class MemberAssessmentOrderUpdateView(PortalAPIView):
    """PATCH: correct an assessment order that has not been confirmed yet.

    Editing is limited to PENDING_SCHEDULE. This is NOT the vendor-evidence lock
    (which forbids CRM writes entirely) -- these are the agent's own wizard answers,
    and an agent who mistypes a phone number should not have to void anything to fix
    it. But once the visit is confirmed the details have been acted on, so they stop
    being editable.
    """

    @transaction.atomic
    def patch(self, request, client_id, order_id):
        client = get_object_or_404(Client, pk=client_id)
        order = get_object_or_404(
            DispatchOrder, pk=order_id, client=client, kind=DispatchKind.ASSESSMENT,
        )
        if order.status != DispatchStatus.PENDING_SCHEDULE:
            return Response(
                {
                    "detail": (
                        f"This order is {order.get_status_display()} and can no "
                        "longer be edited. Reschedule it instead."
                    )
                },
                status=http.HTTP_409_CONFLICT,
            )

        data = request.data or {}
        agent = current_agent(request)
        changed = {}

        for field in EDITABLE_BEFORE_CONFIRMED:
            if field not in data:
                continue
            value = data[field]
            if field == "ecm_billed_confirmed":
                value = bool(value)
            elif field == "contact_email":
                value = (value or "").strip().lower()
            else:
                value = (value or "").strip() if isinstance(value, str) else value
            if getattr(order, field) != value:
                changed[field] = value
                setattr(order, field, value)

        # Same required-field rules as creation: an edit must not be able to leave
        # an order in a state the wizard would have refused to create.
        if len(re.sub(r"\D", "", order.contact_phone or "")) < 10:
            return Response(
                {"detail": "A full phone number is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if not (order.address_line1 or "").strip():
            return Response(
                {"detail": "An address is required."},
                status=http.HTTP_400_BAD_REQUEST,
            )
        if order.referral_type not in DispatchReferralType.values:
            return Response(
                {"detail": f"Unknown referral type: {order.referral_type}"},
                status=http.HTTP_400_BAD_REQUEST,
            )

        if "vendor_id" in data:
            vendor_id = (data.get("vendor_id") or "").strip()
            vendor = Vendor.objects.filter(pk=vendor_id, is_active=True).first()
            if vendor is None:
                return Response(
                    {"detail": "Unknown or inactive vendor."},
                    status=http.HTTP_400_BAD_REQUEST,
                )
            if order.vendor_id != vendor.pk:
                changed["vendor"] = vendor.name
                order.vendor = vendor

        # Availability is replaced wholesale rather than diffed: the picker submits
        # a complete set of three days, and a partial merge would leave orphaned
        # windows from a previous choice.
        windows = data.get("availability")
        if windows is not None:
            dates = {
                (w.get("date") or "").strip() for w in windows
                if (w.get("date") or "").strip()
            }
            if len(dates) < dispatch_svc.MIN_AVAILABILITY_DATES:
                return Response(
                    {
                        "detail": (
                            f"At least {dispatch_svc.MIN_AVAILABILITY_DATES} "
                            f"different dates are required (got {len(dates)})."
                        )
                    },
                    status=http.HTTP_400_BAD_REQUEST,
                )
            order.availability_windows.all().delete()
            for w in windows:
                if not (w.get("date") and w.get("start_time") and w.get("end_time")):
                    continue
                DispatchAvailabilityWindow.objects.create(
                    dispatch_order=order, date=w["date"],
                    start_time=w["start_time"], end_time=w["end_time"],
                )
            changed["availability"] = f"{len(dates)} dates"

        if not changed:
            return Response(_serialize_order(order))

        order.save()
        # Audited: an order is work promised to a vendor, so a change to its
        # details belongs in the same history as its transitions.
        dispatch_svc.record_transition(
            order, order.status, order.status,
            actor=getattr(request, "user", None),
            source=StageEventSource.MANUAL,
            note=(
                f"order edited by {agent.name}" if agent else "order edited"
            ),
            metadata={
                "changed": sorted(changed),
                "agent_code": getattr(agent, "agent_code", ""),
            },
        )
        return Response(_serialize_order(order))
