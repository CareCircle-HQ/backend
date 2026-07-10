import uuid
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from .models import Agent, Client, Insurance, RecordStatus, TimelineEvent
from .serializers import ClientSerializer


class InsuranceReconcileTest(TestCase):
    """Authoritative coverage reconcile on the client upsert."""

    def _save(self, data):
        data = {"first_name": "Test", "last_name": "Client", **data}
        s = ClientSerializer(data=data)
        s.is_valid(raise_exception=True)
        return s.save()

    def _client_with(self, *insurances):
        cid = str(uuid.uuid4())
        client = Client.objects.create(client_id=cid)
        for ins in insurances:
            Insurance.objects.create(client=client, **ins)
        return client, cid

    def test_reconcile_deactivates_absent_policy(self):
        client, cid = self._client_with(
            dict(plan_name="A", external_member_id="1", status=RecordStatus.ACTIVE)
        )
        self._save({
            "client_id": cid,
            "insurances": [
                {"plan_name": "B", "external_member_id": "2", "status": "active"}
            ],
            "reconcile_insurances": True,
        })
        self.assertEqual(
            Insurance.objects.get(client=client, plan_name="A").status,
            RecordStatus.INACTIVE,
        )
        self.assertEqual(
            Insurance.objects.get(client=client, plan_name="B").status,
            RecordStatus.ACTIVE,
        )

    def test_reconcile_reactivates_present_policy(self):
        client, cid = self._client_with(
            dict(plan_name="A", external_member_id="1", status=RecordStatus.INACTIVE)
        )
        self._save({
            "client_id": cid,
            "insurances": [
                {"plan_name": "A", "external_member_id": "1", "status": "active"}
            ],
            "reconcile_insurances": True,
        })
        self.assertEqual(
            Insurance.objects.get(client=client, plan_name="A").status,
            RecordStatus.ACTIVE,
        )

    def test_reconcile_skips_verified_rows(self):
        client, cid = self._client_with(
            dict(
                plan_name="V",
                external_member_id="9",
                status=RecordStatus.ACTIVE,
                verified=True,
            )
        )
        self._save({
            "client_id": cid,
            "insurances": [
                {"plan_name": "B", "external_member_id": "2", "status": "active"}
            ],
            "reconcile_insurances": True,
        })
        self.assertEqual(
            Insurance.objects.get(client=client, plan_name="V").status,
            RecordStatus.ACTIVE,
        )

    def test_reconcile_empty_list_deactivates_all_but_verified(self):
        client, cid = self._client_with(
            dict(plan_name="A", external_member_id="1", status=RecordStatus.ACTIVE),
            dict(
                plan_name="V",
                external_member_id="9",
                status=RecordStatus.ACTIVE,
                verified=True,
            ),
        )
        self._save({
            "client_id": cid,
            "insurances": [],
            "reconcile_insurances": True,
        })
        self.assertEqual(
            Insurance.objects.get(client=client, plan_name="A").status,
            RecordStatus.INACTIVE,
        )
        self.assertEqual(
            Insurance.objects.get(client=client, plan_name="V").status,
            RecordStatus.ACTIVE,
        )

    def test_without_flag_absent_policy_untouched(self):
        client, cid = self._client_with(
            dict(plan_name="A", external_member_id="1", status=RecordStatus.ACTIVE)
        )
        self._save({
            "client_id": cid,
            "insurances": [
                {"plan_name": "B", "external_member_id": "2", "status": "active"}
            ],
        })
        self.assertEqual(
            Insurance.objects.get(client=client, plan_name="A").status,
            RecordStatus.ACTIVE,
        )


class ExtensionTimelineTest(TestCase):
    """Drive the real extension HTTP endpoints as an authenticated agent and
    assert that TimelineEvents are emitted."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Ada Agent", agent_code="355", group="Screeners"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.client_api = APIClient()
        self.client_api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.cid = str(uuid.uuid4())
        self.now = timezone.now().isoformat()

    def _create_client(self, consent=True):
        return self.client_api.post(
            reverse("client-list"),
            {
                "client_id": self.cid,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "consent_accepted": consent,
                "consent_status": "accepted" if consent else "declined",
                "consented_at": self.now,
            },
            format="json",
        )

    def _events(self, event_type=None):
        qs = TimelineEvent.objects.filter(client_id=self.cid)
        if event_type:
            qs = qs.filter(event_type=event_type)
        return qs

    def test_consent_event_on_client_create(self):
        resp = self._create_client(consent=True)
        self.assertEqual(resp.status_code, 201, resp.content)
        ev = self._events("consent_granted").get()
        self.assertEqual(ev.title, "Consent Granted")
        self.assertIn("Ada Lovelace", ev.subtitle)
        self.assertEqual(ev.actor, "agent:355")

    def test_no_consent_event_when_not_accepted(self):
        self._create_client(consent=False)
        self.assertFalse(self._events("consent_granted").exists())

    def test_consent_event_is_one_time(self):
        # First save (create) emits the event; subsequent saves must not re-emit.
        self._create_client(consent=True)
        first = self._events("consent_granted").get()
        url = reverse("client-detail", kwargs={"pk": self.cid})
        for _ in range(2):
            self.client_api.patch(url, {"first_name": "Ada Updated"}, format="json")
        events = self._events("consent_granted")
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().pk, first.pk)
        self.assertEqual(events.get().occurred_at, first.occurred_at)

    def test_consent_emitted_when_granted_on_later_save(self):
        # Created without consent -> no event; consent granted on a later save
        # -> the event fires that first time it becomes accepted.
        self._create_client(consent=False)
        self.assertFalse(self._events("consent_granted").exists())
        url = reverse("client-detail", kwargs={"pk": self.cid})
        self.client_api.patch(
            url, {"consent_accepted": True, "consent_status": "accepted"}, format="json"
        )
        self.assertEqual(self._events("consent_granted").count(), 1)

    def test_screening_bulk_emits_event_with_needs_badge(self):
        self._create_client()
        resp = self.client_api.post(
            reverse("screening-bulk"),
            [{
                "enhanced_screen_id": str(uuid.uuid4()),
                "subject_id": self.cid,
                "screen_created_at": self.now,
                "screen_status": "completed",
                "screen_type": "PHQ-9 Screening",
                "performing_organization_name": "Met Council",
                "identified_social_needs": [{"need": "food"}, {"need": "housing"}, {"need": "transport"}],
            }],
            format="json",
        )
        self.assertIn(resp.status_code, (200, 207), resp.content)
        ev = self._events("screening").get()
        self.assertEqual(ev.title, "PHQ-9 Screening")
        self.assertEqual(ev.badge_text, "3 unmet social needs")
        self.assertEqual(ev.badge_tone, "warning")

    def test_assessment_bulk_emits_event(self):
        self._create_client()
        resp = self.client_api.post(
            reverse("assessment-bulk"),
            [{
                "assessment_id": str(uuid.uuid4()),
                "subject_id": self.cid,
                "screen_created_at": self.now,
                "eligible_status": "Eligible",
                "performing_organization_name": "Met Council",
            }],
            format="json",
        )
        self.assertIn(resp.status_code, (200, 207), resp.content)
        ev = self._events("assessment").get()
        self.assertEqual(ev.badge_text, "Eligible")
        self.assertEqual(ev.badge_tone, "success")

    def test_case_bulk_emits_event(self):
        self._create_client()
        resp = self.client_api.post(
            reverse("case-bulk"),
            [{
                "case_id": str(uuid.uuid4()),
                "client_id": self.cid,
                "program_name": "Meals on Wheels",
                "service_type": "Food",
                "provider_name": "Met Council",
                "date_opened": self.now,
            }],
            format="json",
        )
        self.assertIn(resp.status_code, (200, 207), resp.content)
        ev = self._events("case_opened").get()
        self.assertEqual(ev.title, "Meals on Wheels")

    def test_idempotent_rebulk_does_not_duplicate(self):
        self._create_client()
        screen_id = str(uuid.uuid4())
        payload = [{
            "enhanced_screen_id": screen_id,
            "subject_id": self.cid,
            "screen_created_at": self.now,
            "screen_status": "completed",
            "screen_type": "PHQ-9",
        }]
        url = reverse("screening-bulk")
        self.client_api.post(url, payload, format="json")
        self.client_api.post(url, payload, format="json")
        self.assertEqual(self._events("screening").count(), 1)

    def test_timeline_endpoint_returns_events(self):
        self._create_client()
        self.client_api.post(
            reverse("case-bulk"),
            [{
                "case_id": str(uuid.uuid4()),
                "client_id": self.cid,
                "program_name": "PCA",
                "date_opened": self.now,
            }],
            format="json",
        )
        resp = self.client_api.get(reverse("client-timeline", kwargs={"pk": self.cid}))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["client_id"], self.cid)
        types = {e["event_type"] for e in body["results"]}
        self.assertSetEqual(types, {"consent_granted", "case_opened"})
        occurred = [e["occurred_at"] for e in body["results"]]
        self.assertEqual(occurred, sorted(occurred, reverse=True))


class HouseholdEnrollmentActivationTest(TestCase):
    """When a household enrollment advances, every participant — not just the
    primary — should follow the enrollment's lifecycle stage. The household is
    the unit of verification; there is no per-member exclusion."""

    def _client(self, first="A", last="B"):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )

    def _household_enrollment(self, clients, *, stage=None, profiles_for=None):
        """Build a household + EnrollmentVerification with MemberDietaryProfile
        rows. ``clients`` is a list of clients; the first is the primary.
        ``profiles_for`` (defaults to all) limits which clients get a profile
        row, so the household-membership governance path can be exercised.
        Returns the enrollment.
        """
        from .models import (
            EnrollmentStage,
            EnrollmentVerification,
            Household,
            HouseholdMember,
            MemberDietaryProfile,
        )

        household = Household.objects.create(name="Test Household")
        for i, client in enumerate(clients):
            HouseholdMember.objects.create(
                household=household, client=client, is_primary=(i == 0)
            )
        enrollment = EnrollmentVerification.objects.create(
            client=clients[0],
            household=household,
            stage=stage or EnrollmentStage.PENDING_VERIFICATION,
        )
        for client in (clients if profiles_for is None else profiles_for):
            MemberDietaryProfile.objects.create(enrollment=enrollment, client=client)
        return enrollment

    def test_all_household_members_go_active(self):
        from .models import ClientStage, EnrollmentStage
        from .services.lifecycle import advance_enrollment

        primary = self._client("Pat", "Primary")
        spouse = self._client("Sam", "Spouse")
        child = self._client("Kid", "Child")
        enrollment = self._household_enrollment([primary, spouse, child])

        advance_enrollment(enrollment, EnrollmentStage.VERIFIED, force=True)
        advance_enrollment(enrollment, EnrollmentStage.SERVICE_ACTIVE, force=True)

        for c in (primary, spouse, child):
            c.refresh_from_db()
            self.assertEqual(
                c.lifecycle_stage, ClientStage.ACTIVE,
                f"{c.first_name} should be Active, got {c.lifecycle_stage}",
            )

    def test_member_without_profile_still_goes_active(self):
        # A household member governed purely via membership (no per-member
        # dietary profile row) still follows the enrollment's stage.
        from .models import ClientStage, EnrollmentStage
        from .services.lifecycle import advance_enrollment

        primary = self._client("Pat", "Primary")
        spouse = self._client("Sam", "Spouse")
        enrollment = self._household_enrollment(
            [primary, spouse], profiles_for=[primary]
        )

        advance_enrollment(enrollment, EnrollmentStage.VERIFIED, force=True)
        advance_enrollment(enrollment, EnrollmentStage.SERVICE_ACTIVE, force=True)

        for c in (primary, spouse):
            c.refresh_from_db()
            self.assertEqual(c.lifecycle_stage, ClientStage.ACTIVE)


class SoleInternalServiceDenialTest(TestCase):
    """A client whose ONLY internal-service (meal/box) case is denied is a full
    stop: the enrollment is paused (On Hold) and a follow-up ticket is raised.
    Two-plus internal-service cases are never a full stop. A later re-approval
    resumes the auto-paused enrollment."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Casey", last_name="Case"
        )

    def _enrollment(self, client, stage):
        from .models import EnrollmentStage, EnrollmentVerification, Household, HouseholdMember

        household = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=household, client=client, is_primary=True)
        return EnrollmentVerification.objects.create(
            client=client, household=household, stage=stage,
            verified_at=timezone.now(),
        )

    def _save_case(self, client, case_id, auth_status):
        from .serializers import CaseSerializer

        data = {
            "case_id": case_id,
            "client_id": str(client.client_id),
            "case_type": "internal_service",
            "program_name": "Medically Tailored Meals",
            "service_authorization_status": auth_status,
            "date_opened": timezone.now().isoformat(),
        }
        ser = CaseSerializer(data=data)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_sole_denied_case_pauses_and_tickets(self):
        from .models import EnrollmentStage, Ticket
        from .portal.serializers import verification_status

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        case_id = str(uuid.uuid4())
        # First save as pending -> no pause.
        self._save_case(client, case_id, "pending")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)

        # Deny the sole internal-service case -> full stop (On Hold) + ticket.
        self._save_case(client, case_id, "denied")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        self.assertEqual(verification_status(client), "On Hold")
        self.assertEqual(
            Ticket.objects.filter(client=client, case_id=case_id).count(), 1
        )

    def test_two_internal_cases_denied_does_not_pause(self):
        from .models import EnrollmentStage

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        self._save_case(client, str(uuid.uuid4()), "approved")
        self._save_case(client, str(uuid.uuid4()), "denied")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)

    def test_reapproval_resumes_paused_enrollment(self):
        from .models import EnrollmentStage

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        case_id = str(uuid.uuid4())
        self._save_case(client, case_id, "denied")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)

        # Re-approve the same sole case -> auto-resume to the held-from stage.
        self._save_case(client, case_id, "approved")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)


class LogisticsRosterFilterTest(TestCase):
    """The Logistics (kitchen-assignment) roster hides members who can't be
    assigned a kitchen: out-of-orbit members and members whose internal-service
    case(s) are ALL closed/cancelled. A household with no remaining displayable
    members drops out entirely. The Members page (no scope) still shows them."""

    def _client(self, first, last, stage=None):
        from .models import ClientStage

        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last,
            lifecycle_stage=stage or ClientStage.KITCHEN_ASSIGNMENT,
        )

    def _internal_case(self, client, status=None):
        from .models import Case, CaseStatus, CaseType

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=status or CaseStatus.OPEN,
            program_name="Medically Tailored Meals",
        )

    def _household(self, primary, *dependents):
        from .models import Household, HouseholdMember

        hh = Household.objects.create(name=f"{primary.last_name} Household")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        for dep in dependents:
            HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        return hh

    def _enrollment(self, primary, household, member_statuses):
        """member_statuses: {client: MemberStatus} -> one profile per entry."""
        from .models import (
            EnrollmentStage,
            EnrollmentVerification,
            MemberDietaryProfile,
        )

        enr = EnrollmentVerification.objects.create(
            client=primary, household=household,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        for client, status in member_statuses.items():
            MemberDietaryProfile.objects.create(
                enrollment=enr, client=client, status=status
            )
        return enr

    def _groups(self, **params):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from .portal.views_members import MembersListView

        view = MembersListView()
        view.request = Request(APIRequestFactory().get("/portal/members/", params))
        view.kwargs = {}
        return view._build_groups_for_page(view._group_entries())

    def test_out_of_orbit_member_hidden_from_household(self):
        from .models import ClientStage, MemberStatus

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent", stage=ClientStage.ACTIVE)
        hh = self._household(primary, dep)
        self._internal_case(primary)
        self._enrollment(primary, hh, {
            primary: MemberStatus.ACTIVE,
            dep: MemberStatus.OUT_OF_ORBIT,
        })

        groups = self._groups(scope="logistics")
        self.assertEqual(len(groups), 1)
        ids = {m["id"] for m in groups[0]["members"]}
        self.assertIn(str(primary.client_id), ids)
        self.assertNotIn(str(dep.client_id), ids)
        self.assertEqual(groups[0]["member_count"], 1)

    def test_closed_internal_case_member_hidden(self):
        from .models import CaseStatus, ClientStage, MemberStatus

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent", stage=ClientStage.ACTIVE)
        hh = self._household(primary, dep)
        self._internal_case(primary)  # open -> keeps the household on the page
        self._internal_case(dep, CaseStatus.CLOSED)  # dep's own case is finished
        self._enrollment(primary, hh, {
            primary: MemberStatus.ACTIVE,
            dep: MemberStatus.ACTIVE,
        })

        groups = self._groups(scope="logistics")
        self.assertEqual(len(groups), 1)
        ids = {m["id"] for m in groups[0]["members"]}
        self.assertNotIn(str(dep.client_id), ids)

    def test_household_dropped_when_all_members_hidden(self):
        from .models import MemberStatus

        primary = self._client("Sol", "Solo")
        hh = self._household(primary)
        self._internal_case(primary)
        self._enrollment(primary, hh, {primary: MemberStatus.OUT_OF_ORBIT})

        self.assertEqual(self._groups(scope="logistics"), [])

    def test_members_page_still_shows_out_of_orbit(self):
        from .models import ClientStage, MemberStatus

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent", stage=ClientStage.ACTIVE)
        hh = self._household(primary, dep)
        self._internal_case(primary)
        self._enrollment(primary, hh, {
            primary: MemberStatus.ACTIVE,
            dep: MemberStatus.OUT_OF_ORBIT,
        })

        groups = self._groups()  # no scope == Members page
        hh_group = next(g for g in groups if g["type"] == "household")
        ids = {m["id"] for m in hh_group["members"]}
        self.assertIn(str(dep.client_id), ids)


class DeliveryCoverageEligibilityTest(TestCase):
    """Delivery Coverage Eligibility Check: a member whose delivery-address ZIP
    is in the excluded list is Out of Orbit (reason "Delivery Address Outside
    Coverage Area"), durably through the meal-rule reconcile, with a system note
    + timeline event; and is returned to Active when the ZIP becomes serviceable
    (only if the meal rule also passes)."""

    def setUp(self):
        from .models import ExcludedZipCode

        ExcludedZipCode.objects.get_or_create(zip="11209")

    def _profile(self, zip_code, *, primary_zip=None, status=None, menu_type="Standard"):
        from .models import (
            Address, AddressType, Client, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, MemberDietaryProfile, MemberStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Cov", last_name="Erage",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        addr = Address.objects.create(client=client, type="temporary", zip=zip_code)
        if primary_zip is not None:
            Address.objects.create(
                client=client, type=AddressType.CURRENT, zip=primary_zip
            )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, delivery_address=addr,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        return MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type=menu_type,
            status=status or MemberStatus.ACTIVE,
        )

    def test_reconcile_out_of_range_for_excluded_zip(self):
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output
        from .services.service_area import SERVICE_AREA_REASON

        mv = self._profile("11209")  # Standard menu is otherwise fulfillable
        out, _became, reason = reconcile_member_kitchen_output(mv, None, save=True)
        self.assertTrue(out)
        self.assertEqual(reason, SERVICE_AREA_REASON)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.OUT_OF_RANGE)

    def test_reconcile_active_for_serviceable_zip(self):
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        mv = self._profile("10001")
        out, _became, _reason = reconcile_member_kitchen_output(mv, None, save=True)
        self.assertFalse(out)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.ACTIVE)

    def test_out_of_orbit_when_primary_address_excluded(self):
        # Delivery ZIP serviceable, but the PRIMARY (Current) ZIP is excluded.
        from .models import MemberStatus, Note, NoteSource
        from .portal.views_members import _enforce_delivery_coverage
        from .services.meal_rules import reconcile_member_kitchen_output
        from .services.service_area import SERVICE_AREA_REASON

        mv = self._profile("10001", primary_zip="11209")
        out, _became, reason = reconcile_member_kitchen_output(mv, None, save=True)
        self.assertTrue(out)
        self.assertEqual(reason, SERVICE_AREA_REASON)

        mv2 = self._profile("10001", primary_zip="11209")
        _enforce_delivery_coverage(mv2.enrollment, None)
        mv2.refresh_from_db()
        self.assertEqual(mv2.status, MemberStatus.OUT_OF_RANGE)
        note = Note.objects.filter(client=mv2.client, source=NoteSource.SYSTEM).first()
        self.assertIsNotNone(note)
        self.assertIn("primary address", note.body)
        self.assertIn("11209", note.body)

    def test_enforce_sets_out_of_range_with_note_ticket_hold_and_event(self):
        from .models import (
            EnrollmentStage, MemberStatus, Note, NoteSource, Ticket,
            TicketStatus, TicketTypeCode, TimelineEvent,
        )
        from .portal.views_members import _enforce_delivery_coverage

        mv = self._profile("11209")
        result = _enforce_delivery_coverage(mv.enrollment, None)
        self.assertEqual(len(result["out_of_range"]), 1)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.OUT_OF_RANGE)
        note = Note.objects.filter(client=mv.client, source=NoteSource.SYSTEM).first()
        self.assertIsNotNone(note)
        self.assertIn("11209", note.body)
        self.assertTrue(
            TimelineEvent.objects.filter(
                client=mv.client, event_type="out_of_range"
            ).exists()
        )
        # A Case Closure ticket was opened on the household primary...
        ticket = Ticket.objects.filter(
            client=mv.client, type__code=TicketTypeCode.CASE_CLOSURE,
        ).exclude(status=TicketStatus.RESOLVED).first()
        self.assertIsNotNone(ticket)
        self.assertIn("11209", ticket.reason)
        # ...and the whole household was placed On Hold.
        mv.enrollment.refresh_from_db()
        self.assertEqual(
            EnrollmentStage(mv.enrollment.stage), EnrollmentStage.ON_HOLD
        )

    def test_reactivate_resolves_ticket_and_resumes_hold(self):
        from .models import (
            EnrollmentStage, MemberStatus, Ticket, TicketStatus, TicketTypeCode,
        )
        from .portal.views_members import _enforce_delivery_coverage

        # Excluded ZIP -> Out of Range + ticket + hold.
        mv = self._profile("11209")
        _enforce_delivery_coverage(mv.enrollment, None)
        # Fix the ZIP to a serviceable one and re-run with reactivation.
        addr = mv.enrollment.delivery_address
        addr.zip = "10001"
        addr.save(update_fields=["zip"])
        _enforce_delivery_coverage(mv.enrollment, None, allow_reactivate=True)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.ACTIVE)
        # Ticket resolved + hold resumed.
        open_tickets = Ticket.objects.filter(
            client=mv.client, type__code=TicketTypeCode.CASE_CLOSURE,
        ).exclude(status=TicketStatus.RESOLVED)
        self.assertFalse(open_tickets.exists())
        mv.enrollment.refresh_from_db()
        self.assertNotEqual(
            EnrollmentStage(mv.enrollment.stage), EnrollmentStage.ON_HOLD
        )

    def test_out_of_range_flags_every_non_terminal_household_member(self):
        # An out-of-range DELIVERY ZIP is a household-wide geographic block: every
        # non-terminal member (Active, manually Paused, Out of Orbit) is set Out of
        # Range so each is individually excluded from POs/deliveries and countable.
        # Only terminal INACTIVE members are left alone.
        from .models import (
            Client, HouseholdMember, MemberDietaryProfile, MemberStatus,
        )
        from .portal.views_members import _enforce_delivery_coverage

        primary = self._profile("11209")  # excluded delivery ZIP, Active
        enr, hh = primary.enrollment, primary.enrollment.household

        def _add_member(status):
            c = Client.objects.create(
                client_id=str(uuid.uuid4()), first_name="Dep", last_name="Endent",
            )
            HouseholdMember.objects.create(household=hh, client=c, is_primary=False)
            return MemberDietaryProfile.objects.create(
                enrollment=enr, client=c, menu_type="Standard", status=status,
            )

        paused = _add_member(MemberStatus.PAUSED)
        orbit = _add_member(MemberStatus.OUT_OF_ORBIT)
        inactive = _add_member(MemberStatus.INACTIVE)

        _enforce_delivery_coverage(enr, None)

        for mv in (primary, paused, orbit):
            mv.refresh_from_db()
            self.assertEqual(mv.status, MemberStatus.OUT_OF_RANGE)
        inactive.refresh_from_db()
        self.assertEqual(inactive.status, MemberStatus.INACTIVE)  # terminal, untouched

    def test_reactivate_when_zip_becomes_serviceable(self):
        from .models import MemberStatus
        from .portal.views_members import _enforce_delivery_coverage

        # Out of Range at a now-serviceable ZIP + fulfillable menu -> Active.
        mv = self._profile("10001", status=MemberStatus.OUT_OF_RANGE)
        _enforce_delivery_coverage(mv.enrollment, None, allow_reactivate=True)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.ACTIVE)


class KitchenAwareMealRuleTest(TestCase):
    """A capable kitchen makes an otherwise-"strict" menu (Kosher/Halal/
    Vegetarian) serviceable: the kitchen-agnostic fallback would send the member
    Out of Orbit, but when the assigned kitchen offers the menu and only
    restricts allergens the member doesn't have, the member stays Active with the
    menu type + per-allergen "X Free" notes (mirrors the Williamsburg seed)."""

    def _kosher_kitchen(self):
        from .models import (
            DietaryTag, DietaryTagType, Kitchen, KitchenMenuType, KitchenStatus,
            MenuType,
        )

        other = DietaryTag.objects.create(name="Other", type=DietaryTagType.ALLERGY)
        kosher = MenuType.objects.create(name="Kosher")
        kitchen = Kitchen.objects.create(name="Williamsburg", status=KitchenStatus.ACTIVE)
        kmt = KitchenMenuType.objects.create(kitchen=kitchen, menu_type=kosher)
        # The kitchen's ONLY restriction is the catch-all "Other" allergy.
        kmt.restrictions.set([other])
        return kitchen

    def _profile(self, allergies, *, status=None):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, MemberDietaryProfile, MemberStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ko", last_name="Sher",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        return MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Kosher",
            food_allergies=allergies, status=status or MemberStatus.OUT_OF_ORBIT,
        )

    def test_capable_kitchen_makes_kosher_with_allergies_serviceable(self):
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        kitchen = self._kosher_kitchen()
        mv = self._profile(["shellfish", "pork"])
        out, _became, _reason = reconcile_member_kitchen_output(mv, kitchen, save=True)
        self.assertFalse(out)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.ACTIVE)
        self.assertEqual(mv.kitchen_meal_type, "Kosher")
        self.assertEqual(mv.kitchen_food_notes, "Pork Free, Shellfish Free")

    def test_no_kitchen_assigned_but_capable_kitchen_exists_is_serviceable(self):
        # The household hasn't been assigned a kitchen yet, but at least one
        # ACTIVE kitchen can serve the Kosher + Pork/Shellfish combo -> the
        # member stays serviceable (kitchen chosen later at assignment).
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        self._kosher_kitchen()  # a capable kitchen exists in the system
        mv = self._profile(["shellfish", "pork"])
        out, _became, _reason = reconcile_member_kitchen_output(mv, None, save=True)
        self.assertFalse(out)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.ACTIVE)
        self.assertEqual(mv.kitchen_meal_type, "Kosher")
        self.assertEqual(mv.kitchen_food_notes, "Pork Free, Shellfish Free")

    def test_no_kitchen_anywhere_falls_back_to_out_of_orbit(self):
        # No kitchen assigned AND no kitchen in the system can serve the combo.
        from .services.meal_rules import (
            MENU_ALLERGY_REASON, reconcile_member_kitchen_output,
        )

        mv = self._profile(["shellfish", "pork"])
        out, _became, reason = reconcile_member_kitchen_output(mv, None, save=True)
        self.assertTrue(out)
        self.assertEqual(reason, MENU_ALLERGY_REASON)

    def test_other_allergy_out_of_orbit_even_with_capable_kitchen(self):
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        kitchen = self._kosher_kitchen()
        mv = self._profile(["pork", "other"])
        out, _became, _reason = reconcile_member_kitchen_output(mv, kitchen, save=True)
        self.assertTrue(out)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.OUT_OF_ORBIT)


class ExcludedZipSettingsTest(TestCase):
    """Settings CRUD for the excluded-ZIP list."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Zed Agent", agent_code="900", group="Management"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_add_reject_duplicate_and_delete(self):
        from .models import ExcludedZipCode

        url = reverse("portal-excluded-zip-codes")
        # Invalid ZIP.
        self.assertEqual(self.api.post(url, {"zip": "abc"}, format="json").status_code, 400)
        # Add valid.
        resp = self.api.post(url, {"zip": "11250", "label": "Test"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        zid = resp.json()["id"]
        # Duplicate rejected.
        self.assertEqual(self.api.post(url, {"zip": "11250"}, format="json").status_code, 409)
        # Listed.
        self.assertTrue(any(z["zip"] == "11250" for z in self.api.get(url).json()["results"]))
        # Delete.
        det = reverse("portal-excluded-zip-code-detail", kwargs={"zip_id": zid})
        self.assertEqual(self.api.delete(det).status_code, 204)
        self.assertFalse(ExcludedZipCode.objects.filter(zip="11250").exists())


class ProductKindResolverTest(SimpleTestCase):
    """product_kind_for_enrollment resolves Meals/Boxes across name sources so a
    keyword-less Program row name no longer yields an unresolved '—' kind and a
    mixed meals+boxes cadence list (the Kitchen Assignment popup bug)."""

    def _enr(self, *, program_name="", case=None, schedules=None):
        # A stub delivery_schedules manager (branch 3) that returns nothing.
        sched_mgr = SimpleNamespace(
            filter=lambda **kw: SimpleNamespace(first=lambda: schedules)
        )
        return SimpleNamespace(
            program_name=program_name, case=case, delivery_schedules=sched_mgr,
        )

    def test_enrollment_snapshot_name_used_when_program_row_lacks_keyword(self):
        from .models import ProductTypeKind
        from .services.catalog import product_kind_for_enrollment

        # Linked Program row name has no meal/box keyword, but the enrollment's
        # snapshot name does -> must resolve rather than return None.
        program = SimpleNamespace(name="Enhanced Care Management", product_type_id=None)
        case = SimpleNamespace(program_id=uuid.uuid4(), program=program,
                               program_name="", service_type="")
        enr = self._enr(
            program_name="Medically Tailored Meals (MTM)", case=case,
        )
        self.assertEqual(product_kind_for_enrollment(enr), ProductTypeKind.MEALS)

    def test_case_program_name_used_as_fallback(self):
        from .models import ProductTypeKind
        from .services.catalog import product_kind_for_enrollment

        case = SimpleNamespace(
            program_id=uuid.uuid4(),
            program=SimpleNamespace(name="Navigation", product_type_id=None),
            program_name="MTNA Food Prescription Boxes",
            service_type="Food Assistance",
        )
        enr = self._enr(program_name="", case=case)
        self.assertEqual(product_kind_for_enrollment(enr), ProductTypeKind.BOXES)

    def test_unresolvable_returns_none(self):
        from .services.catalog import product_kind_for_enrollment

        case = SimpleNamespace(program_id=None, program=None,
                               program_name="Housing", service_type="Housing")
        enr = self._enr(program_name="", case=case, schedules=None)
        self.assertIsNone(product_kind_for_enrollment(enr))


class VerificationDisregardTest(TestCase):
    """POST /api/portal/members/<id>/verification/disregard/ moves a pending
    request to the non-terminal DISREGARDED stage, reverting the member out of
    the verification window while KEEPING the row (profiles, address, case) as
    history."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Vera Verifier", agent_code="900", group="Verifiers"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _client(self, first="Pat", last="Primary"):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )

    def _pending_enrollment(self, client, *, case=None, with_profile=True):
        from .models import (
            Address,
            EnrollmentStage,
            EnrollmentVerification,
            MemberDietaryProfile,
        )

        addr = Address.objects.create(client=client, type="delivery", street="1 Main St")
        enr = EnrollmentVerification.objects.create(
            client=client,
            case=case,
            stage=EnrollmentStage.PENDING_VERIFICATION,
            delivery_address=addr,
        )
        if with_profile:
            MemberDietaryProfile.objects.create(enrollment=enr, client=client)
        # Put the client into the verification window.
        from .services.lifecycle import recompute_client_stage
        recompute_client_stage(client)
        return enr, addr

    def _url(self, client):
        return f"/api/portal/members/{client.client_id}/verification/disregard/"

    def test_disregard_reverts_stage_and_keeps_data(self):
        from .models import (
            ClientStage,
            EnrollmentStage,
            MemberDietaryProfile,
            TimelineEvent,
            TimelineEventType,
        )

        client = self._client()
        enr, addr = self._pending_enrollment(client)
        client.refresh_from_db()
        self.assertEqual(client.lifecycle_stage, ClientStage.PENDING_VERIFICATION)

        resp = self.api.post(self._url(client), {"reason": "Requested in error"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

        enr.refresh_from_db()
        client.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.DISREGARDED)
        # Row + its data are kept.
        self.assertIsNone(enr.closed_at)
        self.assertIsNotNone(enr.delivery_address_id)
        self.assertTrue(MemberDietaryProfile.objects.filter(enrollment=enr).exists())
        self.assertIn("Requested in error", enr.note)
        # Client reverts OUT of the verification window.
        self.assertNotEqual(client.lifecycle_stage, ClientStage.PENDING_VERIFICATION)
        # Timeline event carries the reason.
        ev = TimelineEvent.objects.filter(
            client=client, event_type=TimelineEventType.VERIFICATION_DISREGARDED
        ).first()
        self.assertIsNotNone(ev)
        self.assertIn("Requested in error", ev.subtitle)

    def test_reason_required(self):
        client = self._client()
        self._pending_enrollment(client)
        resp = self.api.post(self._url(client), {"reason": "   "}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_disregard_stage_only_pending_client_without_enrollment(self):
        # Many members are Pending Verification purely via lifecycle_stage (set by
        # the data import) with NO EnrollmentVerification row. Disregard must still
        # work: revert the client's stage out of the verification window.
        from .models import ClientStage, TimelineEvent, TimelineEventType

        client = self._client()
        client.lifecycle_stage = ClientStage.PENDING_VERIFICATION
        client.save(update_fields=["lifecycle_stage"])

        resp = self.api.post(self._url(client), {"reason": "Imported in error"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

        client.refresh_from_db()
        self.assertNotEqual(client.lifecycle_stage, ClientStage.PENDING_VERIFICATION)
        ev = TimelineEvent.objects.filter(
            client=client, event_type=TimelineEventType.VERIFICATION_DISREGARDED
        ).first()
        self.assertIsNotNone(ev)
        self.assertIn("Imported in error", ev.subtitle)

    def test_button_visibility_follows_pending_request(self):
        # The Verification button shows ONLY while a pending request exists. After
        # a disregard there is none, so it hides; a fresh request shows it again.
        from .models import EnrollmentStage, EnrollmentVerification
        from .portal.serializers import verification_pending

        client = self._client()
        self._pending_enrollment(client)
        self.assertTrue(verification_pending(client))

        resp = self.api.post(self._url(client), {"reason": "dupe"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        client.refresh_from_db()
        self.assertIsNotNone(client.verification_disregarded_at)  # audit stamp
        self.assertFalse(verification_pending(client))  # button hides

        # A fresh request from the ext shows the button again.
        EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.PENDING_VERIFICATION
        )
        self.assertTrue(verification_pending(client))

    def test_rejects_when_no_pending_request(self):
        from .models import EnrollmentStage
        from .services.lifecycle import advance_enrollment

        client = self._client()
        enr, _ = self._pending_enrollment(client)
        advance_enrollment(enr, EnrollmentStage.VERIFIED, force=True)
        resp = self.api.post(self._url(client), {"reason": "x"}, format="json")
        self.assertEqual(resp.status_code, 409, resp.content)

    def test_same_case_can_be_re_requested_after_disregard(self):
        # Keeping the case on the disregarded row must NOT block a fresh live
        # enrollment for the same case (constraint excludes disregarded rows).
        from .models import CaseType, Case, EnrollmentStage, EnrollmentVerification

        client = self._client()
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client, case_type=CaseType.INTERNAL_SERVICE,
        )
        enr, _ = self._pending_enrollment(client, case=case)
        resp = self.api.post(self._url(client), {"reason": "dupe"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.DISREGARDED)
        self.assertEqual(enr.case_id, case.case_id)  # case link preserved

        # A new LIVE enrollment on the same case is allowed.
        fresh = EnrollmentVerification.objects.create(
            client=client, case=case, stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        self.assertEqual(
            EnrollmentVerification.objects.filter(case=case).count(), 2
        )
        self.assertNotEqual(fresh.pk, enr.pk)


class DashboardServingClientIdsTests(TestCase):
    """serving_client_ids() is the single source of truth for both the dashboard
    counts and the drill-down list. These cover the reasons whose membership rules
    are non-obvious: Services Paused (member Paused OR household On Hold) and the
    insurance/social-coverage watchlist. Uses all-time (start=None), a live
    snapshot over every member profile."""

    def _member(self, *, status=None, stage=None):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, MemberDietaryProfile, MemberStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dash", last_name="Board",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh,
            stage=stage or EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
            status=status or MemberStatus.ACTIVE,
        )
        return client

    def test_services_paused_covers_member_paused_and_household_on_hold(self):
        from .models import EnrollmentStage, MemberStatus
        from .portal.views_dashboard import serving_client_ids

        paused = self._member(status=MemberStatus.PAUSED)
        on_hold = self._member(stage=EnrollmentStage.ON_HOLD)  # status Active
        active = self._member()

        ids = serving_client_ids("services_paused", start=None, end=None)
        self.assertIn(paused.client_id, ids)
        self.assertIn(on_hold.client_id, ids)
        self.assertNotIn(active.client_id, ids)

    def test_no_insurance_clears_when_active_plan_added(self):
        from .models import Insurance, InsurancePlanType, RecordStatus
        from .portal.views_dashboard import serving_client_ids

        c = self._member()
        self.assertIn(
            c.client_id, serving_client_ids("no_insurance", start=None, end=None)
        )
        Insurance.objects.create(
            client=c, plan_type=InsurancePlanType.MEDICAID,
            status=RecordStatus.ACTIVE,
        )
        self.assertNotIn(
            c.client_id, serving_client_ids("no_insurance", start=None, end=None)
        )

    def test_no_social_coverage_clears_when_enrolled_coverage_added(self):
        from .models import SocialCareCoverage, SocialCareCoverageStatus
        from .portal.views_dashboard import serving_client_ids

        c = self._member()
        self.assertIn(
            c.client_id,
            serving_client_ids("no_social_coverage", start=None, end=None),
        )
        SocialCareCoverage.objects.create(
            client=c, status=SocialCareCoverageStatus.ENROLLED,
        )
        self.assertNotIn(
            c.client_id,
            serving_client_ids("no_social_coverage", start=None, end=None),
        )

    def test_insurance_expiring_only_flags_active_medicaid_within_30_days(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import Insurance, InsurancePlanType, RecordStatus
        from .portal.views_dashboard import serving_client_ids

        soon = self._member()
        Insurance.objects.create(
            client=soon, plan_type=InsurancePlanType.MEDICAID,
            status=RecordStatus.ACTIVE,
            expired_at=timezone.now() + timedelta(days=10),
        )
        far = self._member()
        Insurance.objects.create(
            client=far, plan_type=InsurancePlanType.MEDICAID,
            status=RecordStatus.ACTIVE,
            expired_at=timezone.now() + timedelta(days=200),
        )
        commercial = self._member()
        Insurance.objects.create(
            client=commercial, plan_type=InsurancePlanType.COMMERCIAL,
            status=RecordStatus.ACTIVE,
            expired_at=timezone.now() + timedelta(days=10),
        )

        ids = serving_client_ids("insurance_expiring", start=None, end=None)
        self.assertIn(soon.client_id, ids)
        self.assertNotIn(far.client_id, ids)         # expires too far out
        self.assertNotIn(commercial.client_id, ids)  # not Medicaid


class ProgramSwitchClassificationTest(SimpleTestCase):
    """The program_switched blocker must fire only on a REAL meals<->boxes
    switch (the old kind is retired), not when a parallel different-kind case is
    also open + approved -- that would wrongly (and destructively) flip a
    correctly-served member. Pure-function coverage of the guard."""

    def _classify(self, **overrides):
        from .models import ProductTypeKind
        from .services.po_blockers import _classify_reason

        base = dict(
            kitchen_id="k", future=4, has_future_auth=True, plan_ends_on=None,
            kind=ProductTypeKind.MEALS, plan_kind=ProductTypeKind.MEALS,
            governing_kind=ProductTypeKind.BOXES, plan_kind_authorized=False,
            weekday_mismatch=False, switch_pending=False, open_case_count=2,
            enrollment_case_id="c1", governing_case_id="c1",
            today=timezone.localdate(),
        )
        base.update(overrides)
        return _classify_reason(**base)

    def test_real_switch_when_plan_kind_no_longer_authorized(self):
        # Governing case is boxes, plan is meals, and NO open approved meals
        # case remains -> the meals program is retired: a genuine switch.
        self.assertEqual(self._classify(plan_kind_authorized=False), "program_switched")

    def test_parallel_open_case_is_not_a_switch(self):
        # The meals plan is still backed by an open approved meals case; the
        # boxes case is a parallel duplicate -> duplicate_open_cases, not a
        # switch (so the fix never destructively flips the member).
        self.assertEqual(
            self._classify(plan_kind_authorized=True, open_case_count=2),
            "duplicate_open_cases",
        )

    def test_same_kind_is_never_a_switch(self):
        from .models import ProductTypeKind

        self.assertNotEqual(
            self._classify(governing_kind=ProductTypeKind.MEALS,
                           plan_kind_authorized=True, open_case_count=1),
            "program_switched",
        )


class RecomputeSwitchesPlanKindTest(TestCase):
    """Regression: applying the PO Blockers 'program_switched' fix must actually
    flip the plan's KIND snapshot. update_household_cadence previously updated
    prod_per_delivery but never meals_per_day; since plan_built_kind reads
    meals_per_day first, a meals->boxes fix left the plan reading as meals, so
    the blocker never cleared (the row remained after every fix)."""

    def test_meals_to_boxes_flips_meals_per_day(self):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, DeliveryCadence,
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDeliverySchedule, MemberDietaryProfile, ProductType,
            ProductTypeKind, ScheduleStatus, ServiceAuthorizationStatus,
        )
        from .services.delivery import update_household_cadence
        from .services.orders import plan_built_kind

        # Boxes product the switch should land on.
        boxes_pt = ProductType.objects.create(
            type=ProductTypeKind.BOXES, prod_per_delivery=1, meals_per_day=0,
            delivery_days_cadence=DeliveryCadence.ONCE_A_WEEK,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Sw", last_name="Itch",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        now = timezone.now()
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=60),
            date_opened=now,
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
        )
        # A live MEALS plan (meals_per_day set, prod_per_delivery 0).
        sched = MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=member, member_name="Sw Itch",
            delivery_days_cadence=DeliveryCadence.ONCE_A_WEEK,
            meals_per_day=3, prod_per_delivery=0, meals_boxes_total=12,
            status=ScheduleStatus.SCHEDULED,
        )
        self.assertEqual(plan_built_kind(sched), ProductTypeKind.MEALS)

        # Apply the switch to BOXES (what the fix does).
        update_household_cadence(
            enr, cadence=DeliveryCadence.ONCE_A_WEEK, case=case,
            product_kind=ProductTypeKind.BOXES,
        )

        sched.refresh_from_db()
        self.assertEqual(sched.meals_per_day, 0)          # was 3 -> flipped
        self.assertEqual(sched.prod_per_delivery, 1)      # boxes qty
        self.assertEqual(sched.product_type_id, boxes_pt.pk)
        self.assertEqual(plan_built_kind(sched), ProductTypeKind.BOXES)


class PurchaseOrderDedupeByClientTest(SimpleTestCase):
    """The PO guardrail collapses two occurrences of the SAME client on a date to
    one line, preferring the case-linked (legit) enrollment. Guards against the
    duplicate-enrollment anomaly doubling a member in a Purchase Order."""

    @staticmethod
    def _sched(order_id, client_id, case_id):
        return SimpleNamespace(
            order_id=order_id,
            member=SimpleNamespace(client_id=client_id),
            enrollment=SimpleNamespace(case_id=case_id),
        )

    def test_same_client_collapses_prefers_case_linked(self):
        from api.services.purchase_orders import _dedupe_by_client

        caseless = self._sched("o-a", "client-1", None)
        legit = self._sched("o-b", "client-1", "case-1")
        out = _dedupe_by_client([caseless, legit])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].order_id, "o-b")  # kept the case-linked one

    def test_distinct_clients_all_kept(self):
        from api.services.purchase_orders import _dedupe_by_client

        a = self._sched("o-a", "client-1", "case-1")
        b = self._sched("o-b", "client-2", "case-2")
        out = _dedupe_by_client([a, b])
        self.assertEqual({s.order_id for s in out}, {"o-a", "o-b"})

    def test_unassigned_member_rows_are_kept(self):
        from api.services.purchase_orders import _dedupe_by_client

        row = SimpleNamespace(
            order_id="o-a", member=None, enrollment=SimpleNamespace(case_id=None),
        )
        out = _dedupe_by_client([row])
        self.assertEqual(len(out), 1)


class ExclusionStateOverlayTest(SimpleTestCase):
    """The delivery-calendar overlay maps the live household stage / member
    status to the reason a scheduled future date won't be delivered."""

    def test_on_hold_household_takes_precedence(self):
        from api.models import EnrollmentStage, MemberStatus
        from api.portal.views_delivery_calendar import _exclusion_state

        enr = SimpleNamespace(stage=EnrollmentStage.ON_HOLD)
        member = SimpleNamespace(status=MemberStatus.PAUSED)
        self.assertEqual(_exclusion_state(enr, member), ("on_hold", "On Hold"))

    def test_member_statuses(self):
        from api.models import EnrollmentStage, MemberStatus
        from api.portal.views_delivery_calendar import _exclusion_state

        enr = SimpleNamespace(stage=EnrollmentStage.SERVICE_ACTIVE)
        cases = {
            MemberStatus.PAUSED: ("paused", "Paused"),
            MemberStatus.OUT_OF_ORBIT: ("out_of_orbit", "Out of Orbit"),
            MemberStatus.OUT_OF_RANGE: ("out_of_range", "Out of Range"),
            MemberStatus.INACTIVE: ("inactive", "Inactive"),
        }
        for status, expected in cases.items():
            self.assertEqual(
                _exclusion_state(enr, SimpleNamespace(status=status)), expected
            )

    def test_active_member_active_household_has_no_overlay(self):
        from api.models import EnrollmentStage, MemberStatus
        from api.portal.views_delivery_calendar import _exclusion_state

        enr = SimpleNamespace(stage=EnrollmentStage.SERVICE_ACTIVE)
        member = SimpleNamespace(status=MemberStatus.ACTIVE)
        self.assertIsNone(_exclusion_state(enr, member))


class CalendarKeepsOccurrencesOnExclusionTest(TestCase):
    """Excluding a household (On Hold) or member (Paused) must KEEP the future
    delivery occurrences (so the calendar can overlay the reason), NOT delete
    them -- while PO generation still excludes them via live status/stage.
    """

    def _make_active_enrollment(self):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, DeliveryCadence,
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDeliverySchedule, MemberDietaryProfile, MemberStatus,
            ScheduleStatus, ServiceAuthorizationStatus,
        )

        today = timezone.localdate()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Hold", last_name="Er",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        now = timezone.now()
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=60),
            date_opened=now,
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.SERVICE_ACTIVE,
            delivery_weekdays=["mon", "tue", "wed", "thu", "fri"],
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=member, member_name="Hold Er",
            delivery_days_cadence=DeliveryCadence.ONCE_A_WEEK,
            meals_per_day=3, prod_per_delivery=0, meals_boxes_total=12,
            status=ScheduleStatus.SCHEDULED,
            starts_on=today, ends_on=today + timedelta(days=30),
        )
        return enr, member

    def _future_count(self, enr):
        from .models import OrderStatus, OrderSchedule

        return OrderSchedule.objects.filter(
            enrollment=enr, status=OrderStatus.SCHEDULED,
            anticipated_delivery_date__gte=timezone.localdate(),
        ).count()

    def test_hold_keeps_future_occurrences(self):
        from .models import EnrollmentStage
        from .services.lifecycle import advance_enrollment
        from .services.orders import sync_delivery_calendar

        enr, _member = self._make_active_enrollment()
        sync_delivery_calendar(enr)
        n0 = self._future_count(enr)
        self.assertGreater(n0, 0)

        # On Hold must NOT delete the calendar -- the occurrences are kept so the
        # profile can overlay "On Hold"; a follow-up sync must not drop them.
        advance_enrollment(enr, EnrollmentStage.ON_HOLD)
        sync_delivery_calendar(enr)
        self.assertEqual(self._future_count(enr), n0)

    def test_member_pause_keeps_future_occurrences(self):
        from .models import MemberStatus
        from .services.orders import sync_delivery_calendar

        enr, member = self._make_active_enrollment()
        sync_delivery_calendar(enr)
        n0 = self._future_count(enr)
        self.assertGreater(n0, 0)

        # Pausing a member keeps their occurrences (overlaid "Paused"), even
        # after a resync -- they're only excluded from POs, not deleted.
        member.status = MemberStatus.PAUSED
        member.save(update_fields=["status"])
        sync_delivery_calendar(enr)
        self.assertEqual(self._future_count(enr), n0)

    def test_sync_active_calendars_heals_fully_lapsed_calendar(self):
        from .models import OrderSchedule
        from .services.orders import sync_active_calendars, sync_delivery_calendar

        enr, _member = self._make_active_enrollment()
        sync_delivery_calendar(enr)
        n0 = self._future_count(enr)
        self.assertGreater(n0, 0)

        # Simulate a fully-lapsed calendar (0 future occurrences) whose plan
        # window still covers the future -- previously skipped by the nightly
        # refresh forever.
        OrderSchedule.objects.filter(enrollment=enr).delete()
        self.assertEqual(self._future_count(enr), 0)

        sync_active_calendars()
        self.assertEqual(self._future_count(enr), n0)  # regenerated
