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
    DispatchReferralType, DispatchStatus, StageEntityType, StageEvent,
    StageEventSource, Vendor,
)
from ..services import dispatch as dispatch_svc
from ..services import service_area
from .base import PortalAPIView, current_agent

logger = logging.getLogger(__name__)


def _authorization_block(case):
    """``status`` / ``starts_at`` / ``ends_at`` / ``approved`` / ``expired``.

    One shape for a case's authorization, used for both the order header and each
    item, so the two can never disagree about what "expired" means.
    """
    if case is None:
        return {
            "status": "", "starts_at": None, "ends_at": None,
            "approved": False, "expired": False,
        }
    start, end = case.effective_authorization_window()
    status = case.service_authorization_status or ""
    return {
        "status": status,
        "starts_at": start,
        "ends_at": end,
        "approved": status.lower() == "approved",
        "expired": bool(end) and end < timezone.now(),
    }


def _serialize_item(row):
    """One installable item.

    ``authorization`` is read live from the case, unlike ``item``: an approval can
    change or lapse after the item exists, and a stale copy would have a vendor
    fitting something no longer covered.
    """
    return {
        "id": str(row.dispatch_item_id),
        "item": row.item,
        "location": row.location,
        "program_name": row.program_name,
        # The FULL case id: it is what an agent pastes into Unite Us, and a
        # truncated one cannot be searched with.
        "case_id": str(row.case_id) if row.case_id else "",
        "case_status": row.case.case_status if row.case_id else "",
        # The same shape the order header uses, so the two can never disagree
        # about what "expired" means. The WINDOW matters as much as the status: an
        # expired item looks identical to a live one without the dates.
        "authorization": _authorization_block(row.case if row.case_id else None),
        # Which work order covers it, or null when it is still waiting.
        "work_order_id": (
            str(row.dispatch_order_id) if row.dispatch_order_id else None
        ),
        "available": row.is_available,
    }


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
        "location": order.location,
        "program_name": order.case.program_name if order.case_id else "",
        # The governing Dwelling Assessment case -- the FULL id, because an agent
        # pastes it into Unite Us and a truncated one cannot be searched with. The
        # labelled dwellings are exposed too: "primary" is this case, and a
        # "secondary" appears when the member is reassessed at another address.
        # DERIVED from the client rather than stored on the order. A snapshot would
        # be a second copy of the member's name that can drift from the record, and
        # the PDF is rendered server-side from the client anyway -- so there is
        # nothing a stored copy would make correct that this does not.
        "member_name": (
            f"{(order.client.first_name or '').strip()} "
            f"{(order.client.last_name or '').strip()}"
        ).strip(),
        "dwelling_case_id": str(order.case_id) if order.case_id else "",
        "dwellings": order.dwellings or {},
        # The ORDER's own authorization -- the Dwelling Assessment case's window.
        # Shown in the header because it bounds the whole engagement: once it
        # lapses, the assessment itself is out of authorization, regardless of what
        # any individual item says. Uses effective_authorization_window() so it
        # inherits the request-window fallback.
        "authorization": _authorization_block(order.case if order.case_id else None),
        # Whether we serve this dwelling, and which borough it is in. Derived from
        # the ZIP on every read rather than stored: a ZIP removed from the service
        # table means we no longer serve there, and an order holding its old answer
        # would send a vendor somewhere we cannot bill for.
        "service_area": service_area.order_service_area(order),
        # The ITEMS. On an assessment these are every item found; on a work order,
        # the ones that work order covers. An item is not a status -- it is a thing
        # to install -- so it carries its case's AUTHORIZATION rather than a
        # dispatch stage.
        "items": [
            _serialize_item(i)
            for i in (
                order.items.all() if order.kind == DispatchKind.ASSESSMENT
                else order.line_items.all()
            )
        ],
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
                "items__case", "line_items__case",
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
            # Sync items before returning: an order created before its housing
            # cases imported -- or before items existed at all -- would otherwise
            # show an empty Items tab for ever, since nothing else re-checks.
            dispatch_svc.sync_dispatch_items(existing)
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
            agent=agent,
            note="assessment order created",
            metadata={
                "case_id": str(governing.case_id),
                # The wizard's answers, so the history line says what was ordered
                # rather than only that something was. Read from the saved order,
                # not the request, so it records what was actually stored.
                "vendor": vendor.name if vendor else "",
                "referral_type": order.referral_type,
                "address": order.address_formatted or order.address_line1,
                "availability_days": len({w.get("date") for w in windows if w.get("date")}),
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
            # A Save that does nothing has to SAY so. Returning the order silently
            # is why an edit appeared to succeed while leaving no history and no
            # updated_at -- order.save() is never reached on this path, which is
            # exactly how the "nothing was recorded" report arose.
            payload = _serialize_order(order)
            payload["changed_fields"] = []
            return Response(payload)

        order.save()
        # Audited: an order is work promised to a vendor, so a change to its
        # details belongs in the same history as its transitions.
        dispatch_svc.record_transition(
            order, order.status, order.status,
            actor=getattr(request, "user", None),
            source=StageEventSource.MANUAL,
            agent=agent,
            note="order edited",
            metadata={"changed": sorted(changed)},
        )
        payload = _serialize_order(order)
        payload["changed_fields"] = sorted(changed)
        return Response(payload)


class MemberWorkOrderCreateView(PortalAPIView):
    """POST: assemble a work order from approved, undispatched items.

    Creating one is a deliberate agent action, never an import side effect: an
    import discovers ITEMS, a human decides what gets dispatched together.
    """

    @transaction.atomic
    def post(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        assessment = dispatch_svc.assessment_order_for(client)
        if assessment is None:
            return Response(
                {"detail": "This member has no assessment order yet."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        data = request.data or {}
        item_ids = data.get("item_ids") or []
        if not item_ids:
            return Response(
                {"detail": "Select at least one item."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        vendor = None
        vendor_id = (data.get("vendor_id") or "").strip()
        if vendor_id:
            vendor = Vendor.objects.filter(pk=vendor_id, is_active=True).first()
            if vendor is None:
                return Response(
                    {"detail": "Unknown or inactive vendor."},
                    status=http.HTTP_400_BAD_REQUEST,
                )

        try:
            order = dispatch_svc.create_work_order(
                assessment, item_ids, vendor=vendor,
                actor=getattr(request, "user", None),
                agent=current_agent(request),
                notes=(data.get("notes") or "").strip(),
            )
        except ValueError as exc:
            # The message names the offending items, so an agent can see WHICH
            # selection was stale rather than being told "invalid".
            return Response({"detail": str(exc)}, status=http.HTTP_400_BAD_REQUEST)

        return Response(_serialize_order(order), status=http.HTTP_201_CREATED)


class MemberDispatchHistoryView(PortalAPIView):
    """GET: every recorded change to this member's housing dispatch.

    Reads the SHARED StageEvent log, filtered to the member's dispatch orders --
    the same table the food stages use, which is why extending it rather than
    building a dispatch-only log mattered.

    Covers the assessment order AND its work orders in one list, because "what
    happened to this member's housing" is one question, and answering it from two
    places invites the two disagreeing.
    """

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        events = (
            StageEvent.objects
            .filter(
                entity_type=StageEntityType.DISPATCH_ORDER,
                dispatch_order__client=client,
            )
            .select_related("actor", "dispatch_order", "dispatch_order__created_by", "dispatch_order__vendor")
            .order_by("-entered_at")[:200]
        )

        # Rows written before record_transition stored agent_name carry only the
        # CODE. Resolve those in one query rather than leaving a dash where a name
        # belongs -- the history is the audit trail, and "-" reads as "nobody",
        # which is worse than slightly stale.
        # Rows written before record_transition stored agent_name carry only an id
        # or a code. Resolved in one query rather than leaving a dash where a name
        # belongs -- the history is the audit trail, and "-" reads as "nobody".
        #
        # BOTH keys are tried because agent_code is nullable in real data: the agent
        # who created MIRIAM's order has no code at all.
        needs = [e for e in events if not (e.metadata or {}).get("agent_name")]
        ids = {(e.metadata or {}).get("agent_id") for e in needs}
        codes = {(e.metadata or {}).get("agent_code") for e in needs}
        ids.discard(None); ids.discard(""); codes.discard(None); codes.discard("")
        names_by_id, names_by_code = {}, {}
        if ids or codes:
            from ..models import Agent

            if ids:
                names_by_id = {
                    str(pk): name
                    for pk, name in Agent.objects.filter(pk__in=ids)
                    .values_list("pk", "name")
                }
            if codes:
                names_by_code = dict(
                    Agent.objects.filter(agent_code__in=codes)
                    .values_list("agent_code", "name")
                )

        out = []
        for e in events:
            order = e.dispatch_order
            meta = e.metadata or {}
            out.append({
                "id": e.pk,
                "at": e.entered_at,
                # AUTO means the system did it (an import, a cascade); MANUAL means
                # a person. Surfaced so "no user" reads as "not a person's doing"
                # rather than as missing data.
                "source": e.source,
                "from_status": e.from_stage,
                "to_status": e.to_stage,
                "note": e.note,
                # The acting user. StageEvent.actor cannot hold the portal's
                # AgentUser principal, so the agent's NAME is read from the
                # metadata that record_transition writes; a real auth User (a
                # management command, say) still wins.
                "user": (
                    (e.actor.get_full_name() or e.actor.username) if e.actor_id
                    else (
                        meta.get("agent_name")
                        or names_by_id.get(meta.get("agent_id"), "")
                        or names_by_code.get(meta.get("agent_code"), "")
                        # Last resort: the order's own created_by. For the
                        # order-created event that IS the authoritative record of
                        # who ran the wizard, and it rescues every row written
                        # before the metadata carried a name.
                        #
                        # MANUAL events ONLY. An item added by an import was not
                        # done by the agent who created the order, and attributing
                        # it to them would put a name against work nobody did --
                        # which a test caught when this was unscoped.
                        or (
                            order.created_by.name
                            if (
                                e.source == StageEventSource.MANUAL
                                and order and order.created_by_id
                            ) else ""
                        )
                    )
                ),
                "agent_code": meta.get("agent_code") or "",
                "order": {
                    "id": str(order.dispatch_order_id) if order else None,
                    "kind": order.kind if order else "",
                    "kind_label": order.get_kind_display() if order else "",
                },
                # Whatever the change carried: the fields edited, the items added,
                # the case involved. Rendered as-is so a new event type needs no
                # frontend change to become readable.
                "detail": {
                    k: v for k, v in meta.items()
                    if k not in ("agent_name", "agent_code", "agent_id")
                },
            })
        return Response(out)


class MemberAssessmentFormView(PortalAPIView):
    """GET: the Dwelling Assessment form, READ-ONLY.

    Returns the questions with whatever the vendor has answered so far -- every
    question, always, so the CRM shows the blank form before a visit and the
    completed one after. An unanswered form is not an empty response; it is the
    form with nothing ticked, which is what an agent needs to see to know what will
    be asked.

    There is deliberately no POST or PATCH here. Only the vendor completes an
    assessment, and that is enforced by the absence of a route rather than by a
    permission check.
    """

    def get(self, request, client_id):
        client = get_object_or_404(Client, pk=client_id)
        order = dispatch_svc.assessment_order_for(client)
        if order is None:
            return Response(
                {"detail": "This member has no assessment order yet."},
                status=http.HTTP_404_NOT_FOUND,
            )

        from ..models import DispatchQuestionnaire
        from ..services.assessment_forms import (
            build_schema, modules_for_referral,
        )

        form = DispatchQuestionnaire.objects.filter(dispatch_order=order).first()
        if form is not None:
            schema = form.schema()
            answers = form.answers or {}
            others = form.section_other or {}
            chosen = {
                i.get("option"): i.get("qty") or 1
                for i in (form.interventions or []) if i.get("option")
            }
        else:
            # No questionnaire row yet. Render the form the referral type implies,
            # so the tab is useful before the vendor has touched anything.
            schema = build_schema(modules_for_referral(order.referral_type))
            answers, others, chosen = {}, {}, {}

        # ONE form now, not a list of modules. Combined is its own document
        # rather than the two others concatenated, so the response mirrors that.
        sections = []
        for section in schema.get("sections", []):
            sections.append({
                "code": section["code"],
                "title": section["title"],
                "allows_other": section.get("allows_other", False),
                "other": others.get(section["code"], ""),
                "groups": [
                    {
                        "code": g["code"],
                        "label": g["label"],
                        # What the group points at, so the CRM can show WHY a
                        # category of products was offered -- and which sections
                        # owed a photo.
                        "category": g.get("category") or "",
                        "requires_photo": bool(g.get("requires_photo")),
                        "questions": [
                            {
                                "code": q["code"],
                                "label": q["label"],
                                "checked": bool(answers.get(q["code"])),
                            }
                            for q in g["questions"]
                        ],
                    }
                    for g in section["groups"]
                ],
            })

        photos_by_group = {}
        for proof in order.proofs.all():
            photos_by_group.setdefault(proof.intervention_group or "", 0)
            photos_by_group[proof.intervention_group or ""] += 1

        categories = []
        for category in schema.get("categories", []):
            categories.append({
                "code": category["code"],
                "label": category["label"],
                "groups": [
                    {
                        "code": g["code"],
                        "label": g["label"],
                        "program_item": g.get("program_item") or "",
                        "options": [
                            {
                                "code": o["code"],
                                "label": o["label"],
                                # qty 0 renders as "Not added", matching the
                                # vendor's own form rather than hiding unchosen
                                # options -- the full catalogue is what shows an
                                # agent what COULD have been recommended.
                                "qty": chosen.get(o["code"], 0),
                            }
                            for o in g["options"]
                        ],
                    }
                    for g in category["groups"]
                ],
            })

        active = dispatch_svc.active_submission(order)
        return Response({
            "order_id": str(order.dispatch_order_id),
            "referral_type": order.referral_type,
            "state": form.state if form else "not_started",
            "submitted_at": form.submitted_at if form else None,
            "template_version": (
                form.template_version if form else schema.get("version")
            ),
            "form": schema.get("form") or "",
            "form_label": schema.get("label") or "",
            "service_code": schema.get("service_code") or "",
            "sections": sections,
            "categories": categories,
            # Photos by the SECTION they evidence, so the CRM can show the
            # assessment's evidence against the findings rather than as a pile.
            "photos_by_group": photos_by_group,
            "justification": form.justification if form else "",
            "assessor_notes": form.assessor_notes if form else "",
            # The wrapper the form shares across modules: photos and the two
            # signatures. The real form's submit rule is "at least one photo of the
            # dwelling, your signature, and the member's signature".
            "photo_count": order.proofs.count(),
            "signatures": [
                {"role": sig.signer_role, "signed_at": sig.signed_at,
                 "signer_name": sig.signer_name}
                for sig in (active.signatures.all() if active else [])
            ],
        })


class MemberCaseRecommendationsView(PortalAPIView):
    """GET: the Unite Us cases to open after the vendor's assessment.

    MANY PRODUCTS COLLAPSE INTO ONE CASE. Four grab bars are not four cases; they
    are one "Grab Bars - <borough>" case with four items on it. Opening one per
    product would create duplicates in Unite Us.

    Read-only, and deliberately so: a case is opened in Unite Us, and the CRM only
    learns about it on the next import. This tells an agent what to create.
    """

    def get(self, request, client_id):
        from ..models import DispatchQuestionnaire
        from ..services import case_recommendations as recs

        client = get_object_or_404(Client, pk=client_id)
        order = dispatch_svc.assessment_order_for(client)
        if order is None:
            return Response({
                "state": "no_order", "borough": "", "cases": [],
                "detail": "This member has no assessment order.",
            })

        form = DispatchQuestionnaire.objects.filter(dispatch_order=order).first()
        if form is None or not form.is_submitted:
            # Reported rather than 404'd: "the vendor has not submitted yet" is the
            # answer to the question, not an error.
            return Response({
                "state": form.state if form else "not_started",
                "borough": recs.member_borough(client),
                "cases": [],
                "detail": "The vendor has not submitted the assessment yet.",
            })

        cases = recs.recommended_cases(form)
        return Response({
            "state": "submitted",
            "submitted_at": form.submitted_at,
            "borough": recs.member_borough(client, order),
            "service_area": service_area.order_service_area(order),
            # Reported when the dwelling's ZIP and the governing case disagree: a
            # member who has moved needs cases in the NEW borough, but an agent
            # should be told the records differ rather than find out on an invoice.
            "borough_conflict": recs.borough_conflict(client, order),
            "cases": cases,
            # Counted here so the UI does not have to re-derive the rules it is
            # about to explain.
            "summary": {
                "cases": len(cases),
                "products": sum(len(c["products"]) for c in cases),
                "blocked": sum(
                    1 for c in cases if not (c["exists"] and c["is_internal"])
                ),
                "already_open": sum(1 for c in cases if c["already_open"]),
            },
        })
