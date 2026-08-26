import logging
import os
import uuid

from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone
from rest_framework import generics, permissions, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.decorators import action
from rest_framework import status

from .models import (
    Agent,
    AllowedZipCode,
    Assessment,
    Case,
    CaseType,
    Client,
    ContractedService,
    DietaryRestriction,
    EnrollmentStage,
    EnrollmentVerification,
    FoodAllergy,
    HouseholdMember,
    MemberDietaryProfile,
    MemberStatus,
    MenuCategory,
    MenuType,
    Program,
    ProgramEligibility,
    Provider,
    Screening,
    TimelineEvent,
)
from .serializers import (
    AssessmentSerializer,
    CaseSerializer,
    ClientSerializer,
    ContractedServiceSerializer,
    EnrollmentVerificationSerializer,
    HouseholdSerializer,
    ProgramEligibilitySerializer,
    ProgramSerializer,
    ProviderSerializer,
    RegisterSerializer,
    ScreeningSerializer,
    TimelineEventSerializer,
    UserSerializer,
    add_client_to_household,
    ensure_household_with_primary,
    search_clients,
    sync_household_members,
)
from .history import ChangeSource
from .services import timeline
from .services.lifecycle import (
    InvalidTransition,
    advance_enrollment,
    clear_new_flag_on_verification_request,
    recompute_client_stage,
    recompute_enrollment_household,
    reconcile_enrollment_authorization,
    reconcile_internal_service_authorization,
    split_dependent_into_own_enrollment,
)

logger = logging.getLogger(__name__)


def _agent_actor(request):
    """Attribution string for the authenticated agent, e.g. 'agent:355'."""
    code = getattr(getattr(request, "user", None), "agent_code", None)
    return f"agent:{code}" if code else ""


def _safe_timeline(builder, obj, request):
    """Emit a timeline event, never letting a failure break the API write."""
    try:
        builder(obj, actor=_agent_actor(request))
    except Exception:  # noqa: BLE001
        logger.exception("timeline emit failed for %s", type(obj).__name__)


def _safe_recompute_stage(obj):
    """Recompute the client's lifecycle stage after a screening/assessment write,
    never letting a failure break the API write."""
    client = getattr(obj, "client", None)
    if client is None:
        return
    try:
        recompute_client_stage(client)
    except Exception:  # noqa: BLE001
        logger.exception("recompute_client_stage failed for %s", getattr(client, "pk", None))


def _safe_recompute_household(enrollment):
    """Recompute the lifecycle stage for an enrollment's primary AND every
    non-denied household member (so the whole group tracks the enrollment, e.g.
    all members go to Pending Verification when a verification is requested),
    never letting a failure break the API write."""
    try:
        recompute_enrollment_household(enrollment)
    except Exception:  # noqa: BLE001
        logger.exception(
            "recompute_enrollment_household failed for %s", getattr(enrollment, "pk", None)
        )


class RegisterView(generics.CreateAPIView):
    """Public endpoint to create a new user."""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class MeView(APIView):
    """Return the currently authenticated user (requires a valid JWT)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class HealthView(APIView):
    """Public health check. Reports which environment is responding (so a caller
    can confirm it's hitting the real live prod backend, not local/dev) plus a
    quick database connectivity probe."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from django.conf import settings as dj_settings
        from django.db import connection
        from django.utils import timezone

        db_ok = True
        try:
            connection.ensure_connection()
        except Exception:  # noqa: BLE001 - report unhealthy DB rather than 500
            db_ok = False

        return Response({
            "status": "ok" if db_ok else "degraded",
            "environment": os.getenv("ENVIRONMENT", "local"),
            "debug": dj_settings.DEBUG,
            "host": request.get_host(),
            "database": "ok" if db_ok else "error",
            "server_time": timezone.now().isoformat(),
        })


class ZipCodeCheckView(APIView):
    """Check whether a ZIP code is in the allowed service area.

    GET /api/zipcodes/check/?zip=11201 ->
        {"zip": "11201", "allowed": true, "borough": "...", "scn": "...",
         "platform": "..."}
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        raw = (request.query_params.get("zip") or "").strip()
        # Normalize ZIP+4 to the 5-digit base.
        zip5 = raw.split("-")[0][:5]
        if not zip5:
            return Response(
                {"detail": "A 'zip' query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        match = AllowedZipCode.objects.filter(
            zip_code=zip5, is_active=True
        ).first()
        # TEMPORARY STOPGAP (remove to restore real service-area gating): force
        # every ZIP to report as allowed so the extension's verification
        # preflight never blocks on "outside our service area". Real borough/scn/
        # platform are still returned when the ZIP is known. To restore, change
        # "allowed" back to ``match is not None``.
        return Response({
            "zip": zip5,
            "allowed": True,
            "borough": match.borough if match else "",
            "scn": match.scn if match else "",
            "platform": match.platform if match else "",
        })


class StateCheckView(APIView):
    """Check whether a US state is one we accept clients/cases from.

    GET /api/states/check/?state=NY -> {"state": "NY", "allowed": true}

    ``allowed`` is True for a served state, an unknown/blank state (can't judge),
    or when no states are configured at all (feature inert until an admin opts
    in). This is a soft signal the extension surfaces as a warning; it never
    blocks.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .services.state_area import is_state_allowed, normalize_state

        raw = (request.query_params.get("state") or "").strip()
        code = normalize_state(raw)
        return Response({
            "state": code or raw.upper(),
            "allowed": is_state_allowed(raw),
        })


class BulkUpsertMixin:
    """Adds a /bulk/ action accepting a list of records for batch upsert.

    Each item is upserted independently; per-item errors are collected and
    returned without failing the whole batch.
    """

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        items = request.data
        if not isinstance(items, list):
            return Response(
                {"detail": "Expected a JSON list of records."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        created, errors, saved = [], [], []
        for index, item in enumerate(items):
            serializer = self.get_serializer(data=item)
            if serializer.is_valid():
                obj = serializer.save()
                self.post_upsert(obj)
                created.append(str(obj.pk))
                saved.append(obj)
            else:
                errors.append({"index": index, "errors": serializer.errors})
        body = {
            "received": len(items),
            "succeeded": len(created),
            "failed": len(errors),
            "ids": created,
            "errors": errors,
        }
        body.update(self.bulk_response_extra(saved) or {})
        return Response(
            body,
            status=status.HTTP_207_MULTI_STATUS if errors else status.HTTP_200_OK,
        )

    def post_upsert(self, obj):
        """Hook called after a successful bulk upsert. No-op by default."""
        return None

    def bulk_response_extra(self, objs):
        """Hook to add extra keys to the bulk response (e.g. warnings). Receives
        the successfully-saved objects. Returns a dict merged into the response."""
        return {}


class ClientViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for clients (keyed on source client_id UUID)."""

    queryset = Client.objects.all().prefetch_related(
        "addresses", "insurances", "social_care_coverages", "military_profile"
    )
    serializer_class = ClientSerializer

    def _agent_save_kwargs(self):
        """Stamp the authenticated agent's real code + full name onto the client.

        The extension sends the agent's NAME in ``agent_code``; the canonical
        code and name live on the agent JWT (``request.user.agent_code`` /
        ``request.user.name``). Override them so downstream (GHL Agent Code /
        Assigned Agent) resolves reliably.
        """
        user = self.request.user
        kwargs = {}
        code = getattr(user, "agent_code", None)
        if code:
            kwargs["agent_code"] = code
        name = getattr(user, "name", None)
        if name:
            kwargs["agent_name"] = name
        # Williamsburg agents (Settings > Williamsburg Setup): force every client
        # they save to lead_source="Williamsburg". Passing lead_source through
        # serializer.save() lands it in validated_data, so ClientSerializer's
        # existing derivation flips is_williamsburg=True automatically.
        if self._is_williamsburg_agent(user):
            kwargs["lead_source"] = "Williamsburg"
        return kwargs

    @staticmethod
    def _is_williamsburg_agent(user):
        """True when the authenticated agent is flagged as a Williamsburg agent."""
        agent_id = getattr(user, "agent_id", None)
        if not agent_id:
            return False
        return Agent.objects.filter(
            pk=agent_id, is_williamsburg_agent=True
        ).exists()

    def _reconcile_eligibility(self, obj):
        """Run the import-time eligibility gates (medical-insurance expiry/type,
        social-care hold, out-of-range address) after an extension client
        upsert, so the INELIGIBLE off-ramp is SET when the ext saves bad data and
        RECOVERED when the ext saves a fix -- exactly as on the CSV import. This
        is the ext half of the "recovery-on-fix" path.

        Runs AFTER ``serializer.save`` has persisted the client's insurances /
        addresses (the gates read those relations). Fetches a fresh client so
        newly-written child rows aren't read from a stale prefetch cache.
        Best-effort: eligibility must never break the client save.
        """
        if obj is None:
            return
        try:
            from api.history import ChangeSource
            from api.services.eligibility import reconcile_client_eligibility

            actor_label = getattr(self.request.user, "name", "") or ""
            reconcile_client_eligibility(
                Client.objects.get(pk=obj.pk),
                actor_label=actor_label,
                source=ChangeSource.EXTENSION,
            )
        except Exception:  # never let eligibility break the client upsert
            pass

    def perform_create(self, serializer):
        serializer.save(**self._agent_save_kwargs())
        self._reconcile_eligibility(serializer.instance)
        _safe_timeline(timeline.event_for_consent, serializer.instance, self.request)

    def perform_update(self, serializer):
        serializer.save(**self._agent_save_kwargs())
        self._reconcile_eligibility(serializer.instance)
        _safe_timeline(timeline.event_for_consent, serializer.instance, self.request)

    def post_upsert(self, obj):
        # Bulk path: stamp the agent code + name from the JWT if available.
        user = self.request.user
        updates = []
        code = getattr(user, "agent_code", None)
        if code and obj.agent_code != code:
            obj.agent_code = code
            updates.append("agent_code")
        name = getattr(user, "name", None)
        if name and obj.agent_name != name:
            obj.agent_name = name
            updates.append("agent_name")
        # Williamsburg agents: force lead_source="Williamsburg" (+ derived flag).
        # The bulk path saves via the serializer without _agent_save_kwargs, so
        # apply the same rule here to keep both write paths consistent.
        if self._is_williamsburg_agent(user):
            if obj.lead_source != "Williamsburg":
                obj.lead_source = "Williamsburg"
                updates.append("lead_source")
            if not obj.is_williamsburg:
                obj.is_williamsburg = True
                updates.append("is_williamsburg")
        if updates:
            obj.save(update_fields=updates)
        self._reconcile_eligibility(obj)
        _safe_timeline(timeline.event_for_consent, obj, self.request)

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        """Central client history: all domain events newest-first, plus renewal
        grouping metadata for the dashboard's "Renewal #N" section headers."""
        client = self.get_object()
        events = list(
            TimelineEvent.objects.filter(client=client)
            .select_related("content_type", "enrollment")
            .order_by("-occurred_at", "-created_at")
        )
        by_cycle = {}
        for ev in events:
            by_cycle.setdefault(ev.renewal_number, []).append(ev)
        renewals = []
        for num in sorted(by_cycle, reverse=True):
            if num < 2:  # cycle 1 is the initial (ungrouped) timeline
                continue
            dates = [e.occurred_at for e in by_cycle[num] if e.occurred_at]
            renewals.append({
                "renewal_number": num,
                "label": f"Renewal #{num}",
                "period_start": min(dates).date().isoformat() if dates else None,
                "period_end": max(dates).date().isoformat() if dates else None,
                "count": len(by_cycle[num]),
            })
        return Response({
            "client_id": str(client.pk),
            "renewals": renewals,
            "results": TimelineEventSerializer(events, many=True).data,
        })

    @action(detail=False, methods=["get"])
    def search(self, request):
        """Find existing clients by member ID (client UUID) or by Medicaid /
        insurance member ID (external_member_id). Used by the household member
        picker. Returns lightweight rows, not the full client serializer."""
        return Response(search_clients(request.query_params.get("q")))

    def _household_response(self, client):
        household = ensure_household_with_primary(client)
        data = HouseholdSerializer(household).data
        data["max_members"] = client.total_family_members or 1
        return Response(data)

    @action(detail=True, methods=["get"])
    def household(self, request, pk=None):
        """Get-or-create this client's household (with the client as primary)
        and return it with its members and the max member cap."""
        return self._household_response(self.get_object())

    @action(detail=True, methods=["post"], url_path="household/add")
    def household_add(self, request, pk=None):
        """Add an existing client to this client's household. Enforces the
        family-size cap and the one-household-per-client rule."""
        primary = self.get_object()
        member_id = request.data.get("client_id")
        if not member_id:
            return Response(
                {"detail": "client_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        household = ensure_household_with_primary(primary)

        # Idempotent: already a member of THIS household -> just return it.
        if household.members.filter(client_id=member_id).exists():
            return self._household_response(primary)

        max_members = primary.total_family_members or 1
        if household.members.count() >= max_members:
            return Response(
                {"detail": f"Household is full ({max_members} member(s) allowed)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            member_client = Client.objects.get(pk=member_id)
        except (Client.DoesNotExist, ValueError):
            return Response(
                {"detail": "Client not found."}, status=status.HTTP_404_NOT_FOUND
            )
        # Add + move (detaches from any other household) + mirror into the
        # active enrollment as a dietary profile. See add_client_to_household.
        add_client_to_household(primary, member_client)
        return self._household_response(primary)

    @action(detail=True, methods=["post"], url_path="household/remove")
    def household_remove(self, request, pk=None):
        """Remove a member from this client's household. The primary member
        cannot be removed."""
        primary = self.get_object()
        member_id = request.data.get("client_id")
        household = ensure_household_with_primary(primary)
        member = household.members.filter(client_id=member_id).first()
        if member is None:
            return Response(
                {"detail": "Not a member of this household."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if member.is_primary:
            return Response(
                {"detail": "The primary member cannot be removed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        member.delete()
        # Also drop the member's dietary profile(s) on this household's
        # enrollments -- otherwise the read-side sync (which ties any profiled
        # client back into the roster) would immediately re-add them.
        MemberDietaryProfile.objects.filter(
            client_id=member_id, enrollment__household=household
        ).delete()
        return self._household_response(primary)


def _dependent_household_warning(client):
    """Return a warning dict when ``client`` is a NON-primary member of a shared
    household (a dependent) -- so the extension can prompt the agent that saving
    this member's own case means they should be split out via Request
    Verification. ``None`` for a primary / household-less client (no warning)."""
    if client is None:
        return None
    from .models import HouseholdMember

    membership = (
        HouseholdMember.objects.filter(client=client, is_primary=False)
        .select_related("household").first()
    )
    if membership is None:
        return None
    primary = (
        membership.household.members.filter(is_primary=True)
        .select_related("client").first()
    )
    primary_name = ""
    if primary and primary.client:
        primary_name = f"{primary.client.first_name} {primary.client.last_name}".strip()
    member_name = f"{client.first_name} {client.last_name}".strip()
    return {
        "type": "dependent_in_household",
        "client_id": str(client.pk),
        "member_name": member_name,
        "primary_name": primary_name,
        "message": (
            f"{member_name or 'This member'} is part of "
            f"{(primary_name + chr(39) + 's') if primary_name else 'an existing'} "
            "household. To give them their own case, request verification for "
            "them in the CRM — they'll be split into their own household."
        ),
    }


class CaseViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for cases (keyed on source case_id UUID)."""

    queryset = Case.objects.select_related(
        "client", "provider", "originating_provider", "program"
    )
    serializer_class = CaseSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs

    # No case-ticket actions are suppressed for extension writes (the
    # case_no_services rule was removed globally). Kept as a hook for future use.
    _SKIP_TICKET_ACTIONS = frozenset()

    def _record_case_change(self, case):
        """Emit case-change TIMELINE events for a case written by the extension,
        attributed to the acting agent. Opens NO follow-up tickets: case
        status/authorization changes (closed, denied, approved, ...) are surfaced
        via the member timeline + the lifecycle reconcile (pause/cancel/resume),
        not tickets."""
        from .services import case_events

        try:
            case_events.record_case_change(
                case,
                previous_status=getattr(case, "_prev_status", None),
                previous_auth=getattr(case, "_prev_auth", None),
                source=ChangeSource.EXTENSION,
                actor=_agent_actor(self.request),
                create_tickets=False,
                skip_actions=self._SKIP_TICKET_ACTIONS,
            )
        except Exception:  # noqa: BLE001 - tracking must never break the write
            logger.exception("record_case_change failed for %s", getattr(case, "pk", None))

    def perform_create(self, serializer):
        serializer.save()
        _safe_timeline(timeline.event_for_case, serializer.instance, self.request)
        self._record_case_change(serializer.instance)

    def perform_update(self, serializer):
        serializer.save()
        _safe_timeline(timeline.event_for_case, serializer.instance, self.request)
        self._record_case_change(serializer.instance)

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        """Batch case upsert for the extension.

        A client can carry several cases in one payload, so the client-wide
        internal-service reconcile (governing-case detection + Objective 1-3
        actions) must run ONCE on the COMPLETE case picture -- never per row
        against a partial one (which could, e.g., hard off-ramp a client to
        Ineligible off a closed case written before its open successor). Defer the
        per-save reconcile for the loop, collect the touched clients, then
        reconcile each exactly once. Mirrors the CSV import + Unite Us pull.
        """
        from .services.lifecycle import (
            deferred_internal_service_reconcile,
            reconcile_internal_service_authorization,
        )

        self._bulk_reconcile_client_ids = set()
        try:
            with deferred_internal_service_reconcile():
                response = super().bulk(request)
        finally:
            client_ids = getattr(self, "_bulk_reconcile_client_ids", set())
            self._bulk_reconcile_client_ids = set()

        actor = getattr(request, "user", None)
        for cid in client_ids:
            client = Client.objects.filter(pk=cid).first()
            if client is None:
                continue
            try:
                reconcile_internal_service_authorization(client, actor=actor)
            except Exception:  # noqa: BLE001 - never fail the write on a reconcile
                logger.exception("bulk case reconcile failed for client %s", cid)
        return response

    def post_upsert(self, obj):
        _safe_timeline(timeline.event_for_case, obj, self.request)
        self._record_case_change(obj)
        # Collect the client for the once-per-client deferred reconcile run by
        # ``bulk`` after the full case picture is written.
        if getattr(obj, "client_id", None) and hasattr(self, "_bulk_reconcile_client_ids"):
            self._bulk_reconcile_client_ids.add(obj.client_id)

    def bulk_response_extra(self, objs):
        """Surface a "this member is a dependent in a household" warning per
        distinct client whose case was saved, so the extension can prompt the
        agent to Request Verification (which splits them into their own case)."""
        warnings, seen = [], set()
        for obj in objs:
            cid = getattr(obj, "client_id", None)
            if not cid or cid in seen:
                continue
            seen.add(cid)
            w = _dependent_household_warning(getattr(obj, "client", None))
            if w:
                warnings.append(w)
        return {"warnings": warnings} if warnings else {}


class ContractedServiceViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for contracted services (keyed on provided_service UUID).

    Filterable by ``?case=<uuid>`` or ``?client=<uuid>`` (via the parent case).
    """

    queryset = ContractedService.objects.select_related("case")
    serializer_class = ContractedServiceSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        case = self.request.query_params.get("case")
        if case:
            qs = qs.filter(case_id=case)
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(case__client_id=client)
        return qs


class ScreeningViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for screenings (keyed on enhanced_screen_id UUID)."""

    queryset = Screening.objects.select_related("client")
    serializer_class = ScreeningSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs

    # Screening save records the Screening timeline event but intentionally does
    # NOT advance the lifecycle stage: the funnel only moves on explicit steps
    # (verification) and the nightly Unite Us import, never on an ext screening
    # write. (Stage is left untouched here by design.)
    def perform_create(self, serializer):
        serializer.save()
        _safe_timeline(timeline.event_for_screening, serializer.instance, self.request)

    def perform_update(self, serializer):
        serializer.save()
        _safe_timeline(timeline.event_for_screening, serializer.instance, self.request)

    def post_upsert(self, obj):
        _safe_timeline(timeline.event_for_screening, obj, self.request)


class AssessmentViewSet(BulkUpsertMixin, viewsets.ModelViewSet):
    """CRUD + upsert for assessments (keyed on assessment_id UUID)."""

    queryset = Assessment.objects.select_related("client")
    serializer_class = AssessmentSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        return qs

    # NOTE: Assessments are intentionally NOT mirrored to GHL. Saving an
    # assessment must not create/update any GHL opportunity, so the
    # sync hooks are deliberately omitted here.
    def perform_create(self, serializer):
        serializer.save()
        _safe_timeline(timeline.event_for_assessment, serializer.instance, self.request)
        _safe_recompute_stage(serializer.instance)

    def perform_update(self, serializer):
        serializer.save()
        _safe_timeline(timeline.event_for_assessment, serializer.instance, self.request)
        _safe_recompute_stage(serializer.instance)

    def post_upsert(self, obj):
        _safe_timeline(timeline.event_for_assessment, obj, self.request)
        _safe_recompute_stage(obj)


def _choices(enum):
    """Serialize a TextChoices enum to [{value, label}] for the wizard UI."""
    return [{"value": v, "label": l} for v, l in enum.choices]


# Pre-verification stages a (never-verified) enrollment can be REQUESTED from:
# the agent can (re-)request verification and the wizard/popup opens. Includes
# VALIDATED so a member whose enrollment was regressed there (e.g. a reconcile
# dropped it to Validated when no open case existed yet) isn't stranded behind a
# "Pending Verification" label with no way to act -- requesting returns them to
# PENDING_VERIFICATION. Excludes VERIFIED/KITCHEN_ASSIGNMENT/SERVICE_ACTIVE (real
# progress we never reset) and terminals.
_REQUESTABLE_STAGES = (
    EnrollmentStage.PENDING_VERIFICATION,
    EnrollmentStage.VALIDATED,
    EnrollmentStage.PENDING_VALIDATION,
)


class EnrollmentVerificationViewSet(viewsets.ModelViewSet):
    """CRUD for household verification enrollments + the wizard data.

    Stage changes go through the ``set-stage`` action (guarded + timeline-logged)
    rather than a plain PATCH, since the Step-4 authorization outcome IS the
    stage. Filterable by ``?client=<uuid>`` or ``?household=<uuid>``.
    """

    queryset = EnrollmentVerification.objects.select_related(
        "client", "household", "case", "delivery_address"
    ).prefetch_related("member_profiles__client")
    serializer_class = EnrollmentVerificationSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        client = self.request.query_params.get("client")
        if client:
            qs = qs.filter(client_id=client)
        household = self.request.query_params.get("household")
        if household:
            qs = qs.filter(household_id=household)
        case = self.request.query_params.get("case")
        if case:
            qs = qs.filter(case_id=case)
        return qs

    def create(self, request, *args, **kwargs):
        # One verification per navigation case (also guarded by a DB constraint).
        # But rather than hard-blocking a re-request, RENEW a still-pending
        # (unverified) enrollment: the agent's action bumps requested_at + the
        # acting agent so the CRM shows it as freshly requested. Only a verification
        # that's already been COMPLETED (or advanced past pending) returns 409 --
        # we never reset a real verification.
        case_id = request.data.get("case_id")
        client_id = request.data.get("client_id")
        existing = None
        if case_id:
            existing = EnrollmentVerification.objects.filter(case_id=case_id).first()
        if existing is None and client_id:
            client_qs = EnrollmentVerification.objects.filter(client_id=client_id)
            # A COMPLETED verification (verified_at set) must NEVER be reset or
            # duplicated -- surface it first so we 409 below, even when the
            # request carries no case_id. Otherwise fall back to a still-pending
            # enrollment, which is renewable.
            existing = (
                client_qs.filter(verified_at__isnull=False)
                .order_by("-opened_at")
                .first()
                or client_qs.filter(
                    verified_at__isnull=True,
                    stage__in=_REQUESTABLE_STAGES,
                )
                .order_by("-opened_at")
                .first()
            )
        if existing is not None:
            renewable = (
                existing.verified_at is None
                and existing.stage in _REQUESTABLE_STAGES
            )
            if not renewable:
                return Response(
                    {"detail": "A verification has already been requested for this case."},
                    status=status.HTTP_409_CONFLICT,
                )
            return self._renew(existing, request, case_id=case_id)
        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError:
            return Response(
                {"detail": "A verification has already been requested for this case."},
                status=status.HTTP_409_CONFLICT,
            )

    def _renew(self, enrollment, request, *, case_id=None):
        """Renew a still-pending verification request: stamp a fresh
        ``requested_at`` + acting agent (overwriting any prior requester -- the
        history keeps every renewal), link the governing case if it was missing
        (e.g. a bulk-imported enrollment), and re-drive the household. Returns the
        renewed enrollment (200)."""
        # A never-verified enrollment sitting BELOW Pending Verification (e.g.
        # regressed to Validated) is put back INTO the verification queue so the
        # wizard/popup appears -- otherwise the member is stranded behind a
        # "Pending Verification" label with no way to act. Real progress
        # (verified/kitchen/active) never reaches here (guarded by the caller).
        if (
            enrollment.verified_at is None
            and enrollment.stage != EnrollmentStage.PENDING_VERIFICATION
        ):
            advance_enrollment(
                enrollment, EnrollmentStage.PENDING_VERIFICATION, force=True,
                actor=getattr(request, "user", None),
                note="Verification requested: returned to Pending Verification.",
                trigger="verification_requested",
            )
        agent_id = getattr(getattr(request, "user", None), "agent_id", None)
        enrollment.requested_at = timezone.now()
        update_fields = ["requested_at"]
        if agent_id:
            enrollment.requested_by_id = agent_id
            update_fields.append("requested_by")
        if case_id and str(enrollment.case_id) != str(case_id):
            case = Case.objects.filter(pk=case_id).first()
            # Switch the enrollment onto the agent-selected case (fills a missing
            # link OR re-points from a prior case). Never steal a case already
            # tied to a DIFFERENT enrollment (per-case unique constraint).
            taken = (
                EnrollmentVerification.objects.filter(case_id=case_id)
                .exclude(pk=enrollment.pk)
                .exists()
            )
            if case is not None and not taken:
                enrollment.case = case
                update_fields.append("case")
        enrollment.save(update_fields=update_fields)
        # Re-requesting means the household is being handled again -> drop it off
        # the Urgent Care list.
        clear_new_flag_on_verification_request(enrollment)
        # A re-request for a splittable dependent still splits them into their own
        # case (heals a pending enrollment created before the split was wired in).
        if not self._maybe_split_dependent(enrollment):
            _safe_recompute_household(enrollment)
        enrollment.refresh_from_db()
        return Response(self.get_serializer(enrollment).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="re-request")
    def re_request(self, request, pk=None):
        """Explicitly renew a pending verification request (fresh date + agent).
        409 if the verification is already completed / advanced past pending."""
        enrollment = self.get_object()
        if (
            enrollment.verified_at is not None
            or enrollment.stage not in _REQUESTABLE_STAGES
        ):
            return Response(
                {"detail": "Only a pending (unverified) verification can be renewed."},
                status=status.HTTP_409_CONFLICT,
            )
        return self._renew(enrollment, request, case_id=enrollment.case_id)

    @action(detail=True, methods=["post"], url_path="set-case")
    def set_case(self, request, pk=None):
        """Re-point the enrollment's governing internal-service case to the
        agent-selected one (the client may hold several meal/box cases). Keeps
        the SAME enrollment -- no duplicate verification is created -- so
        accountability stays with the acting agent.

        Guards: the target must be an internal-service case belonging to the same
        client/household, and must not already be tied to another enrollment
        (per-case unique constraint). Re-runs the authorization projection so an
        approved/denied selection takes effect immediately.
        """
        enrollment = self.get_object()
        case_id = request.data.get("case_id")
        if not case_id:
            return Response(
                {"detail": "case_id is required."}, status=status.HTTP_400_BAD_REQUEST
            )
        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return Response(
                {"detail": "Case not found."}, status=status.HTTP_404_NOT_FOUND
            )
        if case.case_type != CaseType.INTERNAL_SERVICE:
            return Response(
                {"detail": "Only an Internal Service case can govern a verification."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if enrollment.client_id and case.client_id != enrollment.client_id:
            return Response(
                {"detail": "Case belongs to a different client."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        taken = (
            EnrollmentVerification.objects.filter(case_id=case_id)
            .exclude(pk=enrollment.pk)
            .exists()
        )
        if taken:
            return Response(
                {"detail": "That case is already tied to another verification."},
                status=status.HTTP_409_CONFLICT,
            )
        if str(enrollment.case_id) == str(case_id):
            return Response(self.get_serializer(enrollment).data)

        enrollment.case = case
        enrollment.save(update_fields=["case"])
        # Project the newly-selected case's authorization (approved -> advance;
        # denied -> pause) and re-drive the household.
        try:
            reconcile_enrollment_authorization(
                enrollment, actor=getattr(request, "user", None)
            )
            reconcile_internal_service_authorization(
                enrollment.client, actor=getattr(request, "user", None)
            )
        except Exception:  # never let reconcile break the switch
            logger.exception("reconcile after set-case failed for %s", enrollment.pk)
        _safe_recompute_household(enrollment)
        enrollment.refresh_from_db()
        return Response(self.get_serializer(enrollment).data)

    def perform_create(self, serializer):
        serializer.save()
        # Attribute the verification REQUEST to the authenticated ext agent (the
        # one who submitted the E-Form). NULL when the request has no resolvable
        # agent (e.g. bulk/system writes).
        agent_id = getattr(getattr(self.request, "user", None), "agent_id", None)
        if agent_id:
            enrollment = serializer.instance
            enrollment.requested_by_id = agent_id
            enrollment.save(update_fields=["requested_by"])
        _safe_timeline(timeline.event_for_verification, serializer.instance, self.request)
        # Requesting a verification means the household is now being handled, so
        # clear the primary's is_new flag -> drop it off the Urgent Care list.
        clear_new_flag_on_verification_request(serializer.instance)
        # SPLITTABLE DEPENDENT: if this verification is for a non-primary household
        # member who carries a live profile on the shared enrollment, split them
        # into their own internal-service case (carry data + verified fact, detach
        # from the shared household). The split does its own stage projection, so
        # the generic household recompute is skipped for it.
        if self._maybe_split_dependent(serializer.instance):
            return
        # A new enrollment (default Pending Verification) drives the WHOLE
        # household's lifecycle stage: the primary and every non-denied member
        # move to Pending Verification together, not just the primary.
        _safe_recompute_household(serializer.instance)

    def _maybe_split_dependent(self, enrollment):
        """Split a dependent into their own case when verification is requested for
        them, mirroring the split wired into ``MemberVerificationCreateView`` so
        the extension's Request-Verification path (which posts here) also splits.

        Fires only when the enrollment's client is a NON-primary household member
        AND still carries a live dietary profile on the shared household's OTHER
        enrollment. Returns True when a split ran. Best-effort: a split failure
        never breaks the verification request."""
        client = getattr(enrollment, "client", None)
        if client is None:
            return False
        membership = (
            HouseholdMember.objects.filter(client=client, is_primary=False)
            .select_related("household")
            .first()
        )
        if membership is None:
            return False
        is_dependent = (
            MemberDietaryProfile.objects.filter(
                client=client, enrollment__household=membership.household
            )
            .exclude(enrollment=enrollment)
            .exclude(status=MemberStatus.REMOVED)
            .exists()
        )
        if not is_dependent:
            return False
        try:
            result = split_dependent_into_own_enrollment(
                client, enrollment, actor=None, actor_label=_agent_actor(self.request),
            )
            return bool(result and result.get("split"))
        except Exception:  # noqa: BLE001
            logger.exception("dependent split failed for enrollment %s", enrollment.pk)
            return False

    @action(detail=False, methods=["get"])
    def choices(self, request):
        """All enum options the verification wizard needs (stages + Step-2 picks)."""
        return Response({
            "stages": _choices(EnrollmentStage),
            "dietary_restrictions": _choices(DietaryRestriction),
            "food_allergies": _choices(FoodAllergy),
            "meal_categories": _choices(MenuCategory),
            # Menu types come from the admin-managed catalog (so new variants
            # like Kosher/Halal are usable without code changes); the member's
            # menu_type stores the chosen name.
            "menu_types": [
                {"value": mt.name, "label": mt.name}
                for mt in MenuType.objects.filter(is_active=True).order_by("name")
            ],
        })

    @action(detail=True, methods=["post"], url_path="set-stage")
    def set_stage(self, request, pk=None):
        """Advance the enrollment to a new stage (this includes the Step-4
        authorization outcome). Guarded by the transition map; pass
        ``force=true`` to bypass the validation/verification process gates."""
        enrollment = self.get_object()
        to_stage = request.data.get("stage")
        if to_stage not in set(EnrollmentStage.values):
            return Response(
                {"detail": f"Invalid stage. Allowed: {sorted(EnrollmentStage.values)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            advance_enrollment(
                enrollment,
                to_stage,
                actor=getattr(request, "user", None),
                note=request.data.get("note", "") or "",
                force=bool(request.data.get("force")),
            )
            # Once verification completes, immediately project the case's
            # authorization outcome (it may already be Accepted -> orders).
            if enrollment.stage == EnrollmentStage.VERIFIED:
                reconcile_enrollment_authorization(
                    enrollment, actor=getattr(request, "user", None)
                )
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        enrollment.refresh_from_db()
        return Response(self.get_serializer(enrollment).data)


class ProviderViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Provider.objects.all()
    serializer_class = ProviderSerializer


class ProgramViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Program.objects.all()
    serializer_class = ProgramSerializer


def _parse_bool(raw):
    """Parse a query-param boolean; returns True/False or None if unrecognized."""
    val = str(raw or "").strip().lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return None


class ProgramEligibilityListView(generics.ListAPIView):
    """Program eligibilities available for a given household member.

    GET /api/program-eligibilities/?member=<household_member_id>
        -> all eligibility rows for that member (n records), newest first.

    Narrow the result with optional filters (combine to return a single row):
        &program=<program_id>          exact Program UUID
        &is_eligible=true|false        thresholded decision
        &model_version=<str>           a specific scoring model version

    ``member`` is required; an int HouseholdMember id.
    """

    serializer_class = ProgramEligibilitySerializer

    def get_queryset(self):
        member = self.request.query_params.get("member")
        if not member:
            raise ValidationError({"member": "This query parameter is required."})
        try:
            member_id = int(member)
        except (TypeError, ValueError):
            raise ValidationError({"member": "Must be a numeric household member id."})

        qs = (
            ProgramEligibility.objects.select_related(
                "program", "program__provider", "program__main_category"
            )
            .filter(member_id=member_id)
        )

        program = self.request.query_params.get("program")
        if program:
            qs = qs.filter(program_id=program)

        is_eligible = self.request.query_params.get("is_eligible")
        if is_eligible is not None:
            parsed = _parse_bool(is_eligible)
            if parsed is None:
                raise ValidationError(
                    {"is_eligible": "Must be a boolean (true/false)."}
                )
            qs = qs.filter(is_eligible=parsed)

        model_version = self.request.query_params.get("model_version")
        if model_version:
            qs = qs.filter(model_version=model_version)

        return qs
