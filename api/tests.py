import uuid
from datetime import timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings
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

    def test_active_status_not_flipped_to_expired_by_past_end_date(self):
        # The source status is authoritative: an Active policy with a stale past
        # end date must NOT be stored as Expired.
        cid = str(uuid.uuid4())
        self._save({
            "client_id": cid,
            "insurances": [{
                "plan_name": "Medicaid", "external_member_id": "1",
                "status": "active",
                "expired_at": (timezone.now() - timedelta(days=30)).isoformat(),
            }],
        })
        self.assertEqual(
            Insurance.objects.get(client__client_id=cid, plan_name="Medicaid").status,
            RecordStatus.ACTIVE,
        )

    def test_blank_status_derives_expired_from_past_end_date(self):
        # Back-compat: when the source sends no status, derive it from the date.
        cid = str(uuid.uuid4())
        self._save({
            "client_id": cid,
            "insurances": [{
                "plan_name": "P", "external_member_id": "1",
                "expired_at": (timezone.now() - timedelta(days=1)).isoformat(),
            }],
        })
        self.assertEqual(
            Insurance.objects.get(client__client_id=cid, plan_name="P").status,
            RecordStatus.EXPIRED,
        )

    def test_blank_status_no_end_date_derives_active(self):
        cid = str(uuid.uuid4())
        self._save({
            "client_id": cid,
            "insurances": [{"plan_name": "P", "external_member_id": "1"}],
        })
        self.assertEqual(
            Insurance.objects.get(client__client_id=cid, plan_name="P").status,
            RecordStatus.ACTIVE,
        )

    def test_enrolled_social_care_not_flipped_to_expired_by_past_end_date(self):
        from .models import SocialCareCoverage, SocialCareCoverageStatus

        cid = str(uuid.uuid4())
        self._save({
            "client_id": cid,
            "social_care_coverages": [{
                "plan_name": "SCC", "external_member_id": "1",
                "status": "enrolled",
                "expired_at": (timezone.now() - timedelta(days=30)).isoformat(),
            }],
        })
        self.assertEqual(
            SocialCareCoverage.objects.get(
                client__client_id=cid, plan_name="SCC"
            ).status,
            SocialCareCoverageStatus.ENROLLED,
        )

    def test_source_active_status_not_overridden_by_past_end_date(self):
        # Regression: a source status of Active must survive a stale/past end
        # date (previously the date logic flipped it to Expired, so clients with
        # active insurance showed Expired).
        cid = str(uuid.uuid4())
        Client.objects.create(client_id=cid)
        self._save({
            "client_id": cid,
            "insurances": [{
                "insurance_id": "INS1", "plan_name": "A",
                "status": "active",
                "expired_at": (timezone.now() - timedelta(days=30)).isoformat(),
            }],
        })
        self.assertEqual(
            Insurance.objects.get(insurance_id="INS1").status, RecordStatus.ACTIVE
        )

    def test_blank_status_still_derived_from_end_date(self):
        # Fallback preserved: when the source sends NO status, derive it from the
        # end date (past => Expired, none => Active).
        cid = str(uuid.uuid4())
        Client.objects.create(client_id=cid)
        self._save({
            "client_id": cid,
            "insurances": [
                {"insurance_id": "PAST", "plan_name": "P", "status": "",
                 "expired_at": (timezone.now() - timedelta(days=1)).isoformat()},
                {"insurance_id": "OPEN", "plan_name": "O", "status": ""},
            ],
        })
        self.assertEqual(
            Insurance.objects.get(insurance_id="PAST").status, RecordStatus.EXPIRED
        )
        self.assertEqual(
            Insurance.objects.get(insurance_id="OPEN").status, RecordStatus.ACTIVE
        )

    def test_social_care_source_status_not_overridden(self):
        # Same rule for social care coverage: an enrolled status survives a past
        # end date.
        from .models import SocialCareCoverage, SocialCareCoverageStatus

        cid = str(uuid.uuid4())
        Client.objects.create(client_id=cid)
        self._save({
            "client_id": cid,
            "social_care_coverages": [{
                "coverage_id": "SCC1", "plan_name": "S",
                "status": SocialCareCoverageStatus.ENROLLED,
                "expired_at": (timezone.now() - timedelta(days=30)).isoformat(),
            }],
        })
        self.assertEqual(
            SocialCareCoverage.objects.get(coverage_id="SCC1").status,
            SocialCareCoverageStatus.ENROLLED,
        )


class InsuranceExpiringFlagTest(TestCase):
    """`is_insurance_expiring` flags only an ACTIVE plan whose end date is in the
    near future -- never a null end date, and never a stale PAST date (which is
    already expired/terminated, not "expiring soon")."""

    def _plan(self, status, expired_at):
        from .models import Insurance

        return Insurance(status=status, expired_at=expired_at)

    def _expiring(self, status, expired_at):
        from .portal.serializers import is_insurance_expiring

        return is_insurance_expiring(self._plan(status, expired_at))

    def test_active_no_end_date_not_expiring(self):
        self.assertFalse(self._expiring("active", None))

    def test_active_future_within_window_is_expiring(self):
        self.assertTrue(self._expiring("active", timezone.now() + timedelta(days=10)))

    def test_active_future_beyond_window_not_expiring(self):
        self.assertFalse(self._expiring("active", timezone.now() + timedelta(days=120)))

    def test_active_past_end_date_not_expiring(self):
        # Regression: a plan that already terminated must NOT read as "expiring".
        self.assertFalse(self._expiring("active", timezone.now() - timedelta(days=400)))

    def test_inactive_not_expiring(self):
        self.assertFalse(self._expiring("inactive", timezone.now() + timedelta(days=10)))


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


class WilliamsburgAgentLeadSourceTest(TestCase):
    """Settings > Williamsburg Setup: a client saved by an agent flagged
    ``is_williamsburg_agent`` is forced to lead_source="Williamsburg" (which
    derives Client.is_williamsburg), overriding whatever lead source the
    extension sent. A normal agent's save is left untouched."""

    def _api_for(self, agent):
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def _post_client(self, api, lead_source=None):
        cid = str(uuid.uuid4())
        body = {"client_id": cid, "first_name": "Willie", "last_name": "Burg"}
        if lead_source is not None:
            body["lead_source"] = lead_source
        resp = api.post(reverse("client-list"), body, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return Client.objects.get(pk=cid)

    def test_williamsburg_agent_forces_lead_source(self):
        agent = Agent.objects.create(
            name="Will Agent", agent_code="700", group="Screeners",
            is_williamsburg_agent=True,
        )
        # Even with a different lead source picked in the ext, it's overridden.
        client = self._post_client(self._api_for(agent), lead_source="Some Queue")
        self.assertEqual(client.lead_source, "Williamsburg")
        self.assertTrue(client.is_williamsburg)

    def test_williamsburg_agent_forces_lead_source_when_blank(self):
        agent = Agent.objects.create(
            name="Will Agent", agent_code="701", group="Screeners",
            is_williamsburg_agent=True,
        )
        client = self._post_client(self._api_for(agent))
        self.assertEqual(client.lead_source, "Williamsburg")
        self.assertTrue(client.is_williamsburg)

    def test_normal_agent_lead_source_untouched(self):
        agent = Agent.objects.create(
            name="Reg Agent", agent_code="702", group="Screeners",
        )
        client = self._post_client(self._api_for(agent), lead_source="Some Queue")
        self.assertEqual(client.lead_source, "Some Queue")
        self.assertFalse(client.is_williamsburg)


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
        advance_enrollment(enrollment, EnrollmentStage.KITCHEN_ASSIGNMENT, force=True)
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
        advance_enrollment(enrollment, EnrollmentStage.KITCHEN_ASSIGNMENT, force=True)
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

        # Deny the sole internal-service case -> full stop (On Hold). The case
        # stays OPEN (authorization no longer drives case status) and NO ticket
        # is opened -- the pause is surfaced via the timeline + reconcile only.
        self._save_case(client, case_id, "denied")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        self.assertEqual(verification_status(client), "On Hold")
        self.assertEqual(
            Ticket.objects.filter(client=client, case_id=case_id).count(), 0
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


class InternalServiceClosureFullStopTest(TestCase):
    """When a client's LAST open internal-service (meal/box) case CLOSES it is a
    full stop: future deliveries truncated, the household paused (On Hold) then
    CANCELLED, with system notes on the primary and NO tickets. Idempotent on
    re-import."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Clo", last_name="Sure"
        )

    def _enrollment(self, client, stage):
        from .models import EnrollmentVerification, Household, HouseholdMember

        household = Household.objects.create(name="HH")
        HouseholdMember.objects.create(
            household=household, client=client, is_primary=True
        )
        return EnrollmentVerification.objects.create(
            client=client, household=household, stage=stage,
            verified_at=timezone.now(),
        )

    def _save_case(self, client, case_id, *, auth="approved",
                   case_status="open", closed_at=None):
        from .serializers import CaseSerializer

        data = {
            "case_id": case_id,
            "client_id": str(client.client_id),
            "case_type": "internal_service",
            "program_name": "Medically Tailored Meals",
            "service_authorization_status": auth,
            "case_status": case_status,
            "date_opened": timezone.now().isoformat(),
        }
        if closed_at is not None:
            data["case_closed_at"] = closed_at.isoformat()
        ser = CaseSerializer(data=data)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_last_open_case_closing_cancels_household(self):
        from .models import EnrollmentStage, Note, NoteSource, Ticket

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        case_id = str(uuid.uuid4())
        # Open + approved -> served (Rule 2 keeps it on the queue).
        self._save_case(client, case_id, auth="approved", case_status="open")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)

        # Close the sole open case -> full stop -> CANCELLED.
        self._save_case(
            client, case_id, auth="approved", case_status="closed",
            closed_at=timezone.now(),
        )
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.CANCELLED)
        # System notes on the primary (paused + cancelled); NO tickets.
        self.assertGreaterEqual(
            Note.objects.filter(client=client, source=NoteSource.SYSTEM).count(), 2
        )
        self.assertEqual(Ticket.objects.filter(client=client).count(), 0)

    def test_close_out_is_idempotent(self):
        from .models import EnrollmentStage, Note, NoteSource

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        case_id = str(uuid.uuid4())
        self._save_case(
            client, case_id, case_status="closed", closed_at=timezone.now()
        )
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.CANCELLED)
        n1 = Note.objects.filter(client=client, source=NoteSource.SYSTEM).count()

        # Re-import the same closed case -> nothing actionable -> no new notes.
        self._save_case(
            client, case_id, case_status="closed", closed_at=timezone.now()
        )
        n2 = Note.objects.filter(client=client, source=NoteSource.SYSTEM).count()
        self.assertEqual(n1, n2)


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


class RestoreOutOfRangeMemberTest(TestCase):
    """Manual per-member 'Return to service' on the Programs/Household tab
    (restore_range flag): reactivates an Out-of-Range member only when their ZIP
    is now serviceable, otherwise refuses."""

    def setUp(self):
        from .models import ExcludedZipCode

        ExcludedZipCode.objects.get_or_create(zip="11209")
        self.agent = Agent.objects.create(
            name="R Agent", agent_code="911", group="CS"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _member(self, delivery_zip, *, status):
        from .models import (
            Address, AddressType, Client, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, MemberDietaryProfile,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ada", last_name="Range"
        )
        hh = Household.objects.create()
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        addr = Address.objects.create(
            client=client, type=AddressType.CURRENT, zip=delivery_zip
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, delivery_address=addr,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        mv = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard", status=status,
        )
        return client, mv

    def _url(self, client, mv):
        return (
            f"/api/portal/members/{client.client_id}"
            f"/household/members/{mv.pk}/"
        )

    def test_restore_reactivates_when_zip_now_serviceable(self):
        from .models import MemberStatus

        client, mv = self._member("10001", status=MemberStatus.OUT_OF_RANGE)
        resp = self.api.patch(
            self._url(client, mv), {"restore_range": True}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.ACTIVE)

    def test_restore_refused_when_zip_still_excluded(self):
        from .models import MemberStatus

        client, mv = self._member("11209", status=MemberStatus.OUT_OF_RANGE)
        resp = self.api.patch(
            self._url(client, mv), {"restore_range": True}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.OUT_OF_RANGE)


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
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
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
            client_id=uuid.uuid4(), first_name="Dash", last_name="Board",
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

        paused = self._member(
            status=MemberStatus.PAUSED, stage=EnrollmentStage.SERVICE_ACTIVE
        )
        on_hold = self._member(stage=EnrollmentStage.ON_HOLD)  # status Active
        active = self._member()
        # Out-of-Range members of an on-hold household surface under their own
        # reason, so they must NOT also be counted here (no double counting).
        oor_on_hold = self._member(
            status=MemberStatus.OUT_OF_RANGE, stage=EnrollmentStage.ON_HOLD
        )

        ids = serving_client_ids("services_paused", start=None, end=None)
        self.assertIn(paused.client_id, ids)
        self.assertIn(on_hold.client_id, ids)
        self.assertNotIn(active.client_id, ids)
        self.assertNotIn(oor_on_hold.client_id, ids)

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


class NeedsReauthClassificationTest(SimpleTestCase):
    """A member with no future authorization is only a ``needs_reauth`` blocker
    when their authorization actually LAPSED. A member still AWAITING an
    authorization decision (an open pending case) must NOT be flagged -- there's
    nothing to remediate until Unite Us decides."""

    def _classify(self, **overrides):
        from .services.po_blockers import _classify_reason

        base = dict(
            kitchen_id="k", future=0, has_future_auth=False, plan_ends_on=None,
            kind=None, plan_kind=None, governing_kind=None,
            plan_kind_authorized=False, weekday_mismatch=False,
            switch_pending=False, open_case_count=1, enrollment_case_id="c1",
            governing_case_id="c1", today=timezone.localdate(),
            awaiting_auth=False,
        )
        base.update(overrides)
        return _classify_reason(**base)

    def test_awaiting_authorization_is_not_a_blocker(self):
        self.assertEqual(self._classify(awaiting_auth=True), "ok")

    def test_lapsed_without_pending_case_is_needs_reauth(self):
        self.assertEqual(self._classify(awaiting_auth=False), "needs_reauth")


class UniteUsRefreshTest(TestCase):
    """The on-demand Unite Us refresh: the capture-aware server-refresh guard
    (so we don't steal a rotating refresh token from a live agent session) and
    the result summary that tells the UI when to prompt a reconnect."""

    def _cred(self, **kw):
        from .models import UniteUsCredential, UniteUsCredentialStatus

        base = dict(
            provider_id="p", employee_id="e", access_token="tok",
            refresh_token="r", status=UniteUsCredentialStatus.ACTIVE,
        )
        base.update(kw)
        return UniteUsCredential.objects.create(**base)

    def test_recent_capture_skips_server_refresh(self):
        from datetime import timedelta
        from .models import UniteUsCredentialStatus
        from .integrations.uniteus import client as uu_client

        cred = self._cred(
            access_expires_at=timezone.now() - timedelta(minutes=1),  # "expired"
            last_captured_at=timezone.now(),                          # but just captured
        )
        # Expired by the clock, but a fresh capture means the browser is keeping
        # the shared rotating chain alive -> use the token as-is, don't refresh.
        self.assertTrue(uu_client.ensure_fresh(cred))
        cred.refresh_from_db()
        self.assertEqual(cred.status, UniteUsCredentialStatus.ACTIVE)

    @override_settings(UNITEUS_TOKEN_URL="")
    def test_stale_capture_refreshes_and_expires_without_token_url(self):
        from datetime import timedelta
        from .models import UniteUsCredentialStatus
        from .integrations.uniteus import client as uu_client

        cred = self._cred(
            access_expires_at=timezone.now() - timedelta(minutes=1),
            last_captured_at=timezone.now() - timedelta(hours=2),  # stale (off-hours)
        )
        # Stale capture -> the guard allows a server refresh. With no token URL
        # configured, refresh_credential can't call out, so it marks the
        # credential EXPIRED and returns False (no network hit).
        self.assertFalse(uu_client.ensure_fresh(cred))
        cred.refresh_from_db()
        self.assertEqual(cred.status, UniteUsCredentialStatus.EXPIRED)

    def test_summary_flags_reconnect_on_expired(self):
        from .models import ImportRun, ImportRunStatus
        from .services.uniteus_import import _summarize_refresh

        run = ImportRun.objects.create(
            source="uniteus", status=ImportRunStatus.FAILED,
        )
        run.error_log = "credential expired: nope"
        run.save()
        res = _summarize_refresh(run, scope="case")
        self.assertTrue(res["needs_reconnect"])
        self.assertFalse(res["ok"])

    def test_summary_ok_reports_change_count(self):
        from .models import ImportRun, ImportRunStatus
        from .services.uniteus_import import _summarize_refresh

        run = ImportRun.objects.create(
            source="uniteus", status=ImportRunStatus.COMPLETED, updated_count=1,
            stats={
                "cases": {"updated": 1},
                "changes": [
                    {"kind": "authorization",
                     "label": "Authorization: Pending → Approved", "tone": "success"},
                ],
            },
        )
        res = _summarize_refresh(run, scope="member")
        self.assertTrue(res["ok"])
        self.assertFalse(res["needs_reconnect"])
        self.assertEqual(len(res["changes"]), 1)
        self.assertIn("1 update", res["message"])

    def test_summary_ok_no_changes(self):
        from .models import ImportRun, ImportRunStatus
        from .services.uniteus_import import _summarize_refresh

        run = ImportRun.objects.create(
            source="uniteus", status=ImportRunStatus.COMPLETED,
        )
        res = _summarize_refresh(run, scope="case")
        self.assertTrue(res["ok"])
        self.assertEqual(res["changes"], [])
        self.assertIn("No changes", res["message"])


class CsvCaseMappingTest(SimpleTestCase):
    """map_case_row must reliably translate the Unite Us cases-export
    case_status + service_authorization_status onto our enums so a re-import
    actually updates those fields (not just create new rows)."""

    def _map(self, **row):
        from .services.csv_import import map_case_row

        base = {
            "case_id": "c1", "client_id": "cl1", "program_name": "Meals",
            "service_subtype": "Home Delivered Meals",
        }
        base.update(row)
        return map_case_row(base)

    def test_case_status_is_open_or_closed_only(self):
        # Case status is Open/Closed ONLY, driven by the closed date. Without a
        # closed date EVERY raw state (managed/off_platform/cancelled/unknown)
        # maps to Open; the authorization status is a separate dimension.
        self.assertEqual(self._map(case_status="managed")["case_status"], "open")
        self.assertEqual(self._map(case_status="off_platform")["case_status"], "open")
        self.assertEqual(self._map(case_status="somethingelse")["case_status"], "open")
        # A closed date is the only thing that yields Closed.
        self.assertEqual(
            self._map(case_status="managed", case_closed_at="2026-01-02T00:00:00Z")[
                "case_status"
            ],
            "closed",
        )

    def test_closed_date_forces_closed_despite_managed_state(self):
        # Unite Us leaves the exported state "managed" even after closure; a
        # populated closed date is the reliable "closed" signal (mirrors the API
        # import). Previously the CSV import left these reading Managed.
        out = self._map(case_status="managed", case_closed_at="2026-01-02T00:00:00Z")
        self.assertEqual(out["case_status"], "closed")
        out = self._map(
            case_status="managed", user_entered_closed_date="2026-01-02"
        )
        self.assertEqual(out["case_status"], "closed")

    def test_closed_date_yields_closed_regardless_of_raw_state(self):
        # Even a raw "cancelled" state with a closed date is stored as Closed --
        # case status is Open/Closed ONLY.
        out = self._map(case_status="cancelled", case_closed_at="2026-01-02T00:00:00Z")
        self.assertEqual(out["case_status"], "closed")

    def test_no_closed_date_stays_open(self):
        self.assertEqual(self._map(case_status="managed")["case_status"], "open")

    def test_auth_aliases_map(self):
        self.assertEqual(
            self._map(service_authorization_status="accepted")["service_authorization_status"],
            "approved",
        )
        self.assertEqual(
            self._map(service_authorization_status="requested")["service_authorization_status"],
            "pending",
        )
        # A rejected authorization is a denial (which then drives Closed).
        self.assertEqual(
            self._map(service_authorization_status="rejected")["service_authorization_status"],
            "denied",
        )

    def test_auth_direct_enum_values(self):
        for raw in ("pending", "approved", "denied", "expired", "not_required"):
            self.assertEqual(
                self._map(service_authorization_status=raw)["service_authorization_status"],
                raw,
            )


class UniteUsPersonMapperTest(SimpleTestCase):
    """map_person_to_client carries the Unite Us person's own created/updated
    timestamps onto the Client so the member's "Created" date matches Unite Us
    (Client.created_at is nullable, not auto_now_add)."""

    def _map(self, attrs):
        from api.integrations.uniteus.mappers import map_person_to_client

        return map_person_to_client({"data": {"id": "p1", "attributes": attrs}})

    def test_created_and_updated_mapped_from_source(self):
        out = self._map({
            "first_name": "Ada", "last_name": "Lovelace",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-02-02T00:00:00Z",
        })
        # The mapper passes ISO strings through (DRF's DateTimeField parses them
        # on save), so assert the string form.
        self.assertTrue(out["created_at"].startswith("2026-01-01"))
        self.assertTrue(out["updated_at"].startswith("2026-02-02"))

    def test_missing_timestamps_omitted(self):
        out = self._map({"first_name": "Ada", "last_name": "Lovelace"})
        self.assertNotIn("created_at", out)
        self.assertNotIn("updated_at", out)


class UniteUsCaseMapperTest(SimpleTestCase):
    """map_case must mirror the extension: a non-null closed_date means the case
    is CLOSED even though Unite Us leaves state='managed' on closed cases. The
    on-demand refresh previously left such cases reading MANAGED."""

    def _map(self, *, state, closed_date=None, auth=None, attrs=None):
        from api.integrations.uniteus.mappers import map_case

        rec = {
            "id": "case-1",
            "attributes": {"state": state, "closed_date": closed_date, **(attrs or {})},
            "relationships": {"person": {"data": {"id": "person-1"}}},
        }
        return map_case(rec, auth=auth)

    def test_date_opened_prefers_opened_date(self):
        out = self._map(
            state="open",
            attrs={"opened_date": "2026-01-05T00:00:00Z", "created_at": "2026-01-01T00:00:00Z"},
        )
        # map_case passes the ISO string through (DRF parses it on save).
        self.assertTrue(out["date_opened"].startswith("2026-01-05"))

    def test_date_opened_falls_back_to_created_at(self):
        # No opened_date -> use the Unite Us case created timestamp so date_opened
        # is never blank (mirrors the CSV import fallback).
        out = self._map(state="open", attrs={"created_at": "2026-01-01T00:00:00Z"})
        self.assertTrue(out["date_opened"].startswith("2026-01-01"))

    def test_closed_date_marks_case_closed_despite_managed_state(self):
        out = self._map(state="managed", closed_date="2026-01-02T00:00:00Z")
        self.assertEqual(out["case_status"], "closed")

    def test_auth_pre_decision_states_map_to_pending(self):
        # requested / deferred are pre-decision authorization states; both must
        # normalize to Pending so the daily pull updates the status instead of
        # leaving it blank/stale (mirrors the CSV import).
        for raw in ("requested", "deferred"):
            out = self._map(state="managed", auth={"state": raw})
            self.assertEqual(out["service_authorization_status"], "pending")

    def test_auth_accepted_maps_to_approved(self):
        out = self._map(state="managed", auth={"state": "accepted"})
        self.assertEqual(out["service_authorization_status"], "approved")

    def test_auth_rejected_maps_to_denied(self):
        # A rejected authorization normalizes to Denied (which drives Closed).
        out = self._map(state="managed", auth={"state": "rejected"})
        self.assertEqual(out["service_authorization_status"], "denied")

    def test_managed_without_closed_date_is_open(self):
        # Case status is Open/Closed ONLY: no closed date -> Open (regardless of
        # the raw Unite Us state).
        out = self._map(state="managed")
        self.assertEqual(out["case_status"], "open")

    def test_unknown_state_without_closed_date_falls_back_open(self):
        out = self._map(state="requested")
        self.assertEqual(out["case_status"], "open")


class CaseStatusChangeTimelineReasonTest(TestCase):
    """A Closed/Cancelled case-status transition must surface the closure reason
    (the case's ``closed_note``) on the timeline subtitle, so the history
    explains WHY the case was cancelled -- mirroring the client note. Open
    transitions stay a clean 'Prev -> New'."""

    def _client(self):
        return Client.objects.create(
            client_id=uuid.uuid4(), first_name="Case", last_name="Closer"
        )

    def _case(self, client, **fields):
        from .models import Case

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            date_opened=timezone.now(), **fields,
        )

    def test_closed_case_shows_reason_on_timeline(self):
        from .models import CaseStatus
        from .services.timeline import event_for_case_status_change

        c = self._client()
        case = self._case(
            c, case_status=CaseStatus.CLOSED, case_closed_at=timezone.now(),
            closed_note="Client moved out of service area",
        )
        ev = event_for_case_status_change(case, previous_status="open")
        self.assertIsNotNone(ev)
        self.assertIn("Client moved out of service area", ev.subtitle)
        self.assertIn("Closed", ev.subtitle)
        self.assertEqual(
            ev.metadata.get("closed_reason"), "Client moved out of service area"
        )

    def test_open_transition_has_no_reason_suffix(self):
        from .models import CaseStatus
        from .services.timeline import event_for_case_status_change

        c = self._client()
        case = self._case(
            c, case_status=CaseStatus.OPEN, closed_note="ignored while open",
        )
        ev = event_for_case_status_change(case, previous_status="pending_authorization")
        self.assertIsNotNone(ev)
        self.assertNotIn("ignored while open", ev.subtitle)


class AuthDrivesCaseStatusTest(TestCase):
    """Authorization is INDEPENDENT of case status: no auth state (denied,
    approved, pending, ...) ever changes the stored case_status. Case status is
    Open/Closed only, driven by the closed date. A denial instead pauses the
    household via the internal-service reconcile (covered elsewhere). Enforced in
    CaseSerializer._upsert, so it holds on EVERY write path (extension, CSV
    import, daily Unite Us pull, admin/direct API) since they all funnel there."""

    def _client(self):
        return Client.objects.create(
            client_id=uuid.uuid4(), first_name="Man", last_name="Aged"
        )

    def _save(self, client, case_id=None, **fields):
        from .serializers import CaseSerializer

        data = {
            "case_id": case_id or str(uuid.uuid4()),
            "client_id": str(client.client_id),
            **fields,
        }
        ser = CaseSerializer(data=data)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_denied_does_not_change_case_status(self):
        # Authorization no longer drives case status: a denial leaves the case in
        # whatever status it was written with (here Managed), NOT Closed.
        from .models import CaseStatus

        c = self._client()
        case = self._save(
            c, program_name="Medically Tailored Meals",
            service_type="Home Delivered Meals",
            case_status="managed", service_authorization_status="denied",
        )
        self.assertEqual(case.case_status, CaseStatus.MANAGED)

    def test_managed_plus_approved_stays_managed(self):
        from .models import CaseStatus

        c = self._client()
        case = self._save(
            c, program_name="Medically Tailored Meals",
            service_type="Home Delivered Meals",
            case_status="managed", service_authorization_status="approved",
        )
        self.assertEqual(case.case_status, CaseStatus.MANAGED)

    def test_open_plus_denied_stays_open(self):
        # A denial does NOT close an open case -- authorization is independent of
        # case status.
        from .models import CaseStatus

        c = self._client()
        case = self._save(
            c, program_name="Medically Tailored Meals",
            service_type="Home Delivered Meals",
            case_status="open", service_authorization_status="denied",
        )
        self.assertEqual(case.case_status, CaseStatus.OPEN)

    def test_open_plus_pending_stays_open(self):
        from .models import CaseStatus

        c = self._client()
        case = self._save(
            c, program_name="Medically Tailored Meals",
            service_type="Home Delivered Meals",
            case_status="open", service_authorization_status="pending",
        )
        self.assertEqual(case.case_status, CaseStatus.OPEN)

    def test_open_plus_approved_stays_open(self):
        from .models import CaseStatus

        c = self._client()
        case = self._save(
            c, program_name="Medically Tailored Meals",
            service_type="Home Delivered Meals",
            case_status="open", service_authorization_status="approved",
        )
        self.assertEqual(case.case_status, CaseStatus.OPEN)

    def test_denial_on_existing_managed_case_leaves_status_unchanged(self):
        # A later write that flips ONLY the authorization to Denied (status
        # omitted) must NOT change the stored case status -- it stays Managed.
        from .models import CaseStatus

        c = self._client()
        cid = str(uuid.uuid4())
        self._save(
            c, case_id=cid, program_name="Medically Tailored Meals",
            service_type="Home Delivered Meals",
            case_status="managed", service_authorization_status="approved",
        )
        case = self._save(c, case_id=cid, service_authorization_status="denied")
        self.assertEqual(case.case_status, CaseStatus.MANAGED)


class CaseSerializerAuthNormalizationTest(TestCase):
    """CaseSerializer must normalize a RAW Unite Us authorization state on the
    EXTENSION write path (which posts straight through the serializer with no
    pre-mapping), exactly like the CSV / daily-import mappers. Regression for:
    saving a rejected authorization from the extension left the stored status
    reading 'requested'/pending because the raw state was never mapped."""

    def _client(self):
        return Client.objects.create(
            client_id=uuid.uuid4(), first_name="Auth", last_name="Norm"
        )

    def _save(self, client, case_id=None, **fields):
        from .serializers import CaseSerializer

        data = {
            "case_id": case_id or str(uuid.uuid4()),
            "client_id": str(client.client_id),
            "program_name": "Medically Tailored Meals",
            "service_type": "Home Delivered Meals",
            **fields,
        }
        ser = CaseSerializer(data=data)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_raw_rejected_maps_to_denied(self):
        from .models import ServiceAuthorizationStatus

        case = self._save(self._client(), service_authorization_status="rejected")
        self.assertEqual(
            case.service_authorization_status, ServiceAuthorizationStatus.DENIED
        )
        # Raw label preserved for UI fidelity.
        self.assertEqual(case.service_authorization_status_label, "Rejected")

    def test_raw_requested_maps_to_pending(self):
        from .models import ServiceAuthorizationStatus

        case = self._save(self._client(), service_authorization_status="requested")
        self.assertEqual(
            case.service_authorization_status, ServiceAuthorizationStatus.PENDING
        )

    def test_resaving_pending_case_as_rejected_updates_enum(self):
        # The exact reported bug: an existing case at 'requested' (pending) that
        # is later saved from the ext as 'rejected' must flip to Denied, not stay
        # pending.
        from .models import ServiceAuthorizationStatus

        c = self._client()
        cid = str(uuid.uuid4())
        first = self._save(c, case_id=cid, service_authorization_status="requested")
        self.assertEqual(
            first.service_authorization_status, ServiceAuthorizationStatus.PENDING
        )
        second = self._save(c, case_id=cid, service_authorization_status="rejected")
        self.assertEqual(
            second.service_authorization_status, ServiceAuthorizationStatus.DENIED
        )

    def test_clean_enum_value_still_accepted(self):
        from .models import ServiceAuthorizationStatus

        case = self._save(self._client(), service_authorization_status="denied")
        self.assertEqual(
            case.service_authorization_status, ServiceAuthorizationStatus.DENIED
        )


class DailyPullClientSelectionTest(TestCase):
    """The nightly pull must iterate ONLY members who have at least one
    internal-service case (whose Unite Us status/authorization can change), not
    every stored client -- skipping the rest avoids needless API calls."""

    def test_only_members_with_internal_service_case_are_pulled(self):
        import uuid

        from .models import (
            Case, CaseType, Client, UniteUsCredential, UniteUsCredentialStatus,
        )
        from .services import uniteus_import

        UniteUsCredential.objects.create(
            provider_id="p", employee_id="e", access_token="tok",
            refresh_token="r", status=UniteUsCredentialStatus.ACTIVE,
        )
        internal = Client.objects.create(
            client_id=uuid.uuid4(), first_name="Ina", last_name="Ternal"
        )
        external = Client.objects.create(
            client_id=uuid.uuid4(), first_name="Ex", last_name="Ternal"
        )
        # No cases at all -> must be skipped.
        Client.objects.create(
            client_id=uuid.uuid4(), first_name="No", last_name="Case"
        )
        Case.objects.create(
            case_id=uuid.uuid4(), client=internal,
            case_type=CaseType.INTERNAL_SERVICE,
        )
        Case.objects.create(
            case_id=uuid.uuid4(), client=external,
            case_type=CaseType.EXTERNAL_SERVICE,
        )

        seen = []

        class RecordingClient:
            def __init__(self, credential):
                pass

            def get_person(self, person_id, include="addresses"):
                seen.append(str(person_id))
                return {"data": {}}  # empty -> skipped, no further API calls

        original = uniteus_import.uu_api.UniteUsClient
        uniteus_import.uu_api.UniteUsClient = RecordingClient
        try:
            uniteus_import.run_daily_pull(triggered_by="test")
        finally:
            uniteus_import.uu_api.UniteUsClient = original

        self.assertEqual(seen, [str(internal.client_id)])


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

    def test_cancelled_delivery_order_does_not_protect_stale_wrong_day(self):
        """A stale occurrence on a weekday the plan no longer delivers on, whose
        ONLY DeliveryOrder is CANCELLED, must be removable -- otherwise it lingers
        forever and shows up on the wrong day's PO. (A LIVE order still protects.)"""
        from datetime import timedelta

        from .models import (
            DeliveryOrder, DeliveryOrderStatus, OrderSchedule, OrderStatus,
            PurchaseOrder, PurchaseOrderStatus,
        )
        from .services.orders import sync_delivery_calendar

        enr, member = self._make_active_enrollment()
        # Narrow the plan to Mondays only so any non-Monday date is "wrong-day".
        enr.delivery_weekdays = ["mon"]
        enr.save(update_fields=["delivery_weekdays"])

        # A stale occurrence on the next Tuesday (not a plan weekday).
        today = timezone.localdate()
        tue = today + timedelta(days=(1 - today.weekday()) % 7 or 7)
        stale = OrderSchedule.objects.create(
            enrollment=enr, member=member, member_name="Hold Er",
            anticipated_delivery_date=tue, status=OrderStatus.SCHEDULED,
            household_group_code="G", kitchen=enr.kitchen,
        )
        # Only a CANCELLED delivery order references that date.
        po = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)
        DeliveryOrder.objects.create(
            purchase_order=po, member=member.client, expected_delivery_date=tue,
            status=DeliveryOrderStatus.CANCELLED,
        )

        sync_delivery_calendar(enr)
        self.assertFalse(
            OrderSchedule.objects.filter(pk=stale.pk).exists(),
            "cancelled order must not protect a stale wrong-day occurrence",
        )

    def _next_weekday(self, wd):
        from datetime import timedelta
        today = timezone.localdate()
        return today + timedelta(days=(wd - today.weekday()) % 7)

    def test_backfill_late_occurrences_creates_skipped_due_date(self):
        """A date skipped because its cutoff passed (plan starts the following
        week) is backfilled so a late PO can be cut."""
        from datetime import timedelta
        from unittest.mock import patch

        from .models import OrderSchedule, OrderStatus, ProductTypeKind
        from . import services  # noqa
        from .services import purchase_orders as po

        enr, member = self._make_active_enrollment()
        tue = self._next_weekday(1)  # a Tuesday >= today
        enr.delivery_weekdays = ["tue"]
        enr.save(update_fields=["delivery_weekdays"])
        # Plan starts the FOLLOWING Tuesday -> `tue` was skipped by the calendar.
        plan = enr.delivery_schedules.first()
        plan.starts_on = tue + timedelta(days=7)
        plan.ends_on = tue + timedelta(days=60)
        plan.save(update_fields=["starts_on", "ends_on"])

        with patch.object(po, "product_kind_for_enrollment", return_value=ProductTypeKind.MEALS):
            added = po.backfill_late_occurrences(ProductTypeKind.MEALS, tue)
        self.assertEqual(added, 1)
        self.assertTrue(
            OrderSchedule.objects.filter(
                enrollment=enr, member=member, anticipated_delivery_date=tue,
                status=OrderStatus.SCHEDULED,
            ).exists()
        )

    def test_backfill_skips_member_covered_that_week(self):
        """A member already covered by a live delivery that week is not
        backfilled (no double-delivery)."""
        from datetime import timedelta
        from unittest.mock import patch

        from .models import (
            DeliveryOrder, DeliveryOrderStatus, OrderSchedule, ProductTypeKind,
            PurchaseOrder, PurchaseOrderStatus,
        )
        from .services import purchase_orders as po

        enr, member = self._make_active_enrollment()
        tue = self._next_weekday(1)
        enr.delivery_weekdays = ["tue"]
        enr.save(update_fields=["delivery_weekdays"])
        plan = enr.delivery_schedules.first()
        plan.starts_on = tue + timedelta(days=7)
        plan.ends_on = tue + timedelta(days=60)
        plan.save(update_fields=["starts_on", "ends_on"])

        # A live delivery the SAME week (on the Wednesday) covers this member.
        ppo = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)
        DeliveryOrder.objects.create(
            purchase_order=ppo, member=member.client,
            expected_delivery_date=tue + timedelta(days=1),
            status=DeliveryOrderStatus.PENDING,
        )

        with patch.object(po, "product_kind_for_enrollment", return_value=ProductTypeKind.MEALS):
            added = po.backfill_late_occurrences(ProductTypeKind.MEALS, tue)
        self.assertEqual(added, 0)
        self.assertFalse(
            OrderSchedule.objects.filter(
                enrollment=enr, anticipated_delivery_date=tue,
            ).exists()
        )


class RebuildDeliveryCalendarTest(TestCase):
    """A member added to an already-active household never got a delivery plan
    (plans are created once, at kitchen assignment), so they never landed on the
    delivery calendar or any Purchase Order. rebuild_delivery_calendar (the
    manual button + the activation auto-heal) must create the missing plan and
    expand the calendar; out-of-orbit / unserviceable members stay excluded."""

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
            client_id=str(uuid.uuid4()), first_name="Prim", last_name="Ary",
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
            enrollment=enr, client=client, member_name="Prim Ary",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=member, member_name="Prim Ary",
            delivery_days_cadence=DeliveryCadence.ONCE_A_WEEK,
            meals_per_day=3, prod_per_delivery=0, meals_boxes_total=12,
            status=ScheduleStatus.SCHEDULED,
            starts_on=today, ends_on=today + timedelta(days=30),
        )
        return enr, hh

    def _add_member(self, enr, hh, *, status):
        from .models import (
            Client, HouseholdMember, MemberDietaryProfile, MemberStatus,
        )

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="New", last_name="Member",
        )
        HouseholdMember.objects.create(household=hh, client=c, is_primary=False)
        return MemberDietaryProfile.objects.create(
            enrollment=enr, client=c, member_name="New Member",
            menu_type="Standard", status=status,
        )

    def _future_count(self, member):
        from .models import OrderSchedule, OrderStatus

        return OrderSchedule.objects.filter(
            member=member, status=OrderStatus.SCHEDULED,
            anticipated_delivery_date__gte=timezone.localdate(),
        ).count()

    def test_rebuild_creates_plan_and_calendar_for_added_active_member(self):
        from .models import MemberDeliverySchedule, MemberStatus
        from .services.orders import rebuild_delivery_calendar

        enr, hh = self._make_active_enrollment()
        new_member = self._add_member(enr, hh, status=MemberStatus.ACTIVE)

        # Before: the added member has no plan and no calendar occurrences.
        self.assertFalse(
            MemberDeliverySchedule.objects.filter(member_profile=new_member).exists()
        )
        self.assertEqual(self._future_count(new_member), 0)

        result = rebuild_delivery_calendar(enr)

        self.assertEqual(result["plans_created"], 1)
        self.assertTrue(
            MemberDeliverySchedule.objects.filter(member_profile=new_member).exists()
        )
        self.assertGreater(self._future_count(new_member), 0)

    def test_rebuild_skips_out_of_orbit_member(self):
        from .models import MemberDeliverySchedule, MemberStatus
        from .services.orders import rebuild_delivery_calendar

        enr, hh = self._make_active_enrollment()
        oob = self._add_member(enr, hh, status=MemberStatus.OUT_OF_ORBIT)

        result = rebuild_delivery_calendar(enr)

        self.assertEqual(result["plans_created"], 0)
        self.assertFalse(
            MemberDeliverySchedule.objects.filter(member_profile=oob).exists()
        )
        self.assertEqual(self._future_count(oob), 0)

    def test_endpoint_rebuilds_calendar_for_member(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent, MemberDeliverySchedule, MemberStatus

        enr, hh = self._make_active_enrollment()
        new_member = self._add_member(enr, hh, status=MemberStatus.ACTIVE)

        agent = Agent.objects.create(name="Q", agent_code="950", group="CS")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        url = f"/api/portal/members/{new_member.client_id}/delivery-calendar/"
        resp = api.post(url)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["plans_created"], 1)
        self.assertTrue(
            MemberDeliverySchedule.objects.filter(member_profile=new_member).exists()
        )

    def test_sync_active_calendars_creates_missing_plan(self):
        # The nightly self-heal (sync_delivery_calendars command) must now also
        # CREATE the missing plan for a member added to an active household, not
        # just reconcile existing plans.
        from .models import MemberDeliverySchedule, MemberStatus
        from .services.orders import sync_active_calendars

        enr, hh = self._make_active_enrollment()
        new_member = self._add_member(enr, hh, status=MemberStatus.ACTIVE)

        totals = sync_active_calendars()

        self.assertGreaterEqual(totals["plans_created"], 1)
        self.assertTrue(
            MemberDeliverySchedule.objects.filter(member_profile=new_member).exists()
        )

    def test_primary_calendar_is_household_wide_dependent_is_self(self):
        # The PRIMARY's delivery calendar aggregates the WHOLE household; an
        # individual (non-primary) member's calendar shows only their own.
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent, MemberStatus
        from .services.orders import rebuild_delivery_calendar

        enr, hh = self._make_active_enrollment()
        dep = self._add_member(enr, hh, status=MemberStatus.ACTIVE)
        rebuild_delivery_calendar(enr)  # build the calendar for both members

        agent = Agent.objects.create(name="Q", agent_code="951", group="CS")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        # Primary view -> whole household (both members present).
        r = api.get(f"/api/portal/members/{enr.client_id}/delivery-calendar/")
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertTrue(data["summary"]["is_household"])
        member_ids = {row["member_id"] for row in data["occurrences"]}
        self.assertIn(str(enr.client_id), member_ids)
        self.assertIn(str(dep.client_id), member_ids)

        # Dependent view -> only their own deliveries.
        r2 = api.get(f"/api/portal/members/{dep.client_id}/delivery-calendar/")
        self.assertEqual(r2.status_code, 200, r2.content)
        data2 = r2.json()
        self.assertFalse(data2["summary"]["is_household"])
        member_ids2 = {row["member_id"] for row in data2["occurrences"]}
        self.assertEqual(member_ids2, {str(dep.client_id)})


class ProgramCategorySettingsTest(TestCase):
    """Settings > Program Categories: edit / activate / delete the program
    main-category master list. Categories are INACTIVE by default and cannot be
    created (they are built from screening results)."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent

        agent = Agent.objects.create(name="P", agent_code="960", group="CS")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    URL = "/api/portal/settings/program-main-categories/"

    def test_category_inactive_by_default(self):
        from .models import ProgramMainCategory

        self.assertFalse(ProgramMainCategory.objects.create(name="Food").is_active)

    def test_list_edit_toggle_delete_and_no_create(self):
        from .models import Program, ProgramMainCategory

        cat = ProgramMainCategory.objects.create(name="Food")
        Program.objects.create(name="Home Delivered Meals", main_category=cat)
        api = self._api()

        # List: shape + program_count + inactive by default.
        r = api.get(self.URL)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["active_count"], 0)
        self.assertEqual(body["results"][0]["name"], "Food")
        self.assertEqual(body["results"][0]["program_count"], 1)

        # Create is disallowed -- categories come from screening results.
        rc = api.post(self.URL, {"name": "New"}, format="json")
        self.assertEqual(rc.status_code, 405, rc.content)

        # Activate (the opt-in toggle).
        ra = api.patch(f"{self.URL}{cat.id}/", {"is_active": True}, format="json")
        self.assertEqual(ra.status_code, 200, ra.content)
        cat.refresh_from_db()
        self.assertTrue(cat.is_active)

        # Rename.
        re_ = api.patch(f"{self.URL}{cat.id}/", {"name": "Food & Meals"}, format="json")
        self.assertEqual(re_.status_code, 200, re_.content)
        cat.refresh_from_db()
        self.assertEqual(cat.name, "Food & Meals")

        # Delete.
        rd = api.delete(f"{self.URL}{cat.id}/")
        self.assertEqual(rd.status_code, 204, rd.content)
        self.assertFalse(ProgramMainCategory.objects.filter(pk=cat.id).exists())


class ProgramCreationRestrictionTest(TestCase):
    """Programs are only ADDED for the allowed org (Met Council - SCN - PHS).
    Other providers' cases and the provider-less name-based paths (assessment
    eligibility / service catalog) never create new rows -- they only update or
    link to a program that already exists."""

    def test_resolve_program_creates_only_for_allowed_provider(self):
        from .models import Program, Provider
        from .serializers import _resolve_program

        met = Provider.objects.create(
            provider_id=uuid.uuid4(), name="Met Council - SCN - PHS"
        )
        other = Provider.objects.create(provider_id=uuid.uuid4(), name="Other Org")
        pid_allowed = uuid.uuid4()
        pid_blocked = uuid.uuid4()

        # Allowed org -> created.
        prog = _resolve_program(pid_allowed, name="HDM", provider=met)
        self.assertIsNotNone(prog)
        self.assertTrue(Program.objects.filter(program_id=pid_allowed).exists())

        # Other org, unknown program -> not created.
        self.assertIsNone(_resolve_program(pid_blocked, name="X", provider=other))
        self.assertFalse(Program.objects.filter(program_id=pid_blocked).exists())

        # A program we already know is still updated (even from another provider).
        prog2 = _resolve_program(pid_allowed, name="HDM v2", provider=other)
        self.assertIsNotNone(prog2)
        prog.refresh_from_db()
        self.assertEqual(prog.name, "HDM v2")

    def test_upsert_program_never_creates(self):
        from .models import Program
        from .services.catalog import upsert_program

        # Unknown name -> nothing created (was previously auto-created).
        self.assertIsNone(upsert_program("Some Eligible Service"))
        self.assertFalse(
            Program.objects.filter(name="Some Eligible Service").exists()
        )

        # Existing program -> linked/returned, never duplicated.
        p = Program.objects.create(name="Existing Meals")
        self.assertEqual(upsert_program("Existing Meals").pk, p.pk)


class ExternalServiceCaseBlockedTest(TestCase):
    """External Service cases are never persisted -- neither an explicit type nor
    one derived from the program's ProgramPipeline category can be saved. The
    other three types (Navigation, Internal Service, Eligibility) still save."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="C", last_name="D"
        )

    def test_explicit_external_service_rejected(self):
        from rest_framework.exceptions import ValidationError

        from .models import Case, CaseType
        from .serializers import CaseSerializer

        c = self._client()
        cid = str(uuid.uuid4())
        ser = CaseSerializer(data={
            "case_id": cid, "client_id": str(c.client_id),
            "case_type": CaseType.EXTERNAL_SERVICE, "service_type": "Legal Aid",
        })
        ser.is_valid(raise_exception=True)
        with self.assertRaises(ValidationError):
            ser.save()
        self.assertFalse(Case.objects.filter(case_id=cid).exists())

    def test_derived_external_service_rejected(self):
        from rest_framework.exceptions import ValidationError

        from .models import Case, ProgramPipeline
        from .serializers import CaseSerializer

        c = self._client()
        ProgramPipeline.objects.create(
            program_name="Legal Aid", case_category="External Service",
            pipeline_id="p1",
        )
        cid = str(uuid.uuid4())
        ser = CaseSerializer(data={
            "case_id": cid, "client_id": str(c.client_id),
            "program_name": "Legal Aid",
        })
        ser.is_valid(raise_exception=True)
        with self.assertRaises(ValidationError):
            ser.save()
        self.assertFalse(Case.objects.filter(case_id=cid).exists())

    def test_navigation_case_allowed(self):
        from .models import CaseType
        from .serializers import CaseSerializer

        c = self._client()
        cid = str(uuid.uuid4())
        ser = CaseSerializer(data={
            "case_id": cid, "client_id": str(c.client_id),
            "case_type": CaseType.NAVIGATION, "service_type": "Something",
        })
        ser.is_valid(raise_exception=True)
        case = ser.save()
        self.assertEqual(case.case_type, CaseType.NAVIGATION)


class MemberWarningsTest(TestCase):
    """The member/household warning evaluator (api.services.warnings). Each check
    is exercised in isolation on a purpose-built household, plus a clean
    household that must produce no warnings and a multi-warning household."""

    def _client(self, first="Woe", last="Warn"):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )

    def _enrollment(self, client, *, stage=None, kitchen=None, program_name=""):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile,
        )

        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh,
            stage=stage or EnrollmentStage.SERVICE_ACTIVE,
            kitchen=kitchen, program_name=program_name,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=client)
        return enr

    def _internal_case(self, client, *, program="Medically Tailored Meals",
                       status=None, auth_status="", auth_ends=None):
        from .models import Case, CaseStatus, CaseType

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=status or CaseStatus.OPEN,
            program_name=program,
            service_authorization_status=auth_status,
            service_authorization_approval_ends_at=auth_ends,
        )

    def _cadence(self, enr, code):
        from .models import MemberDeliverySchedule, ScheduleStatus

        mp = enr.member_profiles.first()
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=mp, delivery_days_cadence=code,
            status=ScheduleStatus.SCHEDULED,
        )

    def _codes(self, enr):
        from .services.warnings import evaluate_enrollment_warnings

        return {w.code for w in evaluate_enrollment_warnings(enr)}

    def test_no_cadence_flagged_across_assignment_stages(self):
        from .models import EnrollmentStage
        from .services.warnings import NO_CADENCE

        # Both the assignment stage and active service must surface an
        # unassigned cadence, matching what Distribution Overview counts.
        active = self._enrollment(self._client(), stage=EnrollmentStage.SERVICE_ACTIVE)
        self.assertIn(NO_CADENCE, self._codes(active))

        pending = self._enrollment(
            self._client(), stage=EnrollmentStage.KITCHEN_ASSIGNMENT
        )
        self.assertIn(NO_CADENCE, self._codes(pending))

        # A household not yet at assignment must NOT be flagged.
        verified = self._enrollment(
            self._client(), stage=EnrollmentStage.VERIFIED
        )
        self.assertNotIn(NO_CADENCE, self._codes(verified))

    def test_no_kitchen_flagged_across_assignment_stages(self):
        from .models import EnrollmentStage, Kitchen, KitchenProductType, KitchenStatus
        from .services.warnings import NO_KITCHEN

        # Active + assignment stages with no kitchen are unassigned (surfaced
        # on Distribution Overview) and must appear on Care Management.
        active = self._enrollment(self._client(), stage=EnrollmentStage.SERVICE_ACTIVE)
        self.assertIn(NO_KITCHEN, self._codes(active))

        pending = self._enrollment(
            self._client(), stage=EnrollmentStage.KITCHEN_ASSIGNMENT
        )
        self.assertIn(NO_KITCHEN, self._codes(pending))

        # A household with a kitchen assigned is not flagged.
        kitchen = Kitchen.objects.create(
            name="AnyCo", status=KitchenStatus.ACTIVE,
            supported_products=[KitchenProductType.MEAL],
        )
        assigned = self._enrollment(self._client(), kitchen=kitchen)
        self.assertNotIn(NO_KITCHEN, self._codes(assigned))

    def test_plan_warnings_suppressed_when_no_servable_member(self):
        # An active household whose only member is Out of Orbit has no delivery
        # plan by design -- do NOT flag "no kitchen / no cadence". It should
        # still surface as the household out-of-orbit count instead.
        from .models import MemberStatus
        from .services.warnings import (
            HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, NO_CADENCE, NO_KITCHEN,
        )

        c = self._client()
        enr = self._enrollment(c)  # SERVICE_ACTIVE, no kitchen/cadence
        mp = enr.member_profiles.first()
        mp.status = MemberStatus.OUT_OF_ORBIT
        mp.save(update_fields=["status"])

        codes = self._codes(enr)
        self.assertNotIn(NO_CADENCE, codes)
        self.assertNotIn(NO_KITCHEN, codes)
        self.assertIn(HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, codes)

    def test_plan_warnings_still_flag_when_a_member_is_servable(self):
        # A mixed household (one ACTIVE member + one Out of Orbit) still needs a
        # kitchen for the served member, so the warning must remain.
        from .models import HouseholdMember, MemberDietaryProfile, MemberStatus
        from .services.warnings import NO_KITCHEN

        primary = self._client(first="Prim", last="Ary")
        enr = self._enrollment(primary)  # ACTIVE primary, no kitchen
        other = self._client(first="Orb", last="It")
        HouseholdMember.objects.create(household=enr.household, client=other)
        mp2 = MemberDietaryProfile.objects.create(enrollment=enr, client=other)
        mp2.status = MemberStatus.OUT_OF_ORBIT
        mp2.save(update_fields=["status"])

        self.assertIn(NO_KITCHEN, self._codes(enr))

    def test_multiple_open_cases(self):
        from .services.warnings import MULTIPLE_OPEN_CASES

        c = self._client()
        enr = self._enrollment(c)
        self._internal_case(c)
        self.assertNotIn(MULTIPLE_OPEN_CASES, self._codes(enr))
        self._internal_case(c)
        self.assertIn(MULTIPLE_OPEN_CASES, self._codes(enr))

    def test_conflicting_product_types(self):
        from .services.warnings import CONFLICTING_PRODUCT_TYPES

        c = self._client()
        enr = self._enrollment(c)
        self._internal_case(c, program="Medically Tailored Meals")
        self._internal_case(c, program="Grocery Boxes Program")
        self.assertIn(CONFLICTING_PRODUCT_TYPES, self._codes(enr))

    def test_kitchen_missing_product(self):
        from .models import Kitchen, KitchenProductType, KitchenStatus
        from .services.warnings import KITCHEN_MISSING_PRODUCT

        box_only = Kitchen.objects.create(
            name="BoxCo", status=KitchenStatus.ACTIVE,
            supported_products=[KitchenProductType.BOX],
        )
        c = self._client()
        enr = self._enrollment(c, kitchen=box_only)
        self._internal_case(c, program="Medically Tailored Meals")  # meals kind
        self.assertIn(KITCHEN_MISSING_PRODUCT, self._codes(enr))

    def test_cadence_not_supported_by_kitchen(self):
        from .models import Cadence, Kitchen, KitchenProductType, KitchenStatus
        from .services.warnings import CADENCE_NOT_SUPPORTED_BY_KITCHEN

        tue = Cadence.objects.create(code="tue_only", label="Tue", is_active=True)
        kitchen = Kitchen.objects.create(
            name="MealCo", status=KitchenStatus.ACTIVE,
            supported_products=[KitchenProductType.MEAL],
        )
        kitchen.cadences.set([tue])
        c = self._client()
        enr = self._enrollment(c, kitchen=kitchen, program_name="Medically Tailored Meals")
        self._cadence(enr, "mon_thu")  # not one the kitchen runs
        self.assertIn(CADENCE_NOT_SUPPORTED_BY_KITCHEN, self._codes(enr))

    def test_cadence_kind_mismatch(self):
        from .models import ProductType, ProductTypeKind
        from .services.warnings import CADENCE_KIND_MISMATCH

        # 'once_a_week' is configured for BOXES; a meals household on it mismatches.
        ProductType.objects.create(
            type=ProductTypeKind.BOXES, delivery_days_cadence="once_a_week",
        )
        c = self._client()
        enr = self._enrollment(c, program_name="Medically Tailored Meals")
        self._cadence(enr, "once_a_week")
        self.assertIn(CADENCE_KIND_MISMATCH, self._codes(enr))

    def test_insurance_expiring_and_expired(self):
        from .models import Insurance
        from .services.warnings import INSURANCE_EXPIRING

        soon = self._client()
        enr = self._enrollment(soon)
        Insurance.objects.create(
            client=soon, plan_name="P", external_member_id="1",
            expired_at=timezone.now() + timedelta(days=10),
        )
        self.assertIn(INSURANCE_EXPIRING, self._codes(enr))

        healthy = self._client()
        enr2 = self._enrollment(healthy)
        Insurance.objects.create(
            client=healthy, plan_name="P", external_member_id="2",
            expired_at=timezone.now() + timedelta(days=120),
        )
        self.assertNotIn(INSURANCE_EXPIRING, self._codes(enr2))

    def _titles(self, enr):
        from .services.warnings import evaluate_enrollment_warnings

        return [w.title for w in evaluate_enrollment_warnings(enr)]

    def test_active_insurance_never_shows_expired(self):
        # An active Medicaid alongside an OLD expired policy: the member is
        # covered now, so no "Insurance expired" (nor "expiring") warning.
        from .models import Insurance, RecordStatus

        c = self._client()
        enr = self._enrollment(c)
        Insurance.objects.create(
            client=c, plan_name="Medicaid", external_member_id="1",
            status=RecordStatus.ACTIVE,
        )
        Insurance.objects.create(
            client=c, plan_name="Old", external_member_id="2",
            status=RecordStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=100),
        )
        titles = self._titles(enr)
        self.assertNotIn("Insurance expired", titles)
        self.assertNotIn("Insurance expiring", titles)

    def test_active_insurance_with_stale_past_end_date_not_expired(self):
        # Active policy carrying a stale past end date: status wins, no warning.
        from .models import Insurance, RecordStatus

        c = self._client()
        enr = self._enrollment(c)
        Insurance.objects.create(
            client=c, plan_name="Medicaid", external_member_id="1",
            status=RecordStatus.ACTIVE,
            expired_at=timezone.now() - timedelta(days=5),
        )
        self.assertNotIn("Insurance expired", self._titles(enr))

    def test_active_insurance_expiring_soon_still_warns(self):
        # An active policy genuinely lapsing within 30 days still warns (expiring).
        from .models import Insurance, RecordStatus
        from .services.warnings import INSURANCE_EXPIRING, evaluate_enrollment_warnings

        c = self._client()
        enr = self._enrollment(c)
        Insurance.objects.create(
            client=c, plan_name="Medicaid", external_member_id="1",
            status=RecordStatus.ACTIVE,
            expired_at=timezone.now() + timedelta(days=10),
        )
        ws = [w for w in evaluate_enrollment_warnings(enr) if w.code == INSURANCE_EXPIRING]
        self.assertEqual([w.title for w in ws], ["Insurance expiring"])

    def test_no_active_expired_policy_shows_expired(self):
        # With no active policy, a past end date still surfaces "Insurance expired".
        from .models import Insurance, RecordStatus
        from .services.warnings import INSURANCE_EXPIRING, evaluate_enrollment_warnings

        c = self._client()
        enr = self._enrollment(c)
        Insurance.objects.create(
            client=c, plan_name="Medicaid", external_member_id="1",
            status=RecordStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=3),
        )
        ws = [w for w in evaluate_enrollment_warnings(enr) if w.code == INSURANCE_EXPIRING]
        self.assertEqual([w.title for w in ws], ["Insurance expired"])

    def test_internal_case_expired(self):
        from .services.warnings import INTERNAL_CASE_EXPIRED

        c = self._client()
        enr = self._enrollment(c)
        self._internal_case(
            c, program="Medically Tailored Meals",
            auth_ends=timezone.now() - timedelta(days=3),
        )
        self.assertIn(INTERNAL_CASE_EXPIRED, self._codes(enr))

    def test_member_paused_warning(self):
        # Pausing ANY household member surfaces a single household-scope roll-up
        # count (shown on every member), like out of orbit / out of range.
        from .models import MemberStatus
        from .services.warnings import HOUSEHOLD_MEMBERS_PAUSED

        enr = self._enrollment(self._client())
        mp = enr.member_profiles.first()
        mp.status = MemberStatus.PAUSED
        mp.save(update_fields=["status"])
        self.assertIn(HOUSEHOLD_MEMBERS_PAUSED, self._codes(enr))

    def test_out_of_orbit_range_are_household_counts_not_member_warnings(self):
        # Out of orbit / out of range surface as a SINGLE household count
        # warning, never a separate per-member warning.
        from .models import MemberStatus
        from .services.warnings import HOUSEHOLD_MEMBERS_OUT_OF_ORBIT

        enr = self._enrollment(self._client())
        mp = enr.member_profiles.first()
        mp.status = MemberStatus.OUT_OF_ORBIT
        mp.save(update_fields=["status"])
        codes = self._codes(enr)
        self.assertIn(HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, codes)
        self.assertNotIn("member_out_of_orbit", codes)

    def test_household_on_hold(self):
        from .models import EnrollmentStage
        from .services.warnings import HOUSEHOLD_ON_HOLD

        enr = self._enrollment(self._client(), stage=EnrollmentStage.ON_HOLD)
        self.assertIn(HOUSEHOLD_ON_HOLD, self._codes(enr))

    def test_household_cancelled_suppresses_member_status(self):
        from .models import EnrollmentStage, MemberStatus
        from .services.warnings import HOUSEHOLD_CANCELLED, HOUSEHOLD_MEMBERS_PAUSED

        enr = self._enrollment(self._client(), stage=EnrollmentStage.CANCELLED)
        mp = enr.member_profiles.first()
        mp.status = MemberStatus.PAUSED
        mp.save(update_fields=["status"])
        codes = self._codes(enr)
        self.assertIn(HOUSEHOLD_CANCELLED, codes)
        # Member-status roll-up counts are suppressed for a terminal (cancelled)
        # household.
        self.assertNotIn(HOUSEHOLD_MEMBERS_PAUSED, codes)

    def test_household_out_of_service_counts(self):
        from .models import MemberDietaryProfile, MemberStatus
        from .services.warnings import (
            HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, HOUSEHOLD_MEMBERS_OUT_OF_RANGE,
            HOUSEHOLD_MEMBERS_PAUSED, evaluate_enrollment_warnings,
        )

        c = self._client()
        enr = self._enrollment(c)
        # Primary out of orbit; add a second out-of-orbit member, one
        # out-of-range member, and one paused member (all on the same
        # enrollment/household).
        mp = enr.member_profiles.first()
        mp.status = MemberStatus.OUT_OF_ORBIT
        mp.save(update_fields=["status"])
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=self._client(), status=MemberStatus.OUT_OF_ORBIT,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=self._client(), status=MemberStatus.OUT_OF_RANGE,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=self._client(), status=MemberStatus.PAUSED,
        )
        warns = {w.code: w for w in evaluate_enrollment_warnings(enr)}
        self.assertEqual(warns[HOUSEHOLD_MEMBERS_OUT_OF_ORBIT].refs["count"], 2)
        self.assertEqual(warns[HOUSEHOLD_MEMBERS_OUT_OF_RANGE].refs["count"], 1)
        self.assertEqual(warns[HOUSEHOLD_MEMBERS_PAUSED].refs["count"], 1)
        self.assertEqual(warns[HOUSEHOLD_MEMBERS_OUT_OF_ORBIT].title, "2 Out of Orbit")
        self.assertEqual(warns[HOUSEHOLD_MEMBERS_OUT_OF_RANGE].title, "1 Out of Range")
        self.assertEqual(warns[HOUSEHOLD_MEMBERS_PAUSED].title, "1 Paused")
        # Every roll-up count is household-scope so it shows on EVERY member.
        self.assertEqual(warns[HOUSEHOLD_MEMBERS_PAUSED].scope, "household")

    def test_household_open_tickets_count(self):
        # Open (Open / In Progress) follow-up tickets for ANY household member
        # roll up into a single household-scope count shown on every member;
        # resolved tickets don't count and the warning clears when none remain.
        from .models import Ticket, TicketStatus, TicketType, TicketTypeCode
        from .services.warnings import (
            HOUSEHOLD_OPEN_TICKETS, evaluate_enrollment_warnings,
        )

        c = self._client()
        enr = self._enrollment(c)
        ttype, _ = TicketType.objects.get_or_create(
            code=TicketTypeCode.SYSTEM_CHANGE_DETECTED,
            defaults={"label": "System Change Detected"},
        )
        Ticket.objects.create(type=ttype, client=c, status=TicketStatus.OPEN)
        Ticket.objects.create(type=ttype, client=c, status=TicketStatus.IN_PROGRESS)
        Ticket.objects.create(type=ttype, client=c, status=TicketStatus.RESOLVED)

        warns = {w.code: w for w in evaluate_enrollment_warnings(enr)}
        self.assertIn(HOUSEHOLD_OPEN_TICKETS, warns)
        self.assertEqual(warns[HOUSEHOLD_OPEN_TICKETS].refs["count"], 2)
        self.assertEqual(warns[HOUSEHOLD_OPEN_TICKETS].title, "2 open tickets")
        self.assertEqual(warns[HOUSEHOLD_OPEN_TICKETS].scope, "household")

        # Resolving the open tickets clears the warning (self-healing).
        Ticket.objects.filter(client=c).update(status=TicketStatus.RESOLVED)
        codes = self._codes(enr)
        self.assertNotIn(HOUSEHOLD_OPEN_TICKETS, codes)

    def test_clean_household_has_no_warnings(self):
        from .models import Cadence, Kitchen, KitchenProductType, KitchenStatus, ProductType, ProductTypeKind

        mon_thu = Cadence.objects.create(code="mon_thu", label="Mon/Thu", is_active=True)
        ProductType.objects.create(
            type=ProductTypeKind.MEALS, delivery_days_cadence="mon_thu",
        )
        kitchen = Kitchen.objects.create(
            name="GoodCo", status=KitchenStatus.ACTIVE,
            supported_products=[KitchenProductType.MEAL],
        )
        kitchen.cadences.set([mon_thu])
        c = self._client()
        enr = self._enrollment(c, kitchen=kitchen, program_name="Medically Tailored Meals")
        self._cadence(enr, "mon_thu")
        self._internal_case(
            c, program="Medically Tailored Meals", auth_status="approved",
            auth_ends=timezone.now() + timedelta(days=90),
        )
        self.assertEqual(self._codes(enr), set())

    # --- Phase 2: persisted snapshot (sync) --------------------------------
    def test_sync_persists_active_warnings(self):
        from .models import MemberWarning, WarningStatus
        from .services.warnings import NO_CADENCE, sync_household_warnings

        c = self._client()
        enr = self._enrollment(c)  # active, no cadence -> NO_CADENCE
        sync_household_warnings(enr)
        row = MemberWarning.objects.get(client=c, code=NO_CADENCE)
        self.assertEqual(row.status, WarningStatus.ACTIVE)
        self.assertEqual(row.enrollment_id, enr.pk)
        self.assertIsNone(row.resolved_at)

    def test_sync_resolves_when_problem_fixed(self):
        from .models import MemberWarning, WarningStatus
        from .services.warnings import NO_CADENCE, sync_household_warnings

        c = self._client()
        enr = self._enrollment(c)
        sync_household_warnings(enr)
        # Fix it: give the household a cadence, then re-sync.
        self._cadence(enr, "mon_thu")
        sync_household_warnings(enr)
        row = MemberWarning.objects.get(client=c, code=NO_CADENCE)
        self.assertEqual(row.status, WarningStatus.RESOLVED)
        self.assertIsNotNone(row.resolved_at)

    def test_sync_resolves_no_kitchen_when_kitchen_assigned(self):
        # Regression: a household flagged "No kitchen assigned" that then gets a
        # kitchen must have the stale ACTIVE row RESOLVED on re-sync -- otherwise
        # it lingers on Care Management as a false positive (member shows as not
        # kitchen-assigned despite a real kitchen). The kitchen-assignment /
        # kitchen-edit paths now call sync_household_warnings for exactly this.
        from .models import (
            Kitchen, KitchenProductType, KitchenStatus, MemberWarning,
            WarningStatus,
        )
        from .services.warnings import NO_KITCHEN, sync_household_warnings

        c = self._client()
        enr = self._enrollment(c)  # active, servable, no kitchen -> NO_KITCHEN
        sync_household_warnings(enr)
        self.assertEqual(
            MemberWarning.objects.get(client=c, code=NO_KITCHEN).status,
            WarningStatus.ACTIVE,
        )
        # Assign a kitchen, then re-sync (what the assign path now does).
        enr.kitchen = Kitchen.objects.create(
            name="K", status=KitchenStatus.ACTIVE,
            supported_products=[KitchenProductType.MEAL],
        )
        enr.save(update_fields=["kitchen"])
        sync_household_warnings(enr)
        row = MemberWarning.objects.get(client=c, code=NO_KITCHEN)
        self.assertEqual(row.status, WarningStatus.RESOLVED)
        self.assertIsNotNone(row.resolved_at)

    def test_sync_reactivates_resolved_row(self):
        from .models import MemberWarning, WarningStatus
        from .services.warnings import NO_CADENCE, sync_household_warnings

        c = self._client()
        enr = self._enrollment(c)
        sync_household_warnings(enr)
        self._cadence(enr, "mon_thu")
        sync_household_warnings(enr)
        # Regression: remove the cadence again -> the SAME row reactivates.
        enr.delivery_schedules.all().delete()
        sync_household_warnings(enr)
        rows = MemberWarning.objects.filter(client=c, code=NO_CADENCE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().status, WarningStatus.ACTIVE)
        self.assertIsNone(rows.first().resolved_at)

    def test_sync_is_idempotent(self):
        from .models import MemberWarning
        from .services.warnings import sync_household_warnings

        c = self._client()
        enr = self._enrollment(c)
        self._internal_case(c)
        self._internal_case(c)  # multiple_open_cases + no_cadence
        sync_household_warnings(enr)
        first = MemberWarning.objects.filter(client=c).count()
        sync_household_warnings(enr)
        self.assertEqual(MemberWarning.objects.filter(client=c).count(), first)


class AddressQualityTest(SimpleTestCase):
    """The delivery-address format detector (api.services.address_quality)."""

    def _codes(self, **kw):
        from .services.address_quality import detect_address_issues
        return set(detect_address_issues(**kw))

    def test_unit_in_street_when_unit_field_empty(self):
        from .services.address_quality import UNIT_IN_STREET
        codes = self._codes(street="123 Main St Apt 4B", unit="", city="Brooklyn", state="NY")
        self.assertIn(UNIT_IN_STREET, codes)

    def test_hash_unit_in_street(self):
        from .services.address_quality import UNIT_IN_STREET
        codes = self._codes(street="123 Main St #3", unit="", city="Brooklyn", state="NY")
        self.assertIn(UNIT_IN_STREET, codes)

    def test_duplicate_unit_when_both_populated(self):
        from .services.address_quality import DUPLICATE_UNIT, UNIT_IN_STREET
        codes = self._codes(street="115 Clymer St Apt 7D", unit="7D", city="Brooklyn", state="NY")
        self.assertIn(DUPLICATE_UNIT, codes)
        self.assertNotIn(UNIT_IN_STREET, codes)

    def test_po_box(self):
        from .services.address_quality import PO_BOX
        codes = self._codes(street="PO Box 123", unit="", city="Brooklyn", state="NY")
        self.assertIn(PO_BOX, codes)

    def test_missing_components(self):
        from .services.address_quality import MISSING_CITY, MISSING_STATE
        codes = self._codes(street="123 Main St", unit="", city="", state="")
        self.assertIn(MISSING_CITY, codes)
        self.assertIn(MISSING_STATE, codes)

    def test_clean_address_has_no_issues(self):
        codes = self._codes(street="123 Main St", unit="4B", city="Brooklyn", state="NY", zip_code="11201")
        self.assertEqual(codes, set())

    def test_street_name_with_unitlike_word_not_flagged(self):
        # "Flatbush" / "Lott" must NOT trip the \bfl\b / \blot\b markers.
        codes = self._codes(street="1500 Flatbush Ave", unit="", city="Brooklyn", state="NY")
        self.assertEqual(codes, set())
        codes2 = self._codes(street="25 Lott Pl", unit="", city="Brooklyn", state="NY")
        self.assertEqual(codes2, set())


class CareManagementListTest(TestCase):
    """The Care Management queue endpoint. Verifies that households which are not
    being served (On Hold / Cancelled / Closed / Service Complete) are excluded
    even when they carry an active warning snapshot."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Cam CS", agent_code="777", group="CS"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _household_with_warning(self, stage, first="Woe", last="Warn"):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberWarning, WarningStatus,
        )
        from .services.warnings import sync_household_warnings

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        # Build ACTIVE (no kitchen/cadence) so a warning is guaranteed, persist
        # the snapshot, THEN move the household to the target stage WITHOUT
        # re-syncing -- so the active warning row survives and the view is the
        # only thing that can exclude it.
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=c)
        sync_household_warnings(enr)
        assert MemberWarning.objects.filter(
            enrollment=enr, status=WarningStatus.ACTIVE
        ).exists()
        if stage and stage != EnrollmentStage.SERVICE_ACTIVE:
            enr.stage = stage
            enr.save(update_fields=["stage"])
        return c, enr

    def _ids(self, body):
        return {r["client_id"] for r in body["results"]}

    def test_excludes_on_hold_and_terminal_households(self):
        from .models import EnrollmentStage

        served, _ = self._household_with_warning(
            EnrollmentStage.SERVICE_ACTIVE, first="Ser", last="Ved"
        )
        on_hold, _ = self._household_with_warning(
            EnrollmentStage.ON_HOLD, first="Hal", last="Hold"
        )
        cancelled, _ = self._household_with_warning(
            EnrollmentStage.CANCELLED, first="Cam", last="Cancel"
        )

        resp = self.api.get(reverse("portal-care-management"))
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = self._ids(resp.json())
        self.assertIn(str(served.pk), ids)
        self.assertNotIn(str(on_hold.pk), ids)
        self.assertNotIn(str(cancelled.pk), ids)

    def _make_row(self, code, first, last, *, scope="household"):
        """A served household carrying a single ACTIVE warning of ``code``
        (created directly so the test targets the view's allowlist, not the
        detection setup)."""
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberWarning, WarningSeverity, WarningStatus,
        )

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberWarning.objects.create(
            client=c, enrollment=enr, code=code, severity=WarningSeverity.ORANGE,
            scope=scope, title=code, detail="", status=WarningStatus.ACTIVE,
        )
        return c, enr

    def test_excludes_non_actionable_member_and_household_state_warnings(self):
        # Only service-config problems CS can remediate flag a household onto the
        # queue; informational states (out of orbit/range, paused) do not.
        from .services.warnings import (
            HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, HOUSEHOLD_MEMBERS_OUT_OF_RANGE,
            HOUSEHOLD_MEMBERS_PAUSED, NO_KITCHEN,
        )

        actionable, _ = self._make_row(NO_KITCHEN, "Act", "Ionable")
        orbit, _ = self._make_row(HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, "Or", "Bit")
        rng, _ = self._make_row(HOUSEHOLD_MEMBERS_OUT_OF_RANGE, "Ra", "Nge")
        paused, _ = self._make_row(HOUSEHOLD_MEMBERS_PAUSED, "Pau", "Sed")

        resp = self.api.get(reverse("portal-care-management"))
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = self._ids(resp.json())
        self.assertIn(str(actionable.pk), ids)
        self.assertNotIn(str(orbit.pk), ids)
        self.assertNotIn(str(rng.pk), ids)
        self.assertNotIn(str(paused.pk), ids)

    def test_actionable_household_hides_informational_rows(self):
        # A household surfaced for a real issue must not carry informational
        # (out-of-orbit) rows in its warning list.
        from .models import MemberWarning, WarningSeverity, WarningStatus
        from .services.warnings import HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, NO_KITCHEN

        c, enr = self._make_row(NO_KITCHEN, "Mix", "Ed")
        MemberWarning.objects.create(
            client=c, enrollment=enr, code=HOUSEHOLD_MEMBERS_OUT_OF_ORBIT,
            severity=WarningSeverity.ORANGE, scope="household",
            title="1 Out of Orbit", detail="", status=WarningStatus.ACTIVE,
        )

        resp = self.api.get(reverse("portal-care-management"))
        self.assertEqual(resp.status_code, 200, resp.content)
        row = next(
            r for r in resp.json()["results"] if r["client_id"] == str(c.pk)
        )
        codes = {w["code"] for w in row["warnings"]}
        self.assertIn(NO_KITCHEN, codes)
        self.assertNotIn(HOUSEHOLD_MEMBERS_OUT_OF_ORBIT, codes)


class RemovedMemberPromotionTest(TestCase):
    """Removing a non-primary household member who still holds an ACTIVE
    internal-service case promotes them to primary of their OWN household with a
    fresh Pending-Verification enrollment (rather than dropping them)."""

    def _client(self, first="A", last="B"):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )

    def test_member_with_active_internal_case_is_promoted(self):
        from .models import (
            Case, CaseStatus, CaseType, ClientStage, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus,
        )
        from .portal.views_members import _promote_removed_member_to_own_household

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = Household.objects.create(name="Old HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=dep,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.MANAGED,
            program_name="Medically Tailored Meals",
        )

        # Simulate the delete's detach step, then promote.
        HouseholdMember.objects.filter(client=dep, household=hh).delete()
        MemberDietaryProfile.objects.filter(
            client=dep, enrollment__household=hh
        ).delete()
        new_enr = _promote_removed_member_to_own_household(
            dep, case,
            diet_snapshot={"menu_type": "Standard", "status": MemberStatus.ACTIVE,
                           "food_allergies": ["fish"]},
            member_name="Dee Dependent", agent=None, actor="",
        )

        dep.refresh_from_db()
        hm = HouseholdMember.objects.get(client=dep)
        self.assertEqual(new_enr.stage, EnrollmentStage.PENDING_VERIFICATION)
        self.assertEqual(str(new_enr.case_id), str(case.case_id))
        self.assertTrue(hm.is_primary)
        self.assertNotEqual(hm.household_id, hh.household_id)
        self.assertEqual(dep.lifecycle_stage, ClientStage.PENDING_VERIFICATION)
        prof = new_enr.member_profiles.get(client=dep)
        self.assertEqual(prof.menu_type, "Standard")
        self.assertEqual(prof.food_allergies, ["fish"])
        # Old household + primary untouched.
        self.assertTrue(
            HouseholdMember.objects.filter(household=hh, client=primary).exists()
        )

    def test_existing_live_enrollment_is_not_duplicated(self):
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, MemberStatus,
        )
        from .portal.views_members import _promote_removed_member_to_own_household

        dep = self._client("Dee", "Dependent")
        hh = Household.objects.create(name="Own HH")
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=True)
        existing = EnrollmentVerification.objects.create(
            client=dep, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=dep,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.MANAGED,
        )

        result = _promote_removed_member_to_own_household(
            dep, case, diet_snapshot={}, member_name="Dee Dependent",
            agent=None, actor="",
        )
        self.assertEqual(result.pk, existing.pk)
        self.assertEqual(
            EnrollmentVerification.objects.filter(client=dep).count(), 1
        )


class CSDashboardTest(TestCase):
    """The CS command-center endpoints: summary, trends, and manager ticket
    stats. Covers access gating and the core aggregations."""

    def _auth(self, group="CS", agent_code="900", is_manager=False):
        agent = Agent.objects.create(
            name=f"{group} Agent", agent_code=agent_code, group=group,
            is_manager=is_manager,
        )
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        access["agent_is_manager"] = agent.is_manager
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return agent, api

    def _client(self, first="A", last="B"):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )

    def _ticket_type(self):
        from .models import TicketType

        tt, _ = TicketType.objects.get_or_create(
            code="verification", defaults={"label": "Verification"}
        )
        return tt

    def _open_ticket(self, tt, **kw):
        from .models import Ticket, TicketStatus

        defaults = {"type": tt, "status": TicketStatus.OPEN, "severity": "high"}
        defaults.update(kw)
        return Ticket.objects.create(**defaults)

    # ── access gating ─────────────────────────────────────────────────────
    def test_summary_requires_cs_access(self):
        _, verifier = self._auth(group="Verifiers", agent_code="901")
        resp = verifier.get(reverse("portal-cs-dashboard"))
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_ticket_stats_requires_manager(self):
        _, cs = self._auth(group="CS", agent_code="902")
        resp = cs.get(reverse("portal-cs-dashboard-ticket-stats"))
        self.assertEqual(resp.status_code, 403, resp.content)

    # ── summary ───────────────────────────────────────────────────────────
    def test_summary_aggregates_triage_verification_and_personal_slice(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberWarning, Ticket, TicketStatus, WarningSeverity, WarningStatus,
        )
        from .services.warnings import NO_KITCHEN

        agent, api = self._auth(group="CS", agent_code="903")
        tt = self._ticket_type()

        # A served household on the Care Management queue (actionable warning).
        c = self._client("Care", "Queue")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberWarning.objects.create(
            client=c, enrollment=enr, code=NO_KITCHEN,
            severity=WarningSeverity.RED, scope="household", title="No kitchen",
            detail="", status=WarningStatus.ACTIVE,
        )
        # A pending-verification enrollment for the backlog count.
        pc = self._client("Pend", "Verify")
        phh = Household.objects.create(name="PHH")
        HouseholdMember.objects.create(household=phh, client=pc, is_primary=True)
        EnrollmentVerification.objects.create(
            client=pc, household=phh, stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        # One open ticket assigned to me + one resolved by me today.
        self._open_ticket(tt, assigned_to=agent, status=TicketStatus.OPEN)
        Ticket.objects.create(
            type=tt, status=TicketStatus.RESOLVED, severity="low",
            resolved_at=timezone.now(), resolved_by=f"agent:{agent.agent_code}",
        )

        resp = api.get(reverse("portal-cs-dashboard"))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["triage"]["households"], 1)
        self.assertEqual(body["triage"]["red"], 1)
        self.assertEqual(body["triage"]["unassigned_kitchen"], 1)
        self.assertEqual(body["verification"]["pending"], 1)
        self.assertGreaterEqual(body["tickets"]["open"], 1)
        self.assertEqual(body["me"]["open_assigned"], 1)
        self.assertEqual(body["me"]["resolved_today"], 1)

    # ── trends ────────────────────────────────────────────────────────────
    def test_trends_returns_dense_series_and_resolution(self):
        from .models import Ticket, TicketStatus

        _, api = self._auth(group="CS", agent_code="904")
        tt = self._ticket_type()
        now = timezone.now()
        t = Ticket.objects.create(
            type=tt, status=TicketStatus.RESOLVED, severity="low",
            resolved_at=now,
        )
        Ticket.objects.filter(pk=t.pk).update(
            created_at=now - timedelta(hours=4)
        )

        resp = api.get(reverse("portal-cs-dashboard-trends"), {"days": 14})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["days"], 14)
        self.assertEqual(len(body["series"]), 14)
        self.assertEqual(body["resolution"]["count"], 1)
        self.assertGreater(body["resolution"]["avg_hours"], 0)

    # ── manager ticket stats ──────────────────────────────────────────────
    def test_ticket_stats_breakdowns_and_solved_by_agent(self):
        from .models import Ticket, TicketStatus

        agent, api = self._auth(
            group="Management", agent_code="905", is_manager=True
        )
        tt = self._ticket_type()
        # Open, high, unassigned.
        self._open_ticket(tt, status=TicketStatus.OPEN, severity="high")
        # Resolved in range, attributed to agent 905.
        now = timezone.now()
        r = Ticket.objects.create(
            type=tt, status=TicketStatus.RESOLVED, severity="medium",
            resolved_at=now, resolved_by="agent:905",
        )
        Ticket.objects.filter(pk=r.pk).update(created_at=now - timedelta(hours=2))

        resp = api.get(reverse("portal-cs-dashboard-ticket-stats"))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["backlog"]["open"], 1)
        self.assertEqual(body["backlog"]["high_open"], 1)
        self.assertEqual(body["backlog"]["unassigned_open"], 1)
        self.assertEqual(body["backlog"]["resolved_in_range"], 1)
        self.assertTrue(any(row["open"] for row in body["by_type"]))
        solved = {row["name"]: row["resolved"] for row in body["solved_by_agent"]}
        self.assertEqual(solved.get(agent.name), 1)


class IsNewFlagTest(TestCase):
    """Client.is_new (the Urgent Care / 'Need Attention' flag) is raised when a
    client's first internal-service case is created via the EXTENSION (request
    user has an agent_code) or an IMPORT (inside change_context(IMPORT)) -- but
    ONLY when the client also meets the coverage gate (valid Medicaid + valid
    social care). Never by admin/CRM writes, and never re-raised once verified."""

    def _client(self, *, coverage=True):
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="New", last_name="Member"
        )
        if coverage:
            self._add_medicaid(client)
            self._add_social_care(client)
        return client

    def _add_medicaid(self, client):
        from .models import Insurance, InsurancePlanType, RecordStatus

        return Insurance.objects.create(
            client=client, plan_type=InsurancePlanType.MEDICAID,
            status=RecordStatus.ACTIVE, plan_name="NY Medicaid",
        )

    def _add_social_care(self, client):
        from .models import SocialCareCoverage, SocialCareCoverageStatus

        return SocialCareCoverage.objects.create(
            client=client, status=SocialCareCoverageStatus.ENROLLED,
            plan_name="Social Care",
        )

    def _save_internal_case(self, client, *, context):
        from .models import CaseType
        from .serializers import CaseSerializer

        data = {
            "case_id": str(uuid.uuid4()),
            "client_id": str(client.client_id),
            "case_type": CaseType.INTERNAL_SERVICE,
            "program_name": "Medically Tailored Meals",
        }
        ser = CaseSerializer(data=data, context=context)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_extension_write_sets_is_new(self):
        # An extension request: the user carries an agent_code (AgentUser).
        client = self._client()
        request = SimpleNamespace(user=SimpleNamespace(agent_code="123"))
        self._save_internal_case(client, context={"request": request})
        client.refresh_from_db()
        self.assertTrue(client.is_new)

    def test_admin_write_does_not_set_is_new(self):
        # A Django-admin/session write: request present, but the user has no
        # agent_code -- must NOT flag (even with full coverage).
        client = self._client()
        request = SimpleNamespace(user=SimpleNamespace(username="staff"))
        self._save_internal_case(client, context={"request": request})
        client.refresh_from_db()
        self.assertFalse(client.is_new)

    def test_no_medicaid_not_flagged(self):
        # Extension write, valid social care but NO Medicaid -> gate blocks.
        client = self._client(coverage=False)
        self._add_social_care(client)
        request = SimpleNamespace(user=SimpleNamespace(agent_code="123"))
        self._save_internal_case(client, context={"request": request})
        client.refresh_from_db()
        self.assertFalse(client.is_new)

    def test_no_social_care_not_flagged(self):
        # Extension write, valid Medicaid but NO social care -> gate blocks.
        client = self._client(coverage=False)
        self._add_medicaid(client)
        request = SimpleNamespace(user=SimpleNamespace(agent_code="123"))
        self._save_internal_case(client, context={"request": request})
        client.refresh_from_db()
        self.assertFalse(client.is_new)

    def test_import_context_sets_is_new(self):
        # The CSV / Unite Us importers wrap writes in change_context(IMPORT);
        # a new internal-service case there flags the client (case imports are
        # Met Council-only, so only our own cases reach the serializer).
        from .history import ChangeSource, change_context

        client = self._client()
        with change_context(ChangeSource.IMPORT, "system:test"):
            self._save_internal_case(client, context={})
        client.refresh_from_db()
        self.assertTrue(client.is_new)

    def test_bare_write_does_not_set_is_new(self):
        # No request, no change_context (e.g. a plain server-side / CRM write):
        # neither an ext nor an import signal -- must NOT flag.
        client = self._client()
        self._save_internal_case(client, context={})
        client.refresh_from_db()
        self.assertFalse(client.is_new)

    def test_already_verified_client_not_reflagged(self):
        from .models import EnrollmentVerification, Household, HouseholdMember

        client = self._client()
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        EnrollmentVerification.objects.create(
            client=client, household=hh, verified_at=timezone.now(),
        )
        request = SimpleNamespace(user=SimpleNamespace(agent_code="123"))
        self._save_internal_case(client, context={"request": request})
        client.refresh_from_db()
        self.assertFalse(client.is_new)

    def test_extension_write_stamps_created_by(self):
        # An extension write (AgentUser with name + agent_id) stamps the acting
        # agent as the case creator, filling the Urgent Care "Created By" column.
        import uuid as _uuid

        client = self._client()
        agent_id = _uuid.uuid4()
        request = SimpleNamespace(
            user=SimpleNamespace(agent_code="123", name="Ada Agent", agent_id=agent_id)
        )
        case = self._save_internal_case(client, context={"request": request})
        case.refresh_from_db()
        self.assertEqual(case.created_by_name, "Ada Agent")
        self.assertEqual(case.created_by_id, agent_id)

    def test_extension_write_preserves_existing_created_by(self):
        # A payload that already carries created_by (e.g. a Unite Us import row)
        # must NOT be overwritten by the ext stamp.
        from .models import CaseType
        from .serializers import CaseSerializer

        client = self._client()
        request = SimpleNamespace(
            user=SimpleNamespace(agent_code="123", name="Ada Agent")
        )
        ser = CaseSerializer(
            data={
                "case_id": str(uuid.uuid4()),
                "client_id": str(client.client_id),
                "case_type": CaseType.INTERNAL_SERVICE,
                "program_name": "Medically Tailored Meals",
                "created_by_name": "Source Creator",
            },
            context={"request": request},
        )
        ser.is_valid(raise_exception=True)
        case = ser.save()
        case.refresh_from_db()
        self.assertEqual(case.created_by_name, "Source Creator")

    def test_admin_write_does_not_stamp_created_by(self):
        # A Django-admin/session write (no agent_code) leaves created_by blank.
        client = self._client()
        request = SimpleNamespace(user=SimpleNamespace(username="staff"))
        case = self._save_internal_case(client, context={"request": request})
        case.refresh_from_db()
        self.assertEqual(case.created_by_name, "")


class RequestVerificationEndpointTest(TestCase):
    """POST /api/portal/members/<id>/request-verification/ creates the Pending
    Verification enrollment when the client meets the Urgent Care gate, and is
    hard-gated (400) on missing coverage / already-requested."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Req Agent", agent_code="910", group="Management"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _candidate(self, *, medicaid=True, social=True, case=True):
        from .models import (
            Case, CaseStatus, CaseType, InsurancePlanType, RecordStatus,
            SocialCareCoverage, SocialCareCoverageStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Urgent", last_name="Care",
            is_new=True,
        )
        if medicaid:
            Insurance.objects.create(
                client=client, plan_type=InsurancePlanType.MEDICAID,
                status=RecordStatus.ACTIVE, plan_name="Medicaid",
            )
        if social:
            SocialCareCoverage.objects.create(
                client=client, status=SocialCareCoverageStatus.ENROLLED,
                plan_name="Social Care",
            )
        if case:
            Case.objects.create(
                case_id=str(uuid.uuid4()), client=client,
                case_type=CaseType.INTERNAL_SERVICE,
                case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
            )
        return client

    def _url(self, client):
        return f"/api/portal/members/{client.client_id}/request-verification/"

    def test_request_creates_enrollment_and_clears_is_new(self):
        from .models import EnrollmentStage, EnrollmentVerification

        client = self._candidate()
        resp = self.api.post(self._url(client))
        self.assertEqual(resp.status_code, 200, resp.content)
        client.refresh_from_db()
        self.assertFalse(client.is_new)
        enr = EnrollmentVerification.objects.filter(client=client).first()
        self.assertIsNotNone(enr)
        self.assertEqual(enr.stage, EnrollmentStage.PENDING_VERIFICATION)
        self.assertEqual(enr.requested_by_id, self.agent.id)

    def test_missing_medicaid_rejected(self):
        from .models import EnrollmentVerification

        client = self._candidate(medicaid=False)
        resp = self.api.post(self._url(client))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Medicaid", resp.json()["error"])
        self.assertFalse(EnrollmentVerification.objects.filter(client=client).exists())

    def test_missing_social_care_rejected(self):
        client = self._candidate(social=False)
        resp = self.api.post(self._url(client))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("social care", resp.json()["error"])

    def test_already_requested_rejected(self):
        from .models import EnrollmentStage, EnrollmentVerification, Household, HouseholdMember

        client = self._candidate()
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        resp = self.api.post(self._url(client))
        self.assertEqual(resp.status_code, 400, resp.content)


class WorkQueueVipTest(TestCase):
    """The Work Queue VIP flag: created via the ticket POST (default False),
    exposed on the serializer, and filterable via ?vip=1."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Q Agent", agent_code="920", group="CS"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        from .models import TicketType

        self.tt, _ = TicketType.objects.get_or_create(
            code="verification", defaults={"label": "Verification"}
        )

    def _create(self, **body):
        payload = {"type": "verification", "severity": "medium", "reason": "r"}
        payload.update(body)
        return self.api.post(reverse("portal-tickets"), payload, format="json")

    def test_create_defaults_vip_false(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(resp.json()["vip"])

    def test_create_with_vip_true(self):
        resp = self._create(vip=True)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.json()["vip"])

    def test_vip_filter(self):
        self._create(vip=True)
        self._create(vip=False)
        resp = self.api.get(reverse("portal-tickets") + "?vip=1")
        self.assertEqual(resp.status_code, 200, resp.content)
        results = resp.json()["results"]
        self.assertTrue(len(results) >= 1)
        self.assertTrue(all(t["vip"] for t in results))


class ReviewUrgentCareCommandTest(TestCase):
    """The review_urgent_care_candidates command flags is_new for members who
    meet the gate but were missed, and only with --apply."""

    def _candidate(self, *, is_new=False, medicaid=True, social=True):
        from .models import (
            Case, CaseStatus, CaseType, InsurancePlanType, RecordStatus,
            SocialCareCoverage, SocialCareCoverageStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Miss", last_name="Ed",
            is_new=is_new,
        )
        if medicaid:
            Insurance.objects.create(
                client=client, plan_type=InsurancePlanType.MEDICAID,
                status=RecordStatus.ACTIVE, plan_name="Medicaid",
            )
        if social:
            SocialCareCoverage.objects.create(
                client=client, status=SocialCareCoverageStatus.ENROLLED,
                plan_name="Social Care",
            )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            program_name="Medically Tailored Meals",
        )
        return client

    def test_dry_run_does_not_flag(self):
        from django.core.management import call_command
        from io import StringIO

        client = self._candidate()
        call_command("review_urgent_care_candidates", stdout=StringIO())
        client.refresh_from_db()
        self.assertFalse(client.is_new)

    def test_apply_flags_only_candidates(self):
        from django.core.management import call_command
        from io import StringIO

        good = self._candidate()
        no_med = self._candidate(medicaid=False)
        call_command("review_urgent_care_candidates", "--apply", stdout=StringIO())
        good.refresh_from_db()
        no_med.refresh_from_db()
        self.assertTrue(good.is_new)
        self.assertFalse(no_med.is_new)


class CsvImportRulesTest(TestCase):
    """Client + case import data-quality rules: ZIP/phone normalization, the
    Meals/Boxes + household classification, and the strict Met Council-only
    case gate (originating_provider_id)."""

    MET = "12706c81-03a1-4cdb-954a-579929cd05df"

    def test_zip5_normalization(self):
        from .services.csv_import import _zip5

        self.assertEqual(_zip5("11201"), "11201")
        self.assertEqual(_zip5("11201-6789"), "11201")   # ZIP+4 -> base
        self.assertEqual(_zip5("112016789"), "11201")     # 9 straight digits
        self.assertEqual(_zip5("1120"), "")               # too short -> dropped
        self.assertEqual(_zip5("1120167"), "")            # 7 digits -> dropped
        self.assertEqual(_zip5(""), "")

    def test_format_phone(self):
        from .services.csv_import import _format_phone

        self.assertEqual(_format_phone("3473701726"), "(347) 370-1726")
        self.assertEqual(_format_phone("13473701726"), "(347) 370-1726")
        self.assertEqual(_format_phone("+1 (347) 370-1726"), "(347) 370-1726")
        self.assertEqual(_format_phone("347.370.1726"), "(347) 370-1726")
        # Unparseable (extension / foreign) is kept verbatim.
        self.assertEqual(_format_phone("347-370-1726 x12"), "347-370-1726 x12")

    def test_product_kind_voucher_is_boxes(self):
        from .models import ProductTypeKind
        from .services.catalog import product_type_kind_for_name

        self.assertEqual(
            product_type_kind_for_name("Medically Tailored Meals (MTM) - Brooklyn"),
            ProductTypeKind.MEALS,
        )
        self.assertEqual(
            product_type_kind_for_name(
                "Medically Tailored or Nutritionally Appropriate Food "
                "Prescriptions: Voucher - Other Eligible Populations - Queens"
            ),
            ProductTypeKind.BOXES,
        )
        self.assertEqual(
            product_type_kind_for_name("...Food Prescriptions: Boxes - Brooklyn"),
            ProductTypeKind.BOXES,
        )
        self.assertIsNone(product_type_kind_for_name("Health and Wellness"))

    def test_household_type_from_program_token(self):
        from .models import CaseHouseholdType, Client
        from .serializers import derive_household_type

        indiv = Client(client_id=uuid.uuid4(), first_name="A", last_name="B")
        self.assertEqual(
            derive_household_type(indiv, "MTM - Other Eligible Populations - Brooklyn"),
            CaseHouseholdType.INDIVIDUAL,
        )
        self.assertEqual(
            derive_household_type(
                indiv, "MTM - (Household) High-Risk Children Under 18 - Brooklyn"
            ),
            CaseHouseholdType.HOUSEHOLD,
        )
        # Client household data still counts even without the token.
        fam = Client(client_id=uuid.uuid4(), first_name="A", last_name="B", is_a_family=True)
        self.assertEqual(
            derive_household_type(fam, "MTM - Brooklyn"), CaseHouseholdType.HOUSEHOLD
        )

    def _case_row(self, client_id, originating_provider_id, **over):
        row = {
            "case_id": str(uuid.uuid4()),
            "client_id": str(client_id),
            "originating_provider_id": originating_provider_id,
            "program_name": "Medically Tailored Meals (MTM) - (Household) "
                            "High-Risk Children Under 18 - Brooklyn",
            "service_subtype": "Medically Tailored Meals",
            "case_status": "open",
        }
        row.update(over)
        return row

    def test_is_met_council_case_union(self):
        from .services.lifecycle import is_met_council_case

        # Originating OR managing provider counts.
        self.assertTrue(is_met_council_case(originating_provider_id=self.MET))
        self.assertTrue(is_met_council_case(provider_id=self.MET))
        self.assertTrue(is_met_council_case(provider_name="Met Council - SCN - PHS"))
        self.assertTrue(is_met_council_case(provider_name="met council - scn - phs"))
        # No Met Council signal at all -> not Met Council.
        self.assertFalse(is_met_council_case(
            originating_provider_id=str(uuid.uuid4()),
            provider_id=str(uuid.uuid4()),
            provider_name="Selfhelp Community Services",
        ))
        self.assertFalse(is_met_council_case())

        # allow_originating=False (non-internal-service cases): originating alone
        # NO LONGER counts -- Met Council must MANAGE the case. A case Met
        # Council merely referred out to another org is dropped.
        self.assertFalse(is_met_council_case(
            originating_provider_id=self.MET, allow_originating=False,
        ))
        # ...but managing provider still keeps it, regardless of the flag.
        self.assertTrue(is_met_council_case(
            provider_id=self.MET, allow_originating=False,
        ))
        self.assertTrue(is_met_council_case(
            provider_name="Met Council - SCN - PHS", allow_originating=False,
        ))

    def test_import_cases_met_council_only(self):
        from .models import Case, CaseHouseholdType, CaseType, Client, ImportRun, ImportRunStatus
        from .services.csv_import import CsvImporter

        client = Client.objects.create(
            client_id=uuid.uuid4(), first_name="New", last_name="Member"
        )
        run = ImportRun.objects.create(source="csv_uniteus", status=ImportRunStatus.RUNNING)
        importer = CsvImporter(run, emit_side_effects=False)

        mine = self._case_row(client.client_id, self.MET)
        # Originated elsewhere but MANAGED by Met Council -> kept (union rule).
        managed = self._case_row(
            client.client_id, str(uuid.uuid4()),
            provider_name="Met Council - SCN - PHS",
        )
        external = self._case_row(client.client_id, str(uuid.uuid4()))
        blank = self._case_row(client.client_id, "")
        # Originated by Met Council but a NON-internal case it does NOT manage
        # (ECM-style eligibility assessment referred out): dropped, because
        # originating only counts for internal-service (meal/box) cases.
        referred_out = self._case_row(
            client.client_id, self.MET,
            service_subtype="Social Service Case Management",
            program_name="Navigation Services - Eligibility Assessment Level 1 - Brooklyn",
        )
        # Internal-service MEAL case Met Council ORIGINATED but a DIFFERENT named
        # org MANAGES (referred out to God's Love): dropped -- the managing org
        # owns it, so originating must NOT rescue it.
        referred_meal = self._case_row(
            client.client_id, self.MET,
            provider_name="God's Love We Deliver - SCN - PHS",
        )
        importer.import_cases([mine, managed, external, blank, referred_out, referred_meal])

        # Met Council-originated (blank manager) AND Met Council-managed are
        # imported; the rest -- including the meal case referred out to another
        # named org -- are not.
        self.assertTrue(Case.objects.filter(pk=mine["case_id"]).exists())
        self.assertTrue(Case.objects.filter(pk=managed["case_id"]).exists())
        self.assertFalse(Case.objects.filter(pk=external["case_id"]).exists())
        self.assertFalse(Case.objects.filter(pk=blank["case_id"]).exists())
        self.assertFalse(Case.objects.filter(pk=referred_out["case_id"]).exists())
        self.assertFalse(Case.objects.filter(pk=referred_meal["case_id"]).exists())
        self.assertEqual(importer.stats["created"], 2)
        self.assertEqual(importer.stats["skipped"], 4)

        # And it's classified correctly: internal-service + household (token).
        case = Case.objects.get(pk=mine["case_id"])
        self.assertEqual(case.case_type, CaseType.INTERNAL_SERVICE)
        self.assertEqual(case.household_type, CaseHouseholdType.HOUSEHOLD)

        # Both imported cases are Internal Service; the count is surfaced in the
        # cases dataset stats (for the Settings import UI) without inflating the
        # processed/created totals.
        self.assertEqual(importer.internal_service_count, 2)
        importer.finalize()
        self.assertEqual(run.stats["cases"]["internal_service"], 2)
        self.assertEqual(run.created_count, 2)

    def test_delete_non_metcouncil_cases_command(self):
        from django.core.management import call_command
        from .models import Case, CaseStatus, CaseType, Client, Provider

        client = Client.objects.create(
            client_id=uuid.uuid4(), first_name="A", last_name="B"
        )
        met = Provider.objects.create(provider_id=self.MET, name="Met Council - SCN - PHS")
        # Internal-service case Met Council ORIGINATED -> kept (union rule).
        keep_orig = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            originating_provider=met, case_type=CaseType.INTERNAL_SERVICE,
        )
        keep_managed = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            provider_name="Met Council - SCN - PHS",
        )
        # Internal-service meal case with NO named managing org (blank provider
        # columns, as many were imported) -> KEPT: meal/box programs are Met
        # Council's own, so a blank manager is treated as Met Council's.
        keep_blank_internal = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            case_type=CaseType.INTERNAL_SERVICE, provider_name="",
        )
        # Internal-service case attributed to a DIFFERENT named org -> dropped.
        drop = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            provider_name="Selfhelp Community Services",
        )
        # Eligibility case Met Council only ORIGINATED (didn't manage) -> dropped,
        # because originating no longer counts for non-internal-service cases.
        drop_referred_out = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            originating_provider=met, case_type=CaseType.ELIGIBILITY,
        )

        # Dry run: nothing deleted.
        call_command("delete_non_metcouncil_cases")
        self.assertEqual(Case.objects.count(), 5)

        # Apply: the named external-org case AND the originated-only eligibility
        # case go; the blank-provider meal case is preserved.
        call_command("delete_non_metcouncil_cases", "--apply")
        self.assertTrue(Case.objects.filter(pk=keep_orig.pk).exists())
        self.assertTrue(Case.objects.filter(pk=keep_managed.pk).exists())
        self.assertTrue(Case.objects.filter(pk=keep_blank_internal.pk).exists())
        self.assertFalse(Case.objects.filter(pk=drop.pk).exists())
        self.assertFalse(Case.objects.filter(pk=drop_referred_out.pk).exists())


class ReconcileInternalCasesAgainstExportTest(TestCase):
    """The export-anchored cleanup removes blank-provider internal-service cases
    that are ACTIVE but absent from Met Council's export -- while keeping ones in
    the export, closed history, and any actively-served member's case."""

    def _blank_internal(self, status, **over):
        from .models import Case, CaseStatus, CaseType

        return Case.objects.create(
            case_id=uuid.uuid4(), client=over.pop("client", self.client_obj),
            case_type=CaseType.INTERNAL_SERVICE, case_status=status,
            provider_name="", program_name="MTM Boxes - Queens", **over,
        )

    def _export(self, case_ids):
        import csv
        import tempfile

        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, newline=""
        )
        w = csv.DictWriter(fh, fieldnames=["case_id", "case_status"])
        w.writeheader()
        for cid in case_ids:
            w.writerow({"case_id": str(cid), "case_status": "managed"})
        fh.close()
        return fh.name

    def setUp(self):
        self.client_obj = Client.objects.create(
            client_id=uuid.uuid4(), first_name="Aiden", last_name="Buguia"
        )
        self.served_client = Client.objects.create(
            client_id=uuid.uuid4(), first_name="Served", last_name="Member"
        )

    def test_reconcile_partitions_correctly(self):
        from django.core.management import call_command
        from .models import (
            Case, CaseStatus, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember,
        )

        in_export = self._blank_internal(CaseStatus.MANAGED)
        active_absent = self._blank_internal(CaseStatus.MANAGED)
        closed_absent = self._blank_internal(CaseStatus.CLOSED)

        # A served member's active+absent case must be PROTECTED.
        hh = Household.objects.create()
        HouseholdMember.objects.create(
            household=hh, client=self.served_client, is_primary=True
        )
        EnrollmentVerification.objects.create(
            client=self.served_client, household=hh,
            stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        served_absent = self._blank_internal(
            CaseStatus.MANAGED, client=self.served_client
        )

        export_path = self._export([in_export.case_id])  # only this one is "ours"

        # Dry run changes nothing.
        call_command("reconcile_internal_cases_against_export", "--export", export_path)
        self.assertEqual(Case.objects.count(), 4)

        # Apply: only the active, absent, UN-served case is removed.
        call_command(
            "reconcile_internal_cases_against_export", "--export", export_path, "--apply"
        )
        self.assertTrue(Case.objects.filter(pk=in_export.pk).exists())
        self.assertTrue(Case.objects.filter(pk=closed_absent.pk).exists())
        self.assertTrue(Case.objects.filter(pk=served_absent.pk).exists())
        self.assertFalse(Case.objects.filter(pk=active_absent.pk).exists())


class MemberCaseRemoveTest(TestCase):
    """The Cases tab Remove action: DELETE /members/<id>/cases/<case_id>/ removes
    a NON-Met-Council (external) case but refuses a Met Council case (400), and
    the list serializer exposes ``is_met_council`` so the UI knows when to show
    the button."""

    MET = "12706c81-03a1-4cdb-954a-579929cd05df"

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Case Agent", agent_code="930", group="CS"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        self.client_obj = Client.objects.create(
            client_id=uuid.uuid4(), first_name="Case", last_name="Owner"
        )

    def _case(self, **kwargs):
        from .models import Case, CaseStatus

        return Case.objects.create(
            case_id=uuid.uuid4(), client=self.client_obj,
            case_status=CaseStatus.OPEN, **kwargs,
        )

    def _url(self, case):
        return f"/api/portal/members/{self.client_obj.client_id}/cases/{case.case_id}/"

    def test_detail_list_exposes_is_met_council(self):
        self._case(provider_name="Met Council - SCN - PHS")
        self._case(provider_name="Selfhelp Community Services")
        resp = self.api.get(
            f"/api/portal/members/{self.client_obj.client_id}/cases/?detail=1"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        flags = {row["provider_name"]: row["is_met_council"] for row in resp.json()}
        self.assertTrue(flags["Met Council - SCN - PHS"])
        self.assertFalse(flags["Selfhelp Community Services"])

    def test_remove_external_case(self):
        from .models import Case

        external = self._case(provider_name="Selfhelp Community Services")
        resp = self.api.delete(self._url(external))
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertFalse(Case.objects.filter(pk=external.pk).exists())

    def test_remove_metcouncil_case_refused(self):
        from .models import Case

        managed = self._case(provider_name="Met Council - SCN - PHS")
        resp = self.api.delete(self._url(managed))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(Case.objects.filter(pk=managed.pk).exists())

    def test_remove_metcouncil_by_originating_provider_refused(self):
        from .models import Case, Provider

        met = Provider.objects.create(
            provider_id=self.MET, name="Met Council - SCN - PHS"
        )
        originated = self._case(originating_provider=met)
        resp = self.api.delete(self._url(originated))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(Case.objects.filter(pk=originated.pk).exists())

    def test_remove_blank_provider_meal_case_refused(self):
        # Internal-service meal case with blank provider columns is Met Council's
        # own program -> refused (protects the many meal cases imported without a
        # provider), and the list flags it as Met Council.
        from .models import Case, CaseType

        meal = self._case(case_type=CaseType.INTERNAL_SERVICE, provider_name="")
        resp = self.api.delete(self._url(meal))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(Case.objects.filter(pk=meal.pk).exists())


class ProgramStatusComputationTest(TestCase):
    """The computed per-program status (lifecycle.program_status) folds the
    enrollment stage + governing case authorization into one display value."""

    def _enrollment(self, stage, *, kitchen=None):
        from .models import Client, Household, HouseholdMember, EnrollmentVerification

        client = Client.objects.create(
            client_id=uuid.uuid4(), first_name="P", last_name="Q"
        )
        hh = Household.objects.create()
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        return EnrollmentVerification.objects.create(
            client=client, household=hh, stage=stage, kitchen=kitchen,
        )

    def test_on_hold_overrides_everything(self):
        from .models import EnrollmentStage, ProgramStatus
        from .services.lifecycle import program_status

        enr = self._enrollment(EnrollmentStage.ON_HOLD)
        self.assertEqual(program_status(enr), ProgramStatus.ON_HOLD)

    def test_pending_verification(self):
        from .models import EnrollmentStage, ProgramStatus
        from .services.lifecycle import program_status

        enr = self._enrollment(EnrollmentStage.PENDING_VERIFICATION)
        self.assertEqual(program_status(enr), ProgramStatus.PENDING_VERIFICATION)

    def test_verified_without_authorization_is_verified(self):
        from .models import EnrollmentStage, ProgramStatus
        from .services.lifecycle import program_status

        enr = self._enrollment(EnrollmentStage.VERIFIED)
        self.assertEqual(program_status(enr), ProgramStatus.VERIFIED)

    def test_kitchen_assignment_without_kitchen_is_authorized(self):
        from .models import EnrollmentStage, ProgramStatus
        from .services.lifecycle import program_status

        enr = self._enrollment(EnrollmentStage.KITCHEN_ASSIGNMENT)
        self.assertEqual(program_status(enr), ProgramStatus.AUTHORIZED)

    def test_kitchen_assignment_with_kitchen(self):
        from .models import EnrollmentStage, ProgramStatus, Kitchen
        from .services.lifecycle import program_status

        kitchen = Kitchen.objects.create(name="K1")
        enr = self._enrollment(EnrollmentStage.KITCHEN_ASSIGNMENT, kitchen=kitchen)
        self.assertEqual(program_status(enr), ProgramStatus.KITCHEN_ASSIGNMENT)

    def test_service_active_is_active(self):
        from .models import EnrollmentStage, ProgramStatus
        from .services.lifecycle import program_status

        enr = self._enrollment(EnrollmentStage.SERVICE_ACTIVE)
        self.assertEqual(program_status(enr), ProgramStatus.ACTIVE)

    def test_cancelled_is_closed(self):
        from .models import EnrollmentStage, ProgramStatus
        from .services.lifecycle import program_status

        enr = self._enrollment(EnrollmentStage.CANCELLED)
        self.assertEqual(program_status(enr), ProgramStatus.CLOSED)
