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

    def test_active_no_end_date_clears_stale_expired_at(self):
        # A renewed policy: an old span stored a past end date; the new capture
        # sends it ACTIVE with no end date -> the stale expired_at must be CLEARED
        # (so the date-based eligibility gate stops reading it as expired).
        client, cid = self._client_with(
            dict(
                plan_name="Anthem", external_member_id="1",
                status=RecordStatus.ACTIVE,
                expired_at=timezone.now() - timedelta(days=400),
            )
        )
        self._save({
            "client_id": cid,
            "insurances": [
                {"plan_name": "Anthem", "external_member_id": "1", "status": "active"}
            ],
            "reconcile_insurances": True,
        })
        row = Insurance.objects.get(client=client, plan_name="Anthem")
        self.assertIsNone(row.expired_at)

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


class ExtSaveEligibilityReconcileTest(TestCase):
    """The extension client upsert (ClientViewSet) must run the SAME eligibility
    gates as the CSV import: an ext save that persists bad insurance data SETS
    the INELIGIBLE off-ramp, and a later ext save that fixes it (a never-expiring
    9999 plan) RECOVERS the client. This is the ext half of the recovery-on-fix
    path (perform_create / perform_update / bulk post_upsert)."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Ext Agent", agent_code="920", group="CS"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _client_with_expired(self):
        from datetime import datetime, timezone as dtz

        cid = str(uuid.uuid4())
        client = Client.objects.create(
            client_id=cid, first_name="Ext", last_name="Save",
        )
        Insurance.objects.create(
            client=client, plan_name="Healthfirst PHSP (NY)",
            external_member_id="1",
            expired_at=datetime(1999, 12, 31, tzinfo=dtz.utc),
        )
        return client, cid

    def test_ext_update_sets_ineligible(self):
        from .models import ClientStage

        client, cid = self._client_with_expired()
        resp = self.api.patch(
            f"/api/clients/{cid}/", {"first_name": "Ext2"}, format="json",
        )
        self.assertIn(resp.status_code, (200, 202), resp.content)
        client.refresh_from_db()
        self.assertEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_ext_update_recovers_when_fixed(self):
        from .models import ClientStage

        client, cid = self._client_with_expired()
        self.api.patch(f"/api/clients/{cid}/", {"first_name": "Ext2"}, format="json")
        client.refresh_from_db()
        self.assertEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)
        # A second ext save adds a never-expiring (9999) plan -> recover.
        resp = self.api.patch(
            f"/api/clients/{cid}/",
            {
                "insurances": [{
                    "plan_name": "Healthfirst PHSP (NY)", "external_member_id": "1",
                    "expired_at": "9999-12-31T00:00:00Z",
                }],
                "reconcile_insurances": True,
            },
            format="json",
        )
        self.assertIn(resp.status_code, (200, 202), resp.content)
        client.refresh_from_db()
        self.assertNotEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)


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
        from api.services.lifecycle import MET_COUNCIL_PROVIDER_NAME

        # Managing org for extension case writes (the CaseSerializer gate
        # rejects non-Met-Council / blank-org cases logged by an agent).
        self.mc = MET_COUNCIL_PROVIDER_NAME

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
                "provider_name": self.mc,
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
                "provider_name": self.mc,
                "date_opened": self.now,
            }],
            format="json",
        )
        resp = self.client_api.get(reverse("client-timeline", kwargs={"pk": self.cid}))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["client_id"], self.cid)
        types = {e["event_type"] for e in body["results"]}
        # The ext client-save now runs the eligibility gate (parity with the CSV
        # import): this client has no medical insurance on file, so it is marked
        # Ineligible and a member_ineligible event is emitted alongside the
        # consent + case events.
        self.assertSetEqual(
            types, {"consent_granted", "case_opened", "member_ineligible"}
        )
        occurred = [e["occurred_at"] for e in body["results"]]
        self.assertEqual(occurred, sorted(occurred, reverse=True))


class KitchenAndDietaryTimelineBuilderTest(TestCase):
    """Unit-level coverage for the kitchen/cadence-change and dietary-change
    timeline builders: they write a precise before -> after diff when something
    changed and are a no-op otherwise."""

    def setUp(self):
        from .models import EnrollmentVerification

        self.client_obj = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ada", last_name="Lovelace"
        )
        self.enr = EnrollmentVerification.objects.create(client=self.client_obj)

    def _events(self, event_type):
        return TimelineEvent.objects.filter(
            client=self.client_obj, event_type=event_type
        )

    def test_kitchen_changed_logs_diff(self):
        from .services import timeline

        ev = timeline.event_for_kitchen_changed(
            self.enr, previous_kitchen="Old Kitchen", new_kitchen="New Kitchen",
            previous_cadence="Weekly", new_cadence="Biweekly", actor="agent:1",
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.title, "Kitchen Changed")
        self.assertEqual(ev.badge_text, "New Kitchen")
        fields = {c["field"]: (c["from"], c["to"]) for c in ev.metadata["changes"]}
        self.assertEqual(fields["Kitchen"], ("Old Kitchen", "New Kitchen"))
        self.assertEqual(fields["Cadence"], ("Weekly", "Biweekly"))

    def test_kitchen_changed_noop_when_unchanged(self):
        from .services import timeline

        ev = timeline.event_for_kitchen_changed(
            self.enr, previous_kitchen="Same", new_kitchen="Same",
            previous_cadence="Weekly", new_cadence="Weekly",
        )
        self.assertIsNone(ev)
        self.assertFalse(self._events("kitchen_assigned").exists())

    def test_dietary_changed_logs_diff_on_member(self):
        from .models import MemberDietaryProfile
        from .services import timeline

        profile = MemberDietaryProfile.objects.create(
            enrollment=self.enr, client=self.client_obj, member_name="Ada",
        )
        changes = timeline.build_change_list([
            ("Food allergies", ["peanuts"], ["peanuts", "shellfish"]),
            ("Menu type", "Standard", "Standard"),  # unchanged -> dropped
        ])
        ev = timeline.event_for_dietary_changed(
            profile, changes=changes, enrollment=self.enr, actor="agent:1",
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.title, "Dietary Info Updated")
        self.assertEqual([c["field"] for c in ev.metadata["changes"]], ["Food allergies"])

    def test_dietary_changed_noop_when_empty(self):
        from .models import MemberDietaryProfile
        from .services import timeline

        profile = MemberDietaryProfile.objects.create(
            enrollment=self.enr, client=self.client_obj, member_name="Ada",
        )
        ev = timeline.event_for_dietary_changed(
            profile, changes=[], enrollment=self.enr,
        )
        self.assertIsNone(ev)
        self.assertFalse(self._events("dietary_changed").exists())

    def test_delivery_address_change_logs_notes_only_edit(self):
        # Regression: a notes-only edit (the one-line formatted address is
        # unchanged) must still log, via the explicit per-field diff.
        from .models import Address
        from .services import timeline

        addr = Address.objects.create(
            client=self.client_obj, type="temporary",
            street="1 Main St", city="Minneapolis", state="MN", zip="55401",
        )
        changes = timeline.build_change_list([
            ("Street", "1 Main St", "1 Main St"),  # unchanged -> dropped
            ("Delivery notes", "", "Leave at front desk"),
        ])
        ev = timeline.event_for_delivery_address_change(
            self.client_obj, addr, previous="1 Main St, Minneapolis, MN 55401",
            changes=changes, enrollment=self.enr, actor="agent:1",
        )
        self.assertIsNotNone(ev)
        self.assertEqual([c["field"] for c in ev.metadata["changes"]], ["Delivery notes"])

    def test_delivery_address_change_noop_when_unchanged(self):
        from .models import Address
        from .services import timeline

        addr = Address.objects.create(
            client=self.client_obj, type="temporary", street="1 Main St",
        )
        ev = timeline.event_for_delivery_address_change(
            self.client_obj, addr, previous="1 Main St", changes=[],
            enrollment=self.enr,
        )
        self.assertIsNone(ev)
        self.assertFalse(self._events("delivery_address_changed").exists())


class ScreeningAssessmentTimelineMetadataTest(TestCase):
    """The screening/assessment timeline builders record WHAT the member is
    eligible for (eligible_services) and the screening results (social needs +
    Q&A count), and eligibility that arrives late (via the enrichment pull) is
    re-synced onto the already-created, create-once event."""

    def setUp(self):
        self.client_obj = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ada", last_name="Lovelace"
        )

    def _screening(self, **kwargs):
        from .models import Screening

        defaults = dict(
            enhanced_screen_id=uuid.uuid4(),
            subject_id=uuid.uuid4(),
            client=self.client_obj,
            screen_status="completed",
            screen_type="SCN",
        )
        defaults.update(kwargs)
        return Screening.objects.create(**defaults)

    def _assessment(self, **kwargs):
        from .models import Assessment

        defaults = dict(
            assessment_id=uuid.uuid4(),
            subject_id=uuid.uuid4(),
            client=self.client_obj,
            eligible_status="Eligible",
        )
        defaults.update(kwargs)
        return Assessment.objects.create(**defaults)

    def test_screening_event_records_eligibility_and_results(self):
        from .services import timeline

        scr = self._screening(
            eligible_status="Eligible",
            eligible_services=["Medicaid", {"name": "SNAP"}],
            identified_social_needs=["Food", {"identified_social_need_name": "Housing"}],
            questions_answers=[{"question": "Q1", "answer": "A1"}],
        )
        ev = timeline.event_for_screening(scr)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.metadata["eligible_services"], ["Medicaid", "SNAP"])
        self.assertEqual(ev.metadata["identified_social_needs"], ["Food", "Housing"])
        self.assertEqual(ev.metadata["eligible_status"], "Eligible")
        self.assertEqual(ev.metadata["results_count"], 1)

    def test_assessment_event_records_eligibility(self):
        from .services import timeline

        asmt = self._assessment(
            eligible_services=["Enhanced Care Management (Level 2)"],
            form_name="Food Assistance Assessment",
            questions_answers=[{"question": "Q1", "answer": "A1"}],
        )
        ev = timeline.event_for_assessment(asmt)
        self.assertIsNotNone(ev)
        self.assertEqual(
            ev.metadata["eligible_services"], ["Enhanced Care Management (Level 2)"]
        )
        self.assertEqual(ev.metadata["form_name"], "Food Assistance Assessment")
        self.assertEqual(ev.metadata["results_count"], 1)

    def test_late_eligibility_resyncs_onto_existing_event(self):
        from .models import TimelineEvent
        from .services import timeline

        # 1) CSV-style create: no eligible_services yet.
        asmt = self._assessment(eligible_services=[])
        ev1 = timeline.event_for_assessment(asmt)
        self.assertEqual(ev1.metadata["eligible_services"], [])

        # 2) Enrichment lands eligibility, re-emit with resync=True.
        asmt.eligible_services = ["Medicaid"]
        asmt.save(update_fields=["eligible_services"])
        ev2 = timeline.event_for_assessment(
            asmt, source="import", actor="system:assessment-results", resync=True
        )
        # Same create-once row, metadata now back-filled.
        self.assertEqual(ev2.pk, ev1.pk)
        self.assertEqual(ev2.metadata["eligible_services"], ["Medicaid"])
        self.assertEqual(
            TimelineEvent.objects.filter(
                dedupe_key=f"assessment:{asmt.pk}"
            ).count(),
            1,
        )

    def test_without_resync_create_once_preserves_original_metadata(self):
        from .services import timeline

        asmt = self._assessment(eligible_services=[])
        ev1 = timeline.event_for_assessment(asmt)
        asmt.eligible_services = ["Medicaid"]
        asmt.save(update_fields=["eligible_services"])
        # Default resync=False: the existing row is left untouched.
        ev2 = timeline.event_for_assessment(asmt)
        self.assertEqual(ev2.pk, ev1.pk)
        ev2.refresh_from_db()
        self.assertEqual(ev2.metadata["eligible_services"], [])


class VerificationEventTimelineTest(TestCase):
    """The verification events record who + governing case + member roster
    (requested/disregarded) and a full per-member clinical snapshot + governing
    case status/auth at save time (completed)."""

    def setUp(self):
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, EnrollmentVerification,
            ServiceAuthorizationStatus,
        )

        self.client_obj = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ada", last_name="Lovelace"
        )
        self.case = Case.objects.create(
            case_id=uuid.uuid4(), client=self.client_obj,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.MANAGED,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals (MTM)",
        )
        self.enr = EnrollmentVerification.objects.create(
            client=self.client_obj, case=self.case,
            stage=EnrollmentStage.PENDING_VERIFICATION,
            program_name="Medically Tailored Meals (MTM)",
        )

    def _member(self, **kwargs):
        from .models import MemberDietaryProfile

        defaults = dict(
            enrollment=self.enr, client=self.client_obj, member_name="Ada Lovelace",
        )
        defaults.update(kwargs)
        return MemberDietaryProfile.objects.create(**defaults)

    def test_requested_event_has_governing_case_and_members(self):
        from .services import timeline

        self._member()
        ev = timeline.event_for_verification(self.enr, actor="agent:1")
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "verification_requested")
        self.assertEqual(ev.metadata["governing_case_id"], str(self.case.case_id))
        self.assertIn("Ada Lovelace", ev.metadata["members"])
        self.assertEqual(ev.actor, "agent:1")

    def test_completed_event_snapshots_member_clinical_detail(self):
        from .services import timeline

        self._member(
            conditions=["Diabetes"], medications=["Insulin"], weight="180 lb",
            height="5ft 9in", on_medical_diet=True, medical_diet_details="Low sodium",
            menu_type="Standard", food_allergies=["peanuts"],
            other_dietary_restrictions="No pork", general_verification_notes="ok",
        )
        ev = timeline.event_for_verification_submitted(
            self.enr,
            delivery_address="1 Main St, NY",
            governing_case_id=str(self.case.case_id),
            case_status=self.case.case_status,
            auth_status=self.case.service_authorization_status,
            actor="agent:1",
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "verification_completed")
        md = ev.metadata
        self.assertEqual(md["governing_case_id"], str(self.case.case_id))
        self.assertEqual(md["case_status"], self.case.case_status)
        self.assertEqual(md["authorization_status"], self.case.service_authorization_status)
        m = md["members"][0]
        self.assertEqual(m["conditions"], ["Diabetes"])
        self.assertEqual(m["medications"], ["Insulin"])
        self.assertEqual(m["weight"], "180 lb")
        self.assertEqual(m["height"], "5ft 9in")
        self.assertTrue(m["on_medical_diet"])
        self.assertEqual(m["medical_diet_details"], "Low sodium")
        self.assertEqual(m["menu_type"], "Standard")
        self.assertEqual(m["food_allergies"], ["peanuts"])
        self.assertEqual(m["other_dietary_restrictions"], "No pork")
        self.assertEqual(m["general_verification_notes"], "ok")

    def test_completed_snapshot_frozen_against_later_profile_edit(self):
        from .models import MemberDietaryProfile
        from .services import timeline

        p = self._member(menu_type="Standard")
        ev = timeline.event_for_verification_submitted(self.enr, actor="agent:1")
        # Edit the live profile afterward -> the event snapshot must NOT change.
        p.menu_type = "Kosher"
        p.save(update_fields=["menu_type"])
        ev.refresh_from_db()
        self.assertEqual(ev.metadata["members"][0]["menu_type"], "Standard")

    def test_product_type_changed_records_before_after_and_dedupes(self):
        from .services import timeline

        key = f"product_type_changed:{self.client_obj.pk}:a:b"
        ev1 = timeline.event_for_product_type_changed(
            self.enr, previous_label="Meals", new_label="Boxes", dedupe_key=key,
        )
        self.assertIsNotNone(ev1)
        self.assertEqual(ev1.event_type, "product_type_changed")
        self.assertEqual(ev1.metadata, {"previous": "Meals", "new": "Boxes"})
        # Same switch re-run -> create-once (no duplicate row).
        ev2 = timeline.event_for_product_type_changed(
            self.enr, previous_label="Meals", new_label="Boxes", dedupe_key=key,
        )
        self.assertEqual(ev2.pk, ev1.pk)


class GoverningCaseReplacementVerificationGateTest(TestCase):
    """Regression: a governing-case replacement must NOT promote an UNVERIFIED
    household. Previously a pending-verification enrollment was force-stepped to
    Kitchen Assignment when a new governing case was approved -- skipping the
    verification gate. An approved new case must never bypass verification."""

    def setUp(self):
        self.client_obj = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pat", last_name="Pending",
        )

    def _carry(self, *, verified):
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, EnrollmentVerification,
            ProductTypeKind, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import _carry_service_and_activate

        prior = EnrollmentVerification.objects.create(
            client=self.client_obj, stage=EnrollmentStage.PENDING_VERIFICATION,
            program_name="MTM Meals",
        )
        new_case = Case.objects.create(
            case_id=uuid.uuid4(), client=self.client_obj,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.MANAGED,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="MTM Meals",
        )
        now = timezone.now()
        new_enr = EnrollmentVerification.objects.create(
            client=self.client_obj, case=new_case,
            stage=EnrollmentStage.PENDING_VERIFICATION, program_name="MTM Meals",
            verified_at=(now if verified else None),
            # A verified household is also nutritionist-approved here so it clears
            # BOTH gates and advances to Kitchen Assignment (the nutri gate is
            # covered separately in CaseSwitchClosesOldAndRespectsNutriGateTest).
            nutritionist_approved_at=(now if verified else None),
        )
        carried = _carry_service_and_activate(
            new_enr, prior, new_case, ProductTypeKind.MEALS,
            prior_was_serving=False, actor=None, actor_label="",
        )
        new_enr.refresh_from_db()
        return carried, new_enr

    def test_unverified_stays_pending_verification(self):
        from .models import EnrollmentStage

        carried, new_enr = self._carry(verified=False)
        self.assertFalse(carried)
        self.assertEqual(new_enr.stage, EnrollmentStage.PENDING_VERIFICATION)
        self.assertIsNone(new_enr.verified_at)

    def test_verified_advances_to_kitchen_assignment(self):
        from .models import EnrollmentStage

        # A verified + nutritionist-approved household advances to Kitchen
        # Assignment (both gates cleared), even with no prior serving state.
        carried, new_enr = self._carry(verified=True)
        self.assertFalse(carried)  # prior wasn't serving -> no service carried
        self.assertEqual(new_enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)


class CaseInsuranceCoverageTimelineMetadataTest(TestCase):
    """Case / Insurance / Social Care Coverage timeline events record the audit
    fields the manager history needs: product kind (meals/boxes), governing
    flag, authorization status + window (case); Medicaid-rule + status + expiry
    (insurance); status + expiry (coverage)."""

    def setUp(self):
        self.client_obj = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ada", last_name="Lovelace"
        )
        self.now = timezone.now()

    def _case(self, **kwargs):
        from .models import Case, CaseType

        defaults = dict(
            case_id=uuid.uuid4(),
            client=self.client_obj,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status="managed",
            program_name="Medically Tailored Meals (MTM)",
            date_opened=self.now,
        )
        defaults.update(kwargs)
        return Case.objects.create(**defaults)

    def test_case_event_records_kind_governing_and_auth_window(self):
        from datetime import timedelta

        from .models import ServiceAuthorizationStatus
        from .services import timeline

        start = self.now
        end = self.now + timedelta(days=365)
        case = self._case(
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=start,
            service_authorization_approval_ends_at=end,
            authorized_amount="$8,736.00",
        )
        ev = timeline.event_for_case(case)
        self.assertIsNotNone(ev)
        md = ev.metadata
        self.assertEqual(md["product_kind"], "meals")
        self.assertTrue(md["is_governing"])  # the only open internal case
        self.assertEqual(md["auth_status"], ServiceAuthorizationStatus.APPROVED)
        self.assertEqual(md["auth_window_start"], start.isoformat())
        self.assertEqual(md["auth_window_end"], end.isoformat())

    def test_case_event_metadata_resyncs_auth_status(self):
        from .models import ServiceAuthorizationStatus
        from .services import timeline

        case = self._case(
            service_authorization_status=ServiceAuthorizationStatus.PENDING
        )
        ev1 = timeline.event_for_case(case)
        self.assertEqual(ev1.metadata["auth_status"], ServiceAuthorizationStatus.PENDING)
        # Authorization is later approved; re-emitting updates the existing row.
        case.service_authorization_status = ServiceAuthorizationStatus.APPROVED
        case.save(update_fields=["service_authorization_status"])
        ev2 = timeline.event_for_case(case)
        self.assertEqual(ev2.pk, ev1.pk)
        self.assertEqual(ev2.metadata["auth_status"], ServiceAuthorizationStatus.APPROVED)

    def test_insurance_event_records_medicaid_rule_and_expiry(self):
        from .models import Insurance, InsurancePlanType, RecordStatus
        from .services import timeline

        ins = Insurance.objects.create(
            client=self.client_obj,
            plan_type=InsurancePlanType.MEDICAID,
            plan_name="NY Medicaid",
            status=RecordStatus.ACTIVE,
            enrolled_at=self.now,
        )
        ev = timeline.event_for_insurance(ins)
        self.assertIsNotNone(ev)
        md = ev.metadata
        self.assertEqual(md["plan_type"], InsurancePlanType.MEDICAID)
        self.assertEqual(md["status"], RecordStatus.ACTIVE)
        self.assertFalse(md["expired"])
        self.assertIn("meets_medicaid_rule", md)

    def test_coverage_event_records_status_and_expiry(self):
        from datetime import timedelta

        from .models import SocialCareCoverage, SocialCareCoverageStatus
        from .services import timeline

        cov = SocialCareCoverage.objects.create(
            client=self.client_obj,
            plan_name="MLTC Plan",
            status=SocialCareCoverageStatus.EXPIRED,
            enrolled_at=self.now - timedelta(days=400),
            expired_at=self.now - timedelta(days=1),
        )
        ev = timeline.event_for_social_care_coverage(cov)
        self.assertIsNotNone(ev)
        md = ev.metadata
        self.assertEqual(md["status"], SocialCareCoverageStatus.EXPIRED)
        self.assertTrue(md["expired"])
        self.assertEqual(md["expired_at"], cov.expired_at.isoformat())

    def test_member_ineligible_tags_reason_causes(self):
        from .services import timeline

        ev = timeline.event_for_member_ineligible(
            self.client_obj,
            reasons=[
                "no medical insurance on file",
                "Medicaid plan type not served (MLTC/MAP/FFS): MLTC",
                "delivery ZIP 10001 is outside the coverage area",
            ],
        )
        self.assertIsNotNone(ev)
        causes = {rc["cause"] for rc in ev.metadata["reason_causes"]}
        self.assertEqual(causes, {"insurance", "medicaid_type", "address"})
        self.assertEqual(ev.metadata["causes"], ["address", "insurance", "medicaid_type"])


class CareManagementNoCadenceQuerysetTest(TestCase):
    """The Care Management 'No Cadence' tab lists members on a live enrollment
    assigned to a chosen kitchen that still has no delivery cadence."""

    def setUp(self):
        from .models import Kitchen, KitchenStatus

        self.client_obj = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ada", last_name="Lovelace"
        )
        self.kitchen = Kitchen.objects.create(name="K1", status=KitchenStatus.ACTIVE)
        self.other_kitchen = Kitchen.objects.create(name="K2", status=KitchenStatus.ACTIVE)

    def _qs_ids(self, kitchen_id):
        from .portal.views_members import care_management_no_cadence_qs

        return {
            str(cid)
            for cid in care_management_no_cadence_qs(
                Client.objects.all(), str(kitchen_id)
            ).values_list("client_id", flat=True)
        }

    def test_no_kitchen_returns_nothing(self):
        from .models import EnrollmentStage, EnrollmentVerification

        EnrollmentVerification.objects.create(
            client=self.client_obj, kitchen=self.kitchen,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        self.assertEqual(self._qs_ids(""), set())

    def test_kitchen_no_cadence_includes_member(self):
        from .models import EnrollmentStage, EnrollmentVerification

        EnrollmentVerification.objects.create(
            client=self.client_obj, kitchen=self.kitchen,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        self.assertIn(str(self.client_obj.client_id), self._qs_ids(self.kitchen.pk))
        # A different kitchen must not include them.
        self.assertNotIn(str(self.client_obj.client_id), self._qs_ids(self.other_kitchen.pk))

    def test_member_with_cadence_is_excluded(self):
        from .models import (
            DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            MemberDeliverySchedule, ScheduleStatus,
        )

        enr = EnrollmentVerification.objects.create(
            client=self.client_obj, kitchen=self.kitchen,
            stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_name="Ada",
            delivery_days_cadence=DeliveryCadence.MON_THU,
            status=ScheduleStatus.SCHEDULED,
        )
        self.assertNotIn(str(self.client_obj.client_id), self._qs_ids(self.kitchen.pk))


class CareManagementRejectedCaseGoverningTest(TestCase):
    """The 'Rejected Case' tab must key off the GOVERNING internal-service case
    and only when it's OPEN: an open denied case flags the household, but an open
    approved case (which would govern instead) or a merely CLOSED denial does not."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="C", last_name="X"
        )

    def _case(self, client, *, auth, status):
        from .models import Case, CaseType

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=status, service_authorization_status=auth,
        )

    def _rejected_ids(self):
        from .portal.views_members import (
            annotate_care_management, care_management_base_q,
            care_management_tab_queryset,
        )

        qs = annotate_care_management(
            Client.objects.filter(care_management_base_q()).distinct()
        )
        qs = care_management_tab_queryset(qs, "rejected_case")
        return {str(cid) for cid in qs.values_list("client_id", flat=True)}

    def test_open_denied_no_favorable_is_rejected(self):
        from .models import CaseStatus, ServiceAuthorizationStatus

        c = self._client()
        self._case(c, auth=ServiceAuthorizationStatus.DENIED, status=CaseStatus.MANAGED)
        self.assertIn(str(c.client_id), self._rejected_ids())

    def test_open_denied_but_open_approved_not_rejected(self):
        from .models import CaseStatus, ServiceAuthorizationStatus

        c = self._client()
        self._case(c, auth=ServiceAuthorizationStatus.DENIED, status=CaseStatus.MANAGED)
        # An OPEN approved case outranks the denial and would govern instead.
        self._case(c, auth=ServiceAuthorizationStatus.APPROVED, status=CaseStatus.MANAGED)
        self.assertNotIn(str(c.client_id), self._rejected_ids())

    def test_closed_denied_not_rejected(self):
        from .models import CaseStatus, ServiceAuthorizationStatus

        c = self._client()
        # Only a CLOSED denial -> no OPEN governing denial, so not "rejected".
        self._case(c, auth=ServiceAuthorizationStatus.DENIED, status=CaseStatus.CLOSED)
        self.assertNotIn(str(c.client_id), self._rejected_ids())


class CareManagementClosedCaseExclusionTest(TestCase):
    """The Care Management page (all tabs, incl. Out of Range) excludes a member
    whose governing internal-service case is CLOSED -- the household finished
    service and should drop off the queue."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent
        a = Agent.objects.create(name="CM", agent_code="951", group="Management")
        acc = AccessToken()
        acc["agent_id"] = str(a.id); acc["agent_code"] = a.agent_code
        acc["agent_name"] = a.name; acc["agent_group"] = a.group
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION=f"Bearer {acc}")
        return api

    def _oor_member(self, first, *, case_status):
        from .models import (
            Case, CaseStatus, CaseType, Client, ClientStage, Household,
            HouseholdMember,
        )
        from .services.service_area import SERVICE_AREA_REASON

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name="Oor",
            lifecycle_stage=ClientStage.INELIGIBLE,
            ineligible_reasons=[SERVICE_AREA_REASON],
        )
        hh = Household.objects.create(name=f"{first} HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=case_status,
        )
        return c

    def _ids(self, resp):
        out = set()
        for g in resp.json()["results"]:
            out.add(g["primary"]["id"])
            out.update(m["id"] for m in g.get("members", []))
        return out

    def test_out_of_range_excludes_closed_governing_case(self):
        from .models import CaseStatus

        api = self._api()
        openc = self._oor_member("Opal", case_status=CaseStatus.OPEN)
        closed = self._oor_member("Clyde", case_status=CaseStatus.CLOSED)
        ids = self._ids(
            api.get("/api/portal/members/?scope=care_management&tab=out_of_range")
        )
        self.assertIn(str(openc.client_id), ids)      # open case -> shown
        self.assertNotIn(str(closed.client_id), ids)  # closed case -> excluded


class MemberHouseholdAddressTimelineTest(TestCase):
    """PATCH /members/<id>/household/ must log a Delivery Address Changed event
    on the timeline of the member whose profile the agent is viewing -- including
    when that member is a NON-primary household member (who has no enrollment of
    their own and falls back to the household/primary enrollment)."""

    def _api(self, group="CS"):
        agent = Agent.objects.create(name="Casey CS", agent_code="900", group=group)
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def _setup_household(self):
        from .models import (
            Address, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, MemberDietaryProfile,
        )
        primary = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pat", last_name="Primary",
        )
        dep = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dee", last_name="Dependent",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        addr = Address.objects.create(
            client=primary, type="temporary", street="1 Main St",
            city="Minneapolis", state="MN", zip="55401",
        )
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            delivery_address=addr,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=primary)
        MemberDietaryProfile.objects.create(enrollment=enr, client=dep)
        return primary, dep, enr

    def _events(self, client):
        return TimelineEvent.objects.filter(
            client=client, event_type="delivery_address_changed"
        )

    def test_address_edit_from_primary_logs_event(self):
        primary, dep, enr = self._setup_household()
        resp = self._api().patch(
            f"/api/portal/members/{primary.pk}/household/",
            {"unit": "Apt 5"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(self._events(primary).exists())

    def test_address_edit_from_dependent_logs_on_household_owner(self):
        # Editing the (household-wide) address while viewing a non-primary member
        # still records the event -- on the household's enrollment owner (primary),
        # since that's who the shared delivery address belongs to.
        primary, dep, enr = self._setup_household()
        resp = self._api().patch(
            f"/api/portal/members/{dep.pk}/household/",
            {"unit": "Apt 9"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(self._events(primary).exists())


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


class AssignKitchenFromVerifiedTest(TestCase):
    """assign_kitchen_to_household activates a household straight from VERIFIED
    (the Williamsburg fast-track in the verification pop-up, which skips the
    manual Logistics kitchen-assignment step).

    Regression: the helper advanced VERIFIED -> SERVICE_ACTIVE directly, but the
    transition map has no such edge, so it raised InvalidTransition (500 on the
    verification save). It must route through KITCHEN_ASSIGNMENT first.
    """

    def test_activates_from_verified_via_kitchen_assignment(self):
        from unittest.mock import patch

        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Kitchen, KitchenStatus,
        )
        from .portal import views_members as vm

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Will", last_name="Burg",
        )
        kitchen = Kitchen.objects.create(
            name="Williamsburg", status=KitchenStatus.ACTIVE,
        )
        # No member profiles -> the per-member meal-rule loop is skipped, so the
        # test isolates the stage-transition behavior (the actual bug).
        enr = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.VERIFIED,
        )

        # Stub the delivery-plan side effects: this test only asserts the stage
        # routing, not schedule/calendar building.
        with patch.object(vm, "create_member_delivery_schedules", return_value=[1]), \
                patch.object(vm, "generate_delivery_calendar"), \
                patch.object(vm, "resync_scheduled_orders"), \
                patch.object(vm, "sync_household_warnings"):
            vm.assign_kitchen_to_household(
                enr, client, kitchen, cadence=DeliveryCadence.MON_THU,
            )

        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)
        self.assertEqual(enr.kitchen_id, kitchen.pk)

    def test_assign_kitchen_rejected_before_verification(self):
        # Regression: assigning a kitchen to a member still at PENDING_VERIFICATION
        # force-advanced to SERVICE_ACTIVE with no valid transition -> a 500. It
        # must return a clean 400 telling the agent to verify first.
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import (
            Agent, Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, Kitchen, KitchenStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pend", last_name="Ver",
            lifecycle_stage="pending_verification",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        kitchen = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        agent = Agent.objects.create(name="Mgr", agent_code="961", group="Management")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        r = api.post(
            f"/api/portal/members/{client.client_id}/assign-kitchen/",
            {"kitchen_id": str(kitchen.pk), "cadence": "tue_only", "member_overrides": []},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("verification", r.json()["error"].lower())


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
        # First save as pending -> NOT approved, so the pull-back rule returns
        # the enrollment to Verified ("Waiting Authorization"): only an approval
        # keeps a household past Verified.
        self._save_case(client, case_id, "pending")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.VERIFIED)

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

    def test_pending_verification_denied_is_disregarded(self):
        # Objective 3 / task 4.1: a denial while the member is still only at
        # Pending Verification (no service yet) removes the verification request
        # -> the enrollment is DISREGARDED, moving them off the Verification queue.
        from .models import EnrollmentStage, EnrollmentVerification, Household, HouseholdMember

        client = self._client()
        household = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=household, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=household,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        self._save_case(client, str(uuid.uuid4()), "denied")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.DISREGARDED)


class GoverningCaseChangeTest(TestCase):
    """The case reconcile records when a household's GOVERNING internal-service
    case changes (old -> new): it stamps ``Client.governing_internal_case_id``
    and emits a 'Governing Case Changed' timeline event. The FIRST governing
    case to land is recorded SILENTLY (no prior case to switch from)."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Gov", last_name="Case"
        )

    def _save_case(self, client, case_id, auth_status, created_at):
        from .serializers import CaseSerializer

        data = {
            "case_id": case_id,
            "client_id": str(client.client_id),
            "case_type": "internal_service",
            "program_name": "Medically Tailored Meals",
            "service_authorization_status": auth_status,
            "date_opened": created_at.isoformat(),
            "case_created_at": created_at.isoformat(),
        }
        ser = CaseSerializer(data=data)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_first_case_is_silent_then_switch_is_recorded(self):
        from datetime import timedelta

        from .models import TimelineEvent, TimelineEventType

        client = self._client()
        now = timezone.now()
        case_a = str(uuid.uuid4())
        case_b = str(uuid.uuid4())

        # First governing case lands -> pointer set, NO switch event.
        self._save_case(client, case_a, "approved", now - timedelta(days=2))
        client.refresh_from_db()
        self.assertEqual(client.governing_internal_case_id, case_a)
        self.assertFalse(
            TimelineEvent.objects.filter(
                event_type=TimelineEventType.MEMBER_GOVERNING_CASE_CHANGED
            ).exists()
        )

        # A newer approved case supersedes A -> pointer switches + event fires.
        self._save_case(client, case_b, "approved", now)
        client.refresh_from_db()
        self.assertEqual(client.governing_internal_case_id, case_b)
        events = TimelineEvent.objects.filter(
            event_type=TimelineEventType.MEMBER_GOVERNING_CASE_CHANGED
        )
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().metadata["previous_case_id"], case_a)
        self.assertEqual(events.first().metadata["new_case_id"], case_b)

    def test_switch_event_is_recorded_once(self):
        from datetime import timedelta

        from .models import TimelineEvent, TimelineEventType

        client = self._client()
        now = timezone.now()
        case_a = str(uuid.uuid4())
        case_b = str(uuid.uuid4())
        self._save_case(client, case_a, "approved", now - timedelta(days=2))
        self._save_case(client, case_b, "approved", now)
        # Re-saving the governing case (unchanged old->new pair) must not dupe.
        self._save_case(client, case_b, "approved", now)
        self.assertEqual(
            TimelineEvent.objects.filter(
                event_type=TimelineEventType.MEMBER_GOVERNING_CASE_CHANGED
            ).count(),
            1,
        )


class GoverningCaseKeyTest(TestCase):
    """Task 6.1: governing_case_key ordering -- an APPROVED authorization beats
    anything regardless of dates; among equally-favorable cases the most recent
    case_created_at wins; and OPEN outranks closed at the same favor + date."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Key", last_name="Order"
        )

    def _case(self, client, *, auth, status="open", created):
        from .models import Case, CaseType

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            program_name="Medically Tailored Meals",
            service_authorization_status=auth, case_status=status,
            case_created_at=created, date_opened=created,
        )

    def test_approved_beats_pending_regardless_of_date(self):
        from datetime import timedelta

        from .services.lifecycle import governing_case_key

        client = self._client()
        older_approved = self._case(
            client, auth="approved", created=timezone.now() - timedelta(days=10)
        )
        newer_pending = self._case(client, auth="pending", created=timezone.now())
        gov = max([newer_pending, older_approved], key=governing_case_key)
        self.assertEqual(gov.pk, older_approved.pk)

    def test_newer_created_wins_among_approved(self):
        from datetime import timedelta

        from .services.lifecycle import governing_case_key

        client = self._client()
        older = self._case(
            client, auth="approved", created=timezone.now() - timedelta(days=5)
        )
        newer = self._case(client, auth="approved", created=timezone.now())
        gov = max([older, newer], key=governing_case_key)
        self.assertEqual(gov.pk, newer.pk)

    def test_open_beats_closed_at_same_favor_and_date(self):
        from .services.lifecycle import governing_case_key

        client = self._client()
        created = timezone.now()
        closed = self._case(
            client, auth="approved", status="closed", created=created
        )
        open_case = self._case(
            client, auth="approved", status="open", created=created
        )
        gov = max([closed, open_case], key=governing_case_key)
        self.assertEqual(gov.pk, open_case.pk)


class BulkCaseReconcileDeferralTest(TestCase):
    """Phase 5: the extension /cases/bulk/ endpoint must reconcile the client-wide
    internal-service state ONCE on the COMPLETE case picture, not per row. A
    payload writing a CLOSED case before its OPEN+approved successor must NOT
    hard off-ramp the (Kitchen Assignment) client to INELIGIBLE off the partial
    picture -- the final governing case is the open+approved one."""

    def _api(self):
        agent = Agent.objects.create(
            name="Ext Agent", agent_code=str(uuid.uuid4())[:8], group="Screeners"
        )
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def test_bulk_reconciles_once_on_full_picture(self):
        from django.urls import reverse

        from api.services.lifecycle import MET_COUNCIL_PROVIDER_NAME
        from .models import (
            ClientStage, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember,
        )

        mc = MET_COUNCIL_PROVIDER_NAME
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Bulk", last_name="Case"
        )
        household = Household.objects.create(name="HH")
        HouseholdMember.objects.create(
            household=household, client=client, is_primary=True
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=household,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT, verified_at=timezone.now(),
        )

        # One payload: a CLOSED case FIRST, then a newer OPEN + approved case.
        # Deferred reconcile => only the full-picture governing (open+approved)
        # is acted on, so the client is NOT stranded at INELIGIBLE.
        api = self._api()
        resp = api.post(
            reverse("case-bulk"),
            [
                {
                    "case_id": str(uuid.uuid4()),
                    "client_id": str(client.client_id),
                    "case_type": "internal_service",
                    "program_name": "Medically Tailored Meals",
                    "service_authorization_status": "approved",
                    "case_status": "closed",
                    "provider_name": mc,
                    "date_opened": timezone.now().isoformat(),
                    "case_closed_at": timezone.now().isoformat(),
                },
                {
                    "case_id": str(uuid.uuid4()),
                    "client_id": str(client.client_id),
                    "case_type": "internal_service",
                    "program_name": "Medically Tailored Meals",
                    "service_authorization_status": "approved",
                    "case_status": "open",
                    "provider_name": mc,
                    "date_opened": timezone.now().isoformat(),
                },
            ],
            format="json",
        )
        self.assertIn(resp.status_code, (200, 207), resp.content)
        client.refresh_from_db()
        enr.refresh_from_db()
        # Governing = the open + approved case -> served, NOT ineligible/inactive.
        self.assertNotEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)
        self.assertNotEqual(client.lifecycle_stage, ClientStage.SERVICE_INACTIVE)
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)


class ProgramSwitchRequeueTest(TestCase):
    """Task 3.1: when the GOVERNING internal-service case switches product KIND
    (meals<->boxes) to an authorized, still-open successor, the household is
    requeued for a NEW kitchen assignment -- future deliveries stopped, the
    kitchen + delivery cadence cleared, every servable enrollment moved to
    Kitchen Assignment, and a 'Program Switched' timeline event + primary
    system note written. Idempotent on re-save."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pro", last_name="Switch"
        )

    def _enrollment(self, client, stage, *, kitchen=None, weekdays=None):
        from .models import EnrollmentVerification, Household, HouseholdMember

        household = Household.objects.create(name="HH")
        HouseholdMember.objects.create(
            household=household, client=client, is_primary=True
        )
        return EnrollmentVerification.objects.create(
            client=client, household=household, stage=stage,
            verified_at=timezone.now(), kitchen=kitchen,
            delivery_weekdays=weekdays or [],
        )

    def _save_case(self, client, case_id, program_name, created_at):
        from .serializers import CaseSerializer

        data = {
            "case_id": case_id,
            "client_id": str(client.client_id),
            "case_type": "internal_service",
            "program_name": program_name,
            "service_authorization_status": "approved",
            "case_status": "open",
            "date_opened": created_at.isoformat(),
            "case_created_at": created_at.isoformat(),
        }
        ser = CaseSerializer(data=data)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_meals_to_boxes_switch_requeues_household(self):
        from datetime import timedelta

        from .models import (
            EnrollmentStage, Kitchen, Note, NoteSource, TimelineEvent,
            TimelineEventType,
        )

        client = self._client()
        kitchen = Kitchen.objects.create(name="Brooklyn Kitchen")
        enr = self._enrollment(
            client, EnrollmentStage.SERVICE_ACTIVE,
            kitchen=kitchen, weekdays=["mon", "thu"],
        )
        now = timezone.now()

        # Meals case governs first (approved + open) -> household stays served.
        meals = str(uuid.uuid4())
        self._save_case(
            client, meals, "Medically Tailored Meals", now - timedelta(days=2)
        )
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)

        # A newer approved + open BOXES case supersedes it: a genuine switch.
        boxes = str(uuid.uuid4())
        self._save_case(client, boxes, "Food Box", now)

        enr.refresh_from_db()
        client.refresh_from_db()
        # The old meals enrollment is closed as read-only history; a new boxes
        # enrollment is opened and requeued to Kitchen Assignment.
        self.assertEqual(enr.stage, EnrollmentStage.CLOSED)
        new = client.enrollments.exclude(
            stage__in=[EnrollmentStage.CLOSED.value, EnrollmentStage.CANCELLED.value]
        ).first()
        self.assertIsNotNone(new)
        self.assertEqual(new.supersedes_id, enr.pk)
        self.assertEqual(new.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)
        self.assertIsNone(new.kitchen_id)
        self.assertEqual(new.delivery_weekdays, [])
        # Governing pointer advanced to the boxes case.
        self.assertEqual(client.governing_internal_case_id, boxes)
        # Primary system note records the replacement.
        self.assertTrue(
            Note.objects.filter(client=client, source=NoteSource.SYSTEM).exists()
        )

    def test_switch_is_idempotent_on_resave(self):
        from datetime import timedelta

        from .models import (
            EnrollmentStage, EnrollmentVerification, TimelineEvent, TimelineEventType,
        )

        client = self._client()
        self._enrollment(
            client, EnrollmentStage.SERVICE_ACTIVE, weekdays=["mon"]
        )
        now = timezone.now()
        meals = str(uuid.uuid4())
        boxes = str(uuid.uuid4())
        self._save_case(
            client, meals, "Medically Tailored Meals", now - timedelta(days=2)
        )
        self._save_case(client, boxes, "Food Box", now)
        # Re-saving the (now governing) boxes case must NOT open another new
        # enrollment.
        self._save_case(client, boxes, "Food Box", now)
        self.assertEqual(
            EnrollmentVerification.objects.filter(client=client).count(),
            2,  # original (closed) + replacement
        )

    def test_import_switch_opens_new_boxes_enrollment(self):
        # A household served as MEALS whose governing case switches to BOXES:
        # the import closes the old enrollment and opens a new one bound to the
        # boxes case -- no manual Programs-tab step.
        from datetime import timedelta

        from .models import (
            EnrollmentStage, EnrollmentVerification, Kitchen,
        )

        client = self._client()
        kitchen = Kitchen.objects.create(name="Meals Kitchen")
        enr = self._enrollment(
            client, EnrollmentStage.SERVICE_ACTIVE,
            kitchen=kitchen, weekdays=["mon", "thu"],
        )
        now = timezone.now()
        # Meals governs first; no change.
        self._save_case(
            client, str(uuid.uuid4()), "Medically Tailored Meals",
            now - timedelta(days=2),
        )
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)
        # A newer BOXES governing case closes the old enrollment and opens a new one.
        self._save_case(client, str(uuid.uuid4()), "Food Box", now)
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.CLOSED)
        new = EnrollmentVerification.objects.filter(
            client=client,
        ).exclude(stage__in=[EnrollmentStage.CLOSED.value, EnrollmentStage.CANCELLED.value]).first()
        self.assertIsNotNone(new)
        self.assertEqual(new.supersedes_id, enr.pk)
        self.assertEqual(new.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)
        self.assertIsNone(new.kitchen_id)
        self.assertEqual(new.delivery_weekdays, [])


class EffectiveAuthorizationWindowTest(TestCase):
    """Regression: an APPROVED governing case whose export carried only the
    REQUEST window (approval window blank) must still yield a usable service
    window, so a scope/governing switch to such a case doesn't wipe the delivery
    calendar and strand the household out of service. ``Case.effective_
    authorization_window()`` prefers the approval window and falls back to the
    request window (per-endpoint) only for approved / not-required cases."""

    def _case(self, **kw):
        from .models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Win", last_name="Dow"
        )
        defaults = dict(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
        )
        defaults.update(kw)
        return Case.objects.create(**defaults)

    def test_prefers_approval_window(self):
        now = timezone.now()
        case = self._case(
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=60),
            service_authorization_request_starts_at=now - timedelta(days=5),
            service_authorization_request_ends_at=now + timedelta(days=5),
        )
        start, end = case.effective_authorization_window()
        self.assertEqual(start, case.service_authorization_approval_starts_at)
        self.assertEqual(end, case.service_authorization_approval_ends_at)

    def test_approved_falls_back_to_request_window(self):
        now = timezone.now()
        case = self._case(
            service_authorization_approval_starts_at=None,
            service_authorization_approval_ends_at=None,
            service_authorization_request_starts_at=now,
            service_authorization_request_ends_at=now + timedelta(days=90),
        )
        start, end = case.effective_authorization_window()
        self.assertEqual(start, case.service_authorization_request_starts_at)
        self.assertEqual(end, case.service_authorization_request_ends_at)

    def test_pending_case_does_not_fall_back(self):
        from .models import ServiceAuthorizationStatus

        now = timezone.now()
        case = self._case(
            service_authorization_status=ServiceAuthorizationStatus.PENDING,
            service_authorization_approval_starts_at=None,
            service_authorization_approval_ends_at=None,
            service_authorization_request_starts_at=now,
            service_authorization_request_ends_at=now + timedelta(days=90),
        )
        # A pending case is not served -> no fallback, so the delivery pipeline
        # (gated on approved/not-required) still sees no window.
        self.assertEqual(case.effective_authorization_window(), (None, None))


class TimelineDedupeKeyClampTest(TestCase):
    """Regression: the ``TimelineEvent.dedupe_key`` column is varchar(128). The
    governing-case-changed key concatenates the client id + previous + new case
    ids (three UUIDs + prefix -> 133 chars), which overflowed the column and
    raised ``DataError`` on Postgres -- aborting ``_record_governing_case_change``
    (and therefore the whole import reconcile) BEFORE the household scope switch /
    member pause ran, so only the governing pointer moved. (The test DB is SQLite,
    which does NOT enforce varchar length, so the crash never surfaced in tests.)
    ``emit_timeline_event`` now clamps any over-long key to a stable <=128 form."""

    def test_clamp_leaves_short_key_unchanged(self):
        from .services.timeline import _clamp_dedupe_key

        key = "governing_case_changed:short"
        self.assertEqual(_clamp_dedupe_key(key), key)

    def test_clamp_shortens_overlong_key_deterministically(self):
        from .services.timeline import _clamp_dedupe_key

        # The exact shape that overflowed: prefix + 3 UUIDs == 133 chars.
        key = "governing_case_changed:{}:{}:{}".format(
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        )
        self.assertGreater(len(key), 128)
        clamped = _clamp_dedupe_key(key)
        self.assertLessEqual(len(clamped), 128)
        # Deterministic + keeps a readable prefix so it stays diagnosable.
        self.assertEqual(clamped, _clamp_dedupe_key(key))
        self.assertTrue(clamped.startswith("governing_case_changed:"))

    def test_emit_stores_clamped_key_and_dedupes(self):
        from .models import TimelineEventType
        from .services.timeline import (
            _clamp_dedupe_key, emit_timeline_event,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Long", last_name="Key"
        )
        key = "governing_case_changed:{}:{}:{}".format(
            client.pk, uuid.uuid4(), uuid.uuid4()
        )
        self.assertGreater(len(key), 128)
        ev1 = emit_timeline_event(
            client=client,
            event_type=TimelineEventType.MEMBER_GOVERNING_CASE_CHANGED,
            occurred_at=timezone.now(), title="Governing Case Changed",
            dedupe_key=key,
        )
        self.assertIsNotNone(ev1)
        self.assertLessEqual(len(ev1.dedupe_key), 128)
        self.assertEqual(ev1.dedupe_key, _clamp_dedupe_key(key))
        # Re-emitting the same logical key is create-once (no duplicate).
        ev2 = emit_timeline_event(
            client=client,
            event_type=TimelineEventType.MEMBER_GOVERNING_CASE_CHANGED,
            occurred_at=timezone.now(), title="Governing Case Changed",
            dedupe_key=key,
        )
        self.assertEqual(ev1.pk, ev2.pk)
        self.assertEqual(
            TimelineEvent.objects.filter(dedupe_key=ev1.dedupe_key).count(), 1
        )


class UnapprovedActivePullBackTest(TestCase):
    """A household advanced past Verified (Kitchen Assignment / Service Active)
    whose governing internal-service authorization is still PENDING (or blank) is
    pulled BACK to Verified ("Waiting Authorization"): only an approval keeps a
    household in service. A later approval re-advances it. This fixes households
    activated before their authorization landed (the CSV-import gap).
    (NEVER_REQUESTED is NOT a soft pull-back -- it is treated as a denial; see
    ``test_never_requested_full_stops_like_denial``.)"""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ann", last_name="Auth"
        )

    def _enrollment(self, client, stage):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
        )

        household = Household.objects.create(name="HH")
        HouseholdMember.objects.create(
            household=household, client=client, is_primary=True
        )
        # An enrollment created already at/past Kitchen Assignment represents a
        # household that has cleared the Nutritionist gate (grandfathered), so
        # stamp the sign-off -- a pull-back + re-approval must still re-advance.
        past_gate = stage in (
            EnrollmentStage.KITCHEN_ASSIGNMENT,
            EnrollmentStage.SERVICE_ACTIVE,
            EnrollmentStage.SERVICE_COMPLETE,
        )
        return EnrollmentVerification.objects.create(
            client=client, household=household, stage=stage,
            verified_at=timezone.now(),
            nutritionist_approved_at=timezone.now() if past_gate else None,
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

    def test_pending_pulls_service_active_back_to_verified(self):
        from .models import EnrollmentStage, ProgramStatus
        from .services.lifecycle import program_status

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.SERVICE_ACTIVE)
        self._save_case(client, str(uuid.uuid4()), "pending")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.VERIFIED)
        # Grandfathered (nutritionist-approved), pending auth -> waiting on auth,
        # surfaced as Nutritionist Approved.
        self.assertEqual(program_status(enr), ProgramStatus.NUTRITIONIST_APPROVED)

    def test_never_requested_full_stops_like_denial(self):
        # A NEVER_REQUESTED authorization is treated exactly like a DENIAL: an
        # open case that confers no service. A Kitchen-Assignment household is
        # paused (On Hold) and hard off-ramped to INELIGIBLE -- NOT softly pulled
        # back to Verified.
        from .models import ClientStage, EnrollmentStage

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        self._save_case(client, str(uuid.uuid4()), "never_requested")
        enr.refresh_from_db()
        client.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.ON_HOLD)
        self.assertEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_approval_readvances_after_pullback(self):
        from .models import EnrollmentStage

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.SERVICE_ACTIVE)
        case_id = str(uuid.uuid4())
        self._save_case(client, case_id, "pending")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.VERIFIED)

        # Approve the same case -> the enrollment re-advances to Kitchen
        # Assignment (reconcile_enrollment_authorization), so the pull-back is
        # fully reversible.
        self._save_case(client, case_id, "approved")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)


class InternalServiceClosureFullStopTest(TestCase):
    """When a client's LAST open internal-service (meal/box) case CLOSES it is a
    REVERSIBLE full stop: future deliveries truncated, the household paused (On
    Hold), the client parked at SERVICE_INACTIVE, with a system note on the
    primary and NO tickets. Idempotent on re-import; a later open case resumes
    service."""

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

    def test_last_open_case_closing_pauses_and_marks_inactive(self):
        from .models import (
            ClientStage, EnrollmentStage, Note, NoteSource, Ticket,
        )

        # SERVICE_ACTIVE (already-serving) member: closure is the REVERSIBLE
        # SERVICE_INACTIVE off-ramp. (A member still only at Kitchen Assignment
        # is the hard INELIGIBLE off-ramp -- task 4.3 -- covered separately.)
        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.SERVICE_ACTIVE)
        case_id = str(uuid.uuid4())
        # Open + approved -> served (Rule 2 keeps it on the queue).
        self._save_case(client, case_id, auth="approved", case_status="open")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)

        # Close the sole open case -> reversible full stop -> On Hold + inactive.
        self._save_case(
            client, case_id, auth="approved", case_status="closed",
            closed_at=timezone.now(),
        )
        enr.refresh_from_db()
        client.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        self.assertEqual(client.lifecycle_stage, ClientStage.SERVICE_INACTIVE)
        # System note on the primary (paused); NO tickets.
        self.assertGreaterEqual(
            Note.objects.filter(client=client, source=NoteSource.SYSTEM).count(), 1
        )
        self.assertEqual(Ticket.objects.filter(client=client).count(), 0)

    def test_reopening_case_reactivates_paused_household(self):
        from .models import ClientStage, EnrollmentStage

        # SERVICE_ACTIVE member: closure is reversible (SERVICE_INACTIVE).
        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.SERVICE_ACTIVE)
        case_id = str(uuid.uuid4())
        self._save_case(client, case_id, auth="approved", case_status="open")
        # Close it -> paused + inactive.
        self._save_case(
            client, case_id, auth="approved", case_status="closed",
            closed_at=timezone.now(),
        )
        enr.refresh_from_db()
        client.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        self.assertEqual(client.lifecycle_stage, ClientStage.SERVICE_INACTIVE)

        # A new open, approved case opens a new enrollment; the old one stays
        # closed and the client is no longer parked at SERVICE_INACTIVE.
        self._save_case(
            client, str(uuid.uuid4()), auth="approved", case_status="open",
        )
        enr.refresh_from_db()
        client.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.CLOSED)
        new = client.enrollments.exclude(
            stage__in=[EnrollmentStage.CLOSED.value, EnrollmentStage.CANCELLED.value]
        ).first()
        self.assertIsNotNone(new)
        self.assertEqual(new.supersedes_id, enr.pk)
        self.assertNotEqual(client.lifecycle_stage, ClientStage.SERVICE_INACTIVE)

    def test_close_out_is_idempotent(self):
        from .models import EnrollmentStage, Note, NoteSource

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        case_id = str(uuid.uuid4())
        self._save_case(
            client, case_id, case_status="closed", closed_at=timezone.now()
        )
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        n1 = Note.objects.filter(client=client, source=NoteSource.SYSTEM).count()

        # Re-import the same closed case -> nothing actionable -> no new notes.
        self._save_case(
            client, case_id, case_status="closed", closed_at=timezone.now()
        )
        n2 = Note.objects.filter(client=client, source=NoteSource.SYSTEM).count()
        self.assertEqual(n1, n2)

    def test_closing_one_of_two_open_cases_keeps_serving(self):
        # Full stop only fires when the LAST open internal-service case closes.
        # With a second open+approved case remaining, the household stays served
        # and is NOT parked at SERVICE_INACTIVE. Closing the governing case makes
        # the OTHER case govern -> the enrollment is cleanly replaced (old CLOSED,
        # one new live enrollment on the surviving case), NOT left as a duplicate.
        from .models import ClientStage, EnrollmentStage

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        # A realistic Kitchen-Assignment enrollment is past the nutritionist gate.
        enr.nutritionist_approved_at = timezone.now()
        enr.save(update_fields=["nutritionist_approved_at"])
        case_a = str(uuid.uuid4())
        case_b = str(uuid.uuid4())
        self._save_case(client, case_a, auth="approved", case_status="open")
        self._save_case(client, case_b, auth="approved", case_status="open")

        # Close ONE case -> the other keeps the household served.
        self._save_case(
            client, case_a, auth="approved", case_status="closed",
            closed_at=timezone.now(),
        )
        client.refresh_from_db()
        live = client.enrollments.exclude(
            stage__in=[EnrollmentStage.CLOSED.value, EnrollmentStage.CANCELLED.value]
        )
        # Exactly ONE live enrollment (no duplicate), still serving-ready, and the
        # client is not parked at SERVICE_INACTIVE.
        self.assertEqual(live.count(), 1)
        self.assertEqual(
            EnrollmentStage(live.first().stage), EnrollmentStage.KITCHEN_ASSIGNMENT
        )
        self.assertNotEqual(client.lifecycle_stage, ClientStage.SERVICE_INACTIVE)

    def test_ineligible_outranks_service_inactive(self):
        # A hard (unfixable) ineligibility outranks inactivity: closing the last
        # open case must NOT downgrade an INELIGIBLE client to SERVICE_INACTIVE.
        from .models import ClientStage, EnrollmentStage

        client = self._client()
        client.lifecycle_stage = ClientStage.INELIGIBLE
        client.save(update_fields=["lifecycle_stage"])
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)
        case_id = str(uuid.uuid4())
        self._save_case(client, case_id, auth="approved", case_status="open")

        self._save_case(
            client, case_id, auth="approved", case_status="closed",
            closed_at=timezone.now(),
        )
        enr.refresh_from_db()
        client.refresh_from_db()
        # Household still paused, but the client stays INELIGIBLE (not downgraded).
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        self.assertEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_emits_single_service_inactive_timeline_event(self):
        # The 'Service Inactive' timeline event fires once on the transition IN,
        # and a re-import of the same closed case does not duplicate it.
        from .models import EnrollmentStage, TimelineEvent, TimelineEventType

        # SERVICE_ACTIVE member: closure -> reversible SERVICE_INACTIVE event.
        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.SERVICE_ACTIVE)
        case_id = str(uuid.uuid4())
        self._save_case(client, case_id, auth="approved", case_status="open")

        self._save_case(
            client, case_id, auth="approved", case_status="closed",
            closed_at=timezone.now(),
        )
        events = TimelineEvent.objects.filter(
            client=client,
            event_type=TimelineEventType.MEMBER_SERVICE_INACTIVE,
        )
        self.assertEqual(events.count(), 1)

        # Re-import the same closed case -> no duplicate transition-in event.
        self._save_case(
            client, case_id, auth="approved", case_status="closed",
            closed_at=timezone.now(),
        )
        self.assertEqual(
            TimelineEvent.objects.filter(
                client=client,
                event_type=TimelineEventType.MEMBER_SERVICE_INACTIVE,
            ).count(),
            1,
        )


class KitchenAssignmentIneligibleTest(TestCase):
    """Objective 3 / task 4.3: a household still only at KITCHEN_ASSIGNMENT
    (authorized, awaiting a kitchen -- never became an active member) whose
    governing internal-service case CLOSES or is DENIED is a HARD off-ramp: the
    client is set INELIGIBLE (a 'Member marked Ineligible' event fires). Contrast
    with an already-SERVICE_ACTIVE member, whose closure/denial is the reversible
    On Hold / SERVICE_INACTIVE path (covered elsewhere)."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Kit", last_name="Assign"
        )

    def _enrollment(self, client):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
        )

        household = Household.objects.create(name="HH")
        HouseholdMember.objects.create(
            household=household, client=client, is_primary=True
        )
        return EnrollmentVerification.objects.create(
            client=client, household=household,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT, verified_at=timezone.now(),
        )

    def _save_case(self, client, case_id, *, auth="approved", case_status="open",
                   closed_at=None):
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

    def test_closed_case_at_kitchen_assignment_is_ineligible(self):
        from .models import (
            ClientStage, EnrollmentStage, TimelineEvent, TimelineEventType,
        )

        client = self._client()
        enr = self._enrollment(client)
        case_id = str(uuid.uuid4())
        self._save_case(client, case_id, auth="approved", case_status="open")
        # Close the sole open case while still at Kitchen Assignment -> INELIGIBLE.
        self._save_case(
            client, case_id, auth="approved", case_status="closed",
            closed_at=timezone.now(),
        )
        client.refresh_from_db()
        enr.refresh_from_db()
        self.assertEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        self.assertEqual(
            TimelineEvent.objects.filter(
                client=client, event_type=TimelineEventType.MEMBER_INELIGIBLE,
            ).count(),
            1,
        )

    def test_denied_case_at_kitchen_assignment_is_ineligible(self):
        from .models import ClientStage, TimelineEvent, TimelineEventType

        client = self._client()
        enr = self._enrollment(client)
        case_id = str(uuid.uuid4())
        self._save_case(client, case_id, auth="approved", case_status="open")
        # Deny while still at Kitchen Assignment -> INELIGIBLE hard off-ramp.
        self._save_case(client, case_id, auth="denied", case_status="open")
        client.refresh_from_db()
        self.assertEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)
        self.assertEqual(
            TimelineEvent.objects.filter(
                client=client, event_type=TimelineEventType.MEMBER_INELIGIBLE,
            ).count(),
            1,
        )


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
            # A real kitchen-assignment household is always verified; set it so the
            # Logistics queue's verification gate keeps the roster in scope.
            verified_at=timezone.now(),
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

    def _checks(self, primary, member_clients, *, kitchens=None):
        # Refetch from the DB so client_id is a UUID (matching the profile FK),
        # as the real endpoint's querysets provide -- passing in-memory instances
        # created with a str client_id would mismatch the profile lookup.
        from .portal.views_members import MembersListView
        primary = Client.objects.get(pk=primary.pk)
        member_clients = list(
            Client.objects.filter(pk__in=[c.pk for c in member_clients])
        )
        return MembersListView()._logistics_checks(
            primary, member_clients, kitchens or [], is_boxes=False,
        )

    def test_no_menu_member_not_double_counted_as_out_of_orbit(self):
        # A member with no menu type is a 'missing menu type' blocker only -- it
        # must NOT ALSO be counted as 'may get out of orbit' (the old double
        # count that inflated Has Blockers).
        from .models import MemberStatus

        primary = self._client("Nomenu", "Primary")
        hh = self._household(primary)
        self._internal_case(primary)
        self._enrollment(primary, hh, {primary: MemberStatus.ACTIVE})  # no menu_type
        per, agg = self._checks(primary, [primary])
        self.assertFalse(per[str(primary.client_id)]["predicted_out_of_orbit"])
        self.assertEqual(agg["predicted_out_of_orbit"], 0)
        self.assertTrue(any("missing menu" in b for b in agg["blockers"]))
        self.assertFalse(any("out of orbit" in b for b in agg["blockers"]))

    def test_out_of_orbit_prediction_is_kitchen_aware(self):
        # A member WITH a menu but for whom NO available kitchen can serve is
        # predicted out of orbit (kitchen-aware) -- not from the old global rule.
        from .models import EnrollmentStage, MemberDietaryProfile, MemberStatus

        primary = self._client("Menu", "Primary")
        hh = self._household(primary)
        self._internal_case(primary)
        enr = self._enrollment(primary, hh, {})
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=primary, status=MemberStatus.ACTIVE,
            menu_type="Standard",
        )
        per, agg = self._checks(primary, [primary], kitchens=[])  # no kitchens
        self.assertTrue(per[str(primary.client_id)]["predicted_out_of_orbit"])
        self.assertIn("kitchen", per[str(primary.client_id)]["predicted_reason"].lower())

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

    def test_search_by_dependent_medicaid_id_returns_whole_household(self):
        # Searching a DEPENDENT's Medicaid (insurance member) id must surface the
        # entire household -- the primary as the header, full roster nested --
        # not just the matched dependent. Grouped mode loads the full roster.
        from .models import ClientStage, Insurance, MemberStatus

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent", stage=ClientStage.ACTIVE)
        hh = self._household(primary, dep)
        self._internal_case(primary)
        self._enrollment(primary, hh, {
            primary: MemberStatus.ACTIVE,
            dep: MemberStatus.ACTIVE,
        })
        Insurance.objects.create(
            client=dep, plan_name="Medicaid", external_member_id="MCD-9988",
        )

        groups = self._groups(search="MCD-9988")
        self.assertEqual(len(groups), 1, groups)
        g = groups[0]
        self.assertEqual(g["type"], "household")
        # The header is the primary (case-holder), not the matched dependent.
        self.assertEqual(g["primary"]["id"], str(primary.client_id))
        ids = {m["id"] for m in g["members"]}
        self.assertEqual(ids, {str(primary.client_id), str(dep.client_id)})

    def test_search_by_dependent_client_id_returns_whole_household(self):
        from .models import ClientStage, MemberStatus

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent", stage=ClientStage.ACTIVE)
        hh = self._household(primary, dep)
        self._internal_case(primary)
        self._enrollment(primary, hh, {
            primary: MemberStatus.ACTIVE,
            dep: MemberStatus.ACTIVE,
        })

        groups = self._groups(search=str(dep.client_id))
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(groups[0]["primary"]["id"], str(primary.client_id))
        ids = {m["id"] for m in groups[0]["members"]}
        self.assertIn(str(dep.client_id), ids)


class MemberListGroupedStatusFilterTest(TestCase):
    """The Members page grouped status dropdown maps each value to exactly one
    backend query across several axes (Eligibility / Verification /
    Authorization / Logistics / Service / Terminal). Each solo-household member
    is built so a matching status surfaces its group and a non-match hides it."""

    def _member(self, first, *, lifecycle=None, enr_stage=None, verified=False,
                auth=None, case_status=None, member_status=None,
                program_name="Medically Tailored Meals"):
        from django.utils import timezone

        from .models import (
            Case, CaseStatus, CaseType, Client, ClientStage, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name="Member",
            lifecycle_stage=lifecycle or ClientStage.ACTIVE,
        )
        hh = Household.objects.create(name=f"{first} Household")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=case_status or CaseStatus.OPEN,
            service_authorization_status=auth or "",
            program_name=program_name,
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh,
            stage=enr_stage or EnrollmentStage.SERVICE_ACTIVE,
            verified_at=timezone.now() if verified else None,
            program_name=program_name,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=client,
            status=member_status or MemberStatus.ACTIVE,
        )
        return client

    def _ids(self, **params):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from .portal.views_members import MembersListView

        view = MembersListView()
        view.request = Request(APIRequestFactory().get("/portal/members/", params))
        view.kwargs = {}
        groups = view._build_groups_for_page(view._group_entries())
        return {g["primary"]["id"] for g in groups}

    def test_eligibility_axis(self):
        from .models import ClientStage

        eligible = self._member("Ellie", lifecycle=ClientStage.ACTIVE)
        ineligible = self._member("Iggy", lifecycle=ClientStage.INELIGIBLE)

        elig_ids = self._ids(status="eligible")
        self.assertIn(str(eligible.client_id), elig_ids)
        self.assertNotIn(str(ineligible.client_id), elig_ids)

        inelig_ids = self._ids(status="ineligible")
        self.assertIn(str(ineligible.client_id), inelig_ids)
        self.assertNotIn(str(eligible.client_id), inelig_ids)

    def test_standalone_eligibility_filter(self):
        # The dedicated `eligibility` param (its own dimension, composes with the
        # status chips) filters by the eligibility gate independently of `status`.
        from .models import ClientStage

        eligible = self._member("Ellie", lifecycle=ClientStage.ACTIVE)
        ineligible = self._member("Iggy", lifecycle=ClientStage.INELIGIBLE)

        elig_ids = self._ids(eligibility="eligible")
        self.assertIn(str(eligible.client_id), elig_ids)
        self.assertNotIn(str(ineligible.client_id), elig_ids)

        inelig_ids = self._ids(eligibility="ineligible")
        self.assertIn(str(ineligible.client_id), inelig_ids)
        self.assertNotIn(str(eligible.client_id), inelig_ids)

        # No eligibility param => both returned.
        all_ids = self._ids()
        self.assertIn(str(eligible.client_id), all_ids)
        self.assertIn(str(ineligible.client_id), all_ids)

    def test_program_type_filter(self):
        # The `program_type` param (its own dimension, meant to combine with the
        # household-composition filter) filters by the governing program's scope,
        # derived LIVE from the program name: "household" in the name => Household,
        # else Individual.
        household = self._member(
            "Holly", program_name="MTM - (Household) High-Risk Children - Brooklyn",
        )
        individual = self._member(
            "Ivan", program_name="MTM - Individual - Queens",
        )

        hh_ids = self._ids(program_type="household")
        self.assertIn(str(household.client_id), hh_ids)
        self.assertNotIn(str(individual.client_id), hh_ids)

        indiv_ids = self._ids(program_type="individual")
        self.assertIn(str(individual.client_id), indiv_ids)
        self.assertNotIn(str(household.client_id), indiv_ids)

        # No param => both returned.
        all_ids = self._ids()
        self.assertIn(str(household.client_id), all_ids)
        self.assertIn(str(individual.client_id), all_ids)

    def test_member_status_flag_ignores_stale_closed_enrollment_profile(self):
        # The individual member-status flags (Out of Orbit / Out of Range /
        # Paused) reflect the CURRENT enrollment. A stale status on a CLOSED /
        # superseded enrollment (left behind by a governing-case replacement) must
        # NOT surface a member whose live profile is Active.
        from .models import (
            EnrollmentStage, EnrollmentVerification, HouseholdMember,
            MemberDietaryProfile, MemberStatus,
        )

        # Currently ACTIVE, but with a leftover OUT_OF_ORBIT profile on a closed
        # (superseded) enrollment.
        stale = self._member("Stale", member_status=MemberStatus.ACTIVE)
        hh = HouseholdMember.objects.get(client=stale).household
        closed = EnrollmentVerification.objects.create(
            client=stale, household=hh, stage=EnrollmentStage.CLOSED,
            program_name="Medically Tailored Meals",
        )
        MemberDietaryProfile.objects.create(
            enrollment=closed, client=stale, status=MemberStatus.OUT_OF_ORBIT,
        )
        # A genuinely current out-of-orbit member (live enrollment).
        current = self._member("Orbit", member_status=MemberStatus.OUT_OF_ORBIT)

        ids = self._ids(flag="out_of_orbit")
        self.assertNotIn(str(stale.client_id), ids)   # stale closed profile ignored
        self.assertIn(str(current.client_id), ids)     # current profile matches

    def test_term_closed_excludes_members_with_a_live_enrollment(self):
        # "Closed" means NO current live enrollment -- a member actively served on
        # a live enrollment must NOT read as Closed just because an old superseded
        # enrollment in their history is closed.
        from .models import (
            EnrollmentStage, EnrollmentVerification, HouseholdMember,
        )

        served = self._member("Served", enr_stage=EnrollmentStage.SERVICE_ACTIVE)
        hh = HouseholdMember.objects.get(client=served).household
        EnrollmentVerification.objects.create(
            client=served, household=hh, stage=EnrollmentStage.CLOSED,
            program_name="Medically Tailored Meals",
            closed_at=timezone.now(),  # a real closed enrollment is closed
        )
        done = self._member("Done", enr_stage=EnrollmentStage.CLOSED)

        ids = self._ids(status="term_closed")
        self.assertNotIn(str(served.client_id), ids)  # has a live enrollment
        self.assertIn(str(done.client_id), ids)         # only a closed enrollment

    def test_stage_filter_uses_governing_enrollment_not_any(self):
        # A member with a stray ON_HOLD enrollment PLUS a newer live SERVICE_ACTIVE
        # one: the governing (newer, open) enrollment is Active, so they read as
        # Open -- NOT On Hold. The filter must key off the governing enrollment,
        # not "any enrollment at this stage".
        from .models import (
            EnrollmentStage, EnrollmentVerification, HouseholdMember,
        )

        m = self._member("Dual", enr_stage=EnrollmentStage.ON_HOLD)
        hh = HouseholdMember.objects.get(client=m).household
        EnrollmentVerification.objects.create(
            client=m, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals",
        )
        self.assertNotIn(str(m.client_id), self._ids(status="on_hold"))
        self.assertIn(str(m.client_id), self._ids(status="term_open"))

    def test_paused_flag_splits_agent_vs_eligibility(self):
        # Paused splits into agent (manual) vs eligibility (auto) so the two are
        # distinguishable on the list; both are status=PAUSED, told apart by the
        # eligibility_paused flag.
        from .models import MemberDietaryProfile, MemberStatus

        agent = self._member("AgentPause", member_status=MemberStatus.PAUSED)
        elig = self._member("EligPause", member_status=MemberStatus.PAUSED)
        p = MemberDietaryProfile.objects.get(client=elig)
        p.eligibility_paused = True
        p.save(update_fields=["eligibility_paused"])

        agent_ids = self._ids(flag="paused")
        self.assertIn(str(agent.client_id), agent_ids)
        self.assertNotIn(str(elig.client_id), agent_ids)

        elig_ids = self._ids(flag="eligibility_paused")
        self.assertIn(str(elig.client_id), elig_ids)
        self.assertNotIn(str(agent.client_id), elig_ids)

    def test_verification_axis(self):
        from .models import ClientStage, EnrollmentStage

        verified = self._member(
            "Vera", enr_stage=EnrollmentStage.VERIFIED, verified=True,
        )
        pending = self._member(
            "Peny", lifecycle=ClientStage.PENDING_VERIFICATION,
            enr_stage=EnrollmentStage.PENDING_VERIFICATION, verified=False,
        )

        v_ids = self._ids(status="verified")
        self.assertIn(str(verified.client_id), v_ids)
        self.assertNotIn(str(pending.client_id), v_ids)

        p_ids = self._ids(status="pending_verification")
        self.assertIn(str(pending.client_id), p_ids)
        self.assertNotIn(str(verified.client_id), p_ids)

    def test_pending_verification_excludes_members_outside_window(self):
        # A member who never entered verification (Inactive lifecycle, no
        # verified_at) must NOT match the Pending Verification filter -- otherwise
        # they surface with a blank Verification column. Only members actually in
        # the verification window match.
        from .models import ClientStage, EnrollmentStage

        pending = self._member(
            "Peny", lifecycle=ClientStage.PENDING_VERIFICATION,
            enr_stage=EnrollmentStage.PENDING_VERIFICATION, verified=False,
        )
        outside = self._member(
            "Ivy", lifecycle=ClientStage.INACTIVE,
            enr_stage=EnrollmentStage.PENDING_VALIDATION, verified=False,
        )
        p_ids = self._ids(status="pending_verification")
        self.assertIn(str(pending.client_id), p_ids)
        self.assertNotIn(str(outside.client_id), p_ids)

    def test_authorization_axis(self):
        from .models import ServiceAuthorizationStatus

        approved = self._member("Amy", auth=ServiceAuthorizationStatus.APPROVED)
        waiting = self._member("Will", auth=ServiceAuthorizationStatus.PENDING)
        denied = self._member("Dan", auth=ServiceAuthorizationStatus.DENIED)

        self.assertEqual(self._ids(status="authorized") & {
            str(approved.client_id), str(waiting.client_id), str(denied.client_id),
        }, {str(approved.client_id)})
        self.assertEqual(self._ids(status="auth_pending") & {
            str(approved.client_id), str(waiting.client_id), str(denied.client_id),
        }, {str(waiting.client_id)})
        self.assertEqual(self._ids(status="auth_denied") & {
            str(approved.client_id), str(waiting.client_id), str(denied.client_id),
        }, {str(denied.client_id)})

    def test_service_axis(self):
        from .models import ClientStage, EnrollmentStage, MemberStatus

        active = self._member("Ace", lifecycle=ClientStage.ACTIVE)
        on_hold = self._member("Hal", enr_stage=EnrollmentStage.ON_HOLD)
        out_of_range = self._member(
            "Rory", member_status=MemberStatus.OUT_OF_RANGE,
        )

        self.assertIn(str(active.client_id), self._ids(status="active"))
        self.assertIn(str(on_hold.client_id), self._ids(status="on_hold"))
        oor = self._ids(status="out_of_range")
        self.assertIn(str(out_of_range.client_id), oor)
        self.assertNotIn(str(active.client_id), oor)

    def test_logistics_axis(self):
        from .models import ClientStage

        ka = self._member("Kay", lifecycle=ClientStage.KITCHEN_ASSIGNMENT)
        active = self._member("Ace", lifecycle=ClientStage.ACTIVE)
        ids = self._ids(status="kitchen_assignment")
        self.assertIn(str(ka.client_id), ids)
        self.assertNotIn(str(active.client_id), ids)

    def test_terminal_axis(self):
        from .models import EnrollmentStage, ServiceAuthorizationStatus

        open_prog = self._member("Ollie", enr_stage=EnrollmentStage.SERVICE_ACTIVE)
        expired = self._member(
            "Xander", auth=ServiceAuthorizationStatus.EXPIRED,
        )
        closed = self._member("Cleo", enr_stage=EnrollmentStage.CLOSED)

        self.assertIn(str(open_prog.client_id), self._ids(status="term_open"))
        exp_ids = self._ids(status="term_expired")
        self.assertIn(str(expired.client_id), exp_ids)
        self.assertNotIn(str(open_prog.client_id), exp_ids)
        cl_ids = self._ids(status="term_closed")
        self.assertIn(str(closed.client_id), cl_ids)
        self.assertNotIn(str(open_prog.client_id), cl_ids)


class MemberListColumnFieldsTest(TestCase):
    """MemberListSerializer surfaces the Members-page column data: the eligibility
    node verdict, the governing case authorization WINDOW (start/end), and the
    per-program Service status (lifecycle.program_status)."""

    def _data(self, client):
        from .portal.serializers import MemberListSerializer

        return MemberListSerializer(client).data

    def test_eligibility_verdict(self):
        from .models import Client, ClientStage

        eligible = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="El", last_name="Igible",
            lifecycle_stage=ClientStage.ACTIVE,
        )
        ineligible = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="In", last_name="Eligible",
            lifecycle_stage=ClientStage.INELIGIBLE,
        )
        self.assertEqual(self._data(eligible)["eligibility"], "eligible")
        self.assertEqual(self._data(eligible)["eligibility_label"], "Eligible")
        self.assertEqual(self._data(eligible)["eligibility_reasons"], [])
        self.assertEqual(self._data(ineligible)["eligibility"], "ineligible")
        self.assertEqual(self._data(ineligible)["eligibility_label"], "Not Eligible")

    def test_ineligible_reasons_recomputed(self):
        # An INELIGIBLE member with NO insurance on file surfaces the hard-gate
        # reason (recomputed on read via evaluate_client), so the Members page can
        # display why the member is Not Eligible.
        from .models import Client, ClientStage

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="No", last_name="Insurance",
            lifecycle_stage=ClientStage.INELIGIBLE,
        )
        reasons = self._data(client)["eligibility_reasons"]
        self.assertTrue(reasons)
        self.assertIn("no medical insurance on file", reasons)

    def test_authorization_window_and_program_status(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import (
            Case, CaseStatus, CaseType, Client, ClientStage, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            ServiceAuthorizationStatus,
        )

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ann", last_name="Active",
            lifecycle_stage=ClientStage.ACTIVE,
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=60),
            program_name="Medically Tailored Meals",
        )
        EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        data = self._data(client)
        self.assertEqual(data["authorization_window_start"], now.date().isoformat())
        self.assertEqual(
            data["authorization_window_end"],
            (now + timedelta(days=60)).date().isoformat(),
        )
        self.assertEqual(data["program_status"], "active")
        self.assertEqual(data["program_status_label"], "Active")


class MemberListDependentAuthorizationTest(TestCase):
    """Members-list rows must show the authorization of the GOVERNING case. A
    household DEPENDENT owns no case of their own (the meal/box case sits on the
    primary), so their authorization column must inherit the HOUSEHOLD's
    governing case via governing_service_case_for_display -- not read blank."""

    def _client(self, first, last):
        from .models import ClientStage

        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last,
            lifecycle_stage=ClientStage.KITCHEN_ASSIGNMENT,
        )

    def _household(self, primary, *deps):
        from .models import Household, HouseholdMember

        hh = Household.objects.create(name=f"{primary.last_name} Household")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        for d in deps:
            HouseholdMember.objects.create(household=hh, client=d, is_primary=False)
        return hh

    def _approved_case(self, client):
        from .models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals",
        )

    def _enrollment(self, primary, household):
        from .models import EnrollmentStage, EnrollmentVerification

        return EnrollmentVerification.objects.create(
            client=primary, household=household,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )

    def _auth(self, client):
        from .portal.serializers import MemberListSerializer

        return MemberListSerializer(client).data["authorization_status"]

    def test_dependent_inherits_household_governing_authorization(self):
        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = self._household(primary, dep)
        self._approved_case(primary)  # case lives on the primary only
        self._enrollment(primary, hh)

        # Primary owns the governing case; the dependent inherits it via the
        # household enrollment instead of showing a blank.
        self.assertEqual(self._auth(primary), "approved")
        self.assertEqual(self._auth(dep), "approved")

    def test_dependent_with_own_case_uses_own(self):
        from .models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = self._household(primary, dep)
        self._approved_case(primary)
        # The dependent has their OWN internal-service case -> it wins over the
        # household fallback (own governing case first).
        Case.objects.create(
            case_id=uuid.uuid4(), client=dep,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.PENDING,
            program_name="Medically Tailored Meals",
        )
        self._enrollment(primary, hh)

        self.assertEqual(self._auth(dep), "pending")


class VerificationCaseOptionsTest(TestCase):
    """The verification pop-up's Internal Service case dropdown
    (MemberDetailSerializer -> service.cases) lists only cases that are a live
    target for a verification: DENIED-authorization cases and CLOSED/CANCELLED
    cases are excluded."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Case", last_name="Options",
        )

    def _internal_case(self, client, *, status=None, auth=None, program="Medically Tailored Meals"):
        from .models import Case, CaseStatus, CaseType

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=status or CaseStatus.OPEN,
            service_authorization_status=auth or "",
            program_name=program,
        )

    def _case_ids(self, client):
        from .portal.serializers import MemberDetailSerializer

        data = MemberDetailSerializer(client).data
        return {c["case_id"] for c in data["service"]["cases"]}

    def test_open_case_is_listed(self):
        client = self._client()
        case = self._internal_case(client)
        self.assertIn(str(case.case_id), self._case_ids(client))

    def test_closed_case_excluded(self):
        from .models import CaseStatus

        client = self._client()
        open_case = self._internal_case(client, program="Meals A")
        closed_case = self._internal_case(
            client, status=CaseStatus.CLOSED, program="Meals B"
        )
        ids = self._case_ids(client)
        self.assertIn(str(open_case.case_id), ids)
        self.assertNotIn(str(closed_case.case_id), ids)

    def test_cancelled_case_excluded(self):
        from .models import CaseStatus

        client = self._client()
        cancelled = self._internal_case(client, status=CaseStatus.CANCELLED)
        self.assertNotIn(str(cancelled.case_id), self._case_ids(client))

    def test_denied_case_excluded(self):
        from .models import ServiceAuthorizationStatus

        client = self._client()
        denied = self._internal_case(
            client, auth=ServiceAuthorizationStatus.DENIED
        )
        self.assertNotIn(str(denied.case_id), self._case_ids(client))

    def test_non_met_council_case_excluded(self):
        # A meal case explicitly MANAGED by a different named org (referred out)
        # is not a Met Council verification target, so it must not be offered --
        # even while open with no denial. A Met Council-managed open case is.
        from .models import Case, CaseStatus, CaseType

        client = self._client()
        other_org = Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            program_name="Meals (God's Love)",
            provider_name="God's Love We Deliver - SCN - PHS",
        )
        met = self._internal_case(client, program="Met Council Meals")
        ids = self._case_ids(client)
        self.assertNotIn(str(other_org.case_id), ids)
        self.assertIn(str(met.case_id), ids)


class EnsurePrimaryOfOwnHouseholdTest(TestCase):
    """A client who holds their own Internal Service case must be the PRIMARY of
    their own household. `ensure_primary_of_own_household` splits a non-primary
    dependent out into a fresh household (as primary) while leaving the rest of
    the old household intact, and is a no-op for a client who is already primary
    or has no household yet."""

    def _client(self, first, last):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last,
        )

    def test_non_primary_dependent_is_split_into_own_household(self):
        from .models import Household, HouseholdMember
        from .serializers import ensure_primary_of_own_household

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = Household.objects.create(name="Shared HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)

        new_hh = ensure_primary_of_own_household(dep)

        self.assertNotEqual(new_hh.household_id, hh.household_id)
        dep_membership = HouseholdMember.objects.get(client=dep)
        self.assertEqual(dep_membership.household_id, new_hh.household_id)
        self.assertTrue(dep_membership.is_primary)
        # The old household is untouched apart from losing the dependent.
        self.assertTrue(
            HouseholdMember.objects.filter(
                household=hh, client=primary, is_primary=True
            ).exists()
        )
        self.assertFalse(
            HouseholdMember.objects.filter(household=hh, client=dep).exists()
        )

    def test_dependent_dietary_profile_detached_from_old_enrollment(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus,
        )
        from .serializers import ensure_primary_of_own_household

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = Household.objects.create(name="Shared HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=dep, status=MemberStatus.ACTIVE,
        )

        ensure_primary_of_own_household(dep)

        self.assertFalse(
            MemberDietaryProfile.objects.filter(
                client=dep, enrollment=enr
            ).exists()
        )

    def test_split_client_own_enrollment_moves_to_new_household(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
        )
        from .serializers import ensure_primary_of_own_household

        # The split client holds their OWN enrollment while still a non-primary
        # dependent of a shared household (they got their own Internal Service
        # case). The enrollment must move to their new solo household -- else the
        # Program tab keeps rendering the old household's roster.
        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = Household.objects.create(name="Shared HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        dep_enr = EnrollmentVerification.objects.create(
            client=dep, household=hh, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )

        new_hh = ensure_primary_of_own_household(dep)

        dep_enr.refresh_from_db()
        self.assertEqual(dep_enr.household_id, new_hh.household_id)
        self.assertNotEqual(dep_enr.household_id, hh.household_id)

    def test_already_primary_is_noop(self):
        from .models import Household, HouseholdMember
        from .serializers import ensure_primary_of_own_household

        c = self._client("Sol", "Solo")
        hh = Household.objects.create(name="Own HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)

        result = ensure_primary_of_own_household(c)

        self.assertEqual(result.household_id, hh.household_id)
        self.assertEqual(HouseholdMember.objects.filter(client=c).count(), 1)

    def test_no_household_yet_creates_one_as_primary(self):
        from .models import HouseholdMember
        from .serializers import ensure_primary_of_own_household

        c = self._client("New", "Client")

        result = ensure_primary_of_own_household(c)

        membership = HouseholdMember.objects.get(client=c)
        self.assertEqual(membership.household_id, result.household_id)
        self.assertTrue(membership.is_primary)

    def test_empty_old_household_is_removed(self):
        from .models import Household, HouseholdMember
        from .serializers import ensure_primary_of_own_household

        # A shared household whose ONLY row is the (non-primary) dependent -- e.g.
        # the primary was already removed. Splitting the dependent out leaves the
        # old household empty, so it's cleaned up.
        dep = self._client("Lone", "Dependent")
        hh = Household.objects.create(name="Orphan HH")
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)

        ensure_primary_of_own_household(dep)

        self.assertFalse(Household.objects.filter(household_id=hh.household_id).exists())


class CaseSaveKeepsDependentInHouseholdTest(TestCase):
    """Saving an internal-service case for a NON-primary household dependent must
    NOT split them out of the shared household anymore. The split now happens
    only when an agent Requests Verification for them, so until then they stay a
    dependent and the "dependent in a household" warning on the case-save
    response can surface (the eager split used to remove the membership before
    that warning was evaluated). A primary / household-less client is still
    anchored as primary on case save."""

    def _client(self, first, last):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last,
        )

    def _save_internal_case(self, client):
        from api.serializers import CaseSerializer
        from api.services.lifecycle import MET_COUNCIL_PROVIDER_NAME

        ser = CaseSerializer(data={
            "case_id": str(uuid.uuid4()),
            "client_id": str(client.client_id),
            "program_name": "Medically Tailored Meals",
            "service_type": "Medically Tailored Meals",
            "provider_name": MET_COUNCIL_PROVIDER_NAME,
            "date_opened": timezone.now().isoformat(),
        })
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_dependent_case_save_keeps_them_in_shared_household(self):
        from .models import CaseType, Household, HouseholdMember

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = Household.objects.create(name="Shared HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)

        case = self._save_internal_case(dep)
        self.assertEqual(case.case_type, CaseType.INTERNAL_SERVICE)

        membership = HouseholdMember.objects.get(client=dep)
        # Still a non-primary member of the SAME shared household -- not split.
        self.assertEqual(membership.household_id, hh.household_id)
        self.assertFalse(membership.is_primary)

    def test_dependent_case_save_surfaces_split_warning(self):
        from .models import Household, HouseholdMember
        from .views import _dependent_household_warning

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = Household.objects.create(name="Shared HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)

        self._save_internal_case(dep)

        warning = _dependent_household_warning(dep)
        self.assertIsNotNone(warning)
        self.assertEqual(warning["type"], "dependent_in_household")

    def test_primary_client_still_anchored_on_case_save(self):
        from .models import Household, HouseholdMember

        c = self._client("Sol", "Solo")
        hh = Household.objects.create(name="Own HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)

        self._save_internal_case(c)

        membership = HouseholdMember.objects.get(client=c)
        self.assertEqual(membership.household_id, hh.household_id)
        self.assertTrue(membership.is_primary)


class RemovalAndVerificationGuardsTest(TestCase):
    """Two hard invariants shared by the removal + verification surfaces:

    * The PRIMARY member can never be removed (the ext, the program tab and the
      verification pop-up all route through ``HouseholdMemberEditView.delete``).
    * A member who doesn't OWN an open Internal Service case can't be the subject
      of a verification (``MemberVerificationCreateView`` / the pop-up wizard).
    """

    def _client(self, first, last):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last,
        )

    def _internal_case(self, client, status=None):
        from .models import Case, CaseStatus, CaseType

        return Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=status or CaseStatus.OPEN,
        )

    def test_cannot_remove_primary_member(self):
        from rest_framework.test import APIRequestFactory

        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile,
        )
        from .portal.views_members import HouseholdMemberEditView

        primary = self._client("Pat", "Primary")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        mv = MemberDietaryProfile.objects.create(enrollment=enr, client=primary)

        req = APIRequestFactory().delete("/")
        resp = HouseholdMemberEditView().delete(req, primary.pk, mv.pk)

        self.assertEqual(resp.status_code, 400)
        # The primary's roster row + dietary profile survive the refused removal.
        self.assertTrue(
            HouseholdMember.objects.filter(client=primary, is_primary=True).exists()
        )
        self.assertTrue(MemberDietaryProfile.objects.filter(pk=mv.pk).exists())

    def test_can_remove_non_primary_member(self):
        from rest_framework.test import APIRequestFactory

        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile,
        )
        from .portal.views_members import HouseholdMemberEditView

        primary = self._client("Pat", "Primary")
        dep = self._client("Dee", "Dependent")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=primary)
        dep_mv = MemberDietaryProfile.objects.create(enrollment=enr, client=dep)

        req = APIRequestFactory().delete("/")
        resp = HouseholdMemberEditView().delete(req, primary.pk, dep_mv.pk)

        self.assertIn(resp.status_code, (200, 204))
        self.assertFalse(HouseholdMember.objects.filter(client=dep).exists())

    def _patch_member(self, client_pk, mv_pk, body):
        """PATCH the HouseholdMemberEditView directly with a DRF-wrapped request
        (so ``request.data`` parses when calling the view method in-process)."""
        from rest_framework.parsers import JSONParser
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from .portal.views_members import HouseholdMemberEditView

        raw = APIRequestFactory().patch("/", body, format="json")
        req = Request(raw, parsers=[JSONParser()])
        return HouseholdMemberEditView().patch(req, client_pk, mv_pk)

    def _serviced_household(self, *, deps=1):
        """A SERVICE_ACTIVE household: primary + ``deps`` dependents, each with an
        ACTIVE MemberDietaryProfile. Returns (enr, primary_mv, [dep_mv, ...])."""
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus,
        )

        primary = self._client("Pat", "Primary")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        primary_mv = MemberDietaryProfile.objects.create(
            enrollment=enr, client=primary, status=MemberStatus.ACTIVE,
        )
        dep_mvs = []
        for i in range(deps):
            dep = self._client(f"Dep{i}", "Endent")
            HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
            dep_mvs.append(MemberDietaryProfile.objects.create(
                enrollment=enr, client=dep, status=MemberStatus.ACTIVE,
            ))
        return enr, primary_mv, dep_mvs

    def test_can_pause_primary_member(self):
        """The primary is pausable like any member. Pausing them while another
        member is still active does NOT hold the program."""
        from .models import EnrollmentStage, MemberStatus

        enr, primary_mv, (dep_mv,) = self._serviced_household(deps=1)

        resp = self._patch_member(
            enr.client_id, primary_mv.pk, {"pause": True, "pause_reason": "x"},
        )

        self.assertEqual(resp.status_code, 200)
        primary_mv.refresh_from_db()
        self.assertEqual(primary_mv.status, MemberStatus.PAUSED)
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)  # dep still active

    def test_all_members_paused_holds_program(self):
        """Once EVERY household member is paused the program goes On Hold."""
        from .models import EnrollmentStage

        enr, primary_mv, (dep_mv,) = self._serviced_household(deps=1)

        self._patch_member(enr.client_id, dep_mv.pk, {"pause": True, "pause_reason": "a"})
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)  # primary still active

        self._patch_member(enr.client_id, primary_mv.pk, {"pause": True, "pause_reason": "b"})
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)  # all paused -> held

    def test_unpausing_resumes_held_program(self):
        """Unpausing any member lifts the all-paused auto-hold."""
        from .models import EnrollmentStage

        enr, primary_mv, (dep_mv,) = self._serviced_household(deps=1)
        self._patch_member(enr.client_id, dep_mv.pk, {"pause": True, "pause_reason": "a"})
        self._patch_member(enr.client_id, primary_mv.pk, {"pause": True, "pause_reason": "b"})
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)

        self._patch_member(
            enr.client_id, dep_mv.pk, {"unpause": True, "pause_reason": "back"},
        )
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)

    def test_cannot_verify_member_without_internal_service_case(self):
        from rest_framework.test import APIRequestFactory

        from .models import EnrollmentVerification
        from .portal.views_members import MemberVerificationCreateView

        client = self._client("No", "Case")

        req = APIRequestFactory().post("/", {}, format="json")
        resp = MemberVerificationCreateView().post(req, client.pk)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(EnrollmentVerification.objects.filter(client=client).exists())

    def test_cannot_verify_member_whose_only_case_is_closed(self):
        from rest_framework.test import APIRequestFactory

        from .models import CaseStatus, EnrollmentVerification
        from .portal.views_members import MemberVerificationCreateView

        client = self._client("Closed", "Case")
        self._internal_case(client, status=CaseStatus.CLOSED)

        req = APIRequestFactory().post("/", {}, format="json")
        resp = MemberVerificationCreateView().post(req, client.pk)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(EnrollmentVerification.objects.filter(client=client).exists())

    def test_verification_sets_out_of_range_member_ineligible(self):
        # Wiring: completing a verification runs the eligibility gates per
        # household member, so a member whose PRIMARY (Current) address ZIP is
        # outside the coverage area lands on the INELIGIBLE node -- not just held.
        from rest_framework.parsers import JSONParser
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from .models import (
            Address, AddressType, ClientStage, ExcludedZipCode, Insurance,
        )
        from .portal.views_members import MemberVerificationCreateView

        client = self._client("Ora", "Range")
        self._internal_case(client)
        # Valid medical insurance (blank expiry => active) so the ONLY hard gate
        # that can fire is the out-of-range address.
        Insurance.objects.create(client=client, plan_name="P", external_member_id="1")
        ExcludedZipCode.objects.create(zip="11209")
        Address.objects.create(
            client=client, type=AddressType.CURRENT, zip="11209", street="1 St",
        )

        raw = APIRequestFactory().post(
            "/",
            {"members": [{"client_id": str(client.pk), "mobile_number": "3475550142"}], "zip": "10001"},
            format="json",
        )
        req = Request(raw, parsers=[JSONParser()])
        resp = MemberVerificationCreateView().post(req, client.pk)

        self.assertEqual(resp.status_code, 201)
        client.refresh_from_db()
        self.assertEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_verification_leaves_in_range_member_eligible(self):
        # Control: a serviceable primary ZIP + valid insurance is NOT marked
        # ineligible by the verify-time eligibility pass.
        from rest_framework.parsers import JSONParser
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from .models import (
            Address, AddressType, ClientStage, Insurance,
        )
        from .portal.views_members import MemberVerificationCreateView

        client = self._client("In", "Range")
        self._internal_case(client)
        Insurance.objects.create(client=client, plan_name="P", external_member_id="1")
        Address.objects.create(
            client=client, type=AddressType.CURRENT, zip="10001", street="1 St",
        )

        raw = APIRequestFactory().post(
            "/",
            {"members": [{"client_id": str(client.pk), "mobile_number": "3475550142"}], "zip": "10001"},
            format="json",
        )
        req = Request(raw, parsers=[JSONParser()])
        resp = MemberVerificationCreateView().post(req, client.pk)

        self.assertEqual(resp.status_code, 201)
        client.refresh_from_db()
        self.assertNotEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_verification_saves_conditions_and_gestation(self):
        # Conditions multi-select + conditional Weeks Gestation (Pregnant) are
        # persisted on the member's dietary profile; Postpartum stays null.
        from rest_framework.parsers import JSONParser
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from .models import Address, AddressType, Insurance, MemberDietaryProfile
        from .portal.views_members import MemberVerificationCreateView

        client = self._client("Con", "Dition")
        self._internal_case(client)
        Insurance.objects.create(client=client, plan_name="P", external_member_id="1")
        Address.objects.create(
            client=client, type=AddressType.CURRENT, zip="10001", street="1 St",
        )
        raw = APIRequestFactory().post(
            "/",
            {"members": [{
                "client_id": str(client.pk), "mobile_number": "3475550142",
                "conditions": ["Diabetic", "Pregnant"],
                "weeks_gestation": 24, "months_postpartum": 5,
            }], "zip": "10001"},
            format="json",
        )
        req = Request(raw, parsers=[JSONParser()])
        resp = MemberVerificationCreateView().post(req, client.pk)
        self.assertEqual(resp.status_code, 201, resp.data)

        prof = MemberDietaryProfile.objects.get(client=client)
        self.assertEqual(prof.conditions, ["Diabetic", "Pregnant"])
        self.assertEqual(prof.weeks_gestation, 24)
        # Postpartum not selected -> months_postpartum ignored (null).
        self.assertIsNone(prof.months_postpartum)

    def test_verification_defaults_conditions_to_no_restriction(self):
        from rest_framework.parsers import JSONParser
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from .models import Address, AddressType, Insurance, MemberDietaryProfile
        from .portal.views_members import MemberVerificationCreateView

        client = self._client("Def", "Ault")
        self._internal_case(client)
        Insurance.objects.create(client=client, plan_name="P", external_member_id="1")
        Address.objects.create(
            client=client, type=AddressType.CURRENT, zip="10001", street="1 St",
        )
        raw = APIRequestFactory().post(
            "/",
            {"members": [{"client_id": str(client.pk), "mobile_number": "3475550142"}], "zip": "10001"},
            format="json",
        )
        req = Request(raw, parsers=[JSONParser()])
        resp = MemberVerificationCreateView().post(req, client.pk)
        self.assertEqual(resp.status_code, 201, resp.data)
        prof = MemberDietaryProfile.objects.get(client=client)
        self.assertEqual(prof.conditions, ["No Restriction"])
        self.assertIsNone(prof.weeks_gestation)


class MainStageIneligibleTest(TestCase):
    """The headline stage bar's Eligibility node must read Ineligible whenever
    the client is on the hard INELIGIBLE off-ramp -- even when the enrollment
    roll-up would otherwise report Enrolled (live enrollment) or Cancelled (all
    enrollments cancelled), which previously hid the off-ramp."""

    def _client(self, stage):
        from .models import Client, ClientStage

        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="El", last_name="Igible",
            lifecycle_stage=stage,
        )

    def _enrollment(self, client, stage):
        from .models import EnrollmentVerification, Household, HouseholdMember

        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        return EnrollmentVerification.objects.create(
            client=client, household=hh, stage=stage,
        )

    def test_ineligible_wins_over_cancelled_enrollments(self):
        from .models import ClientStage, EnrollmentStage
        from .services.lifecycle import main_stage

        client = self._client(ClientStage.INELIGIBLE)
        self._enrollment(client, EnrollmentStage.CANCELLED)
        self.assertEqual(main_stage(client), ClientStage.INELIGIBLE)

    def test_ineligible_wins_over_live_enrollment(self):
        from .models import ClientStage, EnrollmentStage
        from .services.lifecycle import main_stage

        client = self._client(ClientStage.INELIGIBLE)
        self._enrollment(client, EnrollmentStage.SERVICE_ACTIVE)
        self.assertEqual(main_stage(client), ClientStage.INELIGIBLE)

    def test_non_ineligible_still_rolls_up_to_enrolled(self):
        from .models import ClientStage, EnrollmentStage
        from .services.lifecycle import main_stage

        client = self._client(ClientStage.ELIGIBLE)
        self._enrollment(client, EnrollmentStage.SERVICE_ACTIVE)
        self.assertEqual(main_stage(client), ClientStage.ENROLLED)


class ProgramStageOutOfRangeTest(TestCase):
    """An Out-of-Range (delivery-coverage) HOLD surfaces on the program stage as
    Out of Range, not a generic On Hold -- on both the accordion status
    (program_status) and the stage-bar Service phase (_service_phase). The main
    lifecycle stage separately keeps the Ineligible / Does Not Qualify off-ramp,
    so Out of Range even wins over Does Not Qualify on the Service phase."""

    def _held(self, member_status):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, MemberDietaryProfile,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ora", last_name="Nge",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.ON_HOLD,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, status=member_status,
        )
        return client, enr

    def test_out_of_range_hold_program_status(self):
        from .models import MemberStatus, ProgramStatus
        from .services.lifecycle import program_status

        _client, enr = self._held(MemberStatus.OUT_OF_RANGE)
        self.assertEqual(program_status(enr), ProgramStatus.OUT_OF_RANGE)

    def test_out_of_range_hold_service_phase(self):
        from .models import MemberStatus
        from .services.lifecycle import _service_phase

        client, enr = self._held(MemberStatus.OUT_OF_RANGE)
        self.assertEqual(_service_phase(client, enr, None), ("out_of_range", "Out of Range"))

    def test_plain_hold_still_on_hold(self):
        from .models import MemberStatus, ProgramStatus
        from .services.lifecycle import _service_phase, program_status

        client, enr = self._held(MemberStatus.PAUSED)  # held, but not out of range
        self.assertEqual(program_status(enr), ProgramStatus.ON_HOLD)
        self.assertEqual(_service_phase(client, enr, None), ("on_hold", "On Hold"))

    def test_out_of_range_wins_over_does_not_qualify_on_service_phase(self):
        # Both surfaces coexist: the MAIN lifecycle stage reads Ineligible, but
        # the per-program Service phase still shows Out of Range for the coverage
        # block (Out of Range takes precedence over Does Not Qualify here).
        from .models import ClientStage, MemberStatus
        from .services.lifecycle import _service_phase

        client, enr = self._held(MemberStatus.OUT_OF_RANGE)
        client.lifecycle_stage = ClientStage.INELIGIBLE
        client.save(update_fields=["lifecycle_stage"])
        self.assertEqual(_service_phase(client, enr, None), ("out_of_range", "Out of Range"))


class ExtensionCaseMetCouncilGateTest(TestCase):
    """A case written through the browser extension (an authenticated agent, so
    the request principal carries an ``agent_id``) must be MANAGED by Met
    Council. A case attributed to another org -- or one with a blank managing
    org -- is rejected. Import/daily-sync writes (no request in context) are
    unaffected, so their own gate + blank-org meal-case tolerance still stands.
    """

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Case", last_name="Owner",
        )

    def _ext_ctx(self, agent_code="900"):
        from types import SimpleNamespace

        # Mirror the real AgentUser principal: an authenticated agent always
        # carries ``agent_id``; ``agent_code`` may be null (no dialer extension).
        return {"request": SimpleNamespace(
            user=SimpleNamespace(
                agent_id=str(uuid.uuid4()), agent_code=agent_code, name="Casey CS",
            )
        )}

    def _payload(self, client, **over):
        data = {
            "case_id": str(uuid.uuid4()),
            "client_id": str(client.pk),
            "service_type": "Housing Navigation",
            "program_name": "Housing Navigation",
        }
        data.update(over)
        return data

    def _save(self, client, ctx, **over):
        from api.serializers import CaseSerializer

        ser = CaseSerializer(data=self._payload(client, **over), context=ctx)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_ext_rejects_non_met_council_org(self):
        from rest_framework.exceptions import ValidationError

        client = self._client()
        with self.assertRaises(ValidationError):
            self._save(client, self._ext_ctx(), provider_name="God's Love We Deliver")

    def test_ext_rejects_blank_org(self):
        from rest_framework.exceptions import ValidationError

        client = self._client()
        with self.assertRaises(ValidationError):
            self._save(client, self._ext_ctx())  # no provider id/name

    def test_ext_gate_applies_to_code_less_agent(self):
        # An authenticated agent with a NULL agent_code (no dialer extension)
        # still has an agent_id, so the gate must fire -- a non-Met-Council case
        # from such an agent must be rejected, not slip through.
        from rest_framework.exceptions import ValidationError

        client = self._client()
        with self.assertRaises(ValidationError):
            self._save(
                client, self._ext_ctx(agent_code=None),
                provider_name="God's Love We Deliver",
            )

    def test_ext_accepts_met_council_by_name(self):
        from api.models import Case
        from api.services.lifecycle import MET_COUNCIL_PROVIDER_NAME

        client = self._client()
        case = self._save(
            client, self._ext_ctx(), provider_name=MET_COUNCIL_PROVIDER_NAME
        )
        self.assertTrue(Case.objects.filter(pk=case.pk).exists())

    def test_ext_accepts_met_council_by_id(self):
        from api.models import Case
        from api.services.lifecycle import MET_COUNCIL_PROVIDER_ID

        client = self._client()
        case = self._save(
            client, self._ext_ctx(), provider_id=str(MET_COUNCIL_PROVIDER_ID)
        )
        self.assertTrue(Case.objects.filter(pk=case.pk).exists())

    def test_import_context_not_gated(self):
        # No request in context == import/daily-sync write: the serializer gate
        # must NOT fire, so a blank-org case still saves (imports pre-filter and
        # legitimately keep blank-org internal-service meal cases).
        from api.models import Case

        client = self._client()
        case = self._save(client, {})  # no request context, blank org
        self.assertTrue(Case.objects.filter(pk=case.pk).exists())


class VerificationSubmittedTimelineTest(TestCase):
    """Completing the verification wizard writes a 'Verification Submitted'
    timeline event whose metadata captures WHAT was verified (roster + menus,
    delivery address/days, confirmed checkboxes), so the History detail shows the
    verification data -- not just the bare stage change."""

    def _client(self, first, last):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last,
        )

    def _internal_case(self, client):
        from .models import Case, CaseStatus, CaseType

        return Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
        )

    def _api(self):
        agent = Agent.objects.create(name="Casey CS", agent_code="900", group="CS")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def test_verification_logs_submitted_event_with_metadata(self):
        from .models import TimelineEvent, TimelineEventType

        client = self._client("Vera", "Verified")
        self._internal_case(client)

        payload = {
            "program_name": "MTM Meals",
            "members": [{
                "client_id": str(client.pk), "member_name": "Vera Verified",
                "mobile_number": "3475550142",
                "food_allergies": ["peanuts"], "menu_type": "Standard",
            }],
            "street": "1 Main St", "apt": "2B", "city": "Brooklyn",
            "state": "NY", "zip": "11201",
            "delivery_weekdays": ["mon", "thu"],
            "is_family_verified": True,
            "medicaid_type_verified": True,
            "delivery_address_verified": True,
        }
        resp = self._api().post(
            f"/api/portal/members/{client.pk}/verification/", payload, format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        ev = TimelineEvent.objects.filter(
            client=client, event_type=TimelineEventType.VERIFICATION_COMPLETED,
        ).first()
        self.assertIsNotNone(ev)
        self.assertTrue(ev.metadata)
        self.assertEqual(
            ev.metadata.get("delivery_address"), "1 Main St, 2B, Brooklyn, NY 11201"
        )
        self.assertEqual(ev.metadata.get("delivery_weekdays"), ["mon", "thu"])
        # members is now a per-member clinical/dietary snapshot (list of dicts).
        members = ev.metadata.get("members", [])
        self.assertTrue(any(
            m.get("member_name") == "Vera Verified" and m.get("menu_type") == "Standard"
            and m.get("food_allergies") == ["peanuts"]
            for m in members
        ), members)
        self.assertEqual(ev.metadata.get("verified", {}).get("family"), True)
        # Governing case is now captured on the completion event.
        self.assertTrue(ev.metadata.get("governing_case_id"))

    def test_primary_mobile_required_and_persisted(self):
        from .models import MemberDietaryProfile

        client = self._client("Mo", "Bile")
        self._internal_case(client)
        api = self._api()
        base = {
            "program_name": "MTM Meals",
            "street": "1 Main St", "city": "Brooklyn", "state": "NY", "zip": "11201",
            "delivery_weekdays": ["mon", "thu"],
        }
        # Missing primary mobile -> rejected.
        resp = api.post(
            f"/api/portal/members/{client.pk}/verification/",
            {**base, "members": [{
                "client_id": str(client.pk), "member_name": "Mo Bile",
                "menu_type": "Standard",
            }]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("mobile_number", resp.json())

        # With a primary mobile -> accepted + persisted on the enrollment profile.
        resp = api.post(
            f"/api/portal/members/{client.pk}/verification/",
            {**base, "members": [{
                "client_id": str(client.pk), "member_name": "Mo Bile",
                "menu_type": "Standard", "mobile_number": "(347) 555-0142",
            }]},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        mv = MemberDietaryProfile.objects.get(client=client)
        self.assertEqual(mv.mobile_number, "(347) 555-0142")


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

    def test_new_household_member_inherits_out_of_range(self):
        # Adding a member to a household whose delivery ZIP is outside coverage
        # (so the existing members are already Out of Range) must set the new
        # member Out of Range too — a menu type can't fix a geographic block.
        from .models import (
            Address, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, MemberDietaryProfile, MemberStatus, Note, NoteSource,
        )
        from .serializers import sync_household_members

        primary = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pat", last_name="Primary",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        addr = Address.objects.create(client=primary, type="temporary", zip="11209")
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, delivery_address=addr,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=primary, menu_type="Standard",
            status=MemberStatus.OUT_OF_RANGE,
        )

        # New dependent added via the roster only (no profile yet).
        dep = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dee", last_name="Pendent",
        )
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)

        sync_household_members(primary, enrollment=enr)

        prof = MemberDietaryProfile.objects.get(enrollment=enr, client=dep)
        self.assertEqual(prof.status, MemberStatus.OUT_OF_RANGE)
        note = Note.objects.filter(client=dep, source=NoteSource.SYSTEM).first()
        self.assertIsNotNone(note)
        self.assertIn("11209", note.body)

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
        from .models import ClientStage, MemberStatus, Note, NoteSource
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
        # New behavior: off-ramped to Not Eligible + Paused (like the eligibility
        # off-ramp), NOT the Out-of-Range status.
        self.assertEqual(mv2.status, MemberStatus.PAUSED)
        self.assertTrue(mv2.eligibility_paused)
        mv2.client.refresh_from_db()
        self.assertEqual(mv2.client.lifecycle_stage, ClientStage.INELIGIBLE)
        note = Note.objects.filter(
            client=mv2.client, source=NoteSource.SYSTEM, body__icontains="primary address",
        ).first()
        self.assertIsNotNone(note)
        self.assertIn("11209", note.body)

    def test_enforce_sets_not_eligible_with_note_ticket_and_event(self):
        from .models import (
            ClientStage, MemberStatus, Note, NoteSource, Ticket,
            TicketStatus, TicketTypeCode, TimelineEvent,
        )
        from .portal.views_members import _enforce_delivery_coverage
        from .services.service_area import SERVICE_AREA_REASON

        mv = self._profile("11209")
        result = _enforce_delivery_coverage(mv.enrollment, None)
        self.assertEqual(len(result["out_of_range"]), 1)
        mv.refresh_from_db()
        # Member is PAUSED (eligibility) and their client is Not Eligible with the
        # stable coverage reason stored.
        self.assertEqual(mv.status, MemberStatus.PAUSED)
        self.assertTrue(mv.eligibility_paused)
        mv.client.refresh_from_db()
        self.assertEqual(mv.client.lifecycle_stage, ClientStage.INELIGIBLE)
        self.assertIn(SERVICE_AREA_REASON, mv.client.ineligible_reasons or [])
        note = Note.objects.filter(client=mv.client, source=NoteSource.SYSTEM).first()
        self.assertIsNotNone(note)
        self.assertTrue(
            TimelineEvent.objects.filter(
                client=mv.client, event_type="member_ineligible"
            ).exists()
        )
        # Out of range now also KEEPS the out_of_range tracking event alongside
        # the ineligible off-ramp.
        self.assertTrue(
            TimelineEvent.objects.filter(
                client=mv.client, event_type="out_of_range"
            ).exists()
        )
        # A Case Closure ticket was opened on the household primary (pre-filled ZIP).
        ticket = Ticket.objects.filter(
            client=mv.client, type__code=TicketTypeCode.CASE_CLOSURE,
        ).exclude(status=TicketStatus.RESOLVED).first()
        self.assertIsNotNone(ticket)
        self.assertIn("11209", ticket.reason)

    def test_serviceable_zip_does_not_auto_restore(self):
        # The out-of-range off-ramp is STICKY: editing the delivery ZIP back to a
        # serviceable one does NOT auto-restore the member -- an agent must resolve
        # the Not-Eligible state.
        from .models import (
            ClientStage, MemberStatus, Ticket, TicketStatus, TicketTypeCode,
        )
        from .portal.views_members import _enforce_delivery_coverage

        mv = self._profile("11209")
        _enforce_delivery_coverage(mv.enrollment, None)
        addr = mv.enrollment.delivery_address
        addr.zip = "10001"
        addr.save(update_fields=["zip"])
        # Re-running the coverage check on a serviceable ZIP is a no-op.
        _enforce_delivery_coverage(mv.enrollment, None)
        mv.refresh_from_db()
        mv.client.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.PAUSED)
        self.assertEqual(mv.client.lifecycle_stage, ClientStage.INELIGIBLE)
        # The Case Closure ticket stays open for the agent to resolve.
        self.assertTrue(
            Ticket.objects.filter(
                client=mv.client, type__code=TicketTypeCode.CASE_CLOSURE,
            ).exclude(status=TicketStatus.RESOLVED).exists()
        )

    def test_out_of_range_offramps_every_non_terminal_household_member(self):
        # An out-of-range DELIVERY ZIP is a household-wide geographic block: every
        # non-terminal member (Active, manually Paused, Out of Orbit) is off-ramped
        # to Not Eligible + Paused. Only terminal INACTIVE members are left alone.
        from .models import (
            Client, ClientStage, HouseholdMember, MemberDietaryProfile, MemberStatus,
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
            self.assertEqual(mv.status, MemberStatus.PAUSED)
            mv.client.refresh_from_db()
            self.assertEqual(mv.client.lifecycle_stage, ClientStage.INELIGIBLE)
        inactive.refresh_from_db()
        self.assertEqual(inactive.status, MemberStatus.INACTIVE)  # terminal, untouched


class KitchenExportSharedAddressTest(TestCase):
    """The PO / kitchen export must serve every household member at the SHARED
    verification delivery address (EnrollmentVerification.delivery_address), the
    same for the primary and every other member -- never each member's own
    standalone address."""

    def test_non_primary_member_exports_shared_enrollment_address(self):
        from .models import (
            Address, AddressType, Client, DeliveryOrder, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, PurchaseOrder, PurchaseOrderStatus,
        )
        from .services.purchase_orders import build_kitchen_export_rows

        primary = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pat", last_name="Primary",
        )
        dep = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dee", last_name="Pendent",
        )
        hh = Household.objects.create(name="Primary Household")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)

        # The dependent has their OWN standalone address -- it must NOT be used.
        Address.objects.create(
            client=dep, type=AddressType.CURRENT, street="999 Own St",
            city="Selfville", state="NY", zip="10002",
        )
        # The shared verification delivery address on the household enrollment.
        shared = Address.objects.create(
            client=primary, type=AddressType.DELIVERY, street="1 Shared Ave",
            unit="4B", city="Brooklyn", state="NY", zip="11201",
        )
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, delivery_address=shared,
            stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=primary)
        MemberDietaryProfile.objects.create(enrollment=enr, client=dep)

        po = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)
        DeliveryOrder.objects.create(purchase_order=po, member=primary, group=hh)
        DeliveryOrder.objects.create(purchase_order=po, member=dep, group=hh)

        _headers, rows = build_kitchen_export_rows(po)
        self.assertEqual(len(rows), 2)
        # Address columns: street=8, unit=9, city=10, state=11, zip=12.
        for row in rows:
            self.assertEqual(row[8], "1 Shared Ave")
            self.assertEqual(row[9], "4B")
            self.assertEqual(row[10], "Brooklyn")
            self.assertEqual(row[12], "11201")


class KitchenChangeManagementOnlyTest(TestCase):
    """Changing an ALREADY-assigned household kitchen via /assign-kitchen/ (the
    program tab's "Kitchen & Delivery" Change control) is open to any portal
    agent (the Management-only restriction was removed). The dedicated /kitchen/
    PATCH endpoint remains Management-only."""

    def _api(self, group):
        agent = Agent.objects.create(
            name=f"{group} Agent", agent_code=str(uuid.uuid4())[:8], group=group,
        )
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def _enrollment(self, *, with_kitchen):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            Kitchen, KitchenStatus, MemberDietaryProfile,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pat", last_name="Primary",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        kitchen = (
            Kitchen.objects.create(name="K1", status=KitchenStatus.ACTIVE)
            if with_kitchen else None
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            kitchen=kitchen,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=client)
        return client

    def _assign_url(self, client):
        return f"/api/portal/members/{client.pk}/assign-kitchen/"

    def test_non_management_can_change_assigned_kitchen(self):
        # Kitchen already assigned + non-management agent -> NO LONGER 403. The
        # empty body then fails normal validation (400), proving the old
        # Management-only guard was removed (logistics can change the kitchen).
        client = self._enrollment(with_kitchen=True)
        resp = self._api("CS").post(self._assign_url(client), {}, format="json")
        self.assertNotEqual(resp.status_code, 403)
        self.assertEqual(resp.status_code, 400)

    def test_management_not_blocked_by_kitchen_change_guard(self):
        # Management passes the guard; an empty body then fails normal validation
        # (400) -- proving the management guard did NOT block them.
        client = self._enrollment(with_kitchen=True)
        resp = self._api("Management").post(self._assign_url(client), {}, format="json")
        self.assertNotEqual(resp.status_code, 403)
        self.assertEqual(resp.status_code, 400)

    def test_initial_assignment_open_to_non_management(self):
        # No kitchen yet -> the management guard doesn't apply; an empty body
        # fails normal validation (400), NOT the management 403.
        client = self._enrollment(with_kitchen=False)
        resp = self._api("Logistics").post(self._assign_url(client), {}, format="json")
        self.assertNotEqual(resp.status_code, 403)
        self.assertEqual(resp.status_code, 400)

    def test_member_kitchen_patch_is_management_only(self):
        # The dedicated /kitchen/ PATCH endpoint is locked to Management outright.
        client = self._enrollment(with_kitchen=True)
        cs = self._api("CS").patch(
            f"/api/portal/members/{client.pk}/kitchen/", {"kitchen_id": None},
            format="json",
        )
        self.assertEqual(cs.status_code, 403)


class EnrollmentHistoryViewTest(TestCase):
    """The Program tab's History sub-tab: the enrollment-scoped timeline. Events
    are filtered to THIS enrollment (regardless of which household member each
    is logged on) and bounded at the verification-completion start; the window
    end reflects the governing case close / authorization expiry."""

    def _api(self):
        agent = Agent.objects.create(
            name="CS Agent", agent_code=str(uuid.uuid4())[:8], group="CS",
        )
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def _enrollment(self, *, case=None, verified_at=None):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pat", last_name="Primary",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            case=case, verified_at=verified_at,
            program_name="Medically Tailored Meals",
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=client)
        return client, enr

    def _url(self, client, enr):
        return f"/api/portal/members/{client.pk}/enrollments/{enr.pk}/history/"

    def test_filters_to_enrollment_and_window_start(self):
        from datetime import timedelta

        from .models import TimelineEvent, TimelineEventType

        verified = timezone.now() - timedelta(days=2)
        client, enr = self._enrollment(verified_at=verified)
        # Pre-verification event -> excluded by the window start.
        TimelineEvent.objects.create(
            client=client, enrollment=enr, title="Old edit",
            event_type=TimelineEventType.DIETARY_CHANGED,
            occurred_at=verified - timedelta(days=1),
        )
        # Post-verification event -> included.
        inside = TimelineEvent.objects.create(
            client=client, enrollment=enr, title="Out of Orbit",
            event_type=TimelineEventType.OUT_OF_ORBIT,
            occurred_at=verified + timedelta(hours=3),
        )
        # A different enrollment's event -> excluded.
        _, other_enr = self._enrollment(verified_at=verified)
        TimelineEvent.objects.create(
            client=client, enrollment=other_enr, title="Other program",
            event_type=TimelineEventType.DIETARY_CHANGED,
            occurred_at=verified + timedelta(hours=4),
        )

        resp = self._api().get(self._url(client, enr))
        self.assertEqual(resp.status_code, 200)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertEqual(ids, [inside.pk])
        self.assertTrue(resp.data["window_open"])
        self.assertIsNone(resp.data["window_end"])

    def test_window_end_reflects_closed_case(self):
        from datetime import timedelta

        from .models import Case, CaseStatus, CaseType

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Cee", last_name="Closed",
        )
        closed_at = timezone.now()
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.CLOSED,
            case_closed_at=closed_at,
        )
        # Reuse the enrollment factory but point it at the same client + case.
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
        )

        hh = Household.objects.create(name="HH2")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            case=case, verified_at=closed_at - timedelta(days=10),
        )

        resp = self._api().get(self._url(client, enr))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["window_open"])
        self.assertIsNotNone(resp.data["window_end"])


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

    def test_paused_member_not_auto_resumed_by_kitchen_assignment(self):
        # A manually PAUSED member (e.g. the non-primary members of an
        # individual-scope case) must stay PAUSED when the kitchen-aware meal rule
        # runs for the whole household at kitchen assignment -- even though the
        # assigned kitchen COULD serve them. Regression: kitchen assignment was
        # silently un-pausing paused members, activating an individual case's
        # dependents for service.
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        kitchen = self._kosher_kitchen()
        mv = self._profile(["shellfish", "pork"], status=MemberStatus.PAUSED)
        out, became, reason = reconcile_member_kitchen_output(mv, kitchen, save=True)
        self.assertFalse(out)
        self.assertFalse(became)
        self.assertEqual(reason, "")
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.PAUSED)
        # The pause also leaves the kitchen meal result untouched (not recomputed).
        self.assertEqual(mv.kitchen_meal_type, "")

    def test_inactive_member_not_auto_resumed(self):
        # A terminal INACTIVE member (service ended) is likewise never revived by
        # the automatic meal rule.
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        kitchen = self._kosher_kitchen()
        mv = self._profile(["shellfish", "pork"], status=MemberStatus.INACTIVE)
        reconcile_member_kitchen_output(mv, kitchen, save=True)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.INACTIVE)

    def test_out_of_range_member_not_reactivated_by_kitchen_assignment(self):
        # Out of Range is a ZIP-coverage block a kitchen change can't fix, so the
        # automatic meal rule (kitchen assignment) must respect it -- even when
        # the member's ZIP is otherwise serviceable and the kitchen could serve
        # them. Only the explicit restore-range flow (allow_resume=True) may bring
        # them back.
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        kitchen = self._kosher_kitchen()
        mv = self._profile(["shellfish", "pork"], status=MemberStatus.OUT_OF_RANGE)
        out, became, reason = reconcile_member_kitchen_output(mv, kitchen, save=True)
        self.assertFalse(out)
        self.assertFalse(became)
        self.assertEqual(reason, "")
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.OUT_OF_RANGE)

    def test_out_of_range_member_restored_only_with_allow_resume(self):
        # The explicit restore-range flow passes allow_resume=True; with a
        # serviceable ZIP (no address set here) the member returns to Active.
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        kitchen = self._kosher_kitchen()
        mv = self._profile(["shellfish", "pork"], status=MemberStatus.OUT_OF_RANGE)
        out, _became, _reason = reconcile_member_kitchen_output(
            mv, kitchen, save=True, allow_resume=True,
        )
        self.assertFalse(out)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.ACTIVE)

    def test_paused_member_resumed_only_with_allow_resume(self):
        # The explicit unpause/resume flow passes allow_resume=True, which DOES
        # let the meal rule move the member off PAUSED (to Active here, since the
        # kitchen can serve the Kosher + Pork/Shellfish combo).
        from .models import MemberStatus
        from .services.meal_rules import reconcile_member_kitchen_output

        kitchen = self._kosher_kitchen()
        mv = self._profile(["shellfish", "pork"], status=MemberStatus.PAUSED)
        out, _became, _reason = reconcile_member_kitchen_output(
            mv, kitchen, save=True, allow_resume=True,
        )
        self.assertFalse(out)
        mv.refresh_from_db()
        self.assertEqual(mv.status, MemberStatus.ACTIVE)
        self.assertEqual(mv.kitchen_meal_type, "Kosher")


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


class ProgramTracksTest(TestCase):
    """lifecycle.program_tracks: the Phase 7 per-program display tracks that feed
    the redesigned member stage bar. Each program decomposes into Authorization
    -> Verification -> Service phase + status."""

    def _setup(self, *, stage, auth, case_status=None, client_stage="active",
               household_type=None, nutritionist_approved=True):
        from .models import (
            Case, CaseHouseholdType, CaseStatus, CaseType, Client,
            EnrollmentVerification, Household, HouseholdMember,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pro", last_name="Gram",
            lifecycle_stage=client_stage,
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        ht = household_type or CaseHouseholdType.INDIVIDUAL
        # program_tracks derives the Household/Individual scope LIVE from the
        # PROGRAM NAME (the source of truth), so a Household case must carry the
        # "(Household)" keyword for the scope to read Household.
        program_name = (
            "MTM - (Household) Program"
            if ht == CaseHouseholdType.HOUSEHOLD else "MTM"
        )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=case_status or CaseStatus.OPEN,
            service_type="Medically Tailored Meals", program_name=program_name,
            service_authorization_status=auth,
            household_type=ht,
        )
        EnrollmentVerification.objects.create(
            client=client, household=hh, stage=stage,
            # Post-verification households on the bar have cleared the Nutritionist
            # gate by default (so the Service phase can reach kitchen/active); set
            # False to exercise the Pending Nutritionist state.
            nutritionist_approved_at=timezone.now() if nutritionist_approved else None,
        )
        return client

    def test_no_internal_service_case_is_empty(self):
        from .models import Client
        from .services.lifecycle import program_tracks

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="No", last_name="Case"
        )
        self.assertEqual(program_tracks(c), [])

    def test_single_meals_program_approved_awaiting_kitchen(self):
        from .models import EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        c = self._setup(
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
            auth=ServiceAuthorizationStatus.APPROVED,
        )
        tracks = program_tracks(c)
        self.assertEqual(len(tracks), 1)
        t = tracks[0]
        self.assertEqual(t["service_type"], "Meals")
        self.assertTrue(t["governing"])
        self.assertEqual(t["scope"]["label"], "Individual")
        self.assertEqual(t["authorization"]["label"], "Approved")
        self.assertEqual(t["verification"]["label"], "Verified")
        self.assertEqual(t["service"]["label"], "Kitchen Assignment")

    def test_verified_approved_shows_waiting_for_kitchen(self):
        """A VERIFIED enrollment on an APPROVED case (verification complete but
        not yet advanced to the KITCHEN_ASSIGNMENT stage) reads
        "Waiting for Kitchen Assignment" on the Service phase -- not blank."""
        from .models import EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        c = self._setup(
            stage=EnrollmentStage.VERIFIED,
            auth=ServiceAuthorizationStatus.APPROVED,
        )
        t = program_tracks(c)[0]
        self.assertEqual(t["verification"]["label"], "Verified")
        self.assertEqual(t["service"]["value"], "waiting_kitchen")
        self.assertEqual(t["service"]["label"], "Kitchen Assignment")

    def test_verified_approved_pending_nutritionist_service_blank(self):
        """A VERIFIED + APPROVED household that has NOT been Nutritionist-approved
        is Pending Nutritionist -- it has NOT reached kitchen assignment, so the
        Service phase stays blank (not 'Kitchen Assignment')."""
        from .models import EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        c = self._setup(
            stage=EnrollmentStage.VERIFIED,
            auth=ServiceAuthorizationStatus.APPROVED,
            nutritionist_approved=False,
        )
        t = program_tracks(c)[0]
        self.assertEqual(t["nutritionist"]["value"], "pending")
        self.assertEqual(t["service"]["value"], "")

    def test_verified_pending_auth_keeps_service_blank(self):
        """A VERIFIED enrollment whose authorization is still pending/requested
        (not yet approved) keeps the Service phase blank -- the Authorization
        phase carries the pending state until approval lands."""
        from .models import EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        c = self._setup(
            stage=EnrollmentStage.VERIFIED,
            auth=ServiceAuthorizationStatus.PENDING,
        )
        t = program_tracks(c)[0]
        self.assertEqual(t["authorization"]["label"], "Requested")
        self.assertEqual(t["service"]["label"], "")

    def test_household_scope_label(self):
        from .models import (
            CaseHouseholdType, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import program_tracks

        t = program_tracks(self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE,
            auth=ServiceAuthorizationStatus.APPROVED,
            household_type=CaseHouseholdType.HOUSEHOLD,
        ))[0]
        self.assertEqual(t["scope"]["value"], "household")
        self.assertEqual(t["scope"]["label"], "Household")

    def test_non_food_program_named_from_service_type(self):
        """A non-food internal-service program (e.g. Housing) is its own branch,
        named from its service_type, with Authorization from its own case but
        Verification/Service blank (NOT borrowed from the food enrollment)."""
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import program_tracks

        # Meals is Approved (governs); Housing is a second internal-service case.
        client = self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE,
            auth=ServiceAuthorizationStatus.APPROVED,
        )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_type="Housing", service_category="Housing",
            program_name="Housing Program",
            service_authorization_status=ServiceAuthorizationStatus.PENDING,
        )
        tracks = program_tracks(client)
        self.assertEqual(len(tracks), 2)
        housing = next(t for t in tracks if t["category"] == "Housing")
        self.assertEqual(housing["service_type"], "Housing")
        self.assertEqual(housing["service_type_value"], "housing")
        self.assertFalse(housing["governing"])
        self.assertEqual(housing["authorization"]["label"], "Requested")
        self.assertEqual(housing["verification"]["label"], "")
        self.assertEqual(housing["service"]["label"], "")
        # The food program still resolves its phases from the enrollment.
        meals = next(t for t in tracks if t["service_type"] == "Meals")
        self.assertTrue(meals["governing"])
        self.assertEqual(meals["service"]["label"], "Active")

    def test_two_cases_same_kind_show_as_separate_tracks(self):
        """Two Meals internal-service cases produce TWO tracks (not grouped): the
        governing (Approved) one carries the Verification/Service phases; the
        other (Pending) shows Authorization only."""
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import program_tracks

        client = self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE,
            auth=ServiceAuthorizationStatus.APPROVED,
        )
        second = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_type="Medically Tailored Meals", program_name="MTM 2",
            service_authorization_status=ServiceAuthorizationStatus.PENDING,
        )
        tracks = program_tracks(client)
        self.assertEqual(len(tracks), 2)
        self.assertTrue(all(t["service_type"] == "Meals" for t in tracks))
        gov = next(t for t in tracks if t["governing"])
        other = next(t for t in tracks if not t["governing"])
        self.assertEqual(str(other["case_id"]), str(second.case_id))
        # Governing carries the household service phases.
        self.assertEqual(gov["authorization"]["label"], "Approved")
        self.assertEqual(gov["service"]["label"], "Active")
        # The competing (duplicate) case shares the household verification but
        # is not separately serviced -> "Duplicated".
        self.assertEqual(other["authorization"]["label"], "Requested")
        self.assertEqual(other["verification"]["label"], "Verified")
        self.assertEqual(other["service"]["label"], "Duplicated")
        self.assertEqual(other["service"]["value"], "duplicated")

    def test_non_governing_closed_case_is_not_shown(self):
        # A closed/cancelled NON-governing internal-service case drops off the
        # bar (only the open governing case remains).
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import program_tracks

        client = self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE,
            auth=ServiceAuthorizationStatus.APPROVED,
        )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.CLOSED,
            service_type="Medically Tailored Meals", program_name="MTM closed",
            service_authorization_status=ServiceAuthorizationStatus.DENIED,
        )
        tracks = program_tracks(client)
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0]["governing"])

    def test_governing_case_shown_even_when_closed(self):
        # The governing case is surfaced regardless of case STATUS: a member
        # whose only (governing) case is closed still shows it on the bar, with
        # the Service phase reading the closed state.
        from .models import (
            CaseStatus, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import program_tracks

        client = self._setup(
            stage=EnrollmentStage.SERVICE_COMPLETE,
            auth=ServiceAuthorizationStatus.APPROVED,
            case_status=CaseStatus.CLOSED,
        )
        tracks = program_tracks(client)
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0]["governing"])
        self.assertEqual(tracks[0]["service"]["value"], "closed")
        self.assertEqual(tracks[0]["case_status"], "closed")

    def test_closed_case_over_cancelled_enrollment_reads_closed(self):
        # Regression (member CHAYA ABOUD e85b695c): a CLOSED governing case that
        # sits atop a CANCELLED enrollment must read "Closed" on the Service
        # phase -- the CASE outcome wins over the enrollment stage, which
        # previously leaked "Canceled".
        from .models import (
            CaseStatus, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import program_tracks

        client = self._setup(
            stage=EnrollmentStage.CANCELLED,
            auth=ServiceAuthorizationStatus.APPROVED,
            case_status=CaseStatus.CLOSED,
        )
        t = program_tracks(client)[0]
        self.assertEqual(t["service"]["value"], "closed")
        self.assertEqual(t["service"]["label"], "Closed")
        self.assertEqual(t["case_status"], "closed")

    def test_open_governing_wins_and_hides_closed_non_governing(self):
        # An OPEN approved case governs over a CLOSED one; the closed
        # (non-governing) case still drops off, so only the open governing
        # program renders.
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import program_tracks

        client = self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE,
            auth=ServiceAuthorizationStatus.APPROVED,
        )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.CLOSED,
            service_type="Medically Tailored Meals", program_name="MTM old",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
        )
        tracks = program_tracks(client)
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0]["governing"])
        self.assertEqual(tracks[0]["service"]["value"], "active")

    def test_verification_not_requested_without_enrollment(self):
        # A food case with NO enrollment (no verification request raised yet)
        # must read "Not Requested" -- NOT "Pending Verification", which would
        # imply a request the (correctly hidden) verification button won't offer.
        from .models import (
            Case, CaseStatus, CaseType, Client, Household, HouseholdMember,
        )
        from .services.lifecycle import program_tracks

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="No", last_name="Enr",
            lifecycle_stage="assessment",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_type="Medically Tailored Meals", program_name="MTM",
        )
        t = program_tracks(client)[0]
        self.assertEqual(t["verification"]["value"], "not_requested")
        self.assertEqual(t["verification"]["label"], "Not Requested")

    def test_pending_verification_requested_authorization(self):
        # A PENDING (Unite Us requested/open/deferred) authorization reads the
        # real "Requested" state on the bar -- not a generic "Waiting".
        from .models import EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        t = program_tracks(self._setup(
            stage=EnrollmentStage.PENDING_VERIFICATION,
            auth=ServiceAuthorizationStatus.PENDING,
        ))[0]
        self.assertEqual(t["authorization"]["value"], "requested")
        self.assertEqual(t["authorization"]["label"], "Requested")
        self.assertEqual(t["verification"]["label"], "Pending Verification")
        self.assertEqual(t["service"]["label"], "")

    def test_never_requested_hidden_from_bar(self):
        # A NEVER_REQUESTED authorization is treated like a denial AND is hidden
        # from the stage bar entirely -- an open case that never had an
        # authorization requested is not surfaced as a program track.
        from .models import EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        c = self._setup(
            stage=EnrollmentStage.VERIFIED,
            auth=ServiceAuthorizationStatus.NEVER_REQUESTED,
        )
        self.assertEqual(program_tracks(c), [])

    def test_second_food_kind_conflicts_and_shares_verification(self):
        # A non-governing, DIFFERENT-kind food case (e.g. a Boxes case alongside
        # a governing Meals case) reads the household-wide "Verified" and is
        # flagged "Conflicting" (a household runs one food program; Meals/Boxes
        # are subtypes). (A NEVER_REQUESTED second case would be hidden entirely,
        # so a still-shown DENIED case is used here.)
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import program_tracks

        client = self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE,
            auth=ServiceAuthorizationStatus.APPROVED,
        )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_type="Produce Prescription/Voucher", program_name="Boxes",
            service_authorization_status=ServiceAuthorizationStatus.DENIED,
        )
        tracks = program_tracks(client)
        boxes = next(t for t in tracks if t["service_type"] == "Boxes")
        self.assertFalse(boxes["governing"])
        self.assertEqual(boxes["authorization"]["label"], "Denied")
        self.assertEqual(boxes["verification"]["label"], "Verified")
        self.assertEqual(boxes["service"]["value"], "conflicting")
        self.assertEqual(boxes["service"]["label"], "Conflicting")

    def test_denied_authorization(self):
        from .models import EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        t = program_tracks(self._setup(
            stage=EnrollmentStage.VERIFIED,
            auth=ServiceAuthorizationStatus.DENIED,
        ))[0]
        self.assertEqual(t["authorization"]["label"], "Denied")
        self.assertEqual(t["verification"]["label"], "Verified")

    def test_active_service(self):
        from .models import EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        t = program_tracks(self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE,
            auth=ServiceAuthorizationStatus.APPROVED,
        ))[0]
        self.assertEqual(t["service"]["label"], "Active")

    def test_does_not_qualify_when_ineligible(self):
        from .models import ClientStage, EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import program_tracks

        t = program_tracks(self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE,
            auth=ServiceAuthorizationStatus.APPROVED,
            client_stage=ClientStage.INELIGIBLE,
        ))[0]
        self.assertEqual(t["service"]["label"], "Does Not Qualify")


class ResumeBlockedWhenIneligibleTest(TestCase):
    """A program held because a member is on the hard INELIGIBLE off-ramp must
    NOT be manually resumable: the resume endpoint rejects it (only recovering
    the eligibility data lifts the hold), and the household payload reports
    ``can_resume=False`` so the frontend hides the Resume button."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Elig Agent", agent_code="913", group="CS"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _held_client(self, lifecycle_stage):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, Kitchen,
            KitchenStatus, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import advance_enrollment

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Held", last_name="Member",
            lifecycle_stage=lifecycle_stage,
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        # Realistic serving enrollment: verified, an OPEN APPROVED case within its
        # window, and an assigned kitchen -- so a resume can validly reach Service
        # Active (the resume resolver re-validates all three).
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now - timedelta(days=1),
            service_authorization_approval_ends_at=now + timedelta(days=90),
        )
        kitchen = Kitchen.objects.create(name="ENG", status=KitchenStatus.ACTIVE)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case, kitchen=kitchen,
            stage=EnrollmentStage.SERVICE_ACTIVE, verified_at=now,
        )
        advance_enrollment(enr, EnrollmentStage.ON_HOLD, force=True)
        enr.refresh_from_db()
        return client, enr

    def test_resume_rejected_for_ineligible(self):
        from .models import ClientStage, EnrollmentStage

        client, enr = self._held_client(ClientStage.INELIGIBLE)
        resp = self.api.post(
            f"/api/portal/members/{client.client_id}/resume/",
            {"reason": "try to resume"}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Ineligible", resp.json().get("error", ""))
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.ON_HOLD)

    def test_resume_allowed_for_eligible(self):
        from .models import ClientStage, EnrollmentStage

        client, enr = self._held_client(ClientStage.ACTIVE)
        resp = self.api.post(
            f"/api/portal/members/{client.client_id}/resume/",
            {"reason": "resume"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.SERVICE_ACTIVE)

    def test_household_payload_reports_can_resume_false_when_ineligible(self):
        from .models import ClientStage

        client, _enr = self._held_client(ClientStage.INELIGIBLE)
        resp = self.api.get(f"/api/portal/members/{client.client_id}/household/")
        self.assertEqual(resp.status_code, 200, resp.content)
        enrollment = resp.json()["enrollment"]
        self.assertTrue(enrollment["on_hold"])
        self.assertFalse(enrollment["can_resume"])

    def test_resume_allowed_with_ineligible_dependent(self):
        # An ineligible DEPENDENT is paused individually and must NOT block the
        # whole program's resume when the case-holder (and thus the household) can
        # still be served: the resume succeeds and the dependent stays excluded.
        from .models import (
            Client, ClientStage, EnrollmentStage, HouseholdMember,
            MemberDietaryProfile, MemberStatus,
        )

        holder, enr = self._held_client(ClientStage.ACTIVE)
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=holder, status=MemberStatus.ACTIVE,
        )
        dep = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dep", last_name="X",
            lifecycle_stage=ClientStage.INELIGIBLE,
        )
        HouseholdMember.objects.create(
            household=enr.household, client=dep, is_primary=False,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=dep, status=MemberStatus.PAUSED,
            eligibility_paused=True,
        )
        resp = self.api.post(
            f"/api/portal/members/{holder.client_id}/resume/",
            {"reason": "resume"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.SERVICE_ACTIVE)


class ProgramLockedGuardTest(TestCase):
    """When the governing internal-service case is CLOSED (no open case remains),
    the member's program tab is frozen: every write endpoint rejects with 400 and
    the Household GET reports ``program_locked=True`` with ``can_hold`` /
    ``can_resume`` forced False. An OPEN governing case leaves them all working.
    Belt-and-suspenders behind the frontend controls (guard actions, layered)."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Lock Agent", agent_code="944", group="CS"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _client(self, *, case_status):
        """A primary + household + SERVICE_ACTIVE enrollment, plus a single
        internal-service case in the given status (its governing status decides
        whether the program is locked)."""
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Lock", last_name="Member",
        )
        Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=case_status or CaseStatus.OPEN,
            program_name="Medically Tailored Meals",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        mp = MemberDietaryProfile.objects.create(enrollment=enr, client=client)
        return client, enr, mp

    def test_household_get_reports_locked_for_closed_case(self):
        from .models import CaseStatus

        client, _enr, _mp = self._client(case_status=CaseStatus.CLOSED)
        resp = self.api.get(f"/api/portal/members/{client.client_id}/household/")
        self.assertEqual(resp.status_code, 200, resp.content)
        enrollment = resp.json()["enrollment"]
        self.assertTrue(enrollment["program_locked"])
        self.assertFalse(enrollment["can_hold"])
        self.assertFalse(enrollment["can_resume"])

    def test_household_get_not_locked_for_open_case(self):
        from .models import CaseStatus

        client, _enr, _mp = self._client(case_status=CaseStatus.OPEN)
        resp = self.api.get(f"/api/portal/members/{client.client_id}/household/")
        self.assertEqual(resp.status_code, 200, resp.content)
        enrollment = resp.json()["enrollment"]
        self.assertFalse(enrollment["program_locked"])
        self.assertTrue(enrollment["can_hold"])

    def test_hold_rejected_on_closed_program(self):
        from .models import CaseStatus, EnrollmentStage

        client, enr, _mp = self._client(case_status=CaseStatus.CLOSED)
        resp = self.api.post(
            f"/api/portal/members/{client.client_id}/hold/",
            {"reason": "try to hold"}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("closed", resp.json().get("error", "").lower())
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.SERVICE_ACTIVE)

    def test_address_edit_rejected_on_closed_program(self):
        from .models import CaseStatus

        client, _enr, _mp = self._client(case_status=CaseStatus.CLOSED)
        resp = self.api.patch(
            f"/api/portal/members/{client.client_id}/household/",
            {"unit": "Apt 9"}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_dietary_edit_rejected_on_closed_program(self):
        from .models import CaseStatus

        client, _enr, mp = self._client(case_status=CaseStatus.CLOSED)
        resp = self.api.patch(
            f"/api/portal/members/{client.client_id}/household/members/{mp.pk}/",
            {"menu_type": "Standard"}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_address_edit_allowed_on_open_program(self):
        from .models import CaseStatus

        client, _enr, _mp = self._client(case_status=CaseStatus.OPEN)
        resp = self.api.patch(
            f"/api/portal/members/{client.client_id}/household/",
            {"unit": "Apt 9"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)


class ResumeAfterReactivationTest(TestCase):
    """Regression: Resume must never send a household back into a terminal stage.

    A member CANCELLED then REACTIVATED lands in On Hold via a
    ``cancelled -> on_hold`` StageEvent. Resume used to return to the most-recent
    hold's ``from_stage`` -- which was ``cancelled`` -- so every Resume click
    re-cancelled the household (prod: a member stuck in an endless
    reactivate/resume loop despite an open, approved case). Resume must land on
    the real service stage the member was held from (Kitchen Assignment here)."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Res Agent", agent_code="912", group="CS"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_resume_after_reactivation_does_not_recancel(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember,
        )
        from .services.lifecycle import advance_enrollment

        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, ServiceAuthorizationStatus,
        )

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Loop", last_name="Member"
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        # Verified + OPEN APPROVED case but NO kitchen -> a valid resume lands at
        # Kitchen Assignment (kitchen can't serve), never back into CANCELLED.
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now - timedelta(days=1),
            service_authorization_approval_ends_at=now + timedelta(days=90),
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT, verified_at=now,
        )
        # Reproduce the history: held from Kitchen Assignment, cancelled, then
        # reactivated back to On Hold (a `cancelled -> on_hold` event, which is
        # now the most-recent transition INTO on_hold).
        advance_enrollment(enr, EnrollmentStage.ON_HOLD, force=True)
        advance_enrollment(enr, EnrollmentStage.CANCELLED, force=True)
        advance_enrollment(enr, EnrollmentStage.ON_HOLD, force=True)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.ON_HOLD)

        resp = self.api.post(
            f"/api/portal/members/{client.client_id}/resume/",
            {"reason": "Member requested to resume meals"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        enr.refresh_from_db()
        # The bug resumed to CANCELLED; the fix resumes to the real held-from
        # service stage (Kitchen Assignment).
        self.assertEqual(
            EnrollmentStage(enr.stage), EnrollmentStage.KITCHEN_ASSIGNMENT
        )


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
        # Disregard no longer writes a timeline event (the enrollment note is the
        # record); the DISREGARDED stage is deliberately not surfaced on history.
        self.assertFalse(
            TimelineEvent.objects.filter(
                client=client, event_type=TimelineEventType.VERIFICATION_DISREGARDED
            ).exists()
        )

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
        # No timeline event for a disregard (kept as an audit Note only).
        self.assertFalse(
            TimelineEvent.objects.filter(
                client=client, event_type=TimelineEventType.VERIFICATION_DISREGARDED
            ).exists()
        )

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

    def _member(self, *, status=None, stage=None, case_status=None, auth=None,
                lifecycle=None, elig_paused=False):
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus, ServiceAuthorizationStatus,
        )

        client = Client.objects.create(
            client_id=uuid.uuid4(), first_name="Dash", last_name="Board",
            **({"lifecycle_stage": lifecycle} if lifecycle else {}),
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        # Every dashboard serving/watchlist reason is gated to the member's
        # GOVERNING internal-service case. Give them one -- APPROVED + OPEN by
        # default (blank/never_requested never govern), pass case_status/auth to
        # vary it.
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=case_status or CaseStatus.OPEN,
            service_authorization_status=auth or ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals",
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=stage or EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
            status=status or MemberStatus.ACTIVE,
            eligibility_paused=elig_paused,
        )
        return client

    def test_programs_on_hold_and_members_paused_split(self):
        # On Hold (governing enrollment ON_HOLD, Eligible) vs Members Paused,
        # itself split by who paused: agent (manual) vs eligibility (auto).
        from .models import (
            CaseStatus, ClientStage, EnrollmentStage, MemberStatus,
        )
        from .portal.views_dashboard import serving_client_ids

        # Paused by an AGENT (manual) on an open program.
        agent_paused = self._member(
            status=MemberStatus.PAUSED, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        # Paused by ELIGIBILITY (auto) -- these are typically on the Ineligible
        # off-ramp, and must STILL be counted (no Eligible filter for paused).
        elig_paused = self._member(
            status=MemberStatus.PAUSED, stage=EnrollmentStage.SERVICE_ACTIVE,
            elig_paused=True, lifecycle=ClientStage.INELIGIBLE,
        )
        # Paused but NO open internal-service case -> excluded from both.
        paused_closed = self._member(
            status=MemberStatus.PAUSED, stage=EnrollmentStage.SERVICE_ACTIVE,
            case_status=CaseStatus.CLOSED,
        )
        on_hold = self._member(stage=EnrollmentStage.ON_HOLD)  # status Active
        on_hold_inelig = self._member(
            stage=EnrollmentStage.ON_HOLD, lifecycle=ClientStage.INELIGIBLE
        )
        active = self._member()

        on_hold_ids = serving_client_ids("programs_on_hold", start=None, end=None)
        agent_ids = serving_client_ids("members_paused_agent", start=None, end=None)
        elig_ids = serving_client_ids("members_paused_eligibility", start=None, end=None)

        # On Hold: eligible governing-on-hold programs only.
        self.assertIn(on_hold.client_id, on_hold_ids)
        self.assertNotIn(on_hold_inelig.client_id, on_hold_ids)  # must be Eligible
        # Paused split: agent vs eligibility, each in its own bucket only.
        self.assertIn(agent_paused.client_id, agent_ids)
        self.assertNotIn(agent_paused.client_id, elig_ids)
        self.assertIn(elig_paused.client_id, elig_ids)
        self.assertNotIn(elig_paused.client_id, agent_ids)
        # No open internal-service case -> not counted as paused.
        self.assertNotIn(paused_closed.client_id, agent_ids | elig_ids)
        # A plain active member is in none.
        self.assertNotIn(active.client_id, on_hold_ids | agent_ids | elig_ids)

    def test_closed_governing_case_excludes_from_reasons(self):
        # Every serving/watchlist reason is gated to an OPEN governing case: a
        # member whose governing internal-service case is CLOSED must not be
        # flagged (they're no longer being served).
        from .models import CaseStatus, MemberStatus
        from .portal.views_dashboard import serving_client_ids

        open_m = self._member(status=MemberStatus.OUT_OF_ORBIT)
        closed_m = self._member(
            status=MemberStatus.OUT_OF_ORBIT, case_status=CaseStatus.CLOSED
        )
        oob = serving_client_ids("out_of_orbit", start=None, end=None)
        self.assertIn(open_m.client_id, oob)
        self.assertNotIn(closed_m.client_id, oob)
        # Same gate on a watchlist reason.
        social = serving_client_ids("no_social_coverage", start=None, end=None)
        self.assertIn(open_m.client_id, social)
        self.assertNotIn(closed_m.client_id, social)

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

    def test_insurance_expiring_flags_enrolled_members_without_active_medicaid(self):
        # The "insurance_expiring" watchlist is a STATUS-based "no active
        # Medicaid" rule (imports don't reliably carry a coverage end date, so an
        # expiry-date window can't be trusted). Enrolled members with an ACTIVE
        # Medicaid / Dual plan are cleared; everyone else is flagged.
        from .models import Insurance, InsurancePlanType, RecordStatus
        from .portal.views_dashboard import serving_client_ids

        # Active Medicaid on file -> NOT on the watchlist.
        has_medicaid = self._member()
        Insurance.objects.create(
            client=has_medicaid, plan_type=InsurancePlanType.MEDICAID,
            status=RecordStatus.ACTIVE,
        )
        # Active Dual (Medicare/Medicaid) also counts as Medicaid -> NOT flagged.
        has_dual = self._member()
        Insurance.objects.create(
            client=has_dual, plan_type=InsurancePlanType.DUAL,
            status=RecordStatus.ACTIVE,
        )
        # Only a commercial plan (no active Medicaid) -> flagged.
        commercial_only = self._member()
        Insurance.objects.create(
            client=commercial_only, plan_type=InsurancePlanType.COMMERCIAL,
            status=RecordStatus.ACTIVE,
        )
        # No insurance at all -> flagged.
        no_plan = self._member()

        ids = serving_client_ids("insurance_expiring", start=None, end=None)
        self.assertNotIn(has_medicaid.client_id, ids)
        self.assertNotIn(has_dual.client_id, ids)
        self.assertIn(commercial_only.client_id, ids)
        self.assertIn(no_plan.client_id, ids)


class DashboardGoverningCaseTests(TestCase):
    """The executive dashboard must count ONLY each client's GOVERNING
    internal-service case. A client with a superseded/parallel NON-governing
    case is counted once (via the governing case), never per-case, and a stray
    denied case while the governing case is approved must not surface anywhere.
    The 'Multiple open cases' bucket is removed entirely."""

    def _auth(self):
        agent = Agent.objects.create(
            name="Mgr", agent_code="970", group="Management", is_manager=True,
        )
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        access["agent_is_manager"] = agent.is_manager
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def _client(self, first, last):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )

    def _case(self, client, auth, *, opened):
        from datetime import datetime, timezone as dt_tz

        from .models import Case, CaseStatus, CaseType

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=auth,
            program_name="Medically Tailored Meals",
            date_opened=datetime(2026, 1, opened, 12, tzinfo=dt_tz.utc),
        )

    def test_governing_case_ids_picks_one_per_client(self):
        from .models import ServiceAuthorizationStatus
        from .portal.views_dashboard import governing_internal_case_ids

        c = self._client("Gov", "Erning")
        approved = self._case(c, ServiceAuthorizationStatus.APPROVED, opened=1)
        # A later-dated denied case is NON-governing (approval outranks a denial
        # regardless of dates), so it must be excluded from the dashboard set.
        denied = self._case(c, ServiceAuthorizationStatus.DENIED, opened=20)

        gov = governing_internal_case_ids()
        self.assertIn(approved.case_id, gov)
        self.assertNotIn(denied.case_id, gov)

    def test_open_cases_and_rejected_count_governing_only(self):
        from .models import ServiceAuthorizationStatus
        from .portal.views_dashboard import serving_client_ids

        # Client A: governing APPROVED + a superseded (non-governing) DENIED case.
        a = self._client("Appr", "Oved")
        self._case(a, ServiceAuthorizationStatus.APPROVED, opened=1)
        self._case(a, ServiceAuthorizationStatus.DENIED, opened=20)
        # Client B: sole case DENIED -> that IS their governing case.
        b = self._client("Den", "Ied")
        self._case(b, ServiceAuthorizationStatus.DENIED, opened=5)

        resp = self._auth().get(reverse("portal-dashboard"))
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()

        # One case per client (governing), NOT per case: A(approved)+B(denied).
        self.assertEqual(data["open_cases"]["total"], 2)
        self.assertEqual(data["open_cases"]["accepted"], 1)
        self.assertEqual(data["open_cases"]["rejected"], 1)

        # The removed bucket is gone from the serving payload.
        self.assertNotIn("multiple_cases", data["serving"]["not_being_served"])

        # Rejected-case bucket keys off the GOVERNING case: only B, never A
        # (whose governing case is approved despite the stray denied case).
        self.assertEqual(data["serving"]["not_being_served"]["rejected_case"], 1)
        rejected = {str(x) for x in serving_client_ids("rejected_case", start=None, end=None)}
        self.assertIn(str(b.client_id), rejected)
        self.assertNotIn(str(a.client_id), rejected)


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


class CaseSerializerStatusNormalizationTest(TestCase):
    """CaseSerializer is the central chokepoint for EVERY case write (extension,
    CSV/API import, direct). A populated ``case_closed_at`` must force the stored
    status to CLOSED regardless of the raw incoming ``case_status`` -- the
    extension used to pass the raw Unite Us state ("managed") straight through,
    leaving closed cases reading Managed. A write with NO close date keeps its
    written status untouched (see AuthDrivesCaseStatusTest)."""

    def _client(self):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Case", last_name="Status"
        )

    def _save(self, client, **over):
        from api.serializers import CaseSerializer
        from api.services.lifecycle import MET_COUNCIL_PROVIDER_NAME

        data = {
            "case_id": str(uuid.uuid4()),
            "client_id": str(client.client_id),
            "program_name": "Meals on Wheels",
            "service_type": "Food",
            "provider_name": MET_COUNCIL_PROVIDER_NAME,
            "date_opened": timezone.now().isoformat(),
        }
        data.update(over)
        ser = CaseSerializer(data=data)
        ser.is_valid(raise_exception=True)
        return ser.save()

    def test_managed_with_close_date_persists_as_closed(self):
        from .models import CaseStatus

        client = self._client()
        case = self._save(
            client, case_status="managed",
            case_closed_at="2026-06-26T15:42:50Z",
        )
        self.assertEqual(case.case_status, CaseStatus.CLOSED)

    def test_managed_without_close_date_is_preserved(self):
        # No close date -> the serializer does NOT force a status; the written
        # 'managed' is kept (authorization/status independence contract).
        from .models import CaseStatus

        client = self._client()
        case = self._save(client, case_status="managed")
        self.assertEqual(case.case_status, CaseStatus.MANAGED)


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

    def test_service_columns_split_subtype_and_category(self):
        # CSV service_subtype -> model service_type (the specific service used for
        # classification); CSV service_type -> model service_category (the broad
        # Unite Us grouping).
        out = self._map(
            service_subtype="Medically Tailored Meals",
            service_type="Food Assistance",
        )
        self.assertEqual(out["service_type"], "Medically Tailored Meals")
        self.assertEqual(out["service_category"], "Food Assistance")


class CsvFlexibleDatetimeTest(SimpleTestCase):
    """Regression: a Unite Us export opened + re-saved in Excel/Sheets emits
    US-format datetimes (e.g. ``7/28/26 0:00``) instead of ISO. The importer
    passed the raw cell to DRF, whose DateTimeField accepts ISO only, so EVERY
    row failed validation on ``updated_at`` / the service-authorization windows.
    ``map_case_row`` now normalizes US formats to ISO (and still passes ISO
    through), while junk cells are dropped rather than failing the row."""

    def _dt(self, value):
        from .services.csv_import import _dt

        return _dt({"k": value}, "k")

    def test_us_datetime_normalized_to_iso(self):
        self.assertEqual(self._dt("7/27/26 17:06"), "2026-07-27T17:06:00")
        self.assertEqual(self._dt("7/28/26 0:00"), "2026-07-28T00:00:00")
        self.assertEqual(self._dt("1/28/27 0:00"), "2027-01-28T00:00:00")

    def test_us_date_only_and_4_digit_year(self):
        self.assertEqual(self._dt("3/29/2011"), "2011-03-29T00:00:00")
        self.assertEqual(self._dt("12/1/26"), "2026-12-01T00:00:00")

    def test_iso_passes_through(self):
        self.assertEqual(self._dt("2026-07-29T20:16:00"), "2026-07-29T20:16:00")

    def test_blank_and_junk_return_none(self):
        self.assertIsNone(self._dt(""))
        self.assertIsNone(self._dt("06:02.6"))  # Excel-mangled cell -> dropped

    def test_date_helper_handles_iso_date_only_and_us(self):
        from .services.csv_import import _date

        self.assertEqual(_date({"k": "2020-01-15"}, "k"), "2020-01-15")
        self.assertEqual(_date({"k": "1/15/20"}, "k"), "2020-01-15")
        self.assertIsNone(_date({"k": ""}, "k"))

    def test_expiry_never_expires_sentinel_99_and_9999(self):
        # Unite Us marks a lifetime policy 12/31/9999; Excel truncates that to a
        # two-digit 12/31/99 (which strptime would read as 1999). BOTH spellings
        # -- and an ISO 9999 -- must normalize to the canonical year-9999 date so
        # the policy reads as never-expiring, not as expired in 1999.
        from .services.csv_import import _expiry_dt

        self.assertEqual(_expiry_dt({"k": "12/31/99 0:00"}, "k"), "9999-12-31T00:00:00")
        self.assertEqual(_expiry_dt({"k": "12/31/9999 12:00:00 AM"}, "k"), "9999-12-31T00:00:00")
        self.assertEqual(_expiry_dt({"k": "1/1/9999"}, "k"), "9999-12-31T00:00:00")
        self.assertEqual(_expiry_dt({"k": "2099-12-31T00:00:00"}, "k"), "2099-12-31T00:00:00")

    def test_expiry_genuine_dates_unaffected(self):
        # A real expiry (incl. a genuine 4-digit 1999) parses normally; only the
        # 2-digit /99 and 4-digit /9999 sentinels are promoted to never-expires.
        from .services.csv_import import _expiry_dt

        self.assertEqual(_expiry_dt({"k": "3/31/26 0:00"}, "k"), "2026-03-31T00:00:00")
        self.assertEqual(_expiry_dt({"k": "12/31/1999"}, "k"), "1999-12-31T00:00:00")
        self.assertIsNone(_expiry_dt({"k": ""}, "k"))

    def test_full_row_validates_with_us_dates(self):
        from .serializers import CaseSerializer
        from .services.csv_import import map_case_row

        payload = map_case_row({
            "case_id": "6603dd31-48e0-4fd0-b0c0-c2ff7c4400e6",
            "client_id": "397a5a2d-7af6-4282-a9da-e1430af63a6d",
            "program_name": "Medically Tailored Meals (MTM)",
            "service_subtype": "Medically Tailored Meals",
            "case_status": "open",
            "service_authorization_status": "requested",
            "case_updated_at": "7/27/26 17:06",
            "service_authorization_request_starts_at": "7/28/26 0:00",
            "service_authorization_request_ends_at": "1/28/27 0:00",
        })
        ser = CaseSerializer(data=payload)
        self.assertTrue(ser.is_valid(), ser.errors)


class ReimportCaseWithNullServiceCategoryTest(TestCase):
    """Regression: cases created before the ``service_category`` column existed
    (migration 0150) carry a NULL there. A NOT-NULL column rejected the
    django-simple-history copy on re-save, rolling back the whole import row so a
    re-import silently failed to update the authorization status (prod: ~22,810
    rows errored with 'null value in column service_category ... violates
    not-null constraint'). The column must be nullable so such rows re-save."""

    def test_reimport_updates_auth_on_legacy_null_category_row(self):
        from .models import Case, Client
        from .serializers import CaseSerializer
        from .services.csv_import import map_case_row

        client_id = "11111111-1111-1111-1111-111111111111"
        case_id = "22222222-2222-2222-2222-222222222222"
        Client.objects.create(client_id=client_id, first_name="A", last_name="B")

        # Simulate a legacy row: written before service_category existed, so it's
        # NULL in the DB (bypass the model default via a raw UPDATE).
        Case.objects.create(
            case_id=case_id, client_id=client_id, program_name="Meals",
            service_type="Home Delivered Meals",
            service_authorization_status="pending",
        )
        Case.objects.filter(pk=case_id).update(service_category=None)
        self.assertIsNone(Case.objects.get(pk=case_id).service_category)

        # Re-import the same case with an APPROVED authorization (blank broad
        # category, mirroring the failing prod rows). Must NOT raise and must
        # persist the new auth.
        payload = map_case_row({
            "case_id": case_id, "client_id": client_id, "program_name": "Meals",
            "service_subtype": "Home Delivered Meals",
            "service_authorization_status": "approved",
        })
        ser = CaseSerializer(data=payload)
        ser.is_valid(raise_exception=True)
        ser.save()  # previously raised IntegrityError on the historical copy

        self.assertEqual(
            Case.objects.get(pk=case_id).service_authorization_status, "approved"
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

    def test_date_opened_prefers_created_at(self):
        out = self._map(
            state="open",
            attrs={"opened_date": "2026-01-05T00:00:00Z", "created_at": "2026-01-01T00:00:00Z"},
        )
        # map_case passes the ISO string through (DRF parses it on save). The
        # Unite Us case-created timestamp wins over the agent-entered opened date.
        self.assertTrue(out["date_opened"].startswith("2026-01-01"))

    def test_date_opened_falls_back_to_opened_date(self):
        # No created_at -> fall back to the agent-entered opened date so
        # date_opened is never blank (mirrors the CSV import fallback).
        out = self._map(state="open", attrs={"opened_date": "2026-01-05T00:00:00Z"})
        self.assertTrue(out["date_opened"].startswith("2026-01-05"))

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
        from datetime import timedelta

        from .models import OrderSchedule
        from .services.orders import sync_active_calendars, sync_delivery_calendar

        enr, _member = self._make_active_enrollment()
        # Align the plan window with the authorization end so this test isolates
        # REGENERATION of a lapsed calendar; window-drift healing (extending
        # ends_on to the authorization end) is covered separately in
        # RebuildDeliveryCalendarTest.
        plan = enr.delivery_schedules.get()
        plan.ends_on = timezone.localdate() + timedelta(days=60)
        plan.save(update_fields=["ends_on"])
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

    def test_backfill_second_weekly_delivery_for_2x_cadence(self):
        """A 2x/week (Mon/Thu) member with a live Thu order that week must still
        be backfilled for the skipped Mon -- the week-cover guard is per-cadence
        count, not a flat one-per-week."""
        from datetime import timedelta
        from unittest.mock import patch

        from .models import (
            DeliveryOrder, DeliveryOrderStatus, OrderSchedule, OrderStatus,
            ProductTypeKind, PurchaseOrder, PurchaseOrderStatus,
        )
        from .services import purchase_orders as po

        enr, member = self._make_active_enrollment()
        mon = self._next_weekday(0)
        thu = mon + timedelta(days=3)
        enr.delivery_weekdays = ["mon", "thu"]
        enr.save(update_fields=["delivery_weekdays"])
        plan = enr.delivery_schedules.first()
        plan.starts_on = mon + timedelta(days=7)  # Mon was skipped (cutoff passed)
        plan.ends_on = mon + timedelta(days=60)
        plan.save(update_fields=["starts_on", "ends_on"])

        # A LIVE Thu delivery the SAME week (their other cadence day).
        ppo = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)
        DeliveryOrder.objects.create(
            purchase_order=ppo, member=member.client, expected_delivery_date=thu,
            status=DeliveryOrderStatus.PENDING,
        )

        with patch.object(po, "product_kind_for_enrollment", return_value=ProductTypeKind.MEALS):
            added = po.backfill_late_occurrences(ProductTypeKind.MEALS, mon)
        # 1 live delivery this week < 2 cadence days -> Mon is still backfilled.
        self.assertEqual(added, 1)
        self.assertTrue(
            OrderSchedule.objects.filter(
                enrollment=enr, member=member, anticipated_delivery_date=mon,
                status=OrderStatus.SCHEDULED,
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

    def test_sync_active_calendars_heals_lapsed_but_authorized_window(self):
        # A member whose plan window has LAPSED (ends_on in the past) while the
        # governing authorization still reaches into the future used to be
        # skipped forever by the self-heal (selection required ends_on >= today,
        # and rebuild only regenerated within the stale window). It must now
        # extend the window to the authorization end and regenerate future
        # occurrences so the member returns to the Purchase Order.
        from datetime import timedelta

        from .models import OrderSchedule, OrderStatus
        from .services.orders import sync_active_calendars

        enr, hh = self._make_active_enrollment()
        today = timezone.localdate()
        plan = enr.delivery_schedules.get()
        # Lapse the plan window (ended yesterday) and clear any occurrences, so
        # the enrollment has no future calendar left -- exactly the stalled state.
        plan.starts_on = today - timedelta(days=30)
        plan.ends_on = today - timedelta(days=1)
        plan.save(update_fields=["starts_on", "ends_on"])
        OrderSchedule.objects.filter(enrollment=enr).delete()

        def _future_occurrences():
            return OrderSchedule.objects.filter(
                enrollment=enr, status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today,
            ).count()

        # Before: nothing scheduled in the future.
        self.assertEqual(_future_occurrences(), 0)

        sync_active_calendars()

        plan.refresh_from_db()
        # Window was EXTENDED to the (future) authorization end ...
        self.assertGreater(plan.ends_on, today)
        # ... and future occurrences were regenerated.
        self.assertGreater(_future_occurrences(), 0)

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
    one derived from the program's ActiveProgram category can be saved. The
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

        from .models import ActiveProgram, Case
        from .serializers import CaseSerializer

        c = self._client()
        ActiveProgram.objects.create(
            program_name="Legal Aid", case_category="External Service",
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


class ActiveProgramFieldsTest(TestCase):
    """ActiveProgram.is_for_household auto-derives from the program name on save
    (True only when "household" appears), and case_type defaults to Food."""

    def test_is_for_household_and_case_type_default(self):
        from .models import ActiveProgram

        hh = ActiveProgram.objects.create(
            program_name="MTM - (Household) High-Risk Children - Brooklyn",
            case_category="Internal Service",
        )
        indiv = ActiveProgram.objects.create(
            program_name="MTM - Individual - Queens",
            case_category="Internal Service",
        )
        self.assertTrue(hh.is_for_household)
        self.assertFalse(indiv.is_for_household)
        self.assertEqual(hh.case_type, ActiveProgram.CaseType.FOOD)

    def test_is_for_household_recomputes_on_rename(self):
        from .models import ActiveProgram

        ap = ActiveProgram.objects.create(
            program_name="Transit Assistance - Individual",
            case_category="Internal Service",
            case_type=ActiveProgram.CaseType.TRANSPORTATION,
        )
        self.assertFalse(ap.is_for_household)
        ap.program_name = "Transit Assistance - Household"
        ap.save()
        ap.refresh_from_db()
        self.assertTrue(ap.is_for_household)
        self.assertEqual(ap.case_type, ActiveProgram.CaseType.TRANSPORTATION)


class MemberEligibilityTest(TestCase):
    """api.services.eligibility: import-time gates + disposition (INELIGIBLE +
    note + timeline + stop future deliveries), idempotency and recovery."""

    def _client(self):
        from .models import Client

        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="El", last_name="Ig"
        )

    def _reconcile(self, client):
        from .services.eligibility import reconcile_client_eligibility

        return reconcile_client_eligibility(client)

    def test_missing_insurance_is_ineligible_with_note_and_timeline(self):
        from .models import (
            ClientStage, Note, NoteSource, TimelineEvent, TimelineEventType,
        )

        c = self._client()
        self._reconcile(c)
        c.refresh_from_db()
        self.assertEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)
        # The reason is persisted on the client for display on the Members list.
        self.assertIn("no medical insurance on file", c.ineligible_reasons)
        self.assertTrue(
            Note.objects.filter(client=c, source=NoteSource.SYSTEM).exists()
        )
        self.assertTrue(
            TimelineEvent.objects.filter(
                client=c, event_type=TimelineEventType.MEMBER_INELIGIBLE
            ).exists()
        )

    def test_recovery_clears_stored_reasons(self):
        from .models import ClientStage, Insurance

        c = self._client()
        self._reconcile(c)
        c.refresh_from_db()
        self.assertTrue(c.ineligible_reasons)
        # Add a valid (never-expiring) insurance and re-reconcile: the member
        # recovers and the stored reasons are cleared.
        Insurance.objects.create(client=c, plan_name="P", external_member_id="1")
        self._reconcile(c)
        c.refresh_from_db()
        self.assertNotEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)
        self.assertEqual(c.ineligible_reasons, [])

    def test_active_insurance_is_eligible(self):
        from .models import ClientStage, Insurance

        c = self._client()
        Insurance.objects.create(  # blank expired_at => active
            client=c, plan_name="P", external_member_id="1"
        )
        self._reconcile(c)
        c.refresh_from_db()
        self.assertNotEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_expired_insurance_ineligible_ignores_active_status(self):
        # record_status ACTIVE but a past expired_at => expired (date-based gate).
        from .models import ClientStage, Insurance, RecordStatus

        c = self._client()
        Insurance.objects.create(
            client=c, plan_name="P", external_member_id="1",
            status=RecordStatus.ACTIVE,
            expired_at=timezone.now() - timedelta(days=10),
        )
        self._reconcile(c)
        c.refresh_from_db()
        self.assertEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_lifetime_sentinel_9999_is_active(self):
        from datetime import datetime, timezone as dtz

        from .models import ClientStage, Insurance

        c = self._client()
        Insurance.objects.create(
            client=c, plan_name="P", external_member_id="1",
            expired_at=datetime(9999, 12, 31, tzinfo=dtz.utc),
        )
        self._reconcile(c)
        c.refresh_from_db()
        self.assertNotEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_wrong_medicaid_type_is_ineligible(self):
        from .models import ClientStage, Insurance, InsurancePlanType

        c = self._client()
        Insurance.objects.create(  # active (blank expiry) but bad Medicaid type
            client=c, plan_type=InsurancePlanType.MEDICAID,
            plan_name="New York State Medicaid FFS", external_member_id="1",
        )
        self._reconcile(c)
        c.refresh_from_db()
        self.assertEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_expired_ffs_with_active_commercial_is_eligible(self):
        # The b49f6d32/2e863cc0 scenario: an EXPIRED FFS Medicaid must not
        # off-ramp a member who has current (active, no end date) commercial
        # coverage. The dead FFS is ignored by the wrong-type gate; the active
        # commercial clears the medical gate.
        from .models import ClientStage, Insurance, InsurancePlanType, RecordStatus

        c = self._client()
        Insurance.objects.create(
            client=c, plan_type=InsurancePlanType.MEDICAID,
            plan_name="New York State Medicaid FFS", external_member_id="1",
            status=RecordStatus.INACTIVE,
            expired_at=timezone.now() - timedelta(days=3),
        )
        Insurance.objects.create(
            client=c, plan_type="commercial",
            plan_name="Anthem Blue Cross Blue Shield (NY)", external_member_id="2",
            status=RecordStatus.ACTIVE, expired_at=None,
        )
        self._reconcile(c)
        c.refresh_from_db()
        self.assertNotEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_zip_out_of_range_is_ineligible(self):
        from .models import (
            Address, AddressType, ClientStage, ExcludedZipCode, Insurance,
        )

        c = self._client()
        Insurance.objects.create(client=c, plan_name="P", external_member_id="1")
        ExcludedZipCode.objects.create(zip="11209")
        Address.objects.create(
            client=c, type=AddressType.CURRENT, zip="11209", state="NY"
        )
        self._reconcile(c)
        c.refresh_from_db()
        self.assertEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_state_not_served_is_ineligible(self):
        from .models import (
            Address, AddressType, AllowedState, ClientStage, Insurance,
        )

        c = self._client()
        Insurance.objects.create(client=c, plan_name="P", external_member_id="1")
        AllowedState.objects.create(code="NY", name="New York")
        Address.objects.create(
            client=c, type=AddressType.CURRENT, state="NJ", zip="07030"
        )
        self._reconcile(c)
        c.refresh_from_db()
        self.assertEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_recovery_restores_stage(self):
        from .models import (
            ClientStage, Insurance, TimelineEvent, TimelineEventType,
        )

        c = self._client()
        self._reconcile(c)  # no insurance => ineligible
        c.refresh_from_db()
        self.assertEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)

        Insurance.objects.create(client=c, plan_name="P", external_member_id="1")
        self._reconcile(c)
        c.refresh_from_db()
        self.assertNotEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)
        self.assertTrue(
            TimelineEvent.objects.filter(
                client=c, event_type=TimelineEventType.MEMBER_ELIGIBILITY_RESTORED
            ).exists()
        )

    def test_idempotent_no_duplicate_note_or_event(self):
        from .models import (
            Note, NoteSource, TimelineEvent, TimelineEventType,
        )

        c = self._client()
        self._reconcile(c)
        self._reconcile(c)
        self.assertEqual(
            TimelineEvent.objects.filter(
                client=c, event_type=TimelineEventType.MEMBER_INELIGIBLE
            ).count(),
            1,
        )
        self.assertEqual(
            Note.objects.filter(client=c, source=NoteSource.SYSTEM).count(), 1
        )

    def test_ineligible_stops_future_deliveries(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile,
        )

        c = self._client()  # no insurance => ineligible
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=c)
        self._reconcile(c)
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)

    def test_ineligible_dependent_pauses_member_not_whole_household(self):
        # A household with an eligible primary + one ineligible dependent: the
        # dependent is PAUSED individually (removed from the schedule) while the
        # program keeps serving -- the whole household is NOT held.
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            Insurance, MemberDietaryProfile, MemberStatus, Note, NoteSource,
        )

        primary = self._client()
        Insurance.objects.create(client=primary, plan_name="P", external_member_id="1")
        dep = self._client()  # no insurance => ineligible
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        # Serving (SERVICE_ACTIVE) household -> members are ACTIVE (they went
        # through kitchen assignment); PENDING is only the pre-kitchen default.
        pmv = MemberDietaryProfile.objects.create(
            enrollment=enr, client=primary, status=MemberStatus.ACTIVE,
        )
        dmv = MemberDietaryProfile.objects.create(
            enrollment=enr, client=dep, status=MemberStatus.ACTIVE,
        )

        self._reconcile(dep)

        enr.refresh_from_db(); pmv.refresh_from_db(); dmv.refresh_from_db()
        # Program keeps serving; only the dependent is paused + flagged.
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)
        self.assertEqual(pmv.status, MemberStatus.ACTIVE)
        self.assertEqual(dmv.status, MemberStatus.PAUSED)
        self.assertTrue(dmv.eligibility_paused)
        # A self-descriptive system note (why + what) is written on the member.
        self.assertTrue(
            Note.objects.filter(
                client=dep, source=NoteSource.SYSTEM,
                body__icontains="removed them from the delivery schedule",
            ).exists()
        )

    def test_ineligible_dependent_recovers_and_rejoins(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            Insurance, MemberDietaryProfile, MemberStatus,
        )

        primary = self._client()
        Insurance.objects.create(client=primary, plan_name="P", external_member_id="1")
        dep = self._client()
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=primary)
        dmv = MemberDietaryProfile.objects.create(enrollment=enr, client=dep)

        self._reconcile(dep)  # paused
        dmv.refresh_from_db()
        self.assertEqual(dmv.status, MemberStatus.PAUSED)

        # Dependent's insurance + social-care coverage are fixed -> re-reconcile
        # returns them to service (both the hard gate and the coverage hold pass).
        from .models import SocialCareCoverage

        Insurance.objects.create(client=dep, plan_name="P", external_member_id="2")
        SocialCareCoverage.objects.create(client=dep, plan_name="SC")
        self._reconcile(dep)
        dmv.refresh_from_db()
        self.assertEqual(dmv.status, MemberStatus.ACTIVE)
        self.assertFalse(dmv.eligibility_paused)

    # --- Recoverable social-care-coverage hold ---------------------------------
    def _eligible_client_with_enrollment(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            Insurance, MemberDietaryProfile,
        )

        c = self._client()
        Insurance.objects.create(client=c, plan_name="P", external_member_id="1")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=c)
        return c, enr

    def test_missing_social_coverage_holds_not_ineligible(self):
        from .models import (
            ClientStage, EnrollmentStage, Note, NoteSource, TimelineEvent,
            TimelineEventType,
        )

        c, enr = self._eligible_client_with_enrollment()  # no social coverage
        self._reconcile(c)
        c.refresh_from_db()
        enr.refresh_from_db()
        self.assertNotEqual(c.lifecycle_stage, ClientStage.INELIGIBLE)
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        self.assertTrue(
            TimelineEvent.objects.filter(
                client=c, event_type=TimelineEventType.MEMBER_COVERAGE_HOLD
            ).exists()
        )
        self.assertTrue(
            Note.objects.filter(client=c, source=NoteSource.SYSTEM).exists()
        )

    def test_active_social_coverage_no_hold(self):
        from .models import EnrollmentStage, SocialCareCoverage

        c, enr = self._eligible_client_with_enrollment()
        SocialCareCoverage.objects.create(client=c, plan_name="SC")  # blank => active
        self._reconcile(c)
        enr.refresh_from_db()
        self.assertNotEqual(enr.stage, EnrollmentStage.ON_HOLD)

    def test_coverage_hold_idempotent(self):
        from .models import TimelineEvent, TimelineEventType

        c, enr = self._eligible_client_with_enrollment()
        self._reconcile(c)
        self._reconcile(c)
        self.assertEqual(
            TimelineEvent.objects.filter(
                client=c, event_type=TimelineEventType.MEMBER_COVERAGE_HOLD
            ).count(),
            1,
        )

    def test_coverage_hold_resumes_when_coverage_restored(self):
        from .models import (
            EnrollmentStage, SocialCareCoverage, TimelineEvent, TimelineEventType,
        )

        c, enr = self._eligible_client_with_enrollment()
        self._reconcile(c)  # no coverage => hold
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)

        SocialCareCoverage.objects.create(client=c, plan_name="SC")  # now active
        self._reconcile(c)
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.SERVICE_ACTIVE)
        self.assertTrue(
            TimelineEvent.objects.filter(
                client=c, event_type=TimelineEventType.MEMBER_COVERAGE_RESTORED
            ).exists()
        )


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

    def test_warnings_suppressed_when_governing_case_closed(self):
        # Warnings only make sense while an OPEN governing case backs the member.
        # A SERVICE_ACTIVE household with no cadence normally flags NO_CADENCE;
        # once the governing internal-service case is CLOSED, every warning is
        # suppressed (the program is done).
        from .models import CaseStatus, EnrollmentStage
        from .services.warnings import NO_CADENCE

        # Baseline: an OPEN governing case surfaces the warning.
        c_open = self._client()
        enr_open = self._enrollment(c_open, stage=EnrollmentStage.SERVICE_ACTIVE)
        self._internal_case(c_open, status=CaseStatus.OPEN)
        self.assertIn(NO_CADENCE, self._codes(enr_open))

        # A CLOSED governing case suppresses ALL warnings for the member.
        c_closed = self._client()
        enr_closed = self._enrollment(c_closed, stage=EnrollmentStage.SERVICE_ACTIVE)
        self._internal_case(c_closed, status=CaseStatus.CLOSED)
        self.assertEqual(self._codes(enr_closed), set())

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

    def test_multiple_open_cases_ignores_non_met_council(self):
        # A second OPEN internal-service case managed by a DIFFERENT org is not
        # part of the member base (excluded from the Cases tab / verification),
        # so it must not make "multiple open cases" fire on a member who really
        # has just one Met Council case.
        from .models import Case, CaseStatus, CaseType
        from .services.warnings import MULTIPLE_OPEN_CASES

        c = self._client()
        enr = self._enrollment(c)
        self._internal_case(c)  # Met Council (blank managing org)
        Case.objects.create(
            case_id=uuid.uuid4(), client=c,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            program_name="Medically Tailored Meals",
            provider_name="God's Love We Deliver",
        )
        self.assertNotIn(MULTIPLE_OPEN_CASES, self._codes(enr))

    def test_multiple_open_cases_suppressed_once_verified(self):
        # Two genuine Met Council open cases flag before verification, but once
        # the household is verified (has an active enrollment tied to its case)
        # the extra open case is no longer an actionable problem.
        from django.utils import timezone
        from .services.warnings import MULTIPLE_OPEN_CASES

        c = self._client()
        enr = self._enrollment(c)
        self._internal_case(c)
        self._internal_case(c)
        self.assertIn(MULTIPLE_OPEN_CASES, self._codes(enr))

        enr.verified_at = timezone.now()
        enr.save(update_fields=["verified_at"])
        self.assertNotIn(MULTIPLE_OPEN_CASES, self._codes(enr))

    def test_conflicting_product_types(self):
        from .services.warnings import CONFLICTING_PRODUCT_TYPES

        c = self._client()
        enr = self._enrollment(c)
        self._internal_case(c, program="Medically Tailored Meals")
        self._internal_case(c, program="Grocery Boxes Program")
        self.assertIn(CONFLICTING_PRODUCT_TYPES, self._codes(enr))

    def test_conflicting_product_types_suppressed_once_verified(self):
        # Mirrors multiple_open_cases: before verification, cases spanning
        # different kinds nag "which case governs?". Once verified, the governing
        # case owns the kind and any divergence is handled by the Programs-tab
        # meals<->boxes reconciliation, so the warning clears.
        from django.utils import timezone
        from .services.warnings import CONFLICTING_PRODUCT_TYPES

        c = self._client()
        enr = self._enrollment(c)
        self._internal_case(c, program="Medically Tailored Meals")
        self._internal_case(c, program="Grocery Boxes Program")
        self.assertIn(CONFLICTING_PRODUCT_TYPES, self._codes(enr))

        enr.verified_at = timezone.now()
        enr.save(update_fields=["verified_at"])
        self.assertNotIn(CONFLICTING_PRODUCT_TYPES, self._codes(enr))

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

    def test_wrong_medicaid_type(self):
        # A member whose ONLY Medicaid plan is an unserviceable type
        # (MLTC/MAP/FFS in the name) is flagged; a clean Medicaid plan alongside
        # it clears the flag (all-bad rule); a non-medicaid plan carrying the
        # token is ignored; and "MAPD" does not false-match "MAP".
        from .models import Insurance, InsurancePlanType
        from .services.warnings import WRONG_MEDICAID_TYPE

        bad = self._client()
        enr = self._enrollment(bad)
        Insurance.objects.create(
            client=bad, plan_type=InsurancePlanType.MEDICAID,
            plan_name="New York State Medicaid FFS", external_member_id="1",
        )
        self.assertIn(WRONG_MEDICAID_TYPE, self._codes(enr))

        # A clean Medicaid plan alongside the bad one clears it.
        Insurance.objects.create(
            client=bad, plan_type=InsurancePlanType.MEDICAID,
            plan_name="MetroPlus Medicaid (NY)", external_member_id="2",
        )
        self.assertNotIn(WRONG_MEDICAID_TYPE, self._codes(enr))

        # Token in a NON-medicaid plan type is ignored.
        other = self._client()
        enr2 = self._enrollment(other)
        Insurance.objects.create(
            client=other, plan_type=InsurancePlanType.COMMERCIAL,
            plan_name="Some MAP Commercial", external_member_id="3",
        )
        self.assertNotIn(WRONG_MEDICAID_TYPE, self._codes(enr2))

        # "MAPD" must not match the word "MAP".
        mapd = self._client()
        enr3 = self._enrollment(mapd)
        Insurance.objects.create(
            client=mapd, plan_type=InsurancePlanType.MEDICAID,
            plan_name="MAPD Advantage", external_member_id="4",
        )
        self.assertNotIn(WRONG_MEDICAID_TYPE, self._codes(enr3))

        # Each ineligible type is detected by its abbreviation OR its long-form
        # name (case-insensitive, whitespace-tolerant).
        for i, name in enumerate((
            "PMLTC",
            "MLTCP",
            "Partial Managed Long Term Care",
            "Managed Long Term Care Partial",
            "NYS Medicaid Managed Long Term Care",
            "Fee For Service",
            "Medicaid Advantage Plan",
            "medicaid  fee   for service",  # case + extra whitespace
        )):
            c = self._client()
            enr = self._enrollment(c)
            Insurance.objects.create(
                client=c, plan_type=InsurancePlanType.MEDICAID,
                plan_name=name, external_member_id=f"long-{i}",
            )
            self.assertIn(
                WRONG_MEDICAID_TYPE, self._codes(enr),
                msg=f"expected {name!r} to flag as an ineligible Medicaid type",
            )

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

    def test_cancelled_household_suppresses_member_status(self):
        from .models import EnrollmentStage, MemberStatus
        from .services.warnings import HOUSEHOLD_MEMBERS_PAUSED

        enr = self._enrollment(self._client(), stage=EnrollmentStage.CANCELLED)
        mp = enr.member_profiles.first()
        mp.status = MemberStatus.PAUSED
        mp.save(update_fields=["status"])
        codes = self._codes(enr)
        # Member-status roll-up counts are suppressed for a terminal household,
        # and the retired "Household cancelled" warning is no longer emitted.
        self.assertNotIn(HOUSEHOLD_MEMBERS_PAUSED, codes)
        self.assertNotIn("household_cancelled", codes)

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
        from .services.lifecycle import MET_COUNCIL_PROVIDER_NAME

        data = {
            "case_id": str(uuid.uuid4()),
            "client_id": str(client.client_id),
            "case_type": CaseType.INTERNAL_SERVICE,
            "program_name": "Medically Tailored Meals",
            # Managed by Met Council -- required for extension-context writes
            # (the CaseSerializer gate rejects non-MC / blank-org ext cases).
            "provider_name": MET_COUNCIL_PROVIDER_NAME,
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
        from .services.lifecycle import MET_COUNCIL_PROVIDER_NAME

        ser = CaseSerializer(
            data={
                "case_id": str(uuid.uuid4()),
                "client_id": str(client.client_id),
                "case_type": CaseType.INTERNAL_SERVICE,
                "program_name": "Medically Tailored Meals",
                "provider_name": MET_COUNCIL_PROVIDER_NAME,
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
        # Client household data no longer counts: only the program-name word
        # "Household" makes a case a household case.
        fam = Client(client_id=uuid.uuid4(), first_name="A", last_name="B", is_a_family=True)
        self.assertEqual(
            derive_household_type(fam, "MTM - Brooklyn"), CaseHouseholdType.INDIVIDUAL
        )

    def test_is_extension_from_to_extend_program(self):
        from .models import ActiveProgram
        from .serializers import derive_is_extension

        ActiveProgram.objects.create(
            program_name="Reauthorization: Medically Tailored Meals (MTM) - Brooklyn",
            case_category="Internal Services",
            to_extend=True,
        )
        ActiveProgram.objects.create(
            program_name="Medically Tailored Meals (MTM) - Brooklyn",
            case_category="Internal Services",
            to_extend=False,
        )
        # Matches a to_extend program (case-insensitive) -> True.
        self.assertTrue(
            derive_is_extension("reauthorization: medically tailored meals (mtm) - brooklyn")
        )
        # A normal (non-to_extend) program -> False.
        self.assertFalse(derive_is_extension("Medically Tailored Meals (MTM) - Brooklyn"))
        # Blank / unmatched -> False.
        self.assertFalse(derive_is_extension(""))
        self.assertFalse(derive_is_extension("Some Unlisted Program"))

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

        # Met Council ORIGINATED but a blank managing provider: DROPPED now --
        # originating columns are ignored by the extraction.
        originated_only = self._case_row(client.client_id, self.MET)
        # MANAGED by Met Council (provider_name) -> the only thing kept.
        managed = self._case_row(
            client.client_id, str(uuid.uuid4()),
            provider_name="Met Council - SCN - PHS",
        )
        external = self._case_row(client.client_id, str(uuid.uuid4()))
        blank = self._case_row(client.client_id, "")
        # Originated by Met Council but a NON-internal case it does NOT manage:
        # dropped.
        referred_out = self._case_row(
            client.client_id, self.MET,
            service_subtype="Social Service Case Management",
            program_name="Navigation Services - Eligibility Assessment Level 1 - Brooklyn",
        )
        # Internal-service MEAL case Met Council ORIGINATED but a DIFFERENT named
        # org MANAGES (referred out to God's Love): dropped.
        referred_meal = self._case_row(
            client.client_id, self.MET,
            provider_name="God's Love We Deliver - SCN - PHS",
        )
        importer.import_cases(
            [originated_only, managed, external, blank, referred_out, referred_meal]
        )

        # ONLY the Met Council-MANAGED case is imported. Originating (even for a
        # meal case, even with a blank manager) no longer counts.
        self.assertFalse(Case.objects.filter(pk=originated_only["case_id"]).exists())
        self.assertTrue(Case.objects.filter(pk=managed["case_id"]).exists())
        self.assertFalse(Case.objects.filter(pk=external["case_id"]).exists())
        self.assertFalse(Case.objects.filter(pk=blank["case_id"]).exists())
        self.assertFalse(Case.objects.filter(pk=referred_out["case_id"]).exists())
        self.assertFalse(Case.objects.filter(pk=referred_meal["case_id"]).exists())
        self.assertEqual(importer.stats["created"], 1)
        self.assertEqual(importer.stats["skipped"], 5)

        # And it's classified correctly: internal-service + household (token).
        case = Case.objects.get(pk=managed["case_id"])
        self.assertEqual(case.case_type, CaseType.INTERNAL_SERVICE)
        self.assertEqual(case.household_type, CaseHouseholdType.HOUSEHOLD)

        # The imported case is Internal Service; the count is surfaced in the
        # cases dataset stats (for the Settings import UI) without inflating the
        # processed/created totals.
        self.assertEqual(importer.internal_service_count, 1)
        importer.finalize()
        self.assertEqual(run.stats["cases"]["internal_service"], 1)
        self.assertEqual(run.created_count, 1)

    def test_open_case_blank_auth_becomes_never_requested(self):
        from .models import ServiceAuthorizationStatus
        from .services.csv_import import map_case_row

        # OPEN case + blank authorization request -> Never Requested.
        open_blank = map_case_row(self._case_row(uuid.uuid4(), self.MET))
        self.assertEqual(
            open_blank["service_authorization_status"],
            ServiceAuthorizationStatus.NEVER_REQUESTED,
        )
        self.assertEqual(
            open_blank["service_authorization_status_label"], "Never Requested"
        )

        # An explicit auth value is untouched (not overwritten).
        approved = map_case_row(
            self._case_row(
                uuid.uuid4(), self.MET, service_authorization_status="approved",
            )
        )
        self.assertEqual(
            approved["service_authorization_status"],
            ServiceAuthorizationStatus.APPROVED,
        )

        # A CLOSED case with blank auth stays blank (rule is OPEN-only).
        closed_blank = map_case_row(
            self._case_row(
                uuid.uuid4(), self.MET, case_closed_at="2024-01-01T00:00:00Z",
            )
        )
        self.assertNotIn("service_authorization_status", closed_blank)

    def test_deferred_reconcile_flag_toggles(self):
        from .services.lifecycle import (
            deferred_internal_service_reconcile,
            internal_service_reconcile_deferred,
        )

        self.assertFalse(internal_service_reconcile_deferred())
        with deferred_internal_service_reconcile():
            self.assertTrue(internal_service_reconcile_deferred())
        self.assertFalse(internal_service_reconcile_deferred())

    def test_cases_import_defers_reconcile_until_full_picture(self):
        """A cases sheet is processed one row per case. Reconciling per row would
        evaluate the client-wide rules against a PARTIAL picture -- closing a
        client's currently-only-open case would full-stop CANCEL the household
        before the row that opens their next case is seen. The import defers the
        reconcile to a single post-pass, so the household survives regardless of
        row order."""
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, ImportRun,
            ImportRunStatus,
        )
        from .services.csv_import import CsvImporter
        from .services.lifecycle import deferred_internal_service_reconcile

        client = Client.objects.create(
            client_id=uuid.uuid4(), first_name="Ord", last_name="Ering"
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
            verified_at=timezone.now(),
        )
        # Pre-existing OPEN internal-service case A -- the governing case today
        # (created directly, so no reconcile fires yet).
        case_a = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, service_authorization_status="approved",
            provider_name="",
        )

        # Dangerous order: CLOSE A first, then OPEN a new case B. (The import
        # derives Closed from a populated close date, not the status string.)
        row_close_a = self._case_row(
            client.client_id, self.MET, case_id=str(case_a.case_id),
            provider_name="Met Council - SCN - PHS",
            case_status="closed", case_closed_at=timezone.now().isoformat(),
        )
        case_b_id = str(uuid.uuid4())
        row_open_b = self._case_row(
            client.client_id, self.MET, case_id=case_b_id, case_status="open",
            provider_name="Met Council - SCN - PHS",
        )

        run = ImportRun.objects.create(
            source="csv_uniteus", status=ImportRunStatus.RUNNING
        )
        importer = CsvImporter(run, emit_side_effects=False)
        # Mirror run_import's cases branch: defer during the row loop, then
        # reconcile once per touched client on the full picture.
        with deferred_internal_service_reconcile():
            importer.import_cases([row_close_a, row_open_b])
        importer.reconcile_touched_cases()

        # A closed, B open -> the household still has an open internal-service
        # case, so it must NOT have been cancelled by a partial-picture close-out.
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)
        self.assertEqual(
            Case.objects.get(pk=case_a.case_id).case_status, CaseStatus.CLOSED
        )
        self.assertEqual(
            Case.objects.get(pk=case_b_id).case_status, CaseStatus.OPEN
        )

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

    def test_delete_include_blank_internal_with_enrollment_guard(self):
        # --include-blank-internal purges blank-org meal cases too, BUT the
        # safety guard preserves any case backing a verification enrollment.
        from django.core.management import call_command
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentVerification, Household,
        )

        client = Client.objects.create(
            client_id=uuid.uuid4(), first_name="A", last_name="B"
        )
        keep_managed = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            provider_name="Met Council - SCN - PHS",
        )
        # Blank-org meal case with NO enrollment -> deleted under the flag.
        blank_no_enroll = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.MANAGED,
            case_type=CaseType.INTERNAL_SERVICE, provider_name="",
        )
        # Blank-org meal case that BACKS an enrollment -> preserved by the guard.
        blank_with_enroll = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.MANAGED,
            case_type=CaseType.INTERNAL_SERVICE, provider_name="",
        )
        hh = Household.objects.create(name="HH")
        EnrollmentVerification.objects.create(
            client=client, household=hh, case=blank_with_enroll,
            verified_at=timezone.now(),
        )

        # Default (no flag): both blank-org meal cases are kept.
        call_command("delete_non_metcouncil_cases", "--apply")
        self.assertTrue(Case.objects.filter(pk=blank_no_enroll.pk).exists())
        self.assertTrue(Case.objects.filter(pk=blank_with_enroll.pk).exists())

        # With the flag: the un-enrolled blank meal case goes; the enrolled one
        # is preserved by the safety guard; the managed case stays.
        call_command(
            "delete_non_metcouncil_cases", "--apply", "--include-blank-internal"
        )
        self.assertFalse(Case.objects.filter(pk=blank_no_enroll.pk).exists())
        self.assertTrue(Case.objects.filter(pk=blank_with_enroll.pk).exists())
        self.assertTrue(Case.objects.filter(pk=keep_managed.pk).exists())

        # Override the guard: the enrolled blank meal case is now deleted too,
        # and its enrollment's case link is nulled (SET_NULL), not cascaded.
        call_command(
            "delete_non_metcouncil_cases", "--apply", "--include-blank-internal",
            "--force-enrollment-linked",
        )
        self.assertFalse(Case.objects.filter(pk=blank_with_enroll.pk).exists())
        ev = EnrollmentVerification.objects.get(client=client)
        self.assertIsNone(ev.case_id)


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

    def test_verified_without_nutritionist_is_pending_nutritionist(self):
        # A verified household not yet signed off by a Nutritionist sits at
        # Pending Nutritionist (the gate between Verified and Kitchen Assignment).
        from .models import EnrollmentStage, ProgramStatus
        from .services.lifecycle import program_status

        enr = self._enrollment(EnrollmentStage.VERIFIED)
        self.assertEqual(program_status(enr), ProgramStatus.PENDING_NUTRITIONIST)

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


class ReconcileOrphanEnrollmentsTest(TestCase):
    """The reconcile_orphan_enrollments backfill heals enrollments stuck at an
    advanced stage with no OPEN internal-service case:

    * no internal case at all -> DISREGARDED (both pending & kitchen).
    * has internal case(s) but none open -> CANCELLED (closure full stop).
    """

    def _client(self):
        return Client.objects.create(
            client_id=uuid.uuid4(), first_name="P", last_name="Q"
        )

    def _internal_case(self, client, status):
        from .models import Case, CaseType

        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=status,
        )

    def _enrollment(self, client, stage, *, case=None):
        from .models import EnrollmentVerification

        return EnrollmentVerification.objects.create(
            client=client, case=case, stage=stage,
        )

    def test_no_case_kitchen_is_disregarded(self):
        from .models import CaseType, EnrollmentStage, EnrollmentVerification
        from django.core.management import call_command

        client = self._client()
        # Only non-internal cases -> "no internal case at all".
        from .models import Case
        Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.NAVIGATION,
            case_status="managed",
        )
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)

        call_command("reconcile_orphan_enrollments", "--apply")

        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.DISREGARDED)

    def test_no_case_pending_is_disregarded(self):
        from .models import EnrollmentStage
        from django.core.management import call_command

        client = self._client()
        enr = self._enrollment(client, EnrollmentStage.PENDING_VERIFICATION)

        call_command("reconcile_orphan_enrollments", "--apply")

        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.DISREGARDED)

    def test_closed_internal_case_is_paused_reversibly(self):
        from .models import CaseStatus, ClientStage, EnrollmentStage
        from django.core.management import call_command

        client = self._client()
        self._internal_case(client, CaseStatus.CLOSED)
        # A member still only at Kitchen Assignment (never became active) whose
        # sole case is closed is the HARD INELIGIBLE off-ramp (task 4.3): the
        # enrollment is paused (On Hold) and the client marked INELIGIBLE. The
        # reversible On Hold + SERVICE_INACTIVE path applies to already-serving
        # (SERVICE_ACTIVE) members -- see InternalServiceClosureFullStopTest.
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)

        call_command("reconcile_orphan_enrollments", "--apply")

        enr.refresh_from_db()
        client.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.ON_HOLD)
        self.assertEqual(client.lifecycle_stage, ClientStage.INELIGIBLE)

    def test_open_internal_case_is_left_untouched(self):
        from .models import CaseStatus, EnrollmentStage
        from django.core.management import call_command

        client = self._client()
        self._internal_case(client, CaseStatus.MANAGED)
        enr = self._enrollment(client, EnrollmentStage.KITCHEN_ASSIGNMENT)

        call_command("reconcile_orphan_enrollments", "--apply")

        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)


class ReportExportsTest(TestCase):
    """Admin > Reports CSV exports: the reworked All-members export (one row per
    member, coverage + service columns) and the new Cases export (one row per
    case, creator/provider/program/authorization columns, date-range filtered)."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Rep Manager", agent_code="950", group="Management"
        )
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _client(self, first="Ada", last="Member"):
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )

    def _rows(self, url):
        import csv
        import io

        resp = self.api.get(url)
        # Report CSVs may be streamed (StreamingHttpResponse) or buffered.
        if getattr(resp, "streaming", False):
            body = b"".join(resp.streaming_content)
        else:
            body = resp.content
        self.assertEqual(resp.status_code, 200, body)
        return list(csv.DictReader(io.StringIO(body.decode())))

    def test_all_members_export_columns_and_values(self):
        from datetime import datetime, timezone as dt_tz

        from .models import (
            Assessment, Insurance, SocialCareCoverage, SocialCareCoverageStatus,
        )

        client = self._client("Grace", "Hopper")
        Insurance.objects.create(
            client=client, plan_type="medicaid", plan_name="Fidelis Medicaid",
            status="active", is_primary=True,
            enrolled_at=datetime(2025, 1, 1, 12, tzinfo=dt_tz.utc),
            expired_at=datetime(2030, 12, 31, 12, tzinfo=dt_tz.utc),
        )
        SocialCareCoverage.objects.create(
            client=client, status=SocialCareCoverageStatus.ENROLLED,
            expired_at=datetime(2030, 6, 30, 12, tzinfo=dt_tz.utc),
        )
        # Assessment "Client May Be Eligible For" programs -> the "Eligible for:"
        # column (distinct names joined with "; "). Mixed string/dict entries.
        Assessment.objects.create(
            assessment_id=uuid.uuid4(), subject_id=client.client_id, client=client,
            eligible_services=[
                "Medically Tailored Meals (MTM)",
                {"name": "Clinically Appropriate Meals"},
            ],
        )

        rows = self._rows(reverse("portal-report-all-members"))
        row = next(r for r in rows if r["Member ID"] == str(client.client_id))

        self.assertEqual(
            row["Eligible for:"],
            "Medically Tailored Meals (MTM); Clinically Appropriate Meals",
        )
        self.assertEqual(row["Member Name"], "Grace Hopper")
        # A lone member is their own household primary; household size 1.
        self.assertEqual(row["Household Primary Member ID"], str(client.client_id))
        self.assertEqual(row["Total members in household"], "1")
        self.assertEqual(row["Medicaid Plan"], "Fidelis Medicaid")
        self.assertEqual(row["Medicaid Type"], "OK")
        self.assertEqual(row["Insurance Effective Date"], "2025-01-01")
        self.assertEqual(row["Insurance Expiration Date"], "2030-12-31")
        self.assertEqual(row["Social Care Coverage Status"], "Enrolled")
        self.assertEqual(row["Social Care Coverage Expiration Date"], "2030-06-30")
        self.assertEqual(row["Enrollment Platform"], "UniteUs")
        self.assertEqual(row["Out of Orbit?"], "No")
        self.assertEqual(row["Out of Range"], "No")
        self.assertEqual(row["Client Eligibility"], "Eligible")
        self.assertEqual(row["Currently servicing"], "")
        # No cases/screenings on file -> the presence flags read No.
        self.assertEqual(row["Is there Screening"], "No")
        self.assertEqual(row["Is there Internal Service Case"], "No")
        self.assertEqual(row["Is there Eligibility"], "No")
        self.assertEqual(row["Is there Navigation"], "No")

    def test_all_members_team_of_case_creator(self):
        from .models import Case, CaseStatus, CaseType, UniteUsAgent

        creator_id = uuid.uuid4()
        UniteUsAgent.objects.create(
            user_id=creator_id, name="Cara Creator",
            originating_team="CareCircle Call Center",
        )
        # Member whose governing internal-service case was created by a rostered
        # Unite Us user -> that user's Originating Team.
        rostered = self._client("Team", "Rostered")
        Case.objects.create(
            case_id=uuid.uuid4(), client=rostered,
            created_by_id=creator_id,
            case_status=CaseStatus.OPEN, case_type=CaseType.INTERNAL_SERVICE,
            program_name="MTM - Medically Tailored Meals",
        )
        # Governing case created by someone NOT on the roster -> Met Council Team.
        offroster = self._client("Team", "Metcouncil")
        Case.objects.create(
            case_id=uuid.uuid4(), client=offroster,
            created_by_id=uuid.uuid4(),
            case_status=CaseStatus.OPEN, case_type=CaseType.INTERNAL_SERVICE,
            program_name="MTM - Medically Tailored Meals",
        )
        # No case at all -> blank.
        none_case = self._client("Team", "Nocase")

        rows = self._rows(reverse("portal-report-all-members"))
        by_id = {r["Member ID"]: r for r in rows}
        self.assertEqual(
            by_id[str(rostered.client_id)]["Team of Case Creator"],
            "CareCircle Call Center",
        )
        self.assertEqual(
            by_id[str(offroster.client_id)]["Team of Case Creator"],
            "Met Council Team",
        )
        self.assertEqual(
            by_id[str(none_case.client_id)]["Team of Case Creator"], ""
        )

    def test_all_members_flags_wrong_medicaid_type_ineligible(self):
        from datetime import datetime, timezone as dt_tz

        from .models import Insurance

        client = self._client("Mel", "Ltc")
        Insurance.objects.create(
            client=client, plan_type="medicaid", plan_name="Elderplan MLTC",
            status="active", is_primary=True,
            expired_at=datetime(2030, 12, 31, tzinfo=dt_tz.utc),
        )

        rows = self._rows(reverse("portal-report-all-members"))
        row = next(r for r in rows if r["Member ID"] == str(client.client_id))
        self.assertEqual(row["Medicaid Type"], "MLTC")
        self.assertEqual(row["Client Eligibility"], "Ineligible")

    def test_cases_export_columns_and_values(self):
        from datetime import datetime, timezone as dt_tz

        from .models import (
            Case, CaseHouseholdType, CaseStatus, CaseType, DeliveryCadence,
            EnrollmentVerification, Kitchen, MemberDeliverySchedule,
            ScheduleStatus, ServiceAuthorizationStatus, UniteUsAgent,
        )

        client = self._client("Case", "Owner")
        creator_id = uuid.uuid4()
        UniteUsAgent.objects.create(
            user_id=creator_id, name="Cara Creator",
            originating_team="CareCircle Call Center",
        )
        Agent.objects.create(name="Coord Person", agent_code="777", group="CS")

        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            created_by_id=creator_id, created_by_name="Cara Creator",
            agent_code="777",
            case_status=CaseStatus.OPEN,
            case_type=CaseType.NAVIGATION,
            household_type=CaseHouseholdType.INDIVIDUAL,
            originating_provider_name="Origin Health",
            provider_name="Met Council",
            program_name="MTM - (Household) Medically Tailored Meals - Queens",
            primary_worker_name="Wanda Worker",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_status_label="Accepted",
            service_authorization_approval_ends_at=datetime(2027, 3, 1, 12, tzinfo=dt_tz.utc),
            date_opened=datetime(2026, 5, 10, 12, tzinfo=dt_tz.utc),
        )
        kitchen = Kitchen.objects.create(name="Williamsburg")
        enrollment = EnrollmentVerification.objects.create(
            client=client, case=case, kitchen=kitchen,
            delivery_weekdays=["mon", "thu"],
        )
        MemberDeliverySchedule.objects.create(
            enrollment=enrollment,
            delivery_days_cadence=DeliveryCadence.MON_THU,
            status=ScheduleStatus.SCHEDULED,
        )

        rows = self._rows(reverse("portal-report-cases"))
        row = next(r for r in rows if r["Case ID"] == str(case.case_id))

        self.assertEqual(row["Client ID"], str(client.client_id))
        self.assertEqual(row["Member Name"], "Case Owner")
        self.assertEqual(row["Team of Case Creator"], "CareCircle Call Center")
        self.assertEqual(row["Case Created By Name"], "Cara Creator")
        self.assertEqual(row["Case Created Date"], "2026-05-10")
        self.assertEqual(row["Case Status"], "Open")
        self.assertEqual(row["Originating Provider Name"], "Origin Health")
        self.assertEqual(row["Provider Name"], "Met Council")
        self.assertEqual(
            row["Program Name"], "MTM - (Household) Medically Tailored Meals - Queens"
        )
        # Household is derived from the program name ("Household"), NOT the
        # stored household_type (which is INDIVIDUAL here).
        self.assertEqual(row["Is Program Household?"], "Yes")
        self.assertEqual(row["Case Type"], "Care Management")
        self.assertEqual(row["Meals/Boxes"], "Meals")
        self.assertEqual(row["Kitchen"], "Williamsburg")
        self.assertEqual(row["Cadence"], "Mon/Thu")
        self.assertEqual(row["Primary Worker Name"], "Wanda Worker")
        self.assertEqual(row["Care Coordinator"], "Coord Person")
        self.assertEqual(row["Service Authorization Status"], "Accepted")
        self.assertEqual(row["Service Authorization End Date"], "2027-03-01")

    def test_cases_export_creator_not_on_roster_is_met_council(self):
        from .models import Case, CaseStatus

        client = self._client("No", "Roster")
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            created_by_id=uuid.uuid4(), case_status=CaseStatus.CLOSED,
        )
        rows = self._rows(reverse("portal-report-cases"))
        row = next(r for r in rows if r["Case ID"] == str(case.case_id))
        self.assertEqual(row["Team of Case Creator"], "Met Council Team")
        self.assertEqual(row["Case Status"], "Closed")

    def test_cases_export_date_range_filters(self):
        from datetime import datetime, timezone as dt_tz

        from .models import Case, CaseStatus

        client = self._client("Range", "Test")
        in_range = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            date_opened=datetime(2026, 6, 15, tzinfo=dt_tz.utc),
        )
        out_range = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            date_opened=datetime(2026, 1, 1, tzinfo=dt_tz.utc),
        )
        url = reverse("portal-report-cases") + "?created_from=2026-06-01&created_to=2026-06-30"
        ids = {r["Case ID"] for r in self._rows(url)}
        self.assertIn(str(in_range.case_id), ids)
        self.assertNotIn(str(out_range.case_id), ids)

    def test_cases_export_created_date_uses_local_timezone(self):
        # An evening EDT case-created timestamp is stored in UTC as the NEXT
        # calendar day (9:34 PM EDT == 01:34 UTC). The export must render the
        # LOCAL date (matching the CRM UI), not the raw UTC date.
        from datetime import datetime, timezone as dt_tz

        from .models import Case, CaseStatus

        client = self._client("Evening", "Case")
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_status=CaseStatus.OPEN,
            date_opened=datetime(2026, 7, 23, 1, 34, 17, tzinfo=dt_tz.utc),
        )
        rows = self._rows(reverse("portal-report-cases"))
        row = next(r for r in rows if r["Case ID"] == str(case.case_id))
        self.assertEqual(row["Case Created Date"], "2026-07-22")

    def test_members_not_served_export_new_columns(self):
        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, Kitchen, MemberDeliverySchedule,
            MemberDietaryProfile, ServiceAuthorizationStatus,
        )

        # A member with an internal-service case, in a multi-member household,
        # and NOT served (no scheduled delivery) -> appears in the report. Give
        # them an enrollment with a kitchen, a delivery schedule (cadence) and a
        # dietary profile (menu type) so the assignment columns are populated.
        client = self._client("Hh", "Member")
        other = self._client("Room", "Mate")
        hh = Household.objects.create(name="Shared HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=other, is_primary=False)
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            program_name="MTM - (Household) Medically Tailored Meals - Queens",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_status_label="Accepted",
        )
        kitchen = Kitchen.objects.create(name="Brooklyn Kitchen")
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case, kitchen=kitchen,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        profile = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Kosher",
        )
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=profile,
            delivery_days_cadence="mon_thu",
        )

        # A solo member (no multi-member household) -> household flag is No.
        solo = self._client("Solo", "Member")
        Case.objects.create(
            case_id=uuid.uuid4(), client=solo,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            program_name="MTM - (Individual) Medically Tailored Meals - Queens",
        )

        rows = self._rows(reverse("portal-report-members-not-served"))
        row = next(r for r in rows if r["Client ID"] == str(client.client_id))
        self.assertEqual(row["Case ID"], str(case.case_id))
        self.assertEqual(row["Case Status"], "Open")
        self.assertEqual(row["Case Type"], "Internal Service")
        self.assertEqual(row["Case Authorization"], "Accepted")
        self.assertEqual(
            row["Program Name"],
            "MTM - (Household) Medically Tailored Meals - Queens",
        )
        self.assertEqual(row["Meals/Boxes"], "Meals")
        self.assertEqual(row["Is Part of a Household"], "Yes")
        # Household grouping columns: stable per-household code, the head's id
        # (this member IS the primary), and the is-primary flag.
        self.assertEqual(
            row["Household Group"], f"HH-{hh.household_id.hex[:12].upper()}"
        )
        self.assertEqual(row["Primary Member ID"], str(client.client_id))
        self.assertEqual(row["Is Primary"], "Yes")
        self.assertEqual(row["Member Stage"], "Kitchen Assignment")
        self.assertEqual(row["Kitchen"], "Brooklyn Kitchen")
        self.assertEqual(row["Cadence"], "Mon/Thu")
        self.assertEqual(row["Menu Type"], "Kosher")

        solo_row = next(r for r in rows if r["Client ID"] == str(solo.client_id))
        self.assertEqual(solo_row["Is Part of a Household"], "No")
        # A lone member (no household record) has no group code but is their own
        # primary/head.
        self.assertEqual(solo_row["Household Group"], "")
        self.assertEqual(solo_row["Primary Member ID"], str(solo.client_id))
        self.assertEqual(solo_row["Is Primary"], "Yes")
        self.assertEqual(solo_row["Meals/Boxes"], "Meals")
        # No enrollment/assignments -> the new columns are blank.
        self.assertEqual(solo_row["Member Stage"], "")
        self.assertEqual(solo_row["Kitchen"], "")
        self.assertEqual(solo_row["Cadence"], "")
        self.assertEqual(solo_row["Menu Type"], "")

    def test_unite_us_agents_export_columns_and_values(self):
        from .models import UniteUsAgent

        a1 = UniteUsAgent.objects.create(
            user_id=uuid.uuid4(),
            name="Rosa Reviewer",
            email="rosa@example.org",
            originating_team="CareCircle Call Center",
            status="active",
            is_us=True,
        )
        # No explicit ``name`` -> falls back to first + last; status title-cased.
        a2 = UniteUsAgent.objects.create(
            user_id=uuid.uuid4(),
            first_name="Manny",
            last_name="Council",
            email="manny@metcouncil.org",
            status="inactive",
        )

        rows = self._rows(reverse("portal-report-unite-us-agents"))
        self.assertEqual(
            list(rows[0].keys()),
            ["Status", "User ID", "Name", "Email", "Team"],
        )

        r1 = next(r for r in rows if r["User ID"] == str(a1.user_id))
        self.assertEqual(r1["Name"], "Rosa Reviewer")
        self.assertEqual(r1["Email"], "rosa@example.org")
        self.assertEqual(r1["Team"], "CareCircle Call Center")
        self.assertEqual(r1["Status"], "Active")

        r2 = next(r for r in rows if r["User ID"] == str(a2.user_id))
        self.assertEqual(r2["Name"], "Manny Council")
        self.assertEqual(r2["Team"], "Met Council Team")
        self.assertEqual(r2["Status"], "Inactive")

    def test_reports_require_management(self):
        agent = Agent.objects.create(
            name="Screener Sam", agent_code="951", group="Screeners"
        )
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(api.get(reverse("portal-report-all-members")).status_code, 403)
        self.assertEqual(api.get(reverse("portal-report-cases")).status_code, 403)
        self.assertEqual(
            api.get(reverse("portal-report-members-not-served")).status_code, 403
        )


class POClosedCaseGuardrailTest(TestCase):
    """A member whose internal-service (meal/box) case is CLOSED/CANCELLED must
    never be selected for a Purchase Order -- even if the enrollment-cancel
    close-out failed to run and left an active enrollment + stale SCHEDULED
    occurrence. Enforced by ``open_internal_service_case_exists`` in the PO
    candidate query."""

    def _setup(self, case_status):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
        )

        today = timezone.localdate()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Guard", last_name="Rail",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=case_status,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        # Enrollment left ACTIVE on purpose (close-out never ran).
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=enr, member=member, member_name="Guard Rail",
            anticipated_delivery_date=today + timedelta(days=1),
            status=OrderStatus.SCHEDULED, household_group_code="G", household=hh,
        )
        return client

    def _po_eligible_client_ids(self):
        from .models import OrderSchedule, OrderStatus
        from .services.purchase_orders import open_internal_service_case_exists

        return {
            str(cid)
            for cid in OrderSchedule.objects.filter(status=OrderStatus.SCHEDULED)
            .annotate(_h=open_internal_service_case_exists())
            .filter(_h=True)
            .values_list("member__client_id", flat=True)
        }

    def test_open_case_member_is_po_eligible(self):
        from .models import CaseStatus

        client = self._setup(CaseStatus.OPEN)
        self.assertIn(str(client.client_id), self._po_eligible_client_ids())

    def test_closed_case_member_excluded_even_when_enrollment_active(self):
        from .models import CaseStatus

        client = self._setup(CaseStatus.CLOSED)
        self.assertNotIn(str(client.client_id), self._po_eligible_client_ids())

    def test_cancelled_case_member_excluded(self):
        from .models import CaseStatus

        client = self._setup(CaseStatus.CANCELLED)
        self.assertNotIn(str(client.client_id), self._po_eligible_client_ids())


class POAuthorizationGuardrailTest(TestCase):
    """A household whose governing OPEN internal-service (meal/box) case is NOT
    approved -- still "Waiting Authorization" (pending / never_requested / blank),
    denied, or expired -- must never be selected for a Purchase Order, even if a
    stale SCHEDULED occurrence survived (e.g. the reconcile pull-back's
    truncation didn't run). Only APPROVED / Not Required authorizes a future
    delivery. Enforced by ``authorized_internal_service_case_exists`` in the PO
    candidate query."""

    def _setup(self, auth_status):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
        )

        today = timezone.localdate()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Auth", last_name="Gate",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=auth_status,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        # Enrollment left ACTIVE + a stale SCHEDULED occurrence on purpose (the
        # pull-back truncation never ran).
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh,
            stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=enr, member=member, member_name="Auth Gate",
            anticipated_delivery_date=today + timedelta(days=1),
            status=OrderStatus.SCHEDULED, household_group_code="G", household=hh,
        )
        return client

    def _po_eligible_client_ids(self):
        from .models import OrderSchedule, OrderStatus
        from .services.purchase_orders import (
            authorized_internal_service_case_exists,
        )

        return {
            str(cid)
            for cid in OrderSchedule.objects.filter(status=OrderStatus.SCHEDULED)
            .annotate(_a=authorized_internal_service_case_exists())
            .filter(_a=True)
            .values_list("member__client_id", flat=True)
        }

    def test_approved_is_po_eligible(self):
        from .models import ServiceAuthorizationStatus

        client = self._setup(ServiceAuthorizationStatus.APPROVED)
        self.assertIn(str(client.client_id), self._po_eligible_client_ids())

    def test_not_required_is_po_eligible(self):
        from .models import ServiceAuthorizationStatus

        client = self._setup(ServiceAuthorizationStatus.NOT_REQUIRED)
        self.assertIn(str(client.client_id), self._po_eligible_client_ids())

    def test_pending_excluded(self):
        from .models import ServiceAuthorizationStatus

        client = self._setup(ServiceAuthorizationStatus.PENDING)
        self.assertNotIn(str(client.client_id), self._po_eligible_client_ids())

    def test_never_requested_excluded(self):
        from .models import ServiceAuthorizationStatus

        client = self._setup(ServiceAuthorizationStatus.NEVER_REQUESTED)
        self.assertNotIn(str(client.client_id), self._po_eligible_client_ids())

    def test_denied_excluded(self):
        from .models import ServiceAuthorizationStatus

        client = self._setup(ServiceAuthorizationStatus.DENIED)
        self.assertNotIn(str(client.client_id), self._po_eligible_client_ids())

    def test_expired_excluded(self):
        from .models import ServiceAuthorizationStatus

        client = self._setup(ServiceAuthorizationStatus.EXPIRED)
        self.assertNotIn(str(client.client_id), self._po_eligible_client_ids())


class POKitchenAssignmentExclusionTest(TestCase):
    """A household requeued to KITCHEN_ASSIGNMENT (e.g. a completed meals<->boxes
    product switch) has NO assigned kitchen, so it is not deliverable and must
    never feed a Purchase Order -- even while its OLD calendar still carries
    SCHEDULED occurrences (the switch requeue clears the kitchen/cadence but a
    committed occurrence can linger). Regression for the leak where the primary
    kept landing on a meals PO on the OLD kitchen after switching to boxes.
    KITCHEN_ASSIGNMENT is in ``SERVICE_EXCLUDED_ENROLLMENT_STAGES``; assigning a
    kitchen advances the household to SERVICE_ACTIVE, which restores eligibility.
    """

    def _setup(self, stage):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
            ServiceAuthorizationStatus,
        )

        today = timezone.localdate()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Kitchen", last_name="Queue",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case, stage=stage,
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        # Tomorrow's stale SCHEDULED occurrence (the OLD calendar the switch left
        # behind). Use tomorrow so it is unambiguously "due" on that date.
        self.due_date = today + timedelta(days=1)
        OrderSchedule.objects.create(
            enrollment=enr, member=member, member_name="Kitchen Queue",
            program_name="Medically Tailored Meals",
            anticipated_delivery_date=self.due_date,
            status=OrderStatus.SCHEDULED, household_group_code="G", household=hh,
        )
        return client

    def _due_client_ids(self):
        from .models import ProductTypeKind
        from .services.purchase_orders import _due_schedules

        rows = _due_schedules(ProductTypeKind.MEALS, self.due_date)
        return {str(r.member.client_id) for r in rows if r.member_id}

    def test_kitchen_assignment_excluded(self):
        from .models import EnrollmentStage

        client = self._setup(EnrollmentStage.KITCHEN_ASSIGNMENT)
        self.assertNotIn(str(client.client_id), self._due_client_ids())

    def test_service_active_included(self):
        from .models import EnrollmentStage

        client = self._setup(EnrollmentStage.SERVICE_ACTIVE)
        self.assertIn(str(client.client_id), self._due_client_ids())


class POIneligibleMemberExclusionTest(TestCase):
    """A member on the hard INELIGIBLE eligibility off-ramp (out-of-range
    address, wrong Medicaid type, expired/missing insurance) must never be
    selected for a Purchase Order -- even when their enrollment is at an active,
    approved, open-case stage with a stale SCHEDULED occurrence (i.e. the
    truncate + On Hold from reconcile_client_eligibility could not apply, or the
    calendar was rebuilt while still INELIGIBLE). Keyed per member, so an
    ineligible person is dropped while eligible household members remain."""

    def _member(self, hh, case, enr, *, name, lifecycle_stage, member_status):
        from .models import (
            Client, ClientStage, HouseholdMember, MemberDietaryProfile,
            MemberStatus, OrderSchedule, OrderStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=name, last_name="X",
            lifecycle_stage=lifecycle_stage,
        )
        HouseholdMember.objects.create(household=hh, client=client, is_primary=False)
        mp = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
            status=member_status,
        )
        OrderSchedule.objects.create(
            enrollment=enr, member=mp, member_name=name,
            program_name="Medically Tailored Meals",
            anticipated_delivery_date=self.due_date,
            status=OrderStatus.SCHEDULED, household_group_code="G", household=hh,
        )
        return client, mp

    def setUp(self):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, ClientStage, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, MemberStatus,
            ServiceAuthorizationStatus,
        )

        self.due_date = timezone.localdate() + timedelta(days=1)
        holder = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Holder", last_name="X",
            lifecycle_stage=ClientStage.ACTIVE,
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=holder, is_primary=True)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=holder,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        enr = EnrollmentVerification.objects.create(
            client=holder, household=hh, case=case,
            stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        # Case-holder is a normal, active member with an occurrence.
        from .models import MemberDietaryProfile, OrderSchedule, OrderStatus
        holder_mp = MemberDietaryProfile.objects.create(
            enrollment=enr, client=holder, menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=enr, member=holder_mp, member_name="Holder",
            program_name="Medically Tailored Meals",
            anticipated_delivery_date=self.due_date,
            status=OrderStatus.SCHEDULED, household_group_code="G", household=hh,
        )
        self.holder = holder
        self.hh, self.enr = hh, enr
        # An ineligible dependent whose enrollment/member status are otherwise
        # perfectly serviceable (ACTIVE) -- only lifecycle_stage flags them.
        self.ineligible, _ = self._member(
            hh, case, enr, name="Nope",
            lifecycle_stage=ClientStage.INELIGIBLE,
            member_status=MemberStatus.ACTIVE,
        )

    def _due_client_ids(self):
        from .models import ProductTypeKind
        from .services.purchase_orders import _due_schedules

        rows = _due_schedules(ProductTypeKind.MEALS, self.due_date)
        return {str(r.member.client_id) for r in rows if r.member_id}

    def test_ineligible_member_excluded_from_due(self):
        ids = self._due_client_ids()
        self.assertNotIn(str(self.ineligible.client_id), ids)
        # Eligible household members are unaffected.
        self.assertIn(str(self.holder.client_id), ids)

    def test_ineligible_member_excluded_from_generation(self):
        from .models import OrderSchedule, ProductTypeKind
        from .services.purchase_orders import generate_purchase_order

        ids = list(
            OrderSchedule.objects.filter(anticipated_delivery_date=self.due_date)
            .values_list("order_id", flat=True)
        )
        po = generate_purchase_order(ProductTypeKind.MEALS, self.due_date, None, ids)
        self.assertIsNotNone(po)
        ordered = {str(o.member_id) for o in po.delivery_orders.all()}
        self.assertNotIn(str(self.ineligible.client_id), ordered)
        self.assertIn(str(self.holder.client_id), ordered)


class POHouseholdDependentEligibilityTest(TestCase):
    """A household dependent (non-case-holder) holds NO internal-service case of
    their own -- the whole household is governed by the case-holder's case. The
    open-case PO guardrail must therefore key on the enrollment applicant, not
    each member, or every dependent is wrongly dropped off the PO even when the
    household is open + approved."""

    def _setup(self, case_status):
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
            ServiceAuthorizationStatus,
        )
        from datetime import timedelta

        today = timezone.localdate()
        holder = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Case", last_name="Holder",
        )
        dep = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Depen", last_name="Dent",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=holder, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        # Only the case-holder has the internal-service case.
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=holder,
            case_type=CaseType.INTERNAL_SERVICE, case_status=case_status,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        enr = EnrollmentVerification.objects.create(
            client=holder, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        for c in (holder, dep):
            m = MemberDietaryProfile.objects.create(
                enrollment=enr, client=c, menu_type="Standard",
                status=MemberStatus.ACTIVE,
            )
            OrderSchedule.objects.create(
                enrollment=enr, member=m, member_name=f"{c.first_name} {c.last_name}",
                anticipated_delivery_date=today + timedelta(days=1),
                status=OrderStatus.SCHEDULED, household_group_code="G", household=hh,
            )
        return holder, dep

    def _po_eligible_client_ids(self):
        from .models import OrderSchedule, OrderStatus
        from .services.purchase_orders import (
            authorized_internal_service_case_exists,
            open_internal_service_case_exists,
        )

        return {
            str(cid)
            for cid in OrderSchedule.objects.filter(status=OrderStatus.SCHEDULED)
            .annotate(_o=open_internal_service_case_exists())
            .filter(_o=True)
            .annotate(_a=authorized_internal_service_case_exists())
            .filter(_a=True)
            .values_list("member__client_id", flat=True)
        }

    def test_dependent_included_when_household_open_approved(self):
        from .models import CaseStatus

        holder, dep = self._setup(CaseStatus.OPEN)
        eligible = self._po_eligible_client_ids()
        self.assertIn(str(holder.client_id), eligible)
        self.assertIn(str(dep.client_id), eligible)

    def test_whole_household_excluded_when_case_holder_case_closed(self):
        from .models import CaseStatus

        holder, dep = self._setup(CaseStatus.CLOSED)
        eligible = self._po_eligible_client_ids()
        self.assertNotIn(str(holder.client_id), eligible)
        self.assertNotIn(str(dep.client_id), eligible)


class PrepareMembersForPOTaskTest(TestCase):
    """The async "Prepare Members for PO" job: the Celery task runs the
    full-calendar reconcile, streams progress to its tracking ``ImportRun``, and
    records the aggregate totals -- so the Orders page can poll a live percentage
    and completion (mirrors the CSV-import flow)."""

    def test_task_reports_progress_and_completes(self):
        from unittest.mock import patch

        from .models import ImportRun, ImportRunStatus
        from .tasks import MEMBER_PREP_SOURCE, prepare_members_for_po

        run = ImportRun.objects.create(
            source=MEMBER_PREP_SOURCE, status=ImportRunStatus.PENDING,
        )
        totals = {"enrollments": 3, "added": 5, "removed": 2, "updated": 1,
                  "plans_created": 4}

        def fake_sync(from_date=None, progress_cb=None):
            # Drive the callback exactly as the real reconcile would.
            progress_cb(0, 3)
            for i in (1, 2, 3):
                progress_cb(i, 3)
            return totals

        with patch("api.services.orders.sync_active_calendars", side_effect=fake_sync):
            prepare_members_for_po.run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, ImportRunStatus.COMPLETED)
        self.assertEqual(run.progress_total, 3)
        self.assertEqual(run.processed_count, 3)
        self.assertEqual(run.stats, {"member_prep": totals})
        self.assertIsNotNone(run.finished_at)

    def test_task_marks_failed_on_error(self):
        from unittest.mock import patch

        from .models import ImportRun, ImportRunStatus
        from .tasks import MEMBER_PREP_SOURCE, prepare_members_for_po

        run = ImportRun.objects.create(
            source=MEMBER_PREP_SOURCE, status=ImportRunStatus.PENDING,
        )
        with patch("api.services.orders.sync_active_calendars",
                   side_effect=RuntimeError("boom")):
            prepare_members_for_po.run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, ImportRunStatus.FAILED)
        self.assertIn("boom", run.error_log)
        self.assertIsNotNone(run.finished_at)


class ReconcileDeliveryStateAuthorizationTest(TestCase):
    """``reconcile_delivery_state`` must only keep serving through a pending
    case when the household holds an OPEN approved authorization (a genuine
    in-flight renewal/switch). When the sole/governing authorization is pending
    -- an initial request not yet granted, or an approval that sits only on a
    CLOSED case -- service must NOT run, so future non-batched occurrences are
    truncated. This aligns delivery with the PO guardrail (authorized == OPEN +
    APPROVED) and fixes households that landed on POs while only "Requested"."""

    def _setup(self, *, auth_status, case_status):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, DeliveryCadence, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDeliverySchedule, MemberDietaryProfile, MemberStatus,
            OrderSchedule, OrderStatus, ScheduleStatus,
        )

        today = timezone.localdate()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Del", last_name="State",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=case_status,
            service_authorization_status=auth_status,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.SERVICE_ACTIVE,
            delivery_weekdays=["mon", "thu"],
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        # A live plan + a future occurrence that is NOT batched into any PO.
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=member, member_name="Del State",
            status=ScheduleStatus.SCHEDULED,
            delivery_days_cadence=DeliveryCadence.MON_THU,
            starts_on=today - timedelta(days=7), ends_on=today + timedelta(days=60),
            meals_per_day=3,
        )
        # Land the occurrence on a Monday within the window so a recompute would
        # otherwise re-plan it.
        future = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        while future.weekday() != 0:  # Monday
            future += timedelta(days=1)
        OrderSchedule.objects.create(
            enrollment=enr, member=member, member_name="Del State",
            anticipated_delivery_date=future,
            status=OrderStatus.SCHEDULED, household_group_code="G", household=hh,
        )
        return client, case, enr

    def _future_occurrences(self, client):
        from .models import OrderSchedule, OrderStatus

        return OrderSchedule.objects.filter(
            member__client_id=client.client_id, status=OrderStatus.SCHEDULED,
            anticipated_delivery_date__gte=timezone.localdate(),
        ).count()

    def test_open_pending_only_truncates_future_deliveries(self):
        from .models import CaseStatus, ServiceAuthorizationStatus
        from .services.lifecycle import reconcile_internal_service_authorization

        client, _, _ = self._setup(
            auth_status=ServiceAuthorizationStatus.PENDING,
            case_status=CaseStatus.OPEN,
        )
        self.assertEqual(self._future_occurrences(client), 1)
        reconcile_internal_service_authorization(client)
        self.assertEqual(self._future_occurrences(client), 0)

    def test_closed_approved_plus_open_pending_truncates(self):
        from .models import (
            Case, CaseStatus, CaseType, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import reconcile_internal_service_authorization

        # Governing approval sits on a CLOSED case; the only OPEN case is pending.
        client, closed_case, _ = self._setup(
            auth_status=ServiceAuthorizationStatus.APPROVED,
            case_status=CaseStatus.CLOSED,
        )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.PENDING,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        self.assertEqual(self._future_occurrences(client), 1)
        reconcile_internal_service_authorization(client)
        self.assertEqual(self._future_occurrences(client), 0)

    def test_open_approved_keeps_serving(self):
        from datetime import timedelta

        from .models import CaseStatus, ServiceAuthorizationStatus
        from .services.lifecycle import reconcile_internal_service_authorization

        # OPEN + approved with a future window -> service continues (no truncate).
        client, case, _ = self._setup(
            auth_status=ServiceAuthorizationStatus.APPROVED,
            case_status=CaseStatus.OPEN,
        )
        case.service_authorization_approval_ends_at = timezone.now() + timedelta(days=90)
        case.save(update_fields=["service_authorization_approval_ends_at"])
        self.assertEqual(self._future_occurrences(client), 1)
        reconcile_internal_service_authorization(client)
        # Service continues -- the window heals/regenerates rather than truncating.
        self.assertGreaterEqual(self._future_occurrences(client), 1)

    def test_open_approved_drifted_window_with_open_pending_keeps_serving(self):
        from .models import (
            Case, CaseStatus, CaseType, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import reconcile_internal_service_authorization

        # The household holds an OPEN approved authorization whose window has
        # merely drifted/expired (no future end date), AND an OPEN pending case
        # -- a genuine in-flight renewal/switch. This is the legitimate
        # gap-serving case: service must NOT be truncated while the renewal is
        # authorized by the still-open approval.
        client, approved_case, _ = self._setup(
            auth_status=ServiceAuthorizationStatus.APPROVED,
            case_status=CaseStatus.OPEN,
        )
        # No future authorization window on the approval (drifted/expired).
        self.assertIsNone(approved_case.service_authorization_approval_ends_at)
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.PENDING,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        self.assertEqual(self._future_occurrences(client), 1)
        reconcile_internal_service_authorization(client)
        # Gap-serving branch: the open approval + open pending renewal keeps the
        # current kind flowing rather than truncating.
        self.assertEqual(self._future_occurrences(client), 1)

    def test_open_approved_drifted_window_without_pending_truncates(self):
        from .models import CaseStatus, ServiceAuthorizationStatus
        from .services.lifecycle import reconcile_internal_service_authorization

        # An OPEN approved authorization whose window has drifted/expired (no
        # future end date) but with NO pending renewal in flight. This is the
        # negative counterpart to the gap-serving case: the still-open approval
        # alone does NOT keep service running once its window no longer covers
        # the future -- keeping service requires an accompanying OPEN pending
        # renewal/switch. Without one, future deliveries truncate.
        client, approved_case, _ = self._setup(
            auth_status=ServiceAuthorizationStatus.APPROVED,
            case_status=CaseStatus.OPEN,
        )
        # No future authorization window on the approval (drifted/expired) and
        # no separate pending case.
        self.assertIsNone(approved_case.service_authorization_approval_ends_at)
        self.assertEqual(self._future_occurrences(client), 1)
        reconcile_internal_service_authorization(client)
        self.assertEqual(self._future_occurrences(client), 0)


class CadenceProductQuantitiesSchemaTest(SimpleTestCase):
    """PortalCadenceSerializer.validate_product_quantities enforces the new
    shape: meals carry a weekly target (``per_week``) plus an agent-set
    distribution across delivery days (``per_delivery``) that must sum to the
    target; boxes carry a per-DAY rate (``per_day``)."""

    def _validate(self, value, weekdays=None):
        from .portal.serializers import PortalCadenceSerializer

        payload = dict(value)
        if weekdays is not None:
            payload["_weekdays"] = weekdays
        return PortalCadenceSerializer().validate_product_quantities(payload)

    def test_default_shape_is_valid(self):
        from .models import default_cadence_product_quantities

        clean = self._validate(default_cadence_product_quantities())
        self.assertEqual(clean["meals"], {"per_week": 21, "per_delivery": {}})
        self.assertEqual(clean["boxes"], {"per_day": 1})

    def test_per_delivery_must_sum_to_per_week(self):
        from rest_framework import serializers

        # Balanced distribution passes.
        clean = self._validate({
            "meals": {"per_week": 21, "per_delivery": {"mon": 9, "thu": 12}},
            "boxes": {"per_day": 1},
        }, weekdays=["mon", "thu"])
        self.assertEqual(clean["meals"]["per_delivery"], {"mon": 9, "thu": 12})

        # Unbalanced distribution is rejected.
        with self.assertRaises(serializers.ValidationError):
            self._validate({
                "meals": {"per_week": 21, "per_delivery": {"mon": 9, "thu": 9}},
            }, weekdays=["mon", "thu"])

    def test_per_delivery_rejects_non_delivery_weekday(self):
        from rest_framework import serializers

        with self.assertRaises(serializers.ValidationError):
            self._validate({
                "meals": {"per_week": 21, "per_delivery": {"tue": 21}},
            }, weekdays=["mon", "thu"])

    def test_negative_amounts_rejected(self):
        from rest_framework import serializers

        with self.assertRaises(serializers.ValidationError):
            self._validate({"boxes": {"per_day": -1}})


class DeliveryQuantityCadenceTest(TestCase):
    """PO line quantities are driven by the member's assigned-kitchen cadence:
    meals use the cadence's per-delivery distribution for the weekday; boxes use
    its per-DAY rate times the days the delivery covers. When no cadence resolves
    it falls back to the legacy fixed meal map / the stored per-line count."""

    # 2026-01-01 is a Thursday, so: Mon = Jan 5, Wed = Jan 7, Thu = Jan 8.
    from datetime import date as _date
    MON = _date(2026, 1, 5)
    WED = _date(2026, 1, 7)
    THU = _date(2026, 1, 8)

    def _schedule(self, kitchen, stored=0):
        return SimpleNamespace(kitchen=kitchen, how_many_meals_or_boxes=stored)

    def test_meals_use_cadence_per_delivery(self):
        from .models import (
            Cadence, Kitchen, KitchenProductType, KitchenStatus, ProductTypeKind,
        )
        from .services.purchase_orders import delivery_quantity

        cadence = Cadence.objects.create(
            code="mon_thu", label="Mon/Thu", is_active=True,
            weekdays=["mon", "thu"],
            product_quantities={
                ProductTypeKind.MEALS: {
                    "per_week": 21, "per_delivery": {"mon": 5, "thu": 16},
                },
                ProductTypeKind.BOXES: {"per_day": 1},
            },
        )
        kitchen = Kitchen.objects.create(
            name="MealCo", status=KitchenStatus.ACTIVE,
            supported_products=[KitchenProductType.MEAL],
        )
        kitchen.cadences.set([cadence])
        sched = self._schedule(kitchen)

        # The agent-set distribution (5 / 16) is used, NOT the legacy 9 / 12 map.
        self.assertEqual(
            delivery_quantity(ProductTypeKind.MEALS, self.MON, sched), 5
        )
        self.assertEqual(
            delivery_quantity(ProductTypeKind.MEALS, self.THU, sched), 16
        )

    def test_meals_fall_back_to_legacy_map_without_kitchen(self):
        from .models import ProductTypeKind
        from .services.purchase_orders import delivery_quantity

        sched = self._schedule(None)
        # Legacy fixed map: Mon = 9, Thu = 12.
        self.assertEqual(
            delivery_quantity(ProductTypeKind.MEALS, self.MON, sched), 9
        )
        self.assertEqual(
            delivery_quantity(ProductTypeKind.MEALS, self.THU, sched), 12
        )

    def test_boxes_use_cadence_per_day_times_coverage(self):
        from .models import (
            Cadence, Kitchen, KitchenProductType, KitchenStatus, ProductTypeKind,
        )
        from .services.purchase_orders import delivery_quantity

        cadence = Cadence.objects.create(
            code="box_weekly", label="Weekly Box", is_active=True,
            weekdays=[],  # once-a-week: a single delivery covers the full week
            product_quantities={ProductTypeKind.BOXES: {"per_day": 1}},
        )
        kitchen = Kitchen.objects.create(
            name="BoxCo", status=KitchenStatus.ACTIVE,
            supported_products=[KitchenProductType.BOX],
        )
        kitchen.cadences.set([cadence])
        sched = self._schedule(kitchen, stored=1)

        # per_day (1) x days covered (7) = a weekly box delivery of 7.
        self.assertEqual(
            delivery_quantity(ProductTypeKind.BOXES, self.WED, sched), 7
        )

    def test_boxes_fall_back_to_stored_count_without_kitchen(self):
        from .models import ProductTypeKind
        from .services.purchase_orders import delivery_quantity

        sched = self._schedule(None, stored=3)
        self.assertEqual(
            delivery_quantity(ProductTypeKind.BOXES, self.WED, sched), 3
        )


class ReplacementKeepsServiceActiveTest(TestCase):
    """Fix (A): a governing-case REPLACEMENT of an already-serving, same-kind
    member must keep the household in Service Active (carrying kitchen + cadence +
    calendar), never strand it at Pending Verification / Kitchen Assignment.

    Covers BOTH replacement paths:
      * create branch  -- no pre-existing enrollment on the new case; and
      * existing-link branch -- a Pending Verification enrollment already exists
        on the new case (the mass-stranding bug: closing the serving enrollment
        and linking the pending one without carrying service)."""

    def _meals_case(self, client, *, opened_day):
        from datetime import timedelta

        from .models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus

        now = timezone.now()
        return Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=90),
            program_name="Medically Tailored Meals",
            case_created_at=now + timedelta(days=opened_day),
            date_opened=now + timedelta(days=opened_day),
        )

    def _serving_setup(self):
        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, Kitchen, KitchenStatus,
            MemberDeliverySchedule, MemberDietaryProfile, MemberStatus,
            ScheduleStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Serve", last_name="Ing",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        kitchen = Kitchen.objects.create(name="ENG", status=KitchenStatus.ACTIVE)
        old_case = self._meals_case(client, opened_day=1)
        live = EnrollmentVerification.objects.create(
            client=client, household=hh, case=old_case, kitchen=kitchen,
            stage=EnrollmentStage.SERVICE_ACTIVE, program_name="Medically Tailored Meals",
            delivery_weekdays=["mon", "thu"], verified_at=timezone.now(),
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=live, client=client, member_name="Serve Ing",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        MemberDeliverySchedule.objects.create(
            enrollment=live, member_profile=member, member_name="Serve Ing",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, status=ScheduleStatus.SCHEDULED,
        )
        new_case = self._meals_case(client, opened_day=30)
        return client, live, new_case, kitchen

    def test_create_branch_keeps_service_active(self):
        from .models import EnrollmentStage, EnrollmentVerification
        from .services.lifecycle import replace_enrollment_for_case_change

        client, live, new_case, kitchen = self._serving_setup()
        new_enr = replace_enrollment_for_case_change(client, new_case)
        self.assertIsNotNone(new_enr)
        new_enr.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(EnrollmentStage(new_enr.stage), EnrollmentStage.SERVICE_ACTIVE)
        self.assertEqual(new_enr.kitchen_id, kitchen.pk)
        self.assertEqual(str(new_enr.case_id), str(new_case.case_id))
        self.assertEqual(EnrollmentStage(live.stage), EnrollmentStage.CLOSED)
        self.assertEqual(live.close_reason, "case_replaced")

    def test_existing_link_branch_keeps_service_active(self):
        from .models import EnrollmentStage, EnrollmentVerification
        from .services.lifecycle import replace_enrollment_for_case_change

        client, live, new_case, kitchen = self._serving_setup()
        # A Pending Verification enrollment already exists on the NEW case (the
        # stranding trigger): the replacement must drive IT to Service Active.
        existing = EnrollmentVerification.objects.create(
            client=client, household=live.household, case=new_case,
            stage=EnrollmentStage.PENDING_VERIFICATION,
            program_name="Medically Tailored Meals",
        )
        result = replace_enrollment_for_case_change(client, new_case)
        self.assertEqual(result.pk, existing.pk)
        existing.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(
            EnrollmentStage(existing.stage), EnrollmentStage.SERVICE_ACTIVE,
            "existing pending-verification enrollment must be driven to Service Active",
        )
        self.assertEqual(existing.kitchen_id, kitchen.pk)
        self.assertEqual(EnrollmentStage(live.stage), EnrollmentStage.CLOSED)
        self.assertEqual(existing.supersedes_id, live.pk)


class StageChangeTimelineMetadataTest(TestCase):
    """A stage change records the FULL context on the timeline event's metadata:
    previous + new stage (value + label), the trigger, and the reason note -- so
    an auto-hold (or any transition) is traceable to what caused it."""

    def test_hold_records_previous_new_and_trigger(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, TimelineEvent,
        )
        from .services.lifecycle import advance_enrollment

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Meta", last_name="Data",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals",
        )
        advance_enrollment(
            enr, EnrollmentStage.ON_HOLD, note="coverage expired",
            trigger="eligibility.coverage_expired",
        )
        ev = (
            TimelineEvent.objects.filter(client=client, enrollment=enr)
            .order_by("-occurred_at").first()
        )
        self.assertIsNotNone(ev)
        md = ev.metadata or {}
        self.assertEqual(md.get("previous_stage"), EnrollmentStage.SERVICE_ACTIVE.value)
        self.assertEqual(md.get("new_stage"), EnrollmentStage.ON_HOLD.value)
        self.assertEqual(md.get("trigger"), "eligibility.coverage_expired")
        self.assertEqual(md.get("reason"), "coverage expired")
        self.assertTrue(md.get("previous_stage_label"))
        self.assertTrue(md.get("new_stage_label"))


class CalendarHidesSupersededFutureRowsTest(TestCase):
    """Regression: the household delivery calendar must not show a superseded
    (closed) enrollment's leftover FUTURE scheduled occurrence as 'Service Ended'
    next to the active enrollment's 'Scheduled' row for the same date."""

    def test_no_service_ended_duplicate_for_superseded_enrollment(self):
        from datetime import timedelta

        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import (
            Agent, Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
        )

        today = timezone.localdate()
        day = today + timedelta(days=3)
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dup", last_name="Cal",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        now = timezone.now()
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status="approved", date_opened=now,
        )
        # Closed (superseded) enrollment with a leftover future scheduled row.
        old = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.CLOSED,
            close_reason="case_replaced",
        )
        old_mp = MemberDietaryProfile.objects.create(
            enrollment=old, client=client, member_name="Dup Cal",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=old, member=old_mp, member_name="Dup Cal",
            anticipated_delivery_date=day, status=OrderStatus.SCHEDULED,
            household_group_code="G", household=hh,
        )
        # Active enrollment (supersedes old) with a scheduled row same date.
        new = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.SERVICE_ACTIVE, supersedes=old,
        )
        new_mp = MemberDietaryProfile.objects.create(
            enrollment=new, client=client, member_name="Dup Cal",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=new, member=new_mp, member_name="Dup Cal",
            anticipated_delivery_date=day, status=OrderStatus.SCHEDULED,
            household_group_code="G", household=hh,
        )

        agent = Agent.objects.create(name="C", agent_code="952", group="CS")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        r = api.get(f"/api/portal/members/{client.client_id}/delivery-calendar/")
        self.assertEqual(r.status_code, 200, r.content)
        rows = [row for row in r.json()["occurrences"] if row["date"] == day.isoformat()]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["state"], "scheduled")

    def test_past_cancelled_and_active_delivery_deduped(self):
        from datetime import timedelta

        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import (
            Agent, Case, CaseStatus, CaseType, Client, DeliveryOrder,
            DeliveryOrderStatus, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, MemberDietaryProfile, MemberStatus,
            OrderSchedule, OrderStatus, PurchaseOrder, PurchaseOrderStatus,
        )

        today = timezone.localdate()
        past = today - timedelta(days=5)
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Past", last_name="Dup",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status="approved", date_opened=timezone.now(),
        )
        old = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.CLOSED,
            close_reason="case_replaced",
        )
        old_mp = MemberDietaryProfile.objects.create(
            enrollment=old, client=client, member_name="Past Dup",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=old, member=old_mp, member_name="Past Dup",
            anticipated_delivery_date=past, status=OrderStatus.SCHEDULED,
            household_group_code="G", household=hh,
        )
        new = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.SERVICE_ACTIVE, supersedes=old,
        )
        new_mp = MemberDietaryProfile.objects.create(
            enrollment=new, client=client, member_name="Past Dup",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=new, member=new_mp, member_name="Past Dup",
            anticipated_delivery_date=past, status=OrderStatus.SCHEDULED,
            household_group_code="G", household=hh,
        )
        # Two committed DeliveryOrders on the same past date: an old CANCELLED
        # (from the replaced PO) and a live ready_for_delivery.
        po_old = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)
        DeliveryOrder.objects.create(
            purchase_order=po_old, member=client, group=hh,
            expected_delivery_date=past, status=DeliveryOrderStatus.CANCELLED,
        )
        po_new = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)
        DeliveryOrder.objects.create(
            purchase_order=po_new, member=client, group=hh,
            expected_delivery_date=past, status=DeliveryOrderStatus.READY_FOR_DELIVERY,
        )

        agent = Agent.objects.create(name="C2", agent_code="953", group="CS")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        r = api.get(f"/api/portal/members/{client.client_id}/delivery-calendar/")
        self.assertEqual(r.status_code, 200, r.content)
        rows = [row for row in r.json()["occurrences"] if row["date"] == past.isoformat()]
        # A single row for the date, keyed off the live (non-cancelled) delivery.
        self.assertEqual(len(rows), 1, rows)
        self.assertNotEqual(rows[0]["status"], "cancelled")


class CloseDuplicateHoldsTest(TestCase):
    """close_duplicate_holds closes an ON_HOLD enrollment that duplicates a
    member's live SERVICE_ACTIVE enrollment (revived case-less duplicate), but
    leaves a legit different-product hold alone."""

    def _client(self):
        from .models import Client, Household, HouseholdMember
        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dup", last_name="Hold",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        return c, hh

    def test_closes_unbound_hold_next_to_active(self):
        from django.core.management import call_command

        from .models import EnrollmentStage, EnrollmentVerification

        c, hh = self._client()
        active = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals",
        )
        held = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.ON_HOLD,
            program_name="Medically Tailored Meals",
        )
        call_command("close_duplicate_holds", "--apply", "--client", str(c.client_id))
        active.refresh_from_db(); held.refresh_from_db()
        self.assertEqual(EnrollmentStage(active.stage), EnrollmentStage.SERVICE_ACTIVE)
        self.assertEqual(EnrollmentStage(held.stage), EnrollmentStage.CLOSED)
        self.assertEqual(held.close_reason, "duplicate_of_active")

    def test_keeps_different_kind_hold(self):
        from datetime import timedelta

        from django.core.management import call_command

        from .models import (
            Case, CaseStatus, CaseType, EnrollmentStage, EnrollmentVerification,
        )

        c, hh = self._client()
        meals_case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
            date_opened=timezone.now(),
        )
        boxes_case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            program_name="Fresh Produce and Nonperishable Groceries: Pantry Stocking",
            date_opened=timezone.now(),
        )
        EnrollmentVerification.objects.create(
            client=c, household=hh, case=meals_case,
            stage=EnrollmentStage.SERVICE_ACTIVE, program_name=meals_case.program_name,
        )
        boxes_hold = EnrollmentVerification.objects.create(
            client=c, household=hh, case=boxes_case,
            stage=EnrollmentStage.ON_HOLD, program_name=boxes_case.program_name,
        )
        call_command("close_duplicate_holds", "--apply", "--client", str(c.client_id))
        boxes_hold.refresh_from_db()
        self.assertEqual(
            EnrollmentStage(boxes_hold.stage), EnrollmentStage.ON_HOLD,
            "a different-product hold must NOT be closed",
        )


class TicketGoverningCaseAutoLinkTest(TestCase):
    """Every ticket we open for a known member auto-links their GOVERNING
    internal-service case, so the case travels with the ticket -- not just the
    member. An explicitly passed case still wins."""

    def _client_with_case(self, *, stamp=True, status=None):
        from .models import Case, CaseStatus, CaseType, Client

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Tick", last_name="Case",
        )
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=c,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=status or CaseStatus.OPEN,
            program_name="Medically Tailored Meals", date_opened=timezone.now(),
        )
        if stamp:
            c.governing_internal_case_id = str(case.case_id)
            c.save(update_fields=["governing_internal_case_id"])
        return c, case

    def test_open_ticket_auto_links_stamped_governing_case(self):
        from .models import TicketTypeCode
        from .services.tickets import open_ticket

        c, case = self._client_with_case(stamp=True)
        t, created = open_ticket(
            TicketTypeCode.SYSTEM_CHANGE_DETECTED, reason="x", client=c,
        )
        self.assertTrue(created)
        self.assertEqual(str(t.case_id), str(case.case_id))

    def test_governing_case_falls_back_to_open_internal_case(self):
        from .services.tickets import governing_case_for_client

        c, case = self._client_with_case(stamp=False)  # not stamped
        self.assertEqual(
            str(governing_case_for_client(c).case_id), str(case.case_id)
        )

    def test_open_ticket_explicit_case_is_kept(self):
        from .models import Case, CaseStatus, CaseType, TicketTypeCode
        from .services.tickets import open_ticket

        c, gov = self._client_with_case(stamp=True)
        other = Case.objects.create(
            case_id=str(uuid.uuid4()), client=c,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            date_opened=timezone.now(),
        )
        t, _ = open_ticket(
            TicketTypeCode.SYSTEM_CHANGE_DETECTED, reason="x", client=c, case=other,
        )
        self.assertEqual(str(t.case_id), str(other.case_id))

    def test_no_internal_case_leaves_ticket_member_only(self):
        from .models import Client, TicketTypeCode
        from .services.tickets import open_ticket

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="No", last_name="Case",
        )
        t, _ = open_ticket(TicketTypeCode.SYSTEM_CHANGE_DETECTED, reason="x", client=c)
        self.assertIsNone(t.case_id)

    def test_manual_create_endpoint_auto_links_governing_case(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent, TicketType, TicketTypeCode

        c, case = self._client_with_case(stamp=True)
        tt, _ = TicketType.objects.get_or_create(
            code=TicketTypeCode.VERIFICATION, defaults={"label": "Verification"},
        )
        agent = Agent.objects.create(name="Mgr", agent_code="962", group="Management")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        r = api.post("/api/portal/tickets/", {
            "type": tt.code, "reason": "Follow up", "client_id": str(c.client_id),
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        from .models import Ticket
        t = Ticket.objects.get(pk=r.json()["id"])
        self.assertEqual(str(t.case_id), str(case.case_id))


class TicketCreatedByAndActivityTest(TestCase):
    """Manual ticket creation records created_by + assignment, and every ticket
    action (create / assign / status / note / resolve) appends to the ticket's
    activity feed exposed at /tickets/<id>/activity/."""

    def _api(self, group="Management", code="970"):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent

        agent = Agent.objects.create(name="Quinn", agent_code=code, group=group, status="Active")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api, agent

    def _ticket_type(self):
        from .models import TicketType, TicketTypeCode

        tt, _ = TicketType.objects.get_or_create(
            code=TicketTypeCode.VERIFICATION, defaults={"label": "Verification"},
        )
        return tt

    def test_manual_create_records_created_by_and_activity(self):
        from .models import Agent, Ticket, TicketActivityAction

        api, agent = self._api()
        tt = self._ticket_type()
        assignee = Agent.objects.create(
            name="Dana", agent_code="971", group="CS", status="Active",
        )
        r = api.post("/api/portal/tickets/", {
            "type": tt.code, "reason": "Call the member",
            "assignee_id": str(assignee.id),
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        t = Ticket.objects.get(pk=r.json()["id"])
        self.assertEqual(t.created_by_id, agent.id)
        actions = list(t.activities.values_list("action", flat=True))
        self.assertIn(TicketActivityAction.CREATED, actions)
        self.assertIn(TicketActivityAction.ASSIGNED, actions)

    def test_status_note_and_resolve_append_activity(self):
        from .models import Ticket, TicketActivityAction

        api, agent = self._api()
        tt = self._ticket_type()
        r = api.post("/api/portal/tickets/", {"type": tt.code, "reason": "x"}, format="json")
        tid = r.json()["id"]

        # Add a note.
        api.post(f"/api/portal/tickets/{tid}/notes/", {"body": "Left a voicemail"}, format="json")
        # Resolve it.
        api.patch(f"/api/portal/tickets/{tid}/", {"status": "resolved"}, format="json")

        feed = api.get(f"/api/portal/tickets/{tid}/activity/")
        self.assertEqual(feed.status_code, 200, feed.content)
        actions = [e["action"] for e in feed.json()]
        self.assertIn(TicketActivityAction.CREATED, actions)
        self.assertIn(TicketActivityAction.NOTE_ADDED, actions)
        self.assertIn(TicketActivityAction.RESOLVED, actions)
        # Feed is chronological (created first).
        self.assertEqual(actions[0], TicketActivityAction.CREATED)

    def test_serializer_exposes_created_by(self):
        api, agent = self._api()
        tt = self._ticket_type()
        r = api.post("/api/portal/tickets/", {"type": tt.code, "reason": "x"}, format="json")
        self.assertEqual(r.json()["created_by"], agent.name)
        self.assertEqual(r.json()["created_by_id"], str(agent.id))


class NeedAttentionScopeCaseRuleTest(TestCase):
    """The Urgent Care 'No Verification requested' tab (scope=need_attention)
    must enforce rule 1 LIVE: only members who currently hold an OPEN
    internal-service case appear. A stale is_new flag on a member with no
    internal-service case (e.g. only an eligibility case) must NOT surface."""

    def setUp(self):
        from .models import Agent

        self.agent = Agent.objects.create(name="UC", agent_code="963", group="Management")

    def _member(self, *, case_type, is_new=True):
        from .models import (
            Case, CaseStatus, CaseType, Client, Household, HouseholdMember,
            InsurancePlanType, RecordStatus, SocialCareCoverage,
            SocialCareCoverageStatus,
        )

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="N", last_name="A", is_new=is_new,
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        Insurance.objects.create(
            client=c, plan_type=InsurancePlanType.MEDICAID,
            status=RecordStatus.ACTIVE, plan_name="Medicaid",
        )
        SocialCareCoverage.objects.create(
            client=c, status=SocialCareCoverageStatus.ENROLLED, plan_name="SC",
        )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=c, case_type=case_type,
            case_status=CaseStatus.OPEN, program_name="P",
        )
        return c

    def _ids(self):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from .portal.views_members import MembersListView

        v = MembersListView()
        v.request = Request(APIRequestFactory().get("/x", {"scope": "need_attention"}))
        v.kwargs = {}
        groups = v._build_groups_for_page(v._group_entries())
        return {m["id"] for g in groups for m in ([g["primary"]] + g.get("members", []))}

    def test_internal_service_case_shows_eligibility_only_hidden(self):
        from .models import CaseType

        internal = self._member(case_type=CaseType.INTERNAL_SERVICE)
        elig_only = self._member(case_type=CaseType.ELIGIBILITY)  # stale is_new, no IS case

        ids = self._ids()
        self.assertIn(str(internal.client_id), ids)
        self.assertNotIn(str(elig_only.client_id), ids)


class KitchenAbbreviationPoNumberTest(TestCase):
    """The PO number uses the kitchen's configured abbreviation when set, and
    the abbreviation is editable via the kitchen settings endpoint."""

    def test_po_number_uses_kitchen_abbreviation(self):
        from datetime import date

        from .models import Kitchen, KitchenStatus, ProductTypeKind
        from .services.purchase_orders import build_po_number

        k = Kitchen.objects.create(
            name="Hicksville", abbreviation="HICK", status=KitchenStatus.ACTIVE,
        )
        # 2026-08-06 is a Thursday in ISO week 32.
        meals = build_po_number(ProductTypeKind.MEALS, date(2026, 8, 6), k)
        self.assertEqual(meals, "PO-MEALS-2026-W32-THU-HICK")
        # Boxes omit the weekday.
        boxes = build_po_number(ProductTypeKind.BOXES, date(2026, 8, 6), k)
        self.assertEqual(boxes, "PO-BOX-2026-W32-HICK")

    def test_abbreviation_is_sanitized_and_uppercased(self):
        from datetime import date

        from .models import Kitchen, KitchenStatus, ProductTypeKind
        from .services.purchase_orders import build_po_number

        k = Kitchen.objects.create(name="X", abbreviation="k-1 a")
        self.assertEqual(
            build_po_number(ProductTypeKind.MEALS, date(2026, 8, 6), k),
            "PO-MEALS-2026-W32-THU-K1A",
        )

    def test_kitchen_abbreviation_editable_via_settings_api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent, Kitchen, KitchenStatus

        k = Kitchen.objects.create(name="West Side", status=KitchenStatus.ACTIVE)
        agent = Agent.objects.create(name="S", agent_code="964", group="Management")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        r = api.patch(f"/api/portal/settings/kitchens/{k.pk}/",
                      {"name": "West Side Kitchen", "abbreviation": "WSK"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        k.refresh_from_db()
        self.assertEqual(k.name, "West Side Kitchen")
        self.assertEqual(k.abbreviation, "WSK")


class AllVerificationsReportTest(TestCase):
    """The Admin > Reports 'All Verifications' export: one row per verification
    with Member ID + the milestone dates, management-gated."""

    def _api(self, group="Management", code="965"):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent

        agent = Agent.objects.create(name="M", agent_code=code, group=group)
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def test_export_rows_and_management_gate(self):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            ServiceAuthorizationStatus,
        )

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Vera", last_name="Fied",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now - timedelta(days=1),
            program_name="Medically Tailored Meals",
        )
        EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.VERIFIED,
            requested_at=now - timedelta(days=3),
            verified_at=now - timedelta(days=2),
        )

        # Non-management is refused.
        self.assertEqual(self._api(group="CS", code="966").get("/api/portal/reports/all-verifications/").status_code, 403)

        r = self._api().get("/api/portal/reports/all-verifications/")
        raw = b"".join(r.streaming_content) if getattr(r, "streaming", False) else r.content
        self.assertEqual(r.status_code, 200, raw)
        body = raw.decode()
        lines = [ln for ln in body.splitlines() if ln.strip()]
        self.assertEqual(
            lines[0],
            "Member ID,Household/Individual,Verification Requested,Verification Completed,Authorization Approved",
        )
        self.assertIn(str(client.client_id), lines[1])
        # All three dates present (requested/completed/authorized).
        self.assertEqual(lines[1].count(str((now - timedelta(days=2)).date())), 1)


class DeferredReauthGoverningTest(TestCase):
    """A future-dated reauthorization extension (same kind + scope) must NOT
    become the governing case while the current case still serves; a parked
    SCHEDULED_EXTENSION enrollment is created instead."""

    def _setup(self, *, reauth_start_days, reauth_kind_program=None, reauth_scope=None):
        from datetime import timedelta

        from .models import (
            Case, CaseHouseholdType, CaseStatus, CaseType, Client,
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, ServiceAuthorizationStatus,
        )

        now = timezone.now()
        primary = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Rea", last_name="Auth",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)

        current = Case.objects.create(
            case_id=str(uuid.uuid4()), client=primary,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            household_type=CaseHouseholdType.INDIVIDUAL,
            program_name="Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn",
            service_type="Medically Tailored Meals",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now - timedelta(days=30),
            service_authorization_approval_ends_at=now + timedelta(days=30),
        )
        serving = EnrollmentVerification.objects.create(
            client=primary, household=hh, case=current,
            stage=EnrollmentStage.SERVICE_ACTIVE, verified_at=now - timedelta(days=25),
        )
        MemberDietaryProfile.objects.create(
            enrollment=serving, client=primary, menu_type="Standard",
        )

        reauth = Case.objects.create(
            case_id=str(uuid.uuid4()), client=primary,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            household_type=reauth_scope or CaseHouseholdType.INDIVIDUAL,
            is_extension=True,
            program_name=reauth_kind_program
            or "Reauthorization: Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn",
            service_type="Medically Tailored Meals",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now + timedelta(days=reauth_start_days),
            service_authorization_approval_ends_at=now + timedelta(days=reauth_start_days + 60),
        )
        return primary, hh, current, serving, reauth

    def test_future_reauth_is_deferred_and_parked(self):
        from .models import Case, EnrollmentStage, EnrollmentVerification
        from .services.lifecycle import (
            deferred_extension_case_ids, pick_governing_case,
            reconcile_internal_service_authorization,
        )

        primary, hh, current, serving, reauth = self._setup(reauth_start_days=20)
        cases = list(Case.objects.filter(client=primary))

        # The future reauth is deferred; the current case governs.
        deferred = {str(x) for x in deferred_extension_case_ids(cases)}
        self.assertIn(str(reauth.case_id), deferred)
        self.assertEqual(str(pick_governing_case(cases).case_id), str(current.case_id))

        reconcile_internal_service_authorization(primary)

        # A parked SCHEDULED_EXTENSION enrollment now exists for the reauth case,
        # and the serving enrollment is untouched.
        parked = EnrollmentVerification.objects.filter(
            case=reauth, stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        self.assertTrue(parked.exists())
        serving.refresh_from_db()
        self.assertEqual(EnrollmentStage(serving.stage), EnrollmentStage.SERVICE_ACTIVE)

    def test_backdated_reauth_after_current_ended_not_deferred(self):
        # A reauth whose window started in the past AND whose current case's
        # window has ALSO ended -> nothing left to protect -> NOT deferred
        # (immediate switch). (An overlap with a still-active current is covered
        # separately and DOES defer until the current ends.)
        from datetime import timedelta

        from .models import Case
        from .services.lifecycle import deferred_extension_case_ids

        primary, _hh, current, _serving, reauth = self._setup(reauth_start_days=-5)
        # End the current window in the past so there's no active case to protect.
        current.service_authorization_approval_ends_at = timezone.now() - timedelta(days=1)
        current.save(update_fields=["service_authorization_approval_ends_at"])
        cases = list(Case.objects.filter(client=primary))
        deferred = {str(x) for x in deferred_extension_case_ids(cases)}
        self.assertNotIn(str(reauth.case_id), deferred)

    def test_different_scope_reauth_not_deferred(self):
        from .models import Case, CaseHouseholdType
        from .services.lifecycle import deferred_extension_case_ids

        primary, *_rest, reauth = self._setup(
            reauth_start_days=20, reauth_scope=CaseHouseholdType.HOUSEHOLD,
        )
        cases = list(Case.objects.filter(client=primary))
        # Different scope than the serving case -> a real switch, not deferred.
        deferred = {str(x) for x in deferred_extension_case_ids(cases)}
        self.assertNotIn(str(reauth.case_id), deferred)

    def test_closed_reauth_case_not_deferred(self):
        from .models import Case, CaseStatus
        from .services.lifecycle import deferred_extension_case_ids

        primary, _hh, _current, _serving, reauth = self._setup(reauth_start_days=20)
        # A reauth case that has since closed must never be deferred/parked.
        reauth.case_status = CaseStatus.CLOSED
        reauth.save(update_fields=["case_status"])
        cases = list(Case.objects.filter(client=primary))
        deferred = {str(x) for x in deferred_extension_case_ids(cases)}
        self.assertNotIn(str(reauth.case_id), deferred)

    def test_reauth_not_deferred_when_only_matching_case_is_closed(self):
        # A reauth must NOT be parked when the only same-kind case it could extend
        # is CLOSED (e.g. client is served a DIFFERENT program): it's a real switch,
        # not an extension of a currently-serving program.
        from datetime import timedelta

        from .models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus
        from .services.lifecycle import deferred_extension_case_ids

        primary, _hh, current, _serving, reauth = self._setup(reauth_start_days=20)
        # The same-kind case being "extended" is CLOSED -> not currently served.
        current.case_status = CaseStatus.CLOSED
        current.save(update_fields=["case_status"])
        cases = list(Case.objects.filter(client=primary))
        deferred = {str(x) for x in deferred_extension_case_ids(cases)}
        self.assertNotIn(str(reauth.case_id), deferred)

    def test_overlap_defers_until_current_window_ends(self):
        # Current window ends +30; reauth starts +10 (overlap). Between S2 and E1
        # the reauth must STILL be deferred (switch point = max(E1,S2) = E1).
        from datetime import timedelta

        from .models import Case
        from .services.lifecycle import deferred_extension_case_ids

        primary, _hh, _current, _serving, reauth = self._setup(reauth_start_days=10)
        cases = list(Case.objects.filter(client=primary))
        # "today" = +20: past reauth start (+10) but before current end (+30).
        today = (timezone.now() + timedelta(days=20)).date()
        deferred = {str(x) for x in deferred_extension_case_ids(cases, today=today)}
        self.assertIn(str(reauth.case_id), deferred)
        # "today" = +31: past the current window end -> no longer deferred.
        later = (timezone.now() + timedelta(days=31)).date()
        deferred2 = {str(x) for x in deferred_extension_case_ids(cases, today=later)}
        self.assertNotIn(str(reauth.case_id), deferred2)


class DependentSplitTest(TestCase):
    """split_dependent_into_own_enrollment: peel a household dependent into their
    own internal-service case, carrying their data + status + nutritionist."""

    def _scenario(self, *, dep_status=None, nutritionist_approved=False):
        from datetime import timedelta

        from .models import (
            Address, Case, CaseHouseholdType, CaseStatus, CaseType, Client,
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            Kitchen, KitchenStatus, MemberDeliverySchedule, MemberDietaryProfile,
            MemberStatus, ScheduleStatus, ServiceAuthorizationStatus,
        )

        dep_status = dep_status or MemberStatus.ACTIVE
        now = timezone.now()
        primary = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Prim", last_name="Ary")
        dep = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Dep", last_name="Endent")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        kitchen = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        addr = Address.objects.create(
            client=primary, type="temporary", street="1 St", city="NY",
            state="NY", zip="11201", notes="ring bell",
        )
        prog = "Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn"
        hh_case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=primary, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, household_type=CaseHouseholdType.HOUSEHOLD,
            program_name=prog, service_type="Medically Tailored Meals",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
        )
        hh_enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, case=hh_case, stage=EnrollmentStage.SERVICE_ACTIVE,
            kitchen=kitchen, delivery_address=addr, delivery_weekdays=["mon", "thu"],
            household_size=2, is_family_verified=True, verified_at=now, program_name=prog,
        )
        if nutritionist_approved:
            hh_enr.nutritionist_approved_at = now
            hh_enr.nutritionist_signature = "RD"
            hh_enr.save(update_fields=["nutritionist_approved_at", "nutritionist_signature"])
        MemberDietaryProfile.objects.create(
            enrollment=hh_enr, client=primary, member_name="Prim Ary",
            status=MemberStatus.ACTIVE, menu_type="Standard",
        )
        dep_prof = MemberDietaryProfile.objects.create(
            enrollment=hh_enr, client=dep, member_name="Dep Endent", status=dep_status,
            menu_type="Kosher", conditions=["Diabetes"], medications=["Insulin"],
            weight="150", height="5'6", mobile_number="5551234",
            meal_plan="Plan A" if nutritionist_approved else "",
            assessment_notes="assess" if nutritionist_approved else "",
            nutritionist_pdf_key="key123" if nutritionist_approved else "",
        )
        MemberDeliverySchedule.objects.create(
            enrollment=hh_enr, member_profile=dep_prof, member_name="Dep Endent",
            kitchen=kitchen, delivery_days_cadence="mon_thu", status=ScheduleStatus.SCHEDULED,
            starts_on=now.date(), ends_on=(now + timedelta(days=30)).date(),
        )
        dep_case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=dep, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, household_type=CaseHouseholdType.INDIVIDUAL,
            program_name=prog, service_type="Medically Tailored Meals",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
        )
        new_enr = EnrollmentVerification.objects.create(
            client=dep, household=hh, case=dep_case, stage=EnrollmentStage.VERIFIED,
            verified_at=now, program_name=prog,
        )
        new_prof = MemberDietaryProfile.objects.create(
            enrollment=new_enr, client=dep, member_name="Dep Endent",
            status=MemberStatus.PENDING,
        )
        return locals()

    def test_bare_enrollment_creates_profile_and_carries_everything(self):
        # The CRM "Request Verification" button hands the split a BARE enrollment
        # (no member profile), unlike the wizard which pre-seeds one. The split
        # must CREATE the dependent's profile from their old household profile and
        # carry the same data -- so both buttons do identical work.
        from .models import EnrollmentStage, MemberStatus
        from .services.lifecycle import split_dependent_into_own_enrollment

        s = self._scenario(dep_status=MemberStatus.ACTIVE, nutritionist_approved=True)
        # Simulate the CRM path: strip the pre-seeded profile -> bare enrollment.
        s["new_prof"].delete()
        self.assertFalse(s["new_enr"].member_profiles.exists())

        out = split_dependent_into_own_enrollment(s["dep"], s["new_enr"])
        self.assertTrue(out["split"])
        s["new_enr"].refresh_from_db()

        # A profile was created for the dependent, carrying clinical/dietary data.
        prof = s["new_enr"].member_profiles.get(client=s["dep"])
        self.assertEqual(prof.conditions, ["Diabetes"])
        self.assertEqual(prof.medications, ["Insulin"])
        self.assertEqual(prof.weight, "150")
        self.assertEqual(prof.menu_type, "Kosher")
        self.assertEqual(prof.nutritionist_pdf_key, "key123")
        # Kitchen + address + cadence (delivery_weekdays) carried, service active.
        self.assertEqual(s["new_enr"].kitchen_id, s["kitchen"].pk)
        self.assertEqual(s["new_enr"].delivery_address_id, s["addr"].pk)
        self.assertEqual(s["new_enr"].delivery_weekdays, ["mon", "thu"])
        self.assertEqual(EnrollmentStage(s["new_enr"].stage), EnrollmentStage.SERVICE_ACTIVE)
        # Nutritionist sign-off carried at the enrollment level too.
        self.assertIsNotNone(s["new_enr"].nutritionist_approved_at)

    def test_active_split_carries_and_removes_history(self):
        from .models import (
            HouseholdMember, MemberDietaryProfile, MemberStatus,
        )
        from .serializers import sync_household_members
        from .services.lifecycle import split_dependent_into_own_enrollment

        s = self._scenario(dep_status=MemberStatus.ACTIVE)
        out = split_dependent_into_own_enrollment(s["dep"], s["new_enr"])
        self.assertTrue(out["split"])
        # Old profile kept as REMOVED history.
        s["dep_prof"].refresh_from_db()
        self.assertEqual(s["dep_prof"].status, MemberStatus.REMOVED)
        # Dependent left the shared roster, now primary of their own household.
        self.assertFalse(s["hh"].members.filter(client=s["dep"]).exists())
        own = HouseholdMember.objects.get(client=s["dep"])
        self.assertTrue(own.is_primary)
        self.assertNotEqual(own.household_id, s["hh"].household_id)
        # New enrollment carried the kitchen + address and was activated (the
        # exact member status after the kitchen meal-rule depends on kitchen menu
        # config, so assert the serving carry advanced the stage, not the status).
        from .models import EnrollmentStage
        s["new_enr"].refresh_from_db()
        s["new_prof"].refresh_from_db()
        self.assertEqual(s["new_enr"].kitchen_id, s["kitchen"].pk)
        self.assertEqual(s["new_enr"].delivery_address_id, s["addr"].pk)
        self.assertEqual(EnrollmentStage(s["new_enr"].stage), EnrollmentStage.SERVICE_ACTIVE)
        self.assertNotEqual(s["new_prof"].status, MemberStatus.REMOVED)
        # Per-member data carried (blank-fill from old).
        self.assertEqual(s["new_prof"].conditions, ["Diabetes"])
        # Sync must NOT re-add the removed dependent to the shared roster.
        sync_household_members(s["primary"], enrollment=s["hh_enr"])
        self.assertFalse(s["hh"].members.filter(client=s["dep"]).exists())

    def test_pending_enrollment_carries_verified_and_prunes_to_dependent_only(self):
        # Mirrors the extension's Request-Verification path: the new enrollment is
        # PENDING (not yet verified) and seeded with the WHOLE shared household's
        # participant rows. The split must carry the verified fact from the shared
        # enrollment, prune the enrollment to the dependent alone, and activate.
        from .models import EnrollmentStage, MemberDietaryProfile, MemberStatus
        from .services.lifecycle import split_dependent_into_own_enrollment

        s = self._scenario(dep_status=MemberStatus.ACTIVE)
        new_enr = s["new_enr"]
        new_enr.stage = EnrollmentStage.PENDING_VERIFICATION
        new_enr.verified_at = None
        new_enr.save(update_fields=["stage", "verified_at"])
        # Seed the primary's participant row too (as the ext payload would).
        MemberDietaryProfile.objects.create(
            enrollment=new_enr, client=s["primary"], member_name="Prim Ary",
            status=MemberStatus.PENDING,
        )

        out = split_dependent_into_own_enrollment(s["dep"], new_enr)
        self.assertTrue(out["split"])
        new_enr.refresh_from_db()
        # Verified fact carried from the shared enrollment (no re-verification).
        self.assertIsNotNone(new_enr.verified_at)
        # Pruned to the dependent alone (the primary's seeded row is gone).
        self.assertEqual(
            {str(x) for x in new_enr.member_profiles.values_list("client_id", flat=True)},
            {str(s["dep"].client_id)},
        )
        # Service carried + activated (kitchen/address carried, stage advanced).
        self.assertEqual(new_enr.kitchen_id, s["kitchen"].pk)
        self.assertEqual(new_enr.delivery_address_id, s["addr"].pk)
        self.assertEqual(EnrollmentStage(new_enr.stage), EnrollmentStage.SERVICE_ACTIVE)
        # Old profile REMOVED history.
        s["dep_prof"].refresh_from_db()
        self.assertEqual(s["dep_prof"].status, MemberStatus.REMOVED)
        # The dependent had no INDIVIDUAL nutritionist review (blank per-member
        # nutrition fields), so they must NOT inherit the household's sign-off and
        # must be tagged Pending Nutritionist.
        from .services.lifecycle import PENDING_NUTRITIONIST_TAG_NAME
        self.assertIsNone(new_enr.nutritionist_approved_at)
        self.assertTrue(s["dep"].tags.filter(name=PENDING_NUTRITIONIST_TAG_NAME).exists())

    def test_active_dependent_keeps_active_status_and_only_gets_pending_tag(self):
        # Household is nutritionist-approved but the DEPENDENT was never reviewed
        # individually. Splitting must keep them ACTIVE (carry the household's
        # sign-off so service isn't interrupted) and flag the nutritionist look
        # via the Pending Nutritionist TAG only -- NOT by downgrading their status.
        from .models import EnrollmentStage, MemberStatus
        from .services.lifecycle import (
            PENDING_NUTRITIONIST_TAG_NAME, split_dependent_into_own_enrollment,
        )

        s = self._scenario(nutritionist_approved=True, dep_status=MemberStatus.ACTIVE)
        # Wipe the dependent's per-member nutrition data so they read as NOT
        # individually reviewed (the household enrollment stays approved).
        dep_prof = s["dep_prof"]
        dep_prof.meal_plan = ""
        dep_prof.assessment_notes = ""
        dep_prof.nutritionist_pdf_key = ""
        dep_prof.save(update_fields=["meal_plan", "assessment_notes", "nutritionist_pdf_key"])

        split_dependent_into_own_enrollment(s["dep"], s["new_enr"])
        s["new_enr"].refresh_from_db()
        # Active service preserved: the household's nutritionist sign-off is kept
        # (not cleared), so the enrollment reads active, not "Pending Nutritionist".
        self.assertIsNotNone(s["new_enr"].nutritionist_approved_at)
        self.assertEqual(EnrollmentStage(s["new_enr"].stage), EnrollmentStage.SERVICE_ACTIVE)
        # The nutritionist look is flagged by the TAG only.
        self.assertTrue(s["dep"].tags.filter(name=PENDING_NUTRITIONIST_TAG_NAME).exists())

    def test_paused_split_preserves_status_and_tags_attention(self):
        from .models import MemberDietaryProfile, MemberStatus
        from .services.lifecycle import (
            NEED_ATTENTION_TAG_NAME, split_dependent_into_own_enrollment,
        )

        s = self._scenario(dep_status=MemberStatus.PAUSED)
        split_dependent_into_own_enrollment(s["dep"], s["new_enr"])
        s["new_prof"].refresh_from_db()
        self.assertEqual(s["new_prof"].status, MemberStatus.PAUSED)
        self.assertTrue(s["dep"].tags.filter(name=NEED_ATTENTION_TAG_NAME).exists())

    def test_not_approved_tags_pending_nutritionist(self):
        from .services.lifecycle import (
            PENDING_NUTRITIONIST_TAG_NAME, split_dependent_into_own_enrollment,
        )

        s = self._scenario(nutritionist_approved=False)
        split_dependent_into_own_enrollment(s["dep"], s["new_enr"])
        self.assertTrue(s["dep"].tags.filter(name=PENDING_NUTRITIONIST_TAG_NAME).exists())
        s["new_enr"].refresh_from_db()
        self.assertIsNone(s["new_enr"].nutritionist_approved_at)

    def test_approved_copies_nutritionist_data(self):
        from .models import MemberDietaryProfile
        from .services.lifecycle import (
            PENDING_NUTRITIONIST_TAG_NAME, split_dependent_into_own_enrollment,
        )

        s = self._scenario(nutritionist_approved=True)
        split_dependent_into_own_enrollment(s["dep"], s["new_enr"])
        s["new_enr"].refresh_from_db()
        s["new_prof"].refresh_from_db()
        self.assertIsNotNone(s["new_enr"].nutritionist_approved_at)
        self.assertEqual(s["new_prof"].meal_plan, "Plan A")
        self.assertEqual(s["new_prof"].nutritionist_pdf_key, "key123")
        self.assertFalse(s["dep"].tags.filter(name=PENDING_NUTRITIONIST_TAG_NAME).exists())

    def test_non_dependent_is_noop(self):
        from .models import Client, EnrollmentVerification, EnrollmentStage
        from .services.lifecycle import split_dependent_into_own_enrollment

        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Solo", last_name="Client")
        enr = EnrollmentVerification.objects.create(client=c, stage=EnrollmentStage.VERIFIED)
        out = split_dependent_into_own_enrollment(c, enr)
        self.assertFalse(out["split"])


class ReauthExtensionActivationTest(TestCase):
    """process_scheduled_extensions advances a parked reauthorization by the
    calendar: activate at max(E1,S2), gap-pause between windows."""

    def _build(self, *, current_end_days, reauth_start_days):
        from datetime import timedelta

        from .models import (
            Case, CaseHouseholdType, CaseStatus, CaseType, Client,
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import reconcile_internal_service_authorization

        now = timezone.now()
        primary = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ex", last_name="Tend",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        current = Case.objects.create(
            case_id=str(uuid.uuid4()), client=primary,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            household_type=CaseHouseholdType.INDIVIDUAL,
            program_name="Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn",
            service_type="Medically Tailored Meals",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now - timedelta(days=30),
            service_authorization_approval_ends_at=now + timedelta(days=current_end_days),
        )
        serving = EnrollmentVerification.objects.create(
            client=primary, household=hh, case=current,
            stage=EnrollmentStage.SERVICE_ACTIVE, verified_at=now - timedelta(days=25),
        )
        MemberDietaryProfile.objects.create(
            enrollment=serving, client=primary, menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        Case.objects.create(
            case_id=str(uuid.uuid4()), client=primary,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            household_type=CaseHouseholdType.INDIVIDUAL, is_extension=True,
            program_name="Reauthorization: Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn",
            service_type="Medically Tailored Meals",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now + timedelta(days=reauth_start_days),
            service_authorization_approval_ends_at=now + timedelta(days=reauth_start_days + 60),
        )
        # Park the reauth as a SCHEDULED_EXTENSION enrollment.
        reconcile_internal_service_authorization(primary)
        return primary, serving

    def test_activation_at_switch_point(self):
        from datetime import timedelta

        from .models import EnrollmentStage, EnrollmentVerification, MemberStatus
        from .services.lifecycle import process_scheduled_extensions

        primary, serving = self._build(current_end_days=30, reauth_start_days=20)
        waiting = EnrollmentVerification.objects.get(
            client=primary, stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        # today past max(E1=+30, S2=+20) -> activate.
        today = (timezone.now() + timedelta(days=31)).date()
        res = process_scheduled_extensions(client=primary, today=today, apply=True)
        self.assertEqual(res["activated"], 1)

        waiting.refresh_from_db()
        serving.refresh_from_db()
        # Activated off the parked stage (Service Active when a kitchen+cadence
        # carries; Kitchen Assignment when none is set up to carry, as here).
        self.assertIn(
            EnrollmentStage(waiting.stage),
            (EnrollmentStage.SERVICE_ACTIVE, EnrollmentStage.KITCHEN_ASSIGNMENT),
        )
        self.assertEqual(EnrollmentStage(serving.stage), EnrollmentStage.CLOSED)
        self.assertEqual(waiting.supersedes_id, serving.pk)
        # Member resumed active on the activated extension (park-time snapshot).
        prof = waiting.member_profiles.get(client=primary)
        self.assertEqual(prof.status, MemberStatus.ACTIVE)

    def test_parking_creates_waiting_schedule_not_serving(self):
        from datetime import timedelta

        from .models import (
            EnrollmentStage, EnrollmentVerification, Kitchen, KitchenStatus,
            MemberDeliverySchedule, ScheduleStatus,
        )
        from .services.lifecycle import reconcile_internal_service_authorization

        # Build with a serving enrollment that HAS a kitchen + a cadence schedule,
        # so parking can mirror it as a WAITING schedule.
        primary, serving = self._build(current_end_days=30, reauth_start_days=20)
        kitchen = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        serving.kitchen = kitchen
        serving.save(update_fields=["kitchen"])
        MemberDeliverySchedule.objects.create(
            enrollment=serving,
            member_profile=serving.member_profiles.get(client=primary),
            member_name="Ex Tend", kitchen=kitchen,
            delivery_days_cadence="mon_thu",
            status=ScheduleStatus.SCHEDULED,
            starts_on=timezone.now().date(),
            ends_on=(timezone.now() + timedelta(days=30)).date(),
        )
        # Re-run the reconcile so parking mirrors the (now-present) kitchen/cadence.
        reconcile_internal_service_authorization(primary)

        waiting = EnrollmentVerification.objects.get(
            client=primary, stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        # WAITING schedule(s) exist; NONE are SCHEDULED -> never on a PO.
        self.assertTrue(
            waiting.delivery_schedules.filter(status=ScheduleStatus.WAITING).exists()
        )
        self.assertFalse(
            waiting.delivery_schedules.filter(status=ScheduleStatus.SCHEDULED).exists()
        )
        self.assertEqual(waiting.kitchen_id, kitchen.pk)

    def test_scheduled_extension_calendar_preview(self):
        from datetime import timedelta

        from .models import (
            EnrollmentStage, EnrollmentVerification, Kitchen, KitchenStatus,
            MemberDeliverySchedule, ScheduleStatus,
        )
        from .portal.views_delivery_calendar import MemberDeliveryCalendarView
        from .services.lifecycle import reconcile_internal_service_authorization

        primary, serving = self._build(current_end_days=30, reauth_start_days=20)
        kitchen = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        serving.kitchen = kitchen
        serving.save(update_fields=["kitchen"])
        MemberDeliverySchedule.objects.create(
            enrollment=serving,
            member_profile=serving.member_profiles.get(client=primary),
            member_name="Ex Tend", kitchen=kitchen, delivery_days_cadence="mon_thu",
            status=ScheduleStatus.SCHEDULED, starts_on=timezone.now().date(),
            ends_on=(timezone.now() + timedelta(days=30)).date(),
        )
        reconcile_internal_service_authorization(primary)
        waiting = EnrollmentVerification.objects.get(
            client=primary, stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        v = MemberDeliveryCalendarView()
        preview = v._scheduled_extension_preview(waiting)
        # Preview has planned rows, all in the "reauthorization" state (never on a PO).
        self.assertTrue(len(preview) > 0)
        self.assertTrue(all(r["state"] == "reauthorization" for r in preview))
        self.assertTrue(all(not r["committed"] and not r["po_number"] for r in preview))
        summary = v._scheduled_extension_summary(waiting, preview)
        self.assertEqual(summary["cadence"], "mon_thu")
        self.assertEqual(summary["kitchen_name"], "K")

    def test_waiting_schedule_not_carried_across_kinds(self):
        # A Boxes kitchen must never be mirrored onto a Meals reauthorization.
        from datetime import timedelta

        from .models import (
            Case, CaseHouseholdType, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, Kitchen,
            KitchenStatus, MemberDietaryProfile, MemberStatus,
            ServiceAuthorizationStatus,
        )
        from .services.lifecycle import _carry_waiting_schedule

        now = timezone.now()
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="K", last_name="C")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        kitchen = Kitchen.objects.create(name="BoxKitchen", status=KitchenStatus.ACTIVE)
        # Live enrollment is BOXES (pin the kind via an override so it resolves
        # deterministically regardless of the client's other cases / seed data).
        from .models import ProductType, ProductTypeKind
        box_pt = ProductType.objects.create(type=ProductTypeKind.BOXES)
        boxes_case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, household_type=CaseHouseholdType.INDIVIDUAL,
            program_name="Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Other Eligible Populations - Brooklyn",
            service_type="Produce Prescription/Voucher",
        )
        live = EnrollmentVerification.objects.create(
            client=c, household=hh, case=boxes_case, stage=EnrollmentStage.SERVICE_ACTIVE,
            kitchen=kitchen, verified_at=now, product_type_override=box_pt,
        )
        MemberDietaryProfile.objects.create(enrollment=live, client=c, status=MemberStatus.ACTIVE)
        # Parked MEALS reauth enrollment (different kind).
        meals_case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, household_type=CaseHouseholdType.INDIVIDUAL,
            is_extension=True,
            program_name="Reauthorization: Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn",
            service_type="Medically Tailored Meals",
        )
        waiting = EnrollmentVerification.objects.create(
            client=c, household=hh, case=meals_case, stage=EnrollmentStage.SCHEDULED_EXTENSION,
            verified_at=now,
        )
        _carry_waiting_schedule(waiting, live, meals_case)
        waiting.refresh_from_db()
        # No Boxes kitchen carried, no schedule mirrored across kinds.
        self.assertIsNone(waiting.kitchen_id)
        self.assertFalse(waiting.delivery_schedules.exists())

    def test_scheduled_extension_preview_falls_back_to_serving_plan(self):
        # A parked reauth with NO WAITING schedule still previews deliveries by
        # projecting the CURRENT serving plan over the reauth window.
        from datetime import timedelta

        from .models import (
            Case, CaseHouseholdType, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, Kitchen,
            KitchenStatus, MemberDeliverySchedule, MemberDietaryProfile,
            MemberStatus, ScheduleStatus, ServiceAuthorizationStatus,
        )
        from .portal.views_delivery_calendar import MemberDeliveryCalendarView

        now = timezone.now()
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="P", last_name="V")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        kitchen = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        cur_case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, household_type=CaseHouseholdType.INDIVIDUAL,
            program_name="Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn",
            service_type="Medically Tailored Meals",
        )
        serving = EnrollmentVerification.objects.create(
            client=c, household=hh, case=cur_case, stage=EnrollmentStage.SERVICE_ACTIVE,
            kitchen=kitchen, verified_at=now,
        )
        sp = MemberDietaryProfile.objects.create(enrollment=serving, client=c, status=MemberStatus.ACTIVE)
        MemberDeliverySchedule.objects.create(
            enrollment=serving, member_profile=sp, member_name="P V", kitchen=kitchen,
            delivery_days_cadence="mon_thu", prod_per_delivery=3,
            status=ScheduleStatus.SCHEDULED,
            starts_on=(now - timedelta(days=30)).date(), ends_on=(now + timedelta(days=7)).date(),
        )
        # Parked reauth with NO WAITING schedule, window in the near future.
        reauth = Case.objects.create(
            case_id=str(uuid.uuid4()), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, household_type=CaseHouseholdType.INDIVIDUAL,
            is_extension=True,
            program_name="Reauthorization: Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn",
            service_type="Medically Tailored Meals",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now + timedelta(days=8),
            service_authorization_approval_ends_at=now + timedelta(days=100),
        )
        waiting = EnrollmentVerification.objects.create(
            client=c, household=hh, case=reauth, stage=EnrollmentStage.SCHEDULED_EXTENSION,
            verified_at=now,
        )
        v = MemberDeliveryCalendarView()
        preview = v._scheduled_extension_preview(waiting)
        self.assertTrue(len(preview) > 0)
        self.assertTrue(all(r["state"] == "reauthorization" for r in preview))
        # All preview dates fall within the reauth window (not the serving window).
        self.assertTrue(all(r["date"] >= (now + timedelta(days=8)).date().isoformat() for r in preview))
        summary = v._scheduled_extension_summary(waiting, preview)
        self.assertEqual(summary["cadence"], "mon_thu")
        self.assertEqual(summary["kitchen_name"], "K")

    def test_parking_logs_scheduled_stage_event(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, StageEntityType, StageEvent,
        )

        primary, _serving = self._build(current_end_days=30, reauth_start_days=20)
        waiting = EnrollmentVerification.objects.get(
            client=primary, stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        ev = StageEvent.objects.filter(
            entity_type=StageEntityType.ENROLLMENT, enrollment=waiting,
            to_stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        self.assertTrue(ev.exists())

    def test_activation_refreshes_dietary_from_current(self):
        # A dietary change made on the CURRENT enrollment during the wait must be
        # reflected on the extension at activation (not the stale park snapshot).
        from datetime import timedelta

        from .models import EnrollmentStage, EnrollmentVerification, MemberDietaryProfile
        from .services.lifecycle import process_scheduled_extensions

        primary, serving = self._build(current_end_days=30, reauth_start_days=20)
        waiting = EnrollmentVerification.objects.get(
            client=primary, stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        # Park snapshot carried menu_type="Standard". Change it on the CURRENT
        # enrollment after parking.
        sp = serving.member_profiles.get(client=primary)
        sp.menu_type = "Kosher"
        sp.save(update_fields=["menu_type"])
        # Sanity: the parked snapshot is still stale here.
        self.assertEqual(
            waiting.member_profiles.get(client=primary).menu_type, "Standard"
        )

        today = (timezone.now() + timedelta(days=31)).date()
        process_scheduled_extensions(client=primary, today=today, apply=True)

        waiting.refresh_from_db()
        self.assertEqual(
            waiting.member_profiles.get(client=primary).menu_type, "Kosher"
        )

    def test_gap_pauses_members(self):
        from datetime import timedelta

        from .models import EnrollmentStage, MemberStatus
        from .services.lifecycle import process_scheduled_extensions

        primary, serving = self._build(current_end_days=10, reauth_start_days=20)
        # E1=+10 < today=+15 < S2=+20 -> gap.
        today = (timezone.now() + timedelta(days=15)).date()
        res = process_scheduled_extensions(client=primary, today=today, apply=True)
        self.assertEqual(res["gapped"], 1)

        serving.refresh_from_db()
        self.assertEqual(EnrollmentStage(serving.stage), EnrollmentStage.SERVICE_COMPLETE)
        self.assertEqual(serving.close_reason, "reauth_gap")
        prof = serving.member_profiles.get(client=primary)
        self.assertEqual(prof.status, MemberStatus.PAUSED)

    def test_parking_applies_reauthorized_tag_cleared_on_activation(self):
        from datetime import timedelta

        from .models import EnrollmentStage, EnrollmentVerification
        from .services.lifecycle import (
            REAUTHORIZED_TAG_NAME, process_scheduled_extensions,
        )

        # _build parks a reauth via reconcile -> green "Reauthorized" tag applied.
        primary, serving = self._build(current_end_days=30, reauth_start_days=20)
        self.assertTrue(
            primary.tags.filter(name=REAUTHORIZED_TAG_NAME).exists()
        )
        # Once activated (no parked reauth remains), the tag clears.
        today = (timezone.now() + timedelta(days=31)).date()
        process_scheduled_extensions(client=primary, today=today, apply=True)
        self.assertFalse(
            primary.tags.filter(name=REAUTHORIZED_TAG_NAME).exists()
        )

    def test_gap_applies_reauth_attention_tag(self):
        from datetime import timedelta

        from .services.lifecycle import (
            REAUTH_ATTENTION_TAG_NAME, process_scheduled_extensions,
        )

        primary, serving = self._build(current_end_days=10, reauth_start_days=20)
        today = (timezone.now() + timedelta(days=15)).date()
        process_scheduled_extensions(client=primary, today=today, apply=True)
        self.assertTrue(
            primary.tags.filter(name=REAUTH_ATTENTION_TAG_NAME).exists()
        )

    def test_clean_activation_clears_attention_tag(self):
        from datetime import timedelta

        from .services.lifecycle import (
            REAUTH_ATTENTION_TAG_NAME, _reauth_attention_tag,
            process_scheduled_extensions,
        )

        primary, serving = self._build(current_end_days=30, reauth_start_days=20)
        # Pre-seed the tag as if a gap had flagged it earlier.
        primary.tags.add(_reauth_attention_tag())
        today = (timezone.now() + timedelta(days=31)).date()
        process_scheduled_extensions(client=primary, today=today, apply=True)
        self.assertFalse(
            primary.tags.filter(name=REAUTH_ATTENTION_TAG_NAME).exists()
        )

    def test_gap_program_status_reads_reauthorization(self):
        from datetime import timedelta

        from .models import ProgramStatus
        from .services.lifecycle import process_scheduled_extensions, program_status

        primary, serving = self._build(current_end_days=10, reauth_start_days=20)
        today = (timezone.now() + timedelta(days=15)).date()
        process_scheduled_extensions(client=primary, today=today, apply=True)
        serving.refresh_from_db()
        self.assertEqual(program_status(serving), ProgramStatus.REAUTHORIZATION)


class ScheduledExtensionInertStageTest(TestCase):
    """The parked SCHEDULED_EXTENSION (Reauthorization - Waiting) enrollment must
    be inert: excluded from serving surfaces, never the active/governing
    enrollment, and never shown as pending verification."""

    def _client(self, first="A", last="B"):
        from .models import Client
        return Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last
        )

    def test_stage_in_service_excluded_set(self):
        from .models import SERVICE_EXCLUDED_ENROLLMENT_STAGES, EnrollmentStage
        self.assertIn(
            EnrollmentStage.SCHEDULED_EXTENSION, SERVICE_EXCLUDED_ENROLLMENT_STAGES
        )

    def test_not_chosen_as_active_or_governing_enrollment(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
        )
        from .portal.serializers import active_enrollment
        from .services.lifecycle import _governing_enrollments

        primary = self._client("Pat", "Primary")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        serving = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        waiting = EnrollmentVerification.objects.create(
            client=primary, household=hh,
            stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        # active_enrollment picks the serving one, never the parked reauth.
        self.assertEqual(active_enrollment(primary).pk, serving.pk)
        # governance ignores the parked reauth entirely.
        gov_pks = {e.pk for e in _governing_enrollments(primary)}
        self.assertIn(serving.pk, gov_pks)
        self.assertNotIn(waiting.pk, gov_pks)

    def test_excluded_from_pending_verification_report(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
        )
        from .portal.report_exports import members_pending_verification_rows

        primary = self._client("Wanda", "Waiting")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        # A stale pending row + a parked reauth (already verified) -> excluded.
        EnrollmentVerification.objects.create(
            client=primary, household=hh,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        EnrollmentVerification.objects.create(
            client=primary, household=hh,
            stage=EnrollmentStage.SCHEDULED_EXTENSION,
        )
        ids = {r[0] for r in list(members_pending_verification_rows({}))[1:]}
        self.assertNotIn(str(primary.client_id), ids)


class MembersPendingVerificationReportTest(TestCase):
    """The Admin > Reports 'Members Pending Verification' export: only members
    genuinely awaiting verification (not already verified/served on a later
    enrollment), a Case Created column, and a date filter on the governing
    case's created date."""

    def _rows(self, **params):
        from .portal.report_exports import members_pending_verification_rows
        return list(members_pending_verification_rows(params))

    def _household(self, first, last, *, stage, case_created=None, case_status=None):
        from datetime import date

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentVerification,
            Household, HouseholdMember,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name=first, last_name=last,
            # A real member's lifecycle_stage tracks their enrollment stage; the
            # export (like the Verification page) gates on it being in the
            # verification window.
            lifecycle_stage=stage,
        )
        hh = Household.objects.create(name=f"{last} HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE,
            case_status=case_status or CaseStatus.OPEN,
            date_opened=case_created or timezone.now(),
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case, stage=stage,
        )
        return client, hh, enr

    def test_excludes_already_verified_and_adds_case_created(self):
        from datetime import datetime

        from .models import EnrollmentStage

        pending_dt = timezone.make_aware(datetime(2026, 8, 5, 12, 0))
        pending, _, _ = self._household(
            "Penny", "Pending",
            stage=EnrollmentStage.PENDING_VERIFICATION, case_created=pending_dt,
        )
        # A household with BOTH a stale pending row AND a later served
        # enrollment must be excluded (this is the "already verified" bug).
        served, served_hh, _ = self._household(
            "Vera", "Fied",
            stage=EnrollmentStage.PENDING_VERIFICATION,
            case_created=timezone.now(),
        )
        from .models import EnrollmentVerification
        EnrollmentVerification.objects.create(
            client=served, household=served_hh,
            stage=EnrollmentStage.SERVICE_ACTIVE, verified_at=timezone.now(),
        )

        rows = self._rows()
        header, data = rows[0], rows[1:]
        self.assertEqual(
            header,
            ["Client ID", "First Name", "Last Name", "Phone Numbers", "Case Created"],
        )
        ids = {r[0] for r in data}
        self.assertIn(str(pending.client_id), ids)
        self.assertNotIn(str(served.client_id), ids)  # verified household excluded
        # Case Created column carries the governing case's date_opened.
        penny_row = next(r for r in data if r[0] == str(pending.client_id))
        self.assertEqual(penny_row[4], "2026-08-05")

    def test_date_range_filters_on_case_created(self):
        from datetime import datetime

        from .models import EnrollmentStage

        early, _, _ = self._household(
            "Early", "Bird", stage=EnrollmentStage.PENDING_VERIFICATION,
            case_created=timezone.make_aware(datetime(2026, 7, 1, 9, 0)),
        )
        late, _, _ = self._household(
            "Late", "Comer", stage=EnrollmentStage.PENDING_VERIFICATION,
            case_created=timezone.make_aware(datetime(2026, 8, 20, 9, 0)),
        )

        ids = {r[0] for r in self._rows(created_from="2026-08-01", created_to="2026-08-31")[1:]}
        self.assertIn(str(late.client_id), ids)
        self.assertNotIn(str(early.client_id), ids)


class SyncHouseholdOutOfOrbitEventGateTest(TestCase):
    """A member added via the household roster lands PENDING (the pre-kitchen
    initial state), NOT Out of Orbit -- out of orbit only means a kitchen can't
    fulfill the member and is decided by the meal rule at kitchen assignment. So
    adding a member never emits an Out of Orbit event, whether or not the
    household already has a kitchen; an informational note is left when it does."""

    def _setup(self, *, with_kitchen):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, Kitchen, KitchenStatus, MemberDietaryProfile,
            MemberStatus,
        )

        primary = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pat", last_name="Primary",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        kitchen = (
            Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
            if with_kitchen else None
        )
        enr = EnrollmentVerification.objects.create(
            client=primary, household=hh, kitchen=kitchen,
            stage=EnrollmentStage.KITCHEN_ASSIGNMENT if with_kitchen
            else EnrollmentStage.PENDING_VERIFICATION,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=primary, menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        dep = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dee", last_name="Pendent",
        )
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        return primary, dep, enr

    def test_no_out_of_orbit_event_before_kitchen(self):
        from .models import (
            MemberDietaryProfile, MemberStatus, Note, NoteSource, TimelineEvent,
            TimelineEventType,
        )
        from .serializers import sync_household_members

        primary, dep, enr = self._setup(with_kitchen=False)
        sync_household_members(primary, enrollment=enr)

        prof = MemberDietaryProfile.objects.get(enrollment=enr, client=dep)
        # No kitchen yet -> PENDING (pre-kitchen initial state); the meal rule
        # decides the real status at kitchen assignment. No event, no note.
        self.assertEqual(prof.status, MemberStatus.PENDING)
        self.assertFalse(
            TimelineEvent.objects.filter(
                client=dep, event_type=TimelineEventType.OUT_OF_ORBIT
            ).exists()
        )
        self.assertFalse(Note.objects.filter(client=dep, source=NoteSource.SYSTEM).exists())

    def test_added_member_is_pending_with_kitchen(self):
        # Adding a member to an already-served (kitchen-assigned) household still
        # lands them PENDING -- NOT Out of Orbit -- with an informational note
        # (they need a menu type before they can be activated). No OOB event.
        from .models import (
            MemberDietaryProfile, MemberStatus, Note, NoteSource, TimelineEvent,
            TimelineEventType,
        )
        from .serializers import sync_household_members

        primary, dep, enr = self._setup(with_kitchen=True)
        sync_household_members(primary, enrollment=enr)

        prof = MemberDietaryProfile.objects.get(enrollment=enr, client=dep)
        self.assertEqual(prof.status, MemberStatus.PENDING)
        self.assertFalse(
            TimelineEvent.objects.filter(
                client=dep, event_type=TimelineEventType.OUT_OF_ORBIT
            ).exists()
        )
        self.assertTrue(Note.objects.filter(client=dep, source=NoteSource.SYSTEM).exists())


class DedupePoDeliveryOrdersCommandTest(TestCase):
    """dedupe_po_delivery_orders cancels the extra LIVE delivery order(s) for a
    member duplicated on one PO, keeping one; leaves single/all-cancelled alone."""

    def test_apply_keeps_one_cancels_extras(self):
        from io import StringIO

        from django.core.management import call_command

        from .models import (
            Client, DeliveryOrder, DeliveryOrderStatus, PurchaseOrder,
            PurchaseOrderStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dup", last_name="Member",
        )
        other = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Solo", last_name="Member",
        )
        po = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)
        # Member duplicated: two live orders on the same PO.
        d1 = DeliveryOrder.objects.create(
            purchase_order=po, member=client, status=DeliveryOrderStatus.READY_FOR_DELIVERY,
        )
        d2 = DeliveryOrder.objects.create(
            purchase_order=po, member=client, status=DeliveryOrderStatus.READY_FOR_DELIVERY,
        )
        # A single (fine) order for another member.
        solo = DeliveryOrder.objects.create(
            purchase_order=po, member=other, status=DeliveryOrderStatus.PENDING,
        )

        call_command("dedupe_po_delivery_orders", "--apply", stdout=StringIO())

        live = DeliveryOrder.objects.filter(
            purchase_order=po, member=client,
        ).exclude(status=DeliveryOrderStatus.CANCELLED)
        self.assertEqual(live.count(), 1)  # kept exactly one
        solo.refresh_from_db()
        self.assertEqual(solo.status, DeliveryOrderStatus.PENDING)  # untouched


class DeliveryCalendarNoDuplicateTest(TestCase):
    """_dedupe_calendar_occurrences never lets the same client land on the same
    delivery date + product kind twice -- across enrollments AND within a batch.
    This is the upstream guard that stops duplicate PO lines."""

    def _profile(self, client, enrollment):
        from .models import MemberDietaryProfile, MemberStatus
        return MemberDietaryProfile.objects.create(
            enrollment=enrollment, client=client, status=MemberStatus.ACTIVE,
        )

    def test_dedupes_across_enrollments_and_within_batch(self):
        from datetime import date

        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, OrderSchedule,
            OrderStatus,
        )
        from .services.orders import _dedupe_calendar_occurrences

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dup", last_name="Cal",
        )
        prog = "Medically Tailored Meals"
        d = date(2026, 8, 6)

        # Enrollment A already has a LIVE order for this client on date d.
        enrA = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.SERVICE_ACTIVE, program_name=prog,
        )
        mpA = self._profile(client, enrA)
        OrderSchedule.objects.create(
            enrollment=enrA, member=mpA, program_name=prog,
            anticipated_delivery_date=d, status=OrderStatus.SCHEDULED,
        )

        # Enrollment B (same client, same program) tries to build the SAME date
        # plus a different date -> only the new date survives.
        enrB = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.SERVICE_ACTIVE, program_name=prog,
        )
        mpB = self._profile(client, enrB)
        other = date(2026, 8, 13)
        candidates = [
            OrderSchedule(enrollment=enrB, member=mpB, program_name=prog,
                          anticipated_delivery_date=d),           # dup of enrA -> drop
            OrderSchedule(enrollment=enrB, member=mpB, program_name=prog,
                          anticipated_delivery_date=other),       # new date -> keep
            OrderSchedule(enrollment=enrB, member=mpB, program_name=prog,
                          anticipated_delivery_date=other),       # intra-batch dup -> drop
        ]
        kept = _dedupe_calendar_occurrences(candidates)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].anticipated_delivery_date, other)

    def test_cancelled_existing_does_not_block(self):
        from datetime import date

        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, OrderSchedule,
            OrderStatus,
        )
        from .services.orders import _dedupe_calendar_occurrences

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Canc", last_name="Cal",
        )
        prog = "Medically Tailored Meals"
        d = date(2026, 8, 6)
        enr = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.SERVICE_ACTIVE, program_name=prog,
        )
        mp = self._profile(client, enr)
        OrderSchedule.objects.create(
            enrollment=enr, member=mp, program_name=prog,
            anticipated_delivery_date=d, status=OrderStatus.CANCELLED,
        )
        kept = _dedupe_calendar_occurrences([
            OrderSchedule(enrollment=enr, member=mp, program_name=prog,
                          anticipated_delivery_date=d),
        ])
        self.assertEqual(len(kept), 1)  # cancelled order doesn't block a new one


class DeliveryCalendarExcludesDeadEnrollmentsTest(TestCase):
    """The member delivery calendar reflects only LIVE enrollments: a closed
    (superseded) enrollment's occurrences -- even on a different kitchen -- must
    not show, so a member never reads as served by two kitchens. The
    ?enrollment=<id> override still surfaces a dead enrollment read-only."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent

        agent = Agent.objects.create(name="Cal", agent_code="967", group="Management")
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def test_dead_enrollment_kitchen_hidden_by_default(self):
        from datetime import date

        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, Kitchen, KitchenStatus, MemberDietaryProfile,
            MemberStatus, OrderSchedule, OrderStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Two", last_name="Kitchen",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        live_k = Kitchen.objects.create(name="LiveKitchenAAA", status=KitchenStatus.ACTIVE)
        dead_k = Kitchen.objects.create(name="DeadKitchenBBB", status=KitchenStatus.ACTIVE)
        d = date(2026, 8, 7)

        live = EnrollmentVerification.objects.create(
            client=client, household=hh, kitchen=live_k,
            stage=EnrollmentStage.SERVICE_ACTIVE, program_name="Meals",
        )
        live_mp = MemberDietaryProfile.objects.create(
            enrollment=live, client=client, status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=live, member=live_mp, kitchen=live_k, program_name="Meals",
            anticipated_delivery_date=d, status=OrderStatus.SCHEDULED, household=hh,
        )
        dead = EnrollmentVerification.objects.create(
            client=client, household=hh, kitchen=dead_k,
            stage=EnrollmentStage.CLOSED, program_name="Meals",
        )
        dead_mp = MemberDietaryProfile.objects.create(
            enrollment=dead, client=client, status=MemberStatus.ACTIVE,
        )
        OrderSchedule.objects.create(
            enrollment=dead, member=dead_mp, kitchen=dead_k, program_name="Meals",
            anticipated_delivery_date=d, status=OrderStatus.SCHEDULED, household=hh,
        )

        api = self._api()
        url = f"/api/portal/members/{client.client_id}/delivery-calendar/"
        body = api.get(url).content.decode()
        self.assertIn("LiveKitchenAAA", body)
        self.assertNotIn("DeadKitchenBBB", body)  # dead enrollment hidden

        # The override still surfaces the dead enrollment's own calendar.
        override = api.get(f"{url}?enrollment={dead.pk}").content.decode()
        self.assertIn("DeadKitchenBBB", override)


class ClosingEnrollmentClearsCalendarTest(TestCase):
    """Advancing an enrollment to a terminal stage (Closed/Cancelled) must stop
    its future deliveries -- so a superseded enrollment can't keep a live
    calendar (the root cause of a member showing two kitchens after a governing
    case replacement). Centralized in advance_enrollment, covering every close
    path."""

    def test_close_removes_future_scheduled_occurrences(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, MemberDietaryProfile,
            MemberStatus, OrderSchedule, OrderStatus,
        )
        from .services.lifecycle import advance_enrollment

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Close", last_name="Cal",
        )
        enr = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.SERVICE_ACTIVE, program_name="Meals",
        )
        mp = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, status=MemberStatus.ACTIVE,
        )
        future = timezone.localdate() + timedelta(days=7)
        OrderSchedule.objects.create(
            enrollment=enr, member=mp, program_name="Meals",
            anticipated_delivery_date=future, status=OrderStatus.SCHEDULED,
        )

        advance_enrollment(enr, EnrollmentStage.CLOSED, force=True)

        live_future = (
            OrderSchedule.objects.filter(enrollment=enr, anticipated_delivery_date=future)
            .exclude(status=OrderStatus.CANCELLED)
        )
        self.assertEqual(live_future.count(), 0)  # future delivery stopped on close


class ReportExportBackgroundTest(TestCase):
    """Background report export: generator output, the Celery task (storage
    mocked), and the start/poll endpoints incl. the no-S3 sync fallback."""

    def _api(self, group="Management", code="980"):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent

        agent = Agent.objects.create(name="Rep", agent_code=code, group=group)
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def test_all_members_rows_header_and_rows(self):
        from .portal.report_exports import all_members_rows

        Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="One")
        Client.objects.create(client_id=str(uuid.uuid4()), first_name="B", last_name="Two")
        gen = all_members_rows({})
        header = next(gen)
        self.assertIn("Member ID", header)
        rows = list(gen)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(r) == len(header) for r in rows))

    def test_task_completes_and_uploads(self):
        from unittest.mock import patch

        from .models import ReportExport, ReportExportStatus
        from .tasks import generate_report_export

        Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="One")
        exp = ReportExport.objects.create(report_key="all-members", params={}, filename="x.csv")
        with patch("api.services.import_storage.upload_fileobj") as up:
            generate_report_export(str(exp.export_id))
        exp.refresh_from_db()
        self.assertEqual(exp.status, ReportExportStatus.COMPLETED)
        self.assertTrue(exp.file_key.startswith("exports/"))
        self.assertEqual(exp.row_count, 1)
        up.assert_called_once()

    def test_task_unknown_report_fails(self):
        from .models import ReportExport, ReportExportStatus
        from .tasks import generate_report_export

        exp = ReportExport.objects.create(report_key="nope", params={})
        generate_report_export(str(exp.export_id))
        exp.refresh_from_db()
        self.assertEqual(exp.status, ReportExportStatus.FAILED)
        self.assertIn("nope", exp.error_log)

    def test_start_streams_without_s3(self):
        from unittest.mock import patch

        api = self._api(code="984")
        with patch("api.services.import_storage.s3_enabled", return_value=False):
            r = api.post("/api/portal/reports/exports/",
                         {"report_key": "all-members", "params": {}}, format="json")
        self.assertEqual(r.status_code, 200, r)
        self.assertEqual(r["Content-Type"], "text/csv")

    def test_start_creates_job_with_s3(self):
        from unittest.mock import patch

        from .models import ReportExport

        api = self._api(code="985")
        with patch("api.services.import_storage.s3_enabled", return_value=True), \
                patch("api.tasks.generate_report_export.delay") as delay:
            r = api.post("/api/portal/reports/exports/",
                         {"report_key": "all-members", "params": {}}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["status"], "pending")
        self.assertTrue(ReportExport.objects.filter(report_key="all-members").exists())
        delay.assert_called_once()

    def test_detail_returns_download_url_and_gate(self):
        from unittest.mock import patch

        from .models import ReportExport, ReportExportStatus

        exp = ReportExport.objects.create(
            report_key="all-members", filename="x.csv",
            status=ReportExportStatus.COMPLETED, file_key="exports/abc/x.csv",
        )
        api = self._api(code="986")
        with patch("api.services.import_storage.s3_enabled", return_value=True), \
                patch("api.services.import_storage.presign_get", return_value="https://signed/x"):
            r = api.get(f"/api/portal/reports/exports/{exp.export_id}/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["download_url"], "https://signed/x")

        cs = self._api(group="CS", code="987")
        self.assertEqual(
            cs.get(f"/api/portal/reports/exports/{exp.export_id}/").status_code, 403
        )


class PurgeOutOfScopeCasesCommandTest(TestCase):
    """purge_out_of_scope_cases deletes only cases the import would reject
    (out of program scope / not Met Council), keeps in-scope cases, and never
    deletes a case backing a verification enrollment."""

    def test_purges_out_of_scope_keeps_in_scope_and_enrollment_backed(self):
        from io import StringIO

        from django.core.management import call_command

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification,
        )

        MET = "Met Council - SCN - PHS"
        client = Client.objects.create(client_id=str(uuid.uuid4()), first_name="C", last_name="L")

        def mkcase(case_type, service_type="", program_name=""):
            return Case.objects.create(
                case_id=str(uuid.uuid4()), client=client, case_type=case_type,
                provider_name=MET, service_type=service_type,
                program_name=program_name, case_status=CaseStatus.OPEN,
            )

        keeper = mkcase(CaseType.INTERNAL_SERVICE, service_type="medically tailored meals")
        doomed = mkcase(CaseType.EXTERNAL_SERVICE, program_name="Furniture / Home Goods")
        backed = mkcase(CaseType.EXTERNAL_SERVICE, program_name="Housing Case Management")
        EnrollmentVerification.objects.create(
            client=client, case=backed, stage=EnrollmentStage.SERVICE_ACTIVE,
        )

        call_command("purge_out_of_scope_cases", "--apply", stdout=StringIO())

        self.assertTrue(Case.objects.filter(pk=keeper.pk).exists())   # in scope
        self.assertFalse(Case.objects.filter(pk=doomed.pk).exists())  # out of scope
        self.assertTrue(Case.objects.filter(pk=backed.pk).exists())   # enrollment-backed, preserved


class KitchenExportFilenameTest(TestCase):
    """The kitchen export CSV filename: Order#_(Meals|Boxes)_MM.DD.YY_PHS_ABBR.csv
    Order# = stable K-code + PO sequence suffix; abbreviation at the end. Only
    the exported file is renamed -- po.po_number is untouched."""

    def test_meals_and_box_filenames(self):
        from datetime import date

        from .models import (
            Kitchen, KitchenStatus, ProductTypeKind, PurchaseOrder,
            PurchaseOrderStatus,
        )
        from .services.purchase_orders import kitchen_export_filename

        eng = Kitchen.objects.create(name="Englewood", abbreviation="ENG", status=KitchenStatus.ACTIVE)
        ast = Kitchen.objects.create(name="Astoria", abbreviation="AST", status=KitchenStatus.ACTIVE)

        meals = PurchaseOrder.objects.create(
            po_number="PO-MEALS-2026-W30-FRI-ENG-2", kind=ProductTypeKind.MEALS,
            delivery_date=date(2026, 7, 24), kitchen=eng, status=PurchaseOrderStatus.DRAFT,
        )
        box = PurchaseOrder.objects.create(
            po_number="PO-BOX-2026-W30-AST-3", kind=ProductTypeKind.BOXES,
            delivery_date=date(2026, 7, 24), kitchen=ast, status=PurchaseOrderStatus.DRAFT,
        )

        self.assertEqual(kitchen_export_filename(meals), "K01-2_Meals_07.24.26_PHS_ENG.csv")
        self.assertEqual(kitchen_export_filename(box), "K02-3_Boxes_07.24.26_PHS_AST.csv")
        # PO number itself is unchanged.
        self.assertEqual(meals.po_number, "PO-MEALS-2026-W30-FRI-ENG-2")

    def test_no_suffix_and_missing_abbreviation(self):
        from datetime import date

        from .models import (
            Kitchen, KitchenStatus, ProductTypeKind, PurchaseOrder,
            PurchaseOrderStatus,
        )
        from .services.purchase_orders import kitchen_export_filename

        k = Kitchen.objects.create(name="NoAbbr", status=KitchenStatus.ACTIVE)  # no abbreviation
        po = PurchaseOrder.objects.create(
            po_number="PO-MEALS-2026-W30-THU-K01", kind=ProductTypeKind.MEALS,
            delivery_date=date(2026, 7, 23), kitchen=k, status=PurchaseOrderStatus.DRAFT,
        )
        # No suffix -> just the K-code; missing abbreviation -> falls back to K-code.
        self.assertEqual(kitchen_export_filename(po), "K01_Meals_07.23.26_PHS_K01.csv")


class TicketHouseholdPrimaryIdSerializerTest(TestCase):
    """PortalTicketSerializer exposes household_primary_id (the ticket client's
    household primary) so the Work Queue can show the 'open household' icon --
    same as the Members list."""

    def test_household_primary_id(self):
        from .models import Client, Household, HouseholdMember, Ticket, TicketType
        from .portal import serializers as s

        prim = Client.objects.create(client_id=str(uuid.uuid4()), first_name="P", last_name="R")
        dep = Client.objects.create(client_id=str(uuid.uuid4()), first_name="D", last_name="P")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=prim, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        tt, _ = TicketType.objects.get_or_create(code="coverage", defaults={"label": "Coverage"})

        t = Ticket.objects.create(type=tt, client=dep, reason="x")
        self.assertEqual(
            s.PortalTicketSerializer(t).data["household_primary_id"], str(prim.client_id)
        )

        lone = Client.objects.create(client_id=str(uuid.uuid4()), first_name="L", last_name="N")
        tl = Ticket.objects.create(type=tt, client=lone, reason="y")
        self.assertIsNone(s.PortalTicketSerializer(tl).data["household_primary_id"])


class MembersVerifiedByFilterTest(TestCase):
    """/verifiers/ lists Verifier-group agents; the members list ?verified_by=
    filter keeps only members verified by that agent."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent
        a = Agent.objects.create(name="Mgr", agent_code="900", group="Management")
        acc = AccessToken()
        acc["agent_id"] = str(a.id); acc["agent_code"] = a.agent_code
        acc["agent_name"] = a.name; acc["agent_group"] = a.group
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION=f"Bearer {acc}")
        return api

    def test_verifiers_endpoint_and_filter(self):
        from .models import (
            Agent, Client, EnrollmentStage, EnrollmentVerification,
        )

        verifier = Agent.objects.create(name="Vera Verifier", group="Verifiers", status="Active")
        Agent.objects.create(name="Cassie CS", group="CS", status="Active")
        api = self._api()

        # /verifiers/ returns only the Verifier-group agent.
        vs = api.get("/api/portal/verifiers/").json()
        labels = {v["label"] for v in vs}
        self.assertIn("Vera Verifier", labels)
        self.assertNotIn("Cassie CS", labels)

        from datetime import timedelta

        from django.utils import timezone

        other_agent = Agent.objects.create(name="Bob Other", group="Verifiers", status="Active")
        now = timezone.now()

        verified = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Ver", last_name="Ified")
        other = Client.objects.create(client_id=str(uuid.uuid4()), first_name="No", last_name="One")
        EnrollmentVerification.objects.create(
            client=verified, stage=EnrollmentStage.VERIFIED, verified_by=verifier,
            opened_at=now,
        )

        # Real-data pattern: the enrollment our agent VERIFIED is now closed, and a
        # duplicate/unverified enrollment is the live (governing) one. The member
        # must STILL match our agent -- the verification fact survives the close.
        shadowed = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Sha", last_name="Dowed")
        EnrollmentVerification.objects.create(
            client=shadowed, stage=EnrollmentStage.SERVICE_ACTIVE, verified_by=None,
            opened_at=now - timedelta(minutes=5),  # older, but OPEN -> governing
        )
        EnrollmentVerification.objects.create(
            client=shadowed, stage=EnrollmentStage.CLOSED, verified_by=verifier,
            opened_at=now, closed_at=now + timedelta(days=1),
        )

        # A member whose OWN verification was done by the OTHER agent must not
        # match our agent (proves we don't leak via household / other members).
        bob_client = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Bo", last_name="Bby")
        EnrollmentVerification.objects.create(
            client=bob_client, stage=EnrollmentStage.VERIFIED, verified_by=other_agent,
            opened_at=now,
        )

        def ids(resp):
            out = set()
            for g in resp.json()["results"]:
                out.add(g["primary"]["id"])
                out.update(m["id"] for m in g.get("members", []))
            return out

        got = ids(api.get(f"/api/portal/members/?verified_by={verifier.id}"))
        self.assertIn(str(verified.client_id), got)
        self.assertIn(str(shadowed.client_id), got)   # verified enrollment closed, still matches
        self.assertNotIn(str(other.client_id), got)
        self.assertNotIn(str(bob_client.client_id), got)


class CarriedVerificationTest(TestCase):
    """Verification carries forward when a governing-case replacement reuses a
    pre-existing enrollment, and the backfill fixes historical rows."""

    def test_backfill_copies_from_superseded_and_skips_verified(self):
        from io import StringIO

        from django.core.management import call_command
        from django.utils import timezone

        from .models import Agent, Client, EnrollmentStage, EnrollmentVerification

        a = Agent.objects.create(name="Vera", group="Verifiers", status="Active")
        b = Agent.objects.create(name="Bob", group="Verifiers", status="Active")
        now = timezone.now()

        # Live enrollment missing verification, superseding a verified closed one.
        c1 = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="A")
        closed1 = EnrollmentVerification.objects.create(
            client=c1, stage=EnrollmentStage.CLOSED, verified_by=a, verified_at=now,
            closed_at=now,
        )
        live1 = EnrollmentVerification.objects.create(
            client=c1, stage=EnrollmentStage.SERVICE_ACTIVE, supersedes=closed1,
        )

        # Live enrollment ALREADY verified (by Bob) -> must NOT be overwritten.
        c2 = Client.objects.create(client_id=str(uuid.uuid4()), first_name="B", last_name="B")
        closed2 = EnrollmentVerification.objects.create(
            client=c2, stage=EnrollmentStage.CLOSED, verified_by=a, verified_at=now,
            closed_at=now,
        )
        live2 = EnrollmentVerification.objects.create(
            client=c2, stage=EnrollmentStage.SERVICE_ACTIVE, supersedes=closed2,
            verified_by=b, verified_at=now,
        )

        call_command("backfill_carried_verification", "--apply", stdout=StringIO())

        live1.refresh_from_db(); live2.refresh_from_db()
        self.assertEqual(live1.verified_by_id, a.id)   # carried from superseded
        self.assertIsNotNone(live1.verified_at)
        self.assertEqual(live2.verified_by_id, b.id)   # own verification preserved

    def test_dietary_carry_forward(self):
        from io import StringIO

        from django.core.management import call_command
        from django.utils import timezone

        from .models import (
            Agent, Client, EnrollmentStage, EnrollmentVerification,
            MemberDietaryProfile,
        )

        a = Agent.objects.create(name="Vera", group="Verifiers", status="Active")
        now = timezone.now()
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="A")
        closed = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.CLOSED, verified_by=a, verified_at=now, closed_at=now,
        )
        MemberDietaryProfile.objects.create(
            enrollment=closed, client=c, member_name="A A", menu_type="Vegetarian",
            food_allergies=["peanuts"],
        )
        live = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.SERVICE_ACTIVE, supersedes=closed,
        )
        # Survivor's placeholder profile: blank menu (the gap).
        tp = MemberDietaryProfile.objects.create(
            enrollment=live, client=c, member_name="A A", menu_type="",
        )

        call_command("backfill_carried_verification", "--apply", stdout=StringIO())

        tp.refresh_from_db()
        self.assertEqual(tp.menu_type, "Vegetarian")     # carried from superseded
        self.assertEqual(tp.food_allergies, ["peanuts"])

    def test_delivery_address_carries_even_when_already_verified(self):
        # Regression: a survivor that already carried the verified FLAG but not the
        # delivery-address FK reads as "verified but no delivery address". The
        # address must carry regardless of verification state.
        from io import StringIO

        from django.core.management import call_command
        from django.utils import timezone

        from .models import (
            Address, Agent, Client, EnrollmentStage, EnrollmentVerification,
        )

        a = Agent.objects.create(name="Vera", group="Verifiers", status="Active")
        now = timezone.now()
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Tah", last_name="Ji")
        addr = Address.objects.create(
            client=c, type="current", street="69 Gatchell St", city="Buffalo",
            state="NY", zip="14212",
        )
        closed = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.CLOSED, verified_by=a, verified_at=now,
            closed_at=now, delivery_address=addr, delivery_address_verified=True,
        )
        # Survivor: already verified, but NO delivery address (the bug).
        live = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.SERVICE_ACTIVE, supersedes=closed,
            verified_by=a, verified_at=now, delivery_address=None,
        )

        call_command("backfill_carried_verification", "--apply", stdout=StringIO())

        live.refresh_from_db()
        self.assertEqual(live.delivery_address_id, addr.pk)


class CareManagementRescanTest(TestCase):
    """The Care Management list re-scans flagged households on load, so an issue
    an agent just fixed clears from the queue immediately (not only after the
    nightly sweep)."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent
        a = Agent.objects.create(name="CS", agent_code="700", group="CS")
        acc = AccessToken()
        acc["agent_id"] = str(a.id); acc["agent_code"] = a.agent_code
        acc["agent_name"] = a.name; acc["agent_group"] = a.group
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION=f"Bearer {acc}")
        return api

    def test_fixed_warning_clears_on_list_load(self):
        from .models import (
            EnrollmentStage, EnrollmentVerification, Household, HouseholdMember,
            Kitchen, KitchenProductType, KitchenStatus, MemberDietaryProfile,
            MemberWarning, WarningStatus,
        )
        from .services.warnings import NO_KITCHEN, sync_household_warnings

        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Woe", last_name="W")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        MemberDietaryProfile.objects.create(enrollment=enr, client=c)
        sync_household_warnings(enr)  # -> NO_KITCHEN active
        self.assertEqual(
            MemberWarning.objects.get(client=c, code=NO_KITCHEN).status, WarningStatus.ACTIVE
        )

        api = self._api()
        r1 = api.get("/api/portal/care-management/").json()
        self.assertIn("no_kitchen", r1["summary"]["by_code"])

        # Fix the issue -- assign a kitchen -- WITHOUT manually re-syncing.
        enr.kitchen = Kitchen.objects.create(
            name="K", status=KitchenStatus.ACTIVE,
            supported_products=[KitchenProductType.MEAL],
        )
        enr.save(update_fields=["kitchen"])

        # Next list load must re-scan and drop the fixed warning.
        r2 = api.get("/api/portal/care-management/").json()
        self.assertNotIn("no_kitchen", r2["summary"]["by_code"])
        self.assertEqual(
            MemberWarning.objects.get(client=c, code=NO_KITCHEN).status, WarningStatus.RESOLVED
        )


class MemberSearchExpandedTest(TestCase):
    """The Members list omni-search matches phone, email, member address, and the
    TRUE (enrollment) delivery address -- not just name / IDs."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent
        a = Agent.objects.create(name="Mgr", agent_code="905", group="Management")
        acc = AccessToken()
        acc["agent_id"] = str(a.id); acc["agent_code"] = a.agent_code
        acc["agent_name"] = a.name; acc["agent_group"] = a.group
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION=f"Bearer {acc}")
        return api

    def _ids(self, resp):
        out = set()
        for g in resp.json()["results"]:
            out.add(g["primary"]["id"])
            out.update(m["id"] for m in g.get("members", []))
        return out

    def test_search_by_phone_email_and_delivery_address(self):
        from .models import (
            Address, ClientPhone, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember,
        )

        api = self._api()
        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Sam", last_name="Search",
            client_email_address="sam.search@example.com",
        )
        ClientPhone.objects.create(client=c, raw="(716) 555-0142", normalized="7165550142")
        # True delivery address on the enrollment (not the profile).
        addr = Address.objects.create(
            client=c, type="delivery", street="69 Gatchell St", city="Buffalo",
            state="NY", zip="14212",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            delivery_address=addr,
        )

        cid = str(c.client_id)
        # phone (formatted query -> digits)
        self.assertIn(cid, self._ids(api.get("/api/portal/members/?search=716-555-0142")))
        # email
        self.assertIn(cid, self._ids(api.get("/api/portal/members/?search=sam.search@example.com")))
        # delivery address street + zip
        self.assertIn(cid, self._ids(api.get("/api/portal/members/?search=Gatchell")))
        self.assertIn(cid, self._ids(api.get("/api/portal/members/?search=14212")))
        # a non-matching query does not return them
        self.assertNotIn(cid, self._ids(api.get("/api/portal/members/?search=Nonexistent Zzz")))

    def test_search_by_migrated_from_old_id(self):
        # A merged client is findable by the OLD Unite Us id it was migrated from
        # (full or partial), not just its current client_id.
        api = self._api()
        old_id = str(uuid.uuid4())
        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Mig", last_name="Rated",
            migrated_from_id=old_id,
        )
        cid = str(c.client_id)
        self.assertIn(cid, self._ids(api.get(f"/api/portal/members/?search={old_id}")))
        self.assertIn(cid, self._ids(api.get(f"/api/portal/members/?search={old_id[:8]}")))
        # an unrelated old id doesn't match
        self.assertNotIn(cid, self._ids(api.get(f"/api/portal/members/?search={uuid.uuid4()}")))


class NoteAuthorFallbackTest(TestCase):
    """A note with no recorded author shows its SOURCE label, not a blank the UI
    renders as 'Unknown'."""

    def test_blank_author_falls_back_to_source_label(self):
        from .models import Client, Note, NoteSource
        from .portal.serializers import PortalNoteSerializer

        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="B")
        cases = {
            NoteSource.SYSTEM: "System",
            NoteSource.UNITE_US: "Unite Us",
            NoteSource.GHL: "GoHighLevel",
        }
        for src, label in cases.items():
            n = Note.objects.create(client=c, source=src, author_name="", body="x")
            self.assertEqual(PortalNoteSerializer(n).data["author_name"], label)
        # A real author is preserved.
        n = Note.objects.create(client=c, source=NoteSource.UNITE_US, author_name="Jane Doe", body="y")
        self.assertEqual(PortalNoteSerializer(n).data["author_name"], "Jane Doe")


class AttributeSystemNotesCommandTest(TestCase):
    """attribute_system_notes stamps a blank-author SYSTEM note with the agent
    inferred from a co-timed timeline event, and leaves non-agent (import/
    system) ones as System."""

    def test_infers_agent_and_leaves_system(self):
        from io import StringIO

        from django.core.management import call_command

        from .models import Agent, Client, Note, NoteSource, TimelineEvent

        agent = Agent.objects.create(name="Javier Almenar", agent_code="614", group="Verifiers")

        # Agent-triggered: co-timed event with an agent actor -> attributed.
        c1 = Client.objects.create(client_id=str(uuid.uuid4()), first_name="L", last_name="O")
        n1 = Note.objects.create(client=c1, source=NoteSource.SYSTEM, author_name="", body="x")
        from django.utils import timezone as _tz
        TimelineEvent.objects.create(client=c1, event_type="verification_requested", actor="agent:614", source="system", occurred_at=_tz.now())

        # Import-triggered: only a system actor nearby -> stays System (blank).
        c2 = Client.objects.create(client_id=str(uuid.uuid4()), first_name="I", last_name="M")
        n2 = Note.objects.create(client=c2, source=NoteSource.SYSTEM, author_name="", body="y")
        TimelineEvent.objects.create(client=c2, event_type="case_opened", actor="system:csv-import", source="import", occurred_at=_tz.now())

        call_command("attribute_system_notes", "--apply", stdout=StringIO())

        n1.refresh_from_db(); n2.refresh_from_db()
        self.assertEqual(n1.author_name, "Javier Almenar")   # inferred agent
        self.assertEqual(n2.author_name, "")                 # non-agent -> stays System


class FixCaselessServingEnrollmentsTest(TestCase):
    """Binds the governing case back onto a caseless serving enrollment: frees a
    stray holder (SPLIT), binds directly (UNBOUND), and skips two-serving."""

    def _case(self, client):
        from .models import Case, CaseStatus, CaseType
        return Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
        )

    def test_split_unbound_and_ambiguous(self):
        from io import StringIO

        from django.core.management import call_command

        from .models import Client, EnrollmentStage, EnrollmentVerification

        # SPLIT: serving caseless + pending holds the case.
        c1 = Client.objects.create(client_id=str(uuid.uuid4()), first_name="S", last_name="P")
        case1 = self._case(c1)
        serving1 = EnrollmentVerification.objects.create(client=c1, stage=EnrollmentStage.SERVICE_ACTIVE, case=None)
        stray1 = EnrollmentVerification.objects.create(client=c1, stage=EnrollmentStage.PENDING_VERIFICATION, case=case1)

        # UNBOUND: serving caseless + open case on no enrollment.
        c2 = Client.objects.create(client_id=str(uuid.uuid4()), first_name="U", last_name="B")
        case2 = self._case(c2)
        serving2 = EnrollmentVerification.objects.create(client=c2, stage=EnrollmentStage.SERVICE_ACTIVE, case=None)

        # AMBIGUOUS: two serving enrollments, one holds the case -> skip.
        c3 = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="M")
        case3 = self._case(c3)
        servingA = EnrollmentVerification.objects.create(client=c3, stage=EnrollmentStage.SERVICE_ACTIVE, case=None)
        servingB = EnrollmentVerification.objects.create(client=c3, stage=EnrollmentStage.ON_HOLD, case=case3)

        call_command("fix_caseless_serving_enrollments", "--apply", stdout=StringIO())

        serving1.refresh_from_db(); stray1.refresh_from_db()
        serving2.refresh_from_db()
        servingA.refresh_from_db(); servingB.refresh_from_db()

        self.assertEqual(serving1.case_id, case1.case_id)               # bound
        self.assertEqual(stray1.stage, EnrollmentStage.DISREGARDED)     # freed
        self.assertIsNone(stray1.case_id)
        self.assertEqual(serving2.case_id, case2.case_id)               # bound
        self.assertIsNone(servingA.case_id)                            # ambiguous -> skipped
        self.assertEqual(servingB.case_id, case3.case_id)              # untouched


class ReconcileBindsCaselessServingTest(TestCase):
    """Root-cause fix: reconcile_internal_service_authorization binds the
    governing case onto a caseless serving enrollment (freeing a stray), so the
    split can't persist/recur."""

    def test_reconcile_binds_and_frees_stray(self):
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import reconcile_internal_service_authorization

        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="S", last_name="P")
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
        )
        serving = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.SERVICE_ACTIVE, case=None,
        )
        stray = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.PENDING_VERIFICATION, case=case,
        )

        reconcile_internal_service_authorization(c)

        serving.refresh_from_db(); stray.refresh_from_db()
        self.assertEqual(serving.case_id, case.case_id)              # bound
        self.assertEqual(stray.stage, EnrollmentStage.DISREGARDED)   # freed
        self.assertIsNone(stray.case_id)


class FixCaselessTwoServingCompetingTest(TestCase):
    """Two caseless serving enrollments competing for one case are SKIPPED (not
    attempted then failed on the unique constraint)."""

    def test_competing_serving_skipped(self):
        from io import StringIO

        from django.core.management import call_command

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification,
        )

        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="T", last_name="S")
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
        )
        a = EnrollmentVerification.objects.create(client=c, stage=EnrollmentStage.SERVICE_ACTIVE, case=None)
        b = EnrollmentVerification.objects.create(client=c, stage=EnrollmentStage.ON_HOLD, case=None)

        # Must not raise / must leave both caseless (ambiguous -> skipped).
        call_command("fix_caseless_serving_enrollments", "--apply", "--client", str(c.client_id), stdout=StringIO())

        a.refresh_from_db(); b.refresh_from_db()
        self.assertIsNone(a.case_id)
        self.assertIsNone(b.case_id)


class CaselessCrossClientHolderTest(TestCase):
    """When the governing case is held by a DIFFERENT client's enrollment, both
    the command and the reconcile skip it (no bind, no IntegrityError)."""

    def _setup(self):
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification,
        )
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Serv", last_name="Ing")
        other = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Oth", last_name="Er")
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
        )
        serv = EnrollmentVerification.objects.create(client=c, stage=EnrollmentStage.SERVICE_ACTIVE, case=None)
        # A DIFFERENT client's pending enrollment holds c's case (cross-client mislink).
        stray = EnrollmentVerification.objects.create(client=other, stage=EnrollmentStage.PENDING_VERIFICATION, case=case)
        return c, serv, stray

    def test_command_skips_cross_client(self):
        from io import StringIO

        from django.core.management import call_command

        c, serv, stray = self._setup()
        call_command("fix_caseless_serving_enrollments", "--apply", "--client", str(c.client_id), stdout=StringIO())
        serv.refresh_from_db(); stray.refresh_from_db()
        self.assertIsNone(serv.case_id)                      # not bound
        self.assertEqual(stray.stage, "pending_verification")  # untouched

    def test_reconcile_skips_cross_client_without_error(self):
        from .services.lifecycle import reconcile_internal_service_authorization

        c, serv, stray = self._setup()
        reconcile_internal_service_authorization(c)  # must not raise
        serv.refresh_from_db(); stray.refresh_from_db()
        self.assertIsNone(serv.case_id)
        self.assertEqual(stray.stage, "pending_verification")


class VerificationVerifiedByDateRangeTest(TestCase):
    """The 'verified by' filter binds to the SAME enrollment as the verification
    date window (so it means 'verifications this agent completed in the window',
    not a member who merely has some other enrollment in the window)."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent
        a = Agent.objects.create(name="Mgr", agent_code="909", group="Management")
        acc = AccessToken()
        acc["agent_id"] = str(a.id); acc["agent_code"] = a.agent_code
        acc["agent_name"] = a.name; acc["agent_group"] = a.group
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION=f"Bearer {acc}")
        return api

    def _ids(self, resp):
        out = set()
        for g in resp.json()["results"]:
            out.add(g["primary"]["id"])
            out.update(m["id"] for m in g.get("members", []))
        return out

    def test_verified_by_binds_to_same_enrollment(self):
        import datetime

        from .models import (
            Agent, Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
        )

        X = Agent.objects.create(name="Xavier V", agent_code="800", group="Verifiers", status="Active")
        Y = Agent.objects.create(name="Yolanda V", agent_code="801", group="Verifiers", status="Active")
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Vee", last_name="Bee")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        # scope=verification requires the primary to hold an internal-service case.
        Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
        )
        utc = datetime.timezone.utc
        # X verified this member in JANUARY.
        EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.VERIFIED, verified_by=X,
            opened_at=datetime.datetime(2026, 1, 5, tzinfo=utc),
            verified_at=datetime.datetime(2026, 1, 10, tzinfo=utc),
        )
        api = self._api()
        cid = str(c.client_id)

        base = f"/api/portal/members/?scope=verification&verified_by={X.id}"
        self.assertIn(cid, self._ids(api.get(base)))                                   # X's member
        self.assertIn(cid, self._ids(api.get(base + "&completed_from=2026-01-01&completed_to=2026-01-31")))
        self.assertNotIn(cid, self._ids(api.get(base + "&completed_from=2026-02-01&completed_to=2026-02-28")))

        # A LATER enrollment verified by SOMEONE ELSE in February must NOT make the
        # member match "verified_by=X within February" (same-enrollment binding).
        EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.VERIFIED, verified_by=Y,
            opened_at=datetime.datetime(2026, 2, 10, tzinfo=utc),
            verified_at=datetime.datetime(2026, 2, 15, tzinfo=utc),
        )
        self.assertNotIn(cid, self._ids(api.get(base + "&completed_from=2026-02-01&completed_to=2026-02-28")))
        self.assertIn(cid, self._ids(api.get(base + "&completed_from=2026-01-01&completed_to=2026-01-31")))


class LogisticsQueueExclusionsTest(TestCase):
    """The kitchen-assignment (Logistics) queue excludes Not Eligible clients and
    any household that was never verified (verified_at IS NULL)."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent
        a = Agent.objects.create(name="Log", agent_code="915", group="Logistics")
        acc = AccessToken()
        acc["agent_id"] = str(a.id); acc["agent_code"] = a.agent_code
        acc["agent_name"] = a.name; acc["agent_group"] = a.group
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION=f"Bearer {acc}")
        return api

    def _ids(self, resp):
        out = set()
        for g in resp.json()["results"]:
            out.add(g["primary"]["id"])
            out.update(m["id"] for m in g.get("members", []))
        return out

    def _member(self, *, verified, lifecycle="kitchen_assignment"):
        from django.utils import timezone as tz

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
        )

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Kay", last_name="Ann",
            lifecycle_stage=lifecycle,
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
        )
        EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
            verified_at=(tz.now() if verified else None),
        )
        return c

    def test_queue_excludes_unverified_and_ineligible(self):
        from .models import ClientStage

        verified = self._member(verified=True)
        unverified = self._member(verified=False)
        ineligible = self._member(verified=True, lifecycle=ClientStage.INELIGIBLE)
        ids = self._ids(self._api().get("/api/portal/members/?scope=logistics"))
        self.assertIn(str(verified.client_id), ids)
        self.assertNotIn(str(unverified.client_id), ids)
        self.assertNotIn(str(ineligible.client_id), ids)


class GoverningCaseExcludesUnauthorizedTest(TestCase):
    """A blank or never_requested internal-service case can never be a governing
    case (dashboard governing_internal_case_ids)."""

    def _case(self, auth):
        from .models import Case, CaseStatus, CaseType, Client
        c = Client.objects.create(client_id=uuid.uuid4(), first_name="G", last_name="C")
        return c, Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, service_authorization_status=auth,
            program_name="Medically Tailored Meals",
        )

    def test_blank_and_never_requested_never_govern(self):
        from .models import ServiceAuthorizationStatus
        from .portal.views_dashboard import governing_internal_case_ids

        _, approved = self._case(ServiceAuthorizationStatus.APPROVED)
        _, denied = self._case(ServiceAuthorizationStatus.DENIED)
        _, blank = self._case("")
        _, never = self._case(ServiceAuthorizationStatus.NEVER_REQUESTED)

        gov = governing_internal_case_ids()
        self.assertIn(approved.case_id, gov)
        self.assertIn(denied.case_id, gov)          # denied still governs
        self.assertNotIn(blank.case_id, gov)        # blank never governs
        self.assertNotIn(never.case_id, gov)        # never_requested never governs


class CSMemberStatusTest(TestCase):
    """cs_member_status classifies each member into exactly ONE tier, priority
    order (first match wins, no double-counting)."""

    def _m(self, *, status, stage, auth, kitchen=False, weekdays=None,
           verified=False, menu="Standard", medicaid=False, social=False):
        from django.utils import timezone
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, Insurance,
            InsurancePlanType, Kitchen, KitchenStatus, MemberDietaryProfile,
            RecordStatus, SocialCareCoverage, SocialCareCoverageStatus,
        )
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="M", last_name="S")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, service_authorization_status=auth,
            program_name="Medically Tailored Meals",
        )
        k = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE) if kitchen else None
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, case=case, stage=stage, kitchen=k,
            delivery_weekdays=weekdays or [],
            verified_at=timezone.now() if verified else None,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=c, menu_type=menu, status=status,
        )
        if medicaid:
            Insurance.objects.create(client=c, plan_type=InsurancePlanType.MEDICAID, status=RecordStatus.ACTIVE)
        if social:
            SocialCareCoverage.objects.create(client=c, status=SocialCareCoverageStatus.ENROLLED)
        return c

    def test_priority_classification(self):
        from .models import (
            EnrollmentStage, MemberStatus, ServiceAuthorizationStatus as SAS,
        )
        from .portal.views_dashboard import cs_member_status

        self._m(status=MemberStatus.ACTIVE, stage=EnrollmentStage.SERVICE_ACTIVE,
                auth=SAS.APPROVED, kitchen=True, weekdays=["mon"], verified=True,
                medicaid=True, social=True)                                  # receiving
        self._m(status=MemberStatus.ACTIVE, stage=EnrollmentStage.VERIFIED,
                auth=SAS.PENDING, verified=True)                             # pending_meals
        self._m(status=MemberStatus.OUT_OF_RANGE, stage=EnrollmentStage.SERVICE_ACTIVE,
                auth=SAS.APPROVED)                                           # out_of_range
        self._m(status=MemberStatus.ACTIVE, stage=EnrollmentStage.ON_HOLD,
                auth=SAS.DENIED)                                            # rejected (beats paused)
        self._m(status=MemberStatus.PAUSED, stage=EnrollmentStage.SERVICE_ACTIVE,
                auth=SAS.APPROVED, medicaid=True, social=True)               # services_paused
        self._m(status=MemberStatus.ACTIVE, stage=EnrollmentStage.SERVICE_ACTIVE,
                auth=SAS.APPROVED, verified=True)                            # no_coverage
        self._m(status=MemberStatus.ACTIVE, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
                auth=SAS.APPROVED, verified=False, medicaid=True, social=True)  # requires_verification
        self._m(status=MemberStatus.ACTIVE, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
                auth=SAS.APPROVED, verified=True, menu="", medicaid=True, social=True)  # no_menu

        tier_clients, _tags = cs_member_status(None, None)
        ms = {t: len(s) for t, s in tier_clients.items()}
        self.assertEqual(ms, {
            "receiving_meals": 1, "pending_meals": 1, "out_of_range": 1,
            "rejected": 1, "services_paused": 1, "no_coverage": 1,
            "requires_verification": 1, "no_menu": 1,
        })


class NutritionistGateTest(TestCase):
    """The Nutritionist sign-off gate sits between Verified and Kitchen
    Assignment: a verified household can't advance to kitchen (and thus service)
    until a Nutritionist approves, regardless of the case authorization."""

    def _enr(self, auth):
        from django.utils import timezone
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
        )
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Nut", last_name="Ri")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, service_authorization_status=auth,
            program_name="Medically Tailored Meals",
        )
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, case=case, stage=EnrollmentStage.VERIFIED,
            verified_at=timezone.now(),
        )
        return c, enr, case

    def test_gate_blocks_advance_until_approved(self):
        from .models import EnrollmentStage, ProgramStatus, ServiceAuthorizationStatus
        from .services.lifecycle import program_status, reconcile_enrollment_authorization

        _, enr, _ = self._enr(ServiceAuthorizationStatus.APPROVED)
        reconcile_enrollment_authorization(enr)
        enr.refresh_from_db()
        # Approved auth alone does NOT advance -- nutritionist gate not satisfied.
        self.assertEqual(enr.stage, EnrollmentStage.VERIFIED)
        self.assertEqual(program_status(enr), ProgramStatus.PENDING_NUTRITIONIST)

    def test_approve_advances_when_authorized(self):
        from .models import Agent, EnrollmentStage, ServiceAuthorizationStatus
        from .services.lifecycle import nutritionist_approve

        agent = Agent.objects.create(name="Jane RD", agent_code="700", group="Nutritionist")
        _, enr, _ = self._enr(ServiceAuthorizationStatus.APPROVED)
        nutritionist_approve(enr, agent=agent, signature="Jane RD")
        enr.refresh_from_db()
        self.assertIsNotNone(enr.nutritionist_approved_at)
        self.assertEqual(enr.nutritionist_signature, "Jane RD")
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)

    def test_approve_waits_when_auth_pending(self):
        from .models import Agent, EnrollmentStage, ProgramStatus, ServiceAuthorizationStatus
        from .services.lifecycle import nutritionist_approve, program_status

        agent = Agent.objects.create(name="Jane RD", agent_code="701", group="Nutritionist")
        _, enr, _ = self._enr(ServiceAuthorizationStatus.PENDING)
        nutritionist_approve(enr, agent=agent, signature="Jane RD")
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.VERIFIED)
        self.assertEqual(program_status(enr), ProgramStatus.NUTRITIONIST_APPROVED)

    def _api(self, group):
        from .models import Agent
        agent = Agent.objects.create(name="A", agent_code=str(uuid.uuid4())[:8], group=group)
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def test_approve_endpoint_role_and_signature(self):
        from .models import EnrollmentStage, ServiceAuthorizationStatus

        c, enr, _ = self._enr(ServiceAuthorizationStatus.APPROVED)
        url = f"/api/portal/members/{c.client_id}/nutritionist-approve/"
        # Non-nutritionist is forbidden.
        self.assertEqual(self._api("CS").post(url, {"signature": "X"}, format="json").status_code, 403)
        # Nutritionist without a signature -> 400.
        self.assertEqual(self._api("Nutritionist").post(url, {}, format="json").status_code, 400)
        # Nutritionist with signature -> ok, advances (approved auth).
        resp = self._api("Nutritionist").post(url, {"signature": "Jane RD"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        enr.refresh_from_db()
        self.assertEqual(enr.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)


class MeSignatureViewTest(TestCase):
    """A logged-in agent can save + reuse their signature via /portal/me/signature/."""

    def _api(self, agent):
        access = AccessToken()
        access["agent_id"] = str(agent.id)
        access["agent_code"] = agent.agent_code
        access["agent_name"] = agent.name
        access["agent_group"] = agent.group
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return api

    def _agent(self, **kw):
        from .models import Agent

        defaults = dict(
            name="Nita RD", agent_code=str(uuid.uuid4())[:8], group="Nutritionist",
        )
        defaults.update(kw)
        return Agent.objects.create(**defaults)

    def test_get_blank_then_save_then_get(self):
        agent = self._agent()
        api = self._api(agent)
        self.assertEqual(api.get("/api/portal/me/signature/").json()["signature_image"], "")
        img = "data:image/png;base64,iVBORw0KGgo="
        r = api.put("/api/portal/me/signature/", {"signature_image": img}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        agent.refresh_from_db()
        self.assertEqual(agent.signature_image, img)
        self.assertEqual(api.get("/api/portal/me/signature/").json()["signature_image"], img)

    def test_put_rejects_non_data_url(self):
        api = self._api(self._agent())
        r = api.put(
            "/api/portal/me/signature/", {"signature_image": "http://evil/x.png"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    def test_put_blank_clears_saved_signature(self):
        agent = self._agent(signature_image="data:image/png;base64,zzz")
        api = self._api(agent)
        r = api.put("/api/portal/me/signature/", {"signature_image": ""}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        agent.refresh_from_db()
        self.assertEqual(agent.signature_image, "")


class AssessmentApiMapperTest(SimpleTestCase):
    """map_assessment_api mirrors the extension's parse + payload shape."""

    def _detail(self, **over):
        d = {
            "id": "aid-1",
            "template": {"name": "Unite NYC - Food Assistance Assessment"},
            "eligible_services": [
                {"name": "Home Delivered Meals - Level 2"},
                "Produce Prescription",
            ],
            "questions": [
                {"order": 2, "primary_text": "How many in household?",
                 "answer": {"number": 3}},
                {"order": 1, "primary_text": "Do you need food?",
                 "answer": {"boolean": True}},
                {"order": 3, "primary_text": "Blank one", "answer": None},
            ],
        }
        d.update(over)
        return d

    def test_shape_and_ordering(self):
        from api.integrations.uniteus import mappers

        summary = {"id": "aid-1", "status": "complete",
                   "status_at": "2026-08-01T12:00:00Z"}
        out = mappers.map_assessment_api(self._detail(), summary, person_id="p-1")
        self.assertEqual(out["assessment_id"], "aid-1")
        self.assertEqual(out["subject_id"], "p-1")
        self.assertEqual(out["form_name"], "Unite NYC - Food Assistance Assessment")
        self.assertEqual(out["screen_created_at"], "2026-08-01T12:00:00Z")
        self.assertEqual(out["eligible_status"], "eligible")
        # dict + string services both cleaned to names, in source order.
        self.assertEqual(
            out["eligible_services"],
            ["Home Delivered Meals - Level 2", "Produce Prescription"],
        )
        # Questions sorted by order; blank answers dropped; bool -> Yes.
        self.assertEqual(
            out["questions_answers"],
            [
                {"question": "Do you need food?", "answer": "Yes"},
                {"question": "How many in household?", "answer": "3"},
            ],
        )

    def test_incomplete_status_omits_eligible_status(self):
        from api.integrations.uniteus import mappers

        out = mappers.map_assessment_api(
            self._detail(), {"id": "aid-1", "status": "in_progress"}, person_id="p-1"
        )
        self.assertNotIn("eligible_status", out)

    def test_falls_back_to_summary_services(self):
        from api.integrations.uniteus import mappers

        detail = self._detail(eligible_services=None)
        summary = {"id": "aid-1", "eligible_services": ["Level 1 Boxes"]}
        out = mappers.map_assessment_api(detail, summary, person_id="p-1")
        self.assertEqual(out["eligible_services"], ["Level 1 Boxes"])

    def test_missing_services_key_absent(self):
        from api.integrations.uniteus import mappers

        out = mappers.map_assessment_api(
            {"id": "aid-1", "questions": []}, {"id": "aid-1"}, person_id="p-1"
        )
        self.assertNotIn("eligible_services", out)
        self.assertNotIn("questions_answers", out)


class AssessmentApiSerializerTest(TestCase):
    """The mapped payload drives catalog + client level via AssessmentSerializer."""

    def test_upsert_sets_client_level_and_catalog(self):
        from api.integrations.uniteus import mappers
        from api.models import Assessment, ClientLevel
        from api.serializers import AssessmentSerializer

        cid = str(uuid.uuid4())
        client = Client.objects.create(client_id=cid, first_name="A", last_name="B")
        detail = {
            "id": str(uuid.uuid4()),
            "eligible_services": [{"name": "Home Delivered Meals - Level 2"}],
            "questions": [],
        }
        data = mappers.map_assessment_api(
            detail, {"id": detail["id"], "status": "complete"}, person_id=cid
        )
        ser = AssessmentSerializer(data=data)
        self.assertTrue(ser.is_valid(), ser.errors)
        ser.save()

        client.refresh_from_db()
        self.assertEqual(client.is_level, ClientLevel.LEVEL_2)
        assessment = Assessment.objects.get(pk=detail["id"])
        self.assertEqual(assessment.subject_id, client.pk)
        self.assertEqual(assessment.client_id, client.pk)
        self.assertEqual(assessment.eligible_status, "eligible")


class ScreeningsIngestionClientTest(SimpleTestCase):
    """Pagination + provider scoping for the screenings-ingestion client."""

    class _Resp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status
            self.text = ""

        def json(self):
            return self._payload

    class _Session:
        def __init__(self, pages):
            self.pages = pages
            self.calls = []

        def get(self, url, headers=None, params=None, timeout=None):
            self.calls.append(params)
            return ScreeningsIngestionClientTest._Resp(self.pages.pop(0))

    def _client(self, pages):
        from api.integrations.uniteus.screenings_api import ScreeningsIngestionClient

        cred = SimpleNamespace(
            pk=1, access_token="tok", token_type="Bearer",
            employee_id="emp", provider_id="prov",
        )
        c = ScreeningsIngestionClient(cred, allow_refresh=False)
        c._session = self._Session(pages)
        return c

    def test_paginates_until_total_reached(self):
        page1 = {"screens": [{"id": "1", "organization_id": "prov"}] * 20, "total": 25}
        page2 = {"screens": [{"id": "2", "organization_id": "prov"}] * 5, "total": 25}
        c = self._client([page1, page2])
        out = c.list_assessments("p-1")
        self.assertEqual(len(out), 25)
        # Two pages requested at offset 0 then 20, both type=assessment.
        self.assertEqual([p["offset"] for p in c._session.calls], [0, 20])
        self.assertTrue(all(p["type"] == "assessment" for p in c._session.calls))

    def test_provider_scoping_filters_other_orgs(self):
        page = {
            "screens": [
                {"id": "1", "organization_id": "prov"},
                {"id": "2", "organization_id": "OTHER"},
            ],
            "total": 2,
        }
        c = self._client([page])
        out = c.list_assessments("p-1", provider_id="prov")
        self.assertEqual([s["id"] for s in out], ["1"])

    def test_detail_unwraps_screen(self):
        c = self._client([{"screen": {"id": "1", "eligible_services": ["X"]}}])
        self.assertEqual(c.get_screen_detail("1")["eligible_services"], ["X"])


class AssessmentEnrichmentServiceTest(TestCase):
    """The enrichment service: target selection + list/detail/map/upsert + ImportRun."""

    def setUp(self):
        from .models import UniteUsCredential, UniteUsCredentialStatus

        self.cred = UniteUsCredential.objects.create(
            provider_id="prov", employee_id="emp",
            access_token="tok", status=UniteUsCredentialStatus.ACTIVE,
            last_captured_at=timezone.now(),
        )

    def _client_with_bare_assessment(self):
        from .models import Assessment

        cid = str(uuid.uuid4())
        client = Client.objects.create(client_id=cid, first_name="A", last_name="B")
        aid = str(uuid.uuid4())
        Assessment.objects.create(
            assessment_id=aid, subject_id=cid, client=client, eligible_services=[]
        )
        return client, cid, aid

    def _fake_api(self, aid, services):
        """A stand-in ScreeningsIngestionClient class returning canned data."""
        class _FakeApi:
            def __init__(self, cred, allow_refresh=False):
                pass

            def list_assessments(self, person_id, provider_id=None):
                return [{"id": aid, "organization_id": "prov", "status": "complete"}]

            def get_screen_detail(self, screen_id):
                return {"id": aid, "eligible_services": services, "questions": []}

        return _FakeApi

    def test_run_enrichment_writes_results_and_level(self):
        from unittest import mock

        from .models import Assessment, ClientLevel, ImportRunStatus
        from api.services import assessment_enrichment as svc

        client, cid, aid = self._client_with_bare_assessment()
        fake = self._fake_api(aid, [{"name": "Enhanced Care Management (Level 2)"}])
        with mock.patch.object(svc, "ScreeningsIngestionClient", fake):
            run = svc.run_assessment_enrichment(triggered_by="test")

        self.assertEqual(run.status, ImportRunStatus.COMPLETED)
        self.assertEqual(run.updated_count, 1)
        self.assertEqual(run.error_count, 0)
        client.refresh_from_db()
        self.assertEqual(client.is_level, ClientLevel.LEVEL_2)
        assessment = Assessment.objects.get(pk=aid)
        self.assertEqual(
            assessment.eligible_services, ["Enhanced Care Management (Level 2)"]
        )
        self.assertEqual(assessment.eligible_status, "eligible")
        self.assertEqual(run.stats["enriched_preview"][0]["assessment_id"], aid)

    def test_dry_run_writes_nothing(self):
        from unittest import mock

        from .models import Assessment
        from api.services import assessment_enrichment as svc

        client, cid, aid = self._client_with_bare_assessment()
        fake = self._fake_api(aid, ["Home Delivered Meals - Level 1"])
        with mock.patch.object(svc, "ScreeningsIngestionClient", fake):
            enricher = svc.enrich_assessments(apply=False)

        self.assertEqual(enricher.stats["enriched"], 1)
        self.assertEqual(len(enricher.previews), 1)
        # Nothing persisted.
        self.assertEqual(Assessment.objects.get(pk=aid).eligible_services, [])
        client.refresh_from_db()
        self.assertFalse(client.is_level)

    def test_target_person_ids_skips_already_enriched(self):
        from .models import Assessment
        from api.services.assessment_enrichment import target_person_ids

        client, cid, aid = self._client_with_bare_assessment()
        # A second client whose assessment already HAS results is excluded.
        other = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="C", last_name="D"
        )
        Assessment.objects.create(
            assessment_id=str(uuid.uuid4()), subject_id=other.client_id,
            client=other, eligible_services=["X"],
        )
        ids = target_person_ids()
        self.assertIn(cid, ids)
        self.assertNotIn(str(other.client_id), ids)

    def test_no_credential_records_error(self):
        from .models import UniteUsCredential
        from api.services import assessment_enrichment as svc

        UniteUsCredential.objects.all().delete()
        client, cid, aid = self._client_with_bare_assessment()
        enricher = svc.enrich_assessments(apply=True)
        self.assertTrue(any("No active" in e for e in enricher.errors))
        self.assertEqual(enricher.stats["enriched"], 0)


class ReconcileUnverifiedAdvancedEnrollmentsTest(TestCase):
    """The cleanup command reverts kitchen-assignment/verified enrollments that
    never had verified_at set back to Pending Verification, but leaves genuinely
    verified rows and serving rows (by default) alone."""

    def _enr(self, stage, *, verified):
        from django.utils import timezone as tz

        from .models import Client, EnrollmentVerification

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="U", last_name="V",
        )
        return EnrollmentVerification.objects.create(
            client=c, stage=stage, verified_at=(tz.now() if verified else None),
        )

    def test_reverts_only_unverified_pre_service(self):
        from django.core.management import call_command

        from .models import EnrollmentStage

        ka_null = self._enr(EnrollmentStage.KITCHEN_ASSIGNMENT, verified=False)
        ka_ok = self._enr(EnrollmentStage.KITCHEN_ASSIGNMENT, verified=True)
        serving_null = self._enr(EnrollmentStage.SERVICE_ACTIVE, verified=False)

        call_command("reconcile_unverified_advanced_enrollments", "--apply")

        ka_null.refresh_from_db()
        ka_ok.refresh_from_db()
        serving_null.refresh_from_db()
        # Unverified kitchen-assignment -> reverted to Pending Verification.
        self.assertEqual(ka_null.stage, EnrollmentStage.PENDING_VERIFICATION)
        # Genuinely verified -> untouched.
        self.assertEqual(ka_ok.stage, EnrollmentStage.KITCHEN_ASSIGNMENT)
        # Serving row -> untouched by default (needs --include-serving).
        self.assertEqual(serving_null.stage, EnrollmentStage.SERVICE_ACTIVE)


class CaseSwitchCarriesClinicalDataTest(TestCase):
    """A governing-case switch carries the full clinical/nutrition intake AND the
    Nutritionist sign-off forward onto the replacement enrollment (regression:
    these were previously dropped, forcing a needless re-review)."""

    def test_replacement_carries_clinical_and_nutritionist_approval(self):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, Kitchen,
            KitchenStatus, MemberDietaryProfile, MemberStatus,
            ServiceAuthorizationStatus,
        )
        from .services.lifecycle import replace_enrollment_for_case_change

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Cl", last_name="In",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        kitchen = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)

        def _case(day):
            return Case.objects.create(
                case_id=str(uuid.uuid4()), client=client,
                case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
                service_authorization_status=ServiceAuthorizationStatus.APPROVED,
                service_authorization_approval_starts_at=now,
                service_authorization_approval_ends_at=now + timedelta(days=90),
                program_name="Medically Tailored Meals",
                case_created_at=now + timedelta(days=day),
                date_opened=now + timedelta(days=day),
            )

        old_case = _case(1)
        live = EnrollmentVerification.objects.create(
            client=client, household=hh, case=old_case, kitchen=kitchen,
            stage=EnrollmentStage.SERVICE_ACTIVE, program_name="Medically Tailored Meals",
            delivery_weekdays=["mon", "thu"], verified_at=now,
            nutritionist_approved_at=now, nutritionist_signature="Nita RD",
        )
        MemberDietaryProfile.objects.create(
            enrollment=live, client=client, member_name="Cl In",
            status=MemberStatus.ACTIVE, menu_type="Standard",
            conditions=["Diabetes"], medications=["Insulin"], weight="180 lb",
            height="5ft 9in", on_medical_diet=True, medical_diet_details="Low sodium",
            meal_plan="Renal", assessment_notes="ok notes",
        )
        new_case = _case(30)

        new_enr = replace_enrollment_for_case_change(client, new_case)
        self.assertIsNotNone(new_enr)
        new_enr.refresh_from_db()
        # Nutritionist sign-off carried onto the enrollment.
        self.assertIsNotNone(new_enr.nutritionist_approved_at)
        self.assertEqual(new_enr.nutritionist_signature, "Nita RD")
        # Clinical / nutrition intake carried onto the member profile.
        mp = new_enr.member_profiles.get()
        self.assertEqual(mp.conditions, ["Diabetes"])
        self.assertEqual(mp.medications, ["Insulin"])
        self.assertEqual(mp.weight, "180 lb")
        self.assertEqual(mp.height, "5ft 9in")
        self.assertTrue(mp.on_medical_diet)
        self.assertEqual(mp.medical_diet_details, "Low sodium")
        self.assertEqual(mp.meal_plan, "Renal")
        self.assertEqual(mp.assessment_notes, "ok notes")
        self.assertEqual(mp.menu_type, "Standard")


class DistributionOverviewKindFromEnrollmentTest(TestCase):
    """The Distribution matrix classifies meals/boxes from the ENROLLMENT's live
    program, not a stale schedule program snapshot (regression: a meals case with
    a leftover boxes delivery-schedule program was mislabeled as boxes)."""

    def _api(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        from .models import Agent
        a = Agent.objects.create(name="Mgr", agent_code="960", group="Management")
        acc = AccessToken()
        acc["agent_id"] = str(a.id); acc["agent_code"] = a.agent_code
        acc["agent_name"] = a.name; acc["agent_group"] = a.group
        api = APIClient(); api.credentials(HTTP_AUTHORIZATION=f"Bearer {acc}")
        return api

    def test_meals_case_with_stale_boxes_schedule_counts_as_meals(self):
        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, Kitchen, KitchenStatus,
            MemberDeliverySchedule, MemberDietaryProfile, MemberStatus, Program,
            ScheduleStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Wm", last_name="Burg",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        kitchen = Kitchen.objects.create(
            name="Williamsburg", status=KitchenStatus.ACTIVE, supported_products=["meals"],
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, kitchen=kitchen,
            stage=EnrollmentStage.SERVICE_ACTIVE, verified_at=timezone.now(),
            program_name="Clinically Appropriate Meals - Other Eligible Populations - Brooklyn",
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, member_name="Wm Burg",
            status=MemberStatus.ACTIVE,
        )
        # Stale schedule carrying a BOXES program (the leftover snapshot).
        boxes_program = Program.objects.create(
            name="Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Brooklyn",
        )
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=member, member_name="Wm Burg",
            program=boxes_program, delivery_days_cadence=DeliveryCadence.MON_THU,
            meals_per_day=3, prod_per_delivery=0, meals_boxes_total=12,
            status=ScheduleStatus.SCHEDULED,
        )

        resp = self._api().get("/api/portal/dashboard/distribution/?scope=active")
        self.assertEqual(resp.status_code, 200, resp.content)
        grand = resp.json()["grand_total"]
        # Classified from the enrollment's Meals program, NOT the boxes schedule.
        self.assertEqual(grand["meals"], 1)
        self.assertEqual(grand["boxes"], 0)


class CaseSwitchClosesOldAndRespectsNutriGateTest(TestCase):
    """A same-kind governing-case switch on an IN-FUNNEL (never-served)
    enrollment REBINDS the new case onto it -- no fork, no duplicate -- and keeps
    it at its current pre-service stage (so the nutritionist gate is never
    skipped). A SERVED enrollment still forks, force-closing the old as
    read-only history."""

    def _setup(self, *, stage, nutri):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            ServiceAuthorizationStatus,
        )

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Sw", last_name="It",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)

        def _case(day):
            return Case.objects.create(
                case_id=uuid.uuid4(), client=client,
                case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
                service_authorization_status=ServiceAuthorizationStatus.APPROVED,
                service_authorization_approval_starts_at=now,
                service_authorization_approval_ends_at=now + timedelta(days=90),
                program_name="Medically Tailored Meals",
                case_created_at=now + timedelta(days=day),
                date_opened=now + timedelta(days=day),
            )

        old_case = _case(1)
        live = EnrollmentVerification.objects.create(
            client=client, household=hh, case=old_case,
            stage=stage, program_name="Medically Tailored Meals",
            verified_at=now, nutritionist_approved_at=(now if nutri else None),
        )
        new_case = _case(30)
        return client, live, new_case

    def test_pre_service_same_kind_rebinds_no_duplicate(self):
        from .models import EnrollmentStage, EnrollmentVerification
        from .services.lifecycle import replace_enrollment_for_case_change

        client, live, new_case = self._setup(
            stage=EnrollmentStage.VERIFIED, nutri=False,
        )
        result = replace_enrollment_for_case_change(client, new_case)
        # REBIND, not fork -> returns None (no new enrollment created).
        self.assertIsNone(result)
        live.refresh_from_db()
        # The existing enrollment is rebound to the new case and stays VERIFIED
        # (not force-closed, not skipped past the nutritionist gate).
        self.assertEqual(str(live.case_id), str(new_case.case_id))
        self.assertEqual(EnrollmentStage(live.stage), EnrollmentStage.VERIFIED)
        # Exactly ONE live enrollment for the client -> no duplicate.
        live_count = (
            EnrollmentVerification.objects.filter(client=client)
            .exclude(stage__in=["closed", "cancelled"]).count()
        )
        self.assertEqual(live_count, 1)

    def test_served_switch_forks_and_closes_old(self):
        from .models import EnrollmentStage
        from .services.lifecycle import replace_enrollment_for_case_change

        client, live, new_case = self._setup(
            stage=EnrollmentStage.SERVICE_ACTIVE, nutri=True,
        )
        new_enr = replace_enrollment_for_case_change(client, new_case)
        # A SERVED enrollment still forks (old preserved as history).
        self.assertIsNotNone(new_enr)
        live.refresh_from_db(); new_enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(live.stage), EnrollmentStage.CLOSED)
        self.assertEqual(new_enr.supersedes_id, live.pk)
        self.assertIsNotNone(new_enr.nutritionist_approved_at)  # carried forward

    def test_caseless_served_enrollment_retied_to_prior_case_on_fork(self):
        # INVARIANT: an enrollment must never be left tied to no case. A served
        # enrollment that lost its case FK when its governing case closed must be
        # RE-TIED to that prior (closed) case as it's forked to history -- so the
        # superseded "previous enrollment" always references its case.
        from datetime import timedelta
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            ServiceAuthorizationStatus,
        )
        from .services.lifecycle import replace_enrollment_for_case_change

        now = timezone.now()
        client = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Re", last_name="Tie")
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)

        def _case(day, status):
            return Case.objects.create(
                case_id=uuid.uuid4(), client=client,
                case_type=CaseType.INTERNAL_SERVICE, case_status=status,
                service_authorization_status=ServiceAuthorizationStatus.APPROVED,
                service_authorization_approval_starts_at=now,
                service_authorization_approval_ends_at=now + timedelta(days=90),
                program_name="Medically Tailored Meals",
                case_created_at=now + timedelta(days=day),
                date_opened=now + timedelta(days=day),
                case_closed_at=(now + timedelta(days=day + 5)) if status == CaseStatus.CLOSED else None,
            )

        prior_case = _case(1, CaseStatus.CLOSED)  # the case it served under, now closed
        # A serving enrollment that LOST its case FK when the prior case closed.
        live = EnrollmentVerification.objects.create(
            client=client, household=hh, case=None,
            stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals", verified_at=now,
        )
        new_case = _case(30, CaseStatus.OPEN)

        new_enr = replace_enrollment_for_case_change(client, new_case)
        self.assertIsNotNone(new_enr)  # served -> forks
        new_enr.refresh_from_db(); live.refresh_from_db()
        self.assertEqual(str(new_enr.case_id), str(new_case.case_id))
        self.assertEqual(EnrollmentStage(live.stage), EnrollmentStage.CLOSED)
        self.assertEqual(new_enr.supersedes_id, live.pk)
        # CRUCIAL: the superseded enrollment is re-tied to its prior case.
        self.assertEqual(str(live.case_id), str(prior_case.case_id))
        # The survivor tracks the case it replaced via previous_case.
        self.assertEqual(str(new_enr.previous_case_id), str(prior_case.case_id))
        # No caseless enrollment remains for the client.
        self.assertFalse(
            EnrollmentVerification.objects.filter(client=client, case__isnull=True).exists()
        )

    def test_full_reconcile_rebinds_verified_no_duplicate(self):
        # End-to-end via the real entry point: a governing-case-id change on a
        # verified (in-funnel) household rebinds instead of forking, so no
        # duplicate enrollment is ever created for logistics/nutrition to
        # double-work.
        from .models import EnrollmentStage, EnrollmentVerification
        from .services.lifecycle import reconcile_internal_service_authorization

        client, live, new_case = self._setup(
            stage=EnrollmentStage.VERIFIED, nutri=False,
        )
        reconcile_internal_service_authorization(client)
        live.refresh_from_db()
        self.assertEqual(str(live.case_id), str(new_case.case_id))  # rebound
        live_count = (
            EnrollmentVerification.objects.filter(client=client)
            .exclude(stage__in=["closed", "cancelled"]).count()
        )
        self.assertEqual(live_count, 1)  # no duplicate


class ReconcileSupersededLiveEnrollmentsTest(TestCase):
    """The cleanup closes a superseded duplicate when the survivor is still
    serving, but SKIPS it when the superseded row is the only one serving (so it
    never ends a member's service)."""

    def _pair(self, *, survivor_serving):
        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, MemberDeliverySchedule,
            MemberDietaryProfile, MemberStatus, ScheduleStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dup", last_name="Lic",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        old = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            verified_at=timezone.now(),
        )
        survivor = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            verified_at=timezone.now(), supersedes=old,
        )

        def _sched(enr):
            mp = MemberDietaryProfile.objects.create(
                enrollment=enr, client=client, member_name="Dup Lic",
                status=MemberStatus.ACTIVE,
            )
            MemberDeliverySchedule.objects.create(
                enrollment=enr, member_profile=mp, member_name="Dup Lic",
                delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
                prod_per_delivery=0, meals_boxes_total=12,
                status=ScheduleStatus.SCHEDULED,  # ends_on null -> future
            )

        _sched(old)  # the superseded row is serving
        if survivor_serving:
            _sched(survivor)
        return old, survivor

    def test_closes_duplicate_when_survivor_serving(self):
        from django.core.management import call_command

        from .models import EnrollmentStage

        old, survivor = self._pair(survivor_serving=True)
        call_command("reconcile_superseded_live_enrollments", "--apply")
        old.refresh_from_db(); survivor.refresh_from_db()
        self.assertEqual(EnrollmentStage(old.stage), EnrollmentStage.CLOSED)
        self.assertEqual(EnrollmentStage(survivor.stage), EnrollmentStage.SERVICE_ACTIVE)

    def test_skips_when_survivor_not_serving(self):
        from django.core.management import call_command

        from .models import EnrollmentStage

        old, survivor = self._pair(survivor_serving=False)
        call_command("reconcile_superseded_live_enrollments", "--apply")
        old.refresh_from_db()
        # Superseded row is the only server -> must NOT be closed.
        self.assertEqual(EnrollmentStage(old.stage), EnrollmentStage.SERVICE_ACTIVE)

    def test_skips_when_old_holds_distinct_open_case(self):
        from django.core.management import call_command

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Two", last_name="Cases",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        old_case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
        )
        new_case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN, program_name="Medically Tailored Meals",
        )
        # Superseded row bound to a DISTINCT still-open case; survivor on the new
        # case. Closing the old would orphan its open case -> must be skipped.
        old = EnrollmentVerification.objects.create(
            client=client, household=hh, case=old_case,
            stage=EnrollmentStage.PENDING_VERIFICATION, verified_at=timezone.now(),
        )
        EnrollmentVerification.objects.create(
            client=client, household=hh, case=new_case, supersedes=old,
            stage=EnrollmentStage.PENDING_VERIFICATION, verified_at=timezone.now(),
        )
        call_command("reconcile_superseded_live_enrollments", "--apply")
        old.refresh_from_db()
        self.assertEqual(EnrollmentStage(old.stage), EnrollmentStage.PENDING_VERIFICATION)


class ClosedCaseHoldDisplayTest(TestCase):
    """A hold over a CLOSED governing case is the closure full-stop, so it reads
    as Closed / Inactive -- not a confusing On Hold -- on the program + list."""

    def _setup(self, case_status, *, lifecycle=None):
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            ServiceAuthorizationStatus,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Hold", last_name="Case",
            lifecycle_stage=(lifecycle or "active"),
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=case_status,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals",
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.ON_HOLD, verified_at=timezone.now(),
        )
        return client, enr

    def test_program_status_hold_over_closed_case_is_closed(self):
        from .models import CaseStatus, ProgramStatus
        from .services.lifecycle import program_status

        _, enr = self._setup(CaseStatus.CLOSED)
        self.assertEqual(program_status(enr), ProgramStatus.CLOSED)

    def test_program_status_hold_over_open_case_is_on_hold(self):
        from .models import CaseStatus, ProgramStatus
        from .services.lifecycle import program_status

        _, enr = self._setup(CaseStatus.OPEN)
        self.assertEqual(program_status(enr), ProgramStatus.ON_HOLD)

    def test_verification_status_inactive_not_on_hold(self):
        from .models import CaseStatus, ClientStage
        from .portal.serializers import verification_status

        client, _ = self._setup(CaseStatus.CLOSED, lifecycle=ClientStage.SERVICE_INACTIVE)
        # Closure full-stop (service_inactive) -> shows the real "Inactive" label,
        # not the On Hold overlay.
        self.assertNotEqual(verification_status(client), "On Hold")


class ActiveEnrollmentPrefersGoverningCaseTest(TestCase):
    """active_enrollment (used by the program tab + all its actions +
    /assign-kitchen/) targets the enrollment bound to the GOVERNING internal-
    service case, even when a non-governing enrollment opened more recently."""

    def test_prefers_enrollment_on_governing_case(self):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            ServiceAuthorizationStatus,
        )
        from .portal.serializers import active_enrollment

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Gov", last_name="Case",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        gov_case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="MTM", case_created_at=now, date_opened=now,
        )
        other_case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.DENIED,
            program_name="MTM", case_created_at=now + timedelta(days=5),
            date_opened=now + timedelta(days=5),
        )
        gov_enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=gov_case,
            stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        other_enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=other_case,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        # Force the NON-governing enrollment to look more recent (would win by
        # recency without the governing-case preference).
        EnrollmentVerification.objects.filter(pk=gov_enr.pk).update(
            opened_at=now - timedelta(days=5))
        EnrollmentVerification.objects.filter(pk=other_enr.pk).update(opened_at=now)

        self.assertEqual(active_enrollment(client).pk, gov_enr.pk)

    def test_verified_not_hidden_by_newer_pending_on_governing_case(self):
        # Regression: a household with a VERIFIED (pending-nutritionist)
        # enrollment plus a NEWER pending_verification row on the governing case
        # must still resolve to the VERIFIED one -- otherwise the whole
        # Nutritionist queue gets hidden behind unverified renewal rows.
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            ServiceAuthorizationStatus,
        )
        from .portal.serializers import active_enrollment

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Pend", last_name="Hide",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        old_case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="MTM", case_created_at=now,
        )
        gov_case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="MTM", case_created_at=now + timedelta(days=5),
        )
        verified = EnrollmentVerification.objects.create(
            client=client, household=hh, case=old_case,
            stage=EnrollmentStage.VERIFIED, verified_at=now,
        )
        pending = EnrollmentVerification.objects.create(
            client=client, household=hh, case=gov_case,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        # Pending row is NEWER + on the governing case -- must NOT win.
        EnrollmentVerification.objects.filter(pk=verified.pk).update(
            opened_at=now - timedelta(days=5))
        EnrollmentVerification.objects.filter(pk=pending.pk).update(opened_at=now)

        self.assertEqual(active_enrollment(client).pk, verified.pk)


class ReassignRunsFullCalendarReconcileTest(TestCase):
    """A kitchen/cadence CHANGE (re-assignment) runs the same per-enrollment
    reconcile as sync_delivery_calendars (reconcile_enrollment_calendar) so the
    individual household's calendar is fully rebuilt; a FIRST-TIME assignment
    still uses the plain calendar expansion."""

    def _run(self, *, created):
        from unittest.mock import patch

        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Kitchen, KitchenStatus,
        )
        from .portal import views_members as vm

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Re", last_name="Assign",
        )
        kitchen = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        enr = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        with patch.object(vm, "create_member_delivery_schedules", return_value=created), \
                patch.object(vm, "update_household_cadence"), \
                patch.object(vm, "reconcile_enrollment_calendar") as recon, \
                patch.object(vm, "generate_delivery_calendar") as gen, \
                patch.object(vm, "resync_scheduled_orders"), \
                patch.object(vm, "sync_household_warnings"):
            vm.assign_kitchen_to_household(
                enr, client, kitchen, cadence=DeliveryCadence.MON_THU,
            )
        return recon, gen

    def test_reassignment_runs_full_reconcile(self):
        recon, gen = self._run(created=[])   # schedules already exist -> re-assign
        recon.assert_called_once()
        gen.assert_not_called()

    def test_first_time_uses_generate(self):
        recon, gen = self._run(created=[1])  # new plan created -> first-time
        gen.assert_called_once()
        recon.assert_not_called()


class DedupeIgnoresClosedEnrollmentOccurrencesTest(TestCase):
    """_dedupe_calendar_occurrences must NOT let a dead (CLOSED/CANCELLED)
    enrollment's leftover occurrences block the live survivor's calendar -- the
    bug that stranded a re-kitchened member off the PO. A LIVE enrollment's
    occurrence still blocks a duplicate."""

    def _client_hh(self):
        from .models import Client, Household
        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="A", last_name="B",
        )
        return c, Household.objects.create(name="HH")

    def _existing_occurrence(self, *, client, hh, stage, day):
        from .models import (
            EnrollmentVerification, MemberDietaryProfile, OrderSchedule, OrderStatus,
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=stage,
            program_name="Medically Tailored Meals",
        )
        mp = MemberDietaryProfile.objects.create(
            enrollment=enr, client=client, member_name="A B",
        )
        OrderSchedule.objects.create(
            enrollment=enr, member=mp, member_name="A B",
            anticipated_delivery_date=day, program_name="Medically Tailored Meals",
            status=OrderStatus.SCHEDULED,
        )
        return enr

    def _new_occurrence(self, *, client, hh, day):
        from .models import (
            EnrollmentStage, EnrollmentVerification, MemberDietaryProfile,
            OrderSchedule, OrderStatus,
        )
        live = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals",
        )
        lmp = MemberDietaryProfile.objects.create(
            enrollment=live, client=client, member_name="A B",
        )
        return OrderSchedule(
            enrollment=live, member=lmp, member_name="A B",
            anticipated_delivery_date=day, program_name="Medically Tailored Meals",
            status=OrderStatus.SCHEDULED,
        )

    def test_closed_enrollment_occurrence_does_not_block(self):
        from datetime import date

        from .models import EnrollmentStage
        from .services.orders import _dedupe_calendar_occurrences

        c, hh = self._client_hh()
        day = date(2026, 8, 13)
        self._existing_occurrence(client=c, hh=hh, stage=EnrollmentStage.CLOSED, day=day)
        new_occ = self._new_occurrence(client=c, hh=hh, day=day)
        kept = _dedupe_calendar_occurrences([new_occ])
        self.assertEqual(len(kept), 1)  # not blocked by the dead enrollment

    def test_live_enrollment_occurrence_still_blocks(self):
        from datetime import date

        from .models import EnrollmentStage
        from .services.orders import _dedupe_calendar_occurrences

        c, hh = self._client_hh()
        day = date(2026, 8, 13)
        self._existing_occurrence(
            client=c, hh=hh, stage=EnrollmentStage.SERVICE_ACTIVE, day=day,
        )
        new_occ = self._new_occurrence(client=c, hh=hh, day=day)
        kept = _dedupe_calendar_occurrences([new_occ])
        self.assertEqual(len(kept), 0)  # duplicate against a LIVE enrollment


class ReconcileClosedEnrollmentCalendarsTest(TestCase):
    """The sweep truncates stale future occurrences off a CLOSED enrollment and
    rebuilds the client's LIVE enrollment so near-term dates land on the enrollment
    that actually serves."""

    def test_moves_near_term_dates_from_closed_to_live(self):
        from datetime import timedelta

        from django.core.management import call_command

        from .models import (
            Case, CaseStatus, CaseType, Client, DeliveryCadence, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, Kitchen,
            KitchenStatus, MemberDeliverySchedule, MemberDietaryProfile,
            MemberStatus, OrderSchedule, OrderStatus, ScheduleStatus,
            ServiceAuthorizationStatus,
        )

        now = timezone.now()
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Sw", last_name="Eep",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        kitchen = Kitchen.objects.create(name="AST", status=KitchenStatus.ACTIVE)
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=120),
            program_name="Medically Tailored Meals",
        )
        # CLOSED enrollment still holding a future SCHEDULED occurrence.
        closed = EnrollmentVerification.objects.create(
            client=client, household=hh, stage=EnrollmentStage.CLOSED,
            program_name="Medically Tailored Meals",
        )
        cmp_ = MemberDietaryProfile.objects.create(
            enrollment=closed, client=client, member_name="Sw Eep",
        )
        soon = timezone.localdate() + timedelta(days=3)
        OrderSchedule.objects.create(
            enrollment=closed, member=cmp_, member_name="Sw Eep",
            anticipated_delivery_date=soon, program_name="Medically Tailored Meals",
            status=OrderStatus.SCHEDULED, kitchen=kitchen,
        )
        # LIVE enrollment with a plan covering the same window, but NO calendar yet.
        live = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case,
            stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals", kitchen=kitchen,
            verified_at=now, delivery_weekdays=["mon", "thu"],
        )
        lmp = MemberDietaryProfile.objects.create(
            enrollment=live, client=client, member_name="Sw Eep",
            status=MemberStatus.ACTIVE,
        )
        MemberDeliverySchedule.objects.create(
            enrollment=live, member_profile=lmp, member_name="Sw Eep",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, kitchen=kitchen,
            status=ScheduleStatus.SCHEDULED,
            starts_on=timezone.localdate(),
            ends_on=timezone.localdate() + timedelta(days=120),
        )

        call_command("reconcile_closed_enrollment_calendars", "--apply")

        # Closed enrollment's future occurrences are gone; live one now has some.
        today = timezone.localdate()
        closed_future = OrderSchedule.objects.filter(
            enrollment=closed, status=OrderStatus.SCHEDULED,
            anticipated_delivery_date__gte=today,
        ).count()
        live_future = OrderSchedule.objects.filter(
            enrollment=live, status=OrderStatus.SCHEDULED,
            anticipated_delivery_date__gte=today,
        ).count()
        self.assertEqual(closed_future, 0)
        self.assertGreater(live_future, 0)


class VerificationCreateEnforcesSingleEnrollmentTest(TestCase):
    """Creating a verification for a client who already has a live in-funnel
    enrollment REUSES it (rebind) instead of opening a second live row -- so a
    new/renewal case can never fork a duplicate enrollment."""

    def test_reuses_existing_pending_enrollment(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
        )
        from .serializers import EnrollmentVerificationSerializer

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="One", last_name="Enr",
        )
        hh = Household.objects.create(name="HH")
        existing = EnrollmentVerification.objects.create(
            client=client, household=hh,
            stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        ser = EnrollmentVerificationSerializer(data={
            "client_id": str(client.client_id),
            "household_id": str(hh.pk),
            "household_size": 3,
            "members": [{"member_name": "One Enr"}],
        })
        ser.is_valid(raise_exception=True)
        enr = ser.save()
        self.assertEqual(enr.pk, existing.pk)  # reused, not a new row
        self.assertEqual(enr.household_size, 3)  # data applied
        live = EnrollmentVerification.objects.filter(client=client).exclude(
            stage__in=["closed", "cancelled"]).count()
        self.assertEqual(live, 1)  # invariant: one live enrollment

    def test_creates_when_no_live_enrollment(self):
        from .models import Client, EnrollmentVerification, Household
        from .serializers import EnrollmentVerificationSerializer

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="New", last_name="Enr",
        )
        hh = Household.objects.create(name="HH")
        ser = EnrollmentVerificationSerializer(data={
            "client_id": str(client.client_id), "household_id": str(hh.pk),
            "members": [{"member_name": "New Enr"}],
        })
        ser.is_valid(raise_exception=True)
        enr = ser.save()
        self.assertEqual(
            EnrollmentVerification.objects.filter(client=client).count(), 1)


class PoMembershipDiffTest(TestCase):
    """Week-over-week membership churn per kitchen x cadence, from DeliveryOrders:
    New (truly-new vs moved-in) and Dropped (exited vs moved-out)."""

    def test_new_moved_exited_buckets(self):
        from datetime import timedelta

        from .models import (
            Client, DeliveryOrder, DeliveryOrderStatus, Kitchen, KitchenStatus,
            PurchaseOrder, PurchaseOrderStatus,
        )
        from .portal.views_dashboard_logistics import (
            _completed_week_ranges, po_membership_cell_members, po_membership_diff,
        )

        today = timezone.localdate()
        ts, te, ls, le = _completed_week_ranges(today)
        A = Kitchen.objects.create(name="Kitchen A", status=KitchenStatus.ACTIVE)
        B = Kitchen.objects.create(name="Kitchen B", status=KitchenStatus.ACTIVE)
        po = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)

        def client(n):
            return Client.objects.create(
                client_id=str(uuid.uuid4()), first_name=n, last_name="X")

        def mk(c, kitchen, day):
            DeliveryOrder.objects.create(
                purchase_order=po, member=c, kitchen=kitchen,
                expected_delivery_date=day, status=DeliveryOrderStatus.PENDING)

        M1, M2, M3, M4 = client("M1"), client("M2"), client("M3"), client("M4")
        tmon, tthu = ts, ts + timedelta(days=3)      # this completed week Mon/Thu
        lmon, lthu = ls, ls + timedelta(days=3)      # prior completed week Mon/Thu
        for d in (tmon, tthu):                        # this week
            mk(M1, A, d); mk(M2, A, d); mk(M3, B, d)
        for d in (lmon, lthu):                        # last week
            mk(M1, A, d); mk(M3, A, d); mk(M4, A, d)

        diff = po_membership_diff(today)
        cells = {(c["kitchen_name"], c["cadence_key"]): c for c in diff["cells"]}

        a = cells[("Kitchen A", "mon,thu")]
        self.assertEqual((a["this"], a["last"]), (2, 3))       # {M1,M2} vs {M1,M3,M4}
        self.assertEqual(a["new_true"], 1)                     # M2 truly new
        self.assertEqual(a["moved_in"], 0)
        self.assertEqual(a["exited"], 1)                       # M4 off every PO
        self.assertEqual(a["moved_out"], 1)                    # M3 -> Kitchen B

        b = cells[("Kitchen B", "mon,thu")]
        self.assertEqual((b["this"], b["last"]), (1, 0))       # M3 new here
        self.assertEqual(b["new_true"], 0)
        self.assertEqual(b["moved_in"], 1)                     # M3 moved in from A

        s = diff["summary"]
        self.assertEqual((s["this_total"], s["last_total"], s["net"]), (3, 3, 0))
        self.assertEqual(s["new_to_service"], 1)   # M2 (first-ever PO this week)
        self.assertEqual(s["returned"], 0)
        self.assertEqual(s["exited"], 1)           # M4

        dr = po_membership_cell_members(today, a["kitchen_id"], "mon,thu")
        self.assertEqual([r["name"] for r in dr["exited"]], ["M4 X"])
        self.assertEqual([r["name"] for r in dr["moved_out"]], ["M3 X"])
        # Mover carries where they went (Kitchen B / Mon/Thu).
        self.assertTrue(dr["moved_out"][0]["other"])


class DetectPoDropsTest(TestCase):
    """Members served in the past/current week but missing from the upcoming PO
    are flagged with the right reason."""

    def test_drop_reasons(self):
        from datetime import timedelta

        from .models import (
            Client, DeliveryOrder, DeliveryOrderStatus, EnrollmentStage,
            EnrollmentVerification, Household, Kitchen, KitchenStatus,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
            PurchaseOrder, PurchaseOrderStatus,
        )
        from .services.po_blockers import detect_po_drops

        today = timezone.localdate()
        last_monday = today - timedelta(days=today.weekday()) - timedelta(days=7)
        K = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        hh = Household.objects.create(name="HH")
        po = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)

        def client(n):
            return Client.objects.create(
                client_id=str(uuid.uuid4()), first_name=n, last_name="Z")

        def served(c):  # a non-cancelled delivery last week
            DeliveryOrder.objects.create(
                purchase_order=po, member=c, kitchen=K,
                expected_delivery_date=last_monday,
                status=DeliveryOrderStatus.PENDING)

        def enr(c, stage):
            return EnrollmentVerification.objects.create(
                client=c, household=hh, stage=stage, kitchen=K,
                program_name="Medically Tailored Meals")

        # M1: served + has an upcoming occurrence -> still on, NOT dropped.
        M1 = client("M1"); served(M1)
        e1 = enr(M1, EnrollmentStage.SERVICE_ACTIVE)
        p1 = MemberDietaryProfile.objects.create(
            enrollment=e1, client=M1, member_name="M1 Z", status=MemberStatus.ACTIVE)
        OrderSchedule.objects.create(
            enrollment=e1, member=p1, member_name="M1 Z",
            anticipated_delivery_date=today + timedelta(days=1),
            status=OrderStatus.SCHEDULED)

        # M2: served, no upcoming, active on a serving enrollment -> the bug.
        M2 = client("M2"); served(M2)
        e2 = enr(M2, EnrollmentStage.SERVICE_ACTIVE)
        MemberDietaryProfile.objects.create(
            enrollment=e2, client=M2, member_name="M2 Z", status=MemberStatus.ACTIVE)

        # M3: served, no live enrollment (closed) -> off-boarded.
        M3 = client("M3"); served(M3)
        enr(M3, EnrollmentStage.CLOSED)

        # M4: served, no upcoming, member PAUSED on a serving enrollment.
        M4 = client("M4"); served(M4)
        e4 = enr(M4, EnrollmentStage.SERVICE_ACTIVE)
        MemberDietaryProfile.objects.create(
            enrollment=e4, client=M4, member_name="M4 Z", status=MemberStatus.PAUSED)

        # M5: served; the only upcoming occurrence sits on a CLOSED enrollment
        # (never feeds a PO), while the live serving enrollment has none -> the
        # ghost-calendar drop must NOT be masked.
        M5 = client("M5"); served(M5)
        e5 = enr(M5, EnrollmentStage.SERVICE_ACTIVE)
        MemberDietaryProfile.objects.create(
            enrollment=e5, client=M5, member_name="M5 Z", status=MemberStatus.ACTIVE)
        e5closed = enr(M5, EnrollmentStage.CLOSED)
        p5c = MemberDietaryProfile.objects.create(
            enrollment=e5closed, client=M5, member_name="M5 Z", status=MemberStatus.ACTIVE)
        OrderSchedule.objects.create(
            enrollment=e5closed, member=p5c, member_name="M5 Z",
            anticipated_delivery_date=today + timedelta(days=1),
            status=OrderStatus.SCHEDULED)

        # M6: served, no upcoming servable occurrence, enrollment still parked at
        # KITCHEN_ASSIGNMENT (a stale stage) -> reported as kitchen_assignment,
        # NOT missing_from_calendar.
        M6 = client("M6"); served(M6)
        e6 = enr(M6, EnrollmentStage.KITCHEN_ASSIGNMENT)   # enr() assigns kitchen K
        MemberDietaryProfile.objects.create(
            enrollment=e6, client=M6, member_name="M6 Z", status=MemberStatus.ACTIVE)

        # M7: served, KITCHEN_ASSIGNMENT but NO kitchen -> awaiting_kitchen (a
        # genuine assignment to-do, e.g. a meals<->boxes switch), NOT urgent.
        M7 = client("M7"); served(M7)
        EnrollmentVerification.objects.create(
            client=M7, household=hh, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
            kitchen=None, program_name="Medically Tailored Meals")

        # M8: served; has an ON_HOLD (served, case closed) enrollment AND a fresh
        # PENDING_VERIFICATION on a new case -> reason must reflect the served
        # ON_HOLD one, not the pending row.
        M8 = client("M8"); served(M8)
        EnrollmentVerification.objects.create(
            client=M8, household=hh, stage=EnrollmentStage.ON_HOLD, kitchen=K,
            program_name="Medically Tailored Meals")
        EnrollmentVerification.objects.create(
            client=M8, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
            kitchen=None, program_name="Medically Tailored Meals")

        res = detect_po_drops(today)
        by_client = {r["client_id"]: r for r in res["dropped"]}
        self.assertNotIn(str(M1.client_id), by_client)          # still on
        self.assertEqual(by_client[str(M5.client_id)]["reason"], "missing_from_calendar")
        self.assertEqual(by_client[str(M6.client_id)]["reason"], "kitchen_assignment")
        self.assertTrue(by_client[str(M6.client_id)]["urgent"])
        self.assertEqual(by_client[str(M7.client_id)]["reason"], "awaiting_kitchen")
        self.assertFalse(by_client[str(M7.client_id)]["urgent"])
        self.assertEqual(by_client[str(M8.client_id)]["reason"], "on_hold")
        self.assertEqual(by_client[str(M2.client_id)]["reason"], "missing_from_calendar")
        self.assertTrue(by_client[str(M2.client_id)]["urgent"])
        self.assertEqual(by_client[str(M3.client_id)]["reason"], "off_boarded")
        self.assertEqual(by_client[str(M4.client_id)]["reason"], "paused")
        self.assertFalse(by_client[str(M4.client_id)]["urgent"])


class RebuildReactivatesCancelledPlanTest(TestCase):
    """A live (servable) enrollment whose only delivery plan is CANCELLED -- a
    tangled-duplicate drift where the scheduled plan ended up on a dead
    enrollment -- gets its plan re-activated so "Rebuild calendar" works."""

    def test_rebuild_reactivates_cancelled_plan(self):
        from datetime import timedelta

        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Household, Kitchen, KitchenStatus, MemberDeliverySchedule,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
            ScheduleStatus,
        )
        from .services.orders import rebuild_delivery_calendar

        today = timezone.localdate()
        k = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        hh = Household.objects.create(name="HH")
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Al", last_name="H")
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE, kitchen=k,
            program_name="Medically Tailored Meals", delivery_weekdays=["mon", "thu"],
        )
        mp = MemberDietaryProfile.objects.create(
            enrollment=enr, client=c, member_name="Al H", status=MemberStatus.ACTIVE)
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=mp, member_name="Al H",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, kitchen=k,
            status=ScheduleStatus.CANCELLED,               # <- the drift
            starts_on=today, ends_on=today + timedelta(days=120),
        )

        res = rebuild_delivery_calendar(enr)
        self.assertEqual(res["plans_reactivated"], 1)
        self.assertGreater(
            OrderSchedule.objects.filter(
                enrollment=enr, status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today).count(),
            0,
        )


class RebuildHealsFromGoverningCaseTest(TestCase):
    """rebuild_delivery_calendar heals the plan from the GOVERNING case
    (recompute) when one exists, and falls back to a plain sync when the
    enrollment has no case (pre-case), so its window isn't blanked."""

    def _enr(self, *, with_case):
        from datetime import timedelta

        from .models import (
            Case, CaseStatus, CaseType, Client, DeliveryCadence, EnrollmentStage,
            EnrollmentVerification, Household, Kitchen, KitchenStatus,
            MemberDeliverySchedule, MemberDietaryProfile, MemberStatus,
            ScheduleStatus, ServiceAuthorizationStatus,
        )

        now = timezone.now()
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="G", last_name="C")
        hh = Household.objects.create(name="HH")
        k = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        case = None
        if with_case:
            case = Case.objects.create(
                case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
                case_status=CaseStatus.OPEN,
                service_authorization_status=ServiceAuthorizationStatus.APPROVED,
                service_authorization_approval_starts_at=now,
                service_authorization_approval_ends_at=now + timedelta(days=90),
                program_name="Medically Tailored Meals",
            )
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, case=case, kitchen=k,
            stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals", delivery_weekdays=["mon", "thu"],
        )
        mp = MemberDietaryProfile.objects.create(
            enrollment=enr, client=c, member_name="G C", status=MemberStatus.ACTIVE)
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=mp, member_name="G C",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, kitchen=k,
            status=ScheduleStatus.SCHEDULED,
            starts_on=timezone.localdate(),
            ends_on=timezone.localdate() + timedelta(days=90),
        )
        return enr

    def test_rebuild_heals_stale_window_from_case(self):
        from datetime import timedelta

        from .services.orders import rebuild_delivery_calendar

        enr = self._enr(with_case=True)
        today = timezone.localdate()
        plan = enr.delivery_schedules.get()
        # A STALE (short) plan window vs the governing auth (ends ~today+90):
        # rebuild should heal it to the authorization end.
        plan.ends_on = today + timedelta(days=10)
        plan.save(update_fields=["ends_on"])

        rebuild_delivery_calendar(enr)
        plan.refresh_from_db()
        self.assertGreater(plan.ends_on, today + timedelta(days=10))  # window extended

    def test_no_case_keeps_plan_window(self):
        from datetime import timedelta

        from .services.orders import rebuild_delivery_calendar

        enr = self._enr(with_case=False)
        today = timezone.localdate()
        plan = enr.delivery_schedules.get()
        plan.ends_on = today + timedelta(days=10)
        plan.save(update_fields=["ends_on"])

        rebuild_delivery_calendar(enr)
        plan.refresh_from_db()
        self.assertEqual(plan.ends_on, today + timedelta(days=10))  # no case -> unchanged


class RebuildDropsFutureCancelledOccurrencesTest(TestCase):
    """Rebuild clears stale FUTURE cancelled occurrences (old/again kitchen or a
    cancelled PO) so the forward calendar reflects reality, while PRESERVING past
    cancelled rows as history."""

    def test_future_cancelled_removed_past_kept(self):
        from datetime import timedelta

        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Household, Kitchen, KitchenStatus, MemberDeliverySchedule,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
            ScheduleStatus,
        )
        from .services.orders import rebuild_delivery_calendar

        today = timezone.localdate()
        k = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        hh = Household.objects.create(name="HH")
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="C", last_name="X")
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE, kitchen=k,
            program_name="Medically Tailored Meals", delivery_weekdays=["mon", "thu"])
        mp = MemberDietaryProfile.objects.create(
            enrollment=enr, client=c, member_name="C X", status=MemberStatus.ACTIVE)
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=mp, member_name="C X",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, kitchen=k,
            status=ScheduleStatus.SCHEDULED,
            starts_on=today, ends_on=today + timedelta(days=60))
        past = OrderSchedule.objects.create(
            enrollment=enr, member=mp, member_name="C X",
            anticipated_delivery_date=today - timedelta(days=7),
            status=OrderStatus.CANCELLED)
        fut = OrderSchedule.objects.create(
            enrollment=enr, member=mp, member_name="C X",
            anticipated_delivery_date=today + timedelta(days=7),
            status=OrderStatus.CANCELLED)

        rebuild_delivery_calendar(enr)
        self.assertTrue(OrderSchedule.objects.filter(pk=past.pk).exists())   # history kept
        self.assertFalse(OrderSchedule.objects.filter(pk=fut.pk).exists())   # future cancelled dropped


class DeliveryCalendarRebuildTargetsLiveEnrollmentTest(TestCase):
    """The Delivery Schedule 'Rebuild calendar' button must target the client's
    LIVE enrollment, not a more-recently-opened CLOSED one (rebuilding a closed
    enrollment is a no-op -- which made the button look dead)."""

    def test_prefers_live_over_recent_closed(self):
        from datetime import timedelta

        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            MemberDietaryProfile,
        )
        from .portal.views_delivery_calendar import MemberDeliveryCalendarView

        now = timezone.now()
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="H")
        hh = Household.objects.create(name="HH")
        live = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE)
        closed = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.CLOSED)
        # closed opened MORE recently than live
        EnrollmentVerification.objects.filter(pk=live.pk).update(opened_at=now - timedelta(days=30))
        EnrollmentVerification.objects.filter(pk=closed.pk).update(opened_at=now)
        MemberDietaryProfile.objects.create(enrollment=live, client=c, member_name="A H")
        MemberDietaryProfile.objects.create(enrollment=closed, client=c, member_name="A H")

        got = MemberDeliveryCalendarView()._enrollment_for(str(c.client_id))
        self.assertEqual(got.pk, live.pk)


class ResumeDoesNotRegressServingEnrollmentTest(TestCase):
    """_resume_auto_paused_enrollment must NOT force an already-resumed, now
    SERVICE_ACTIVE enrollment back down to a STALE hold's from_stage (which
    stranded served members at Kitchen Assignment)."""

    def test_stale_hold_does_not_regress_serving(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household, StageEvent,
        )
        from .services.eligibility import _INELIGIBLE_HOLD_NOTE
        from .services.lifecycle import _resume_auto_paused_enrollment

        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="H")
        hh = Household.objects.create(name="HH")
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE)
        # A stale ineligible auto-hold from BEFORE she resumed + activated.
        StageEvent.objects.create(
            enrollment=enr, from_stage=EnrollmentStage.VERIFIED,
            to_stage=EnrollmentStage.ON_HOLD, note=_INELIGIBLE_HOLD_NOTE)

        _resume_auto_paused_enrollment(enr, hold_note=_INELIGIBLE_HOLD_NOTE)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.SERVICE_ACTIVE)


class CarryServicePromotesPendingMemberTest(TestCase):
    """Carrying a serving household to a new governing case must leave its
    members ACTIVE (not PENDING) so a delivery plan/cadence is created -- a
    PENDING member is excluded from plan creation and would get no cadence."""

    def test_pending_member_promoted_and_plan_created(self):
        from datetime import timedelta
        from unittest.mock import patch

        from .models import (
            Case, CaseStatus, CaseType, Client, DeliveryCadence, EnrollmentStage,
            EnrollmentVerification, Household, Kitchen, KitchenStatus,
            MemberDeliverySchedule, MemberDietaryProfile, MemberStatus,
            ProductTypeKind, ScheduleStatus, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import _carry_service_and_activate

        now = timezone.now(); today = timezone.localdate()
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="G", last_name="S")
        hh = Household.objects.create(name="HH")
        k = Kitchen.objects.create(name="Williamsburg", status=KitchenStatus.ACTIVE)
        new_case = Case.objects.create(
            case_id=uuid.uuid4(), client=c, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=90),
            program_name="Medically Tailored Meals")
        prior = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE, kitchen=k,
            program_name="Medically Tailored Meals", delivery_weekdays=["mon", "thu"])
        pmp = MemberDietaryProfile.objects.create(
            enrollment=prior, client=c, member_name="G S", status=MemberStatus.ACTIVE)
        MemberDeliverySchedule.objects.create(
            enrollment=prior, member_profile=pmp, member_name="G S",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, kitchen=k,
            status=ScheduleStatus.SCHEDULED, starts_on=today, ends_on=today + timedelta(days=90))
        new_enr = EnrollmentVerification.objects.create(
            client=c, household=hh, case=new_case,
            stage=EnrollmentStage.PENDING_VERIFICATION,
            program_name="Medically Tailored Meals", verified_at=now)
        nmp = MemberDietaryProfile.objects.create(
            enrollment=new_enr, client=c, member_name="G S", status=MemberStatus.PENDING)

        with patch("api.services.meal_rules.reconcile_member_kitchen_output", return_value=None):
            _carry_service_and_activate(
                new_enr, prior, new_case, ProductTypeKind.MEALS,
                prior_was_serving=True, actor=None, actor_label="")

        nmp.refresh_from_db(); new_enr.refresh_from_db()
        self.assertEqual(nmp.status, MemberStatus.ACTIVE)                 # promoted
        self.assertTrue(new_enr.delivery_schedules.exists())             # plan/cadence created
        self.assertEqual(EnrollmentStage(new_enr.stage), EnrollmentStage.SERVICE_ACTIVE)


class ReuseEnrollmentCreatesMissingDependentTest(TestCase):
    """Root-cause regression: the reuse-existing-enrollment replacement path must
    CREATE a household dependent that lived only on the closed enrollment onto the
    reused survivor -- conserving the dependent's PRIOR status -- so they aren't
    stranded off the delivery calendar while the primary keeps serving."""

    def test_missing_dependent_created_conserving_status(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            MemberDietaryProfile, MemberStatus,
        )
        from .services.lifecycle import _create_missing_carried_profiles

        hh = Household.objects.create(name="HH")
        primary = Client.objects.create(client_id=str(uuid.uuid4()), first_name="P", last_name="One")
        dep_active = Client.objects.create(client_id=str(uuid.uuid4()), first_name="D", last_name="Active")
        dep_paused = Client.objects.create(client_id=str(uuid.uuid4()), first_name="D", last_name="Paused")

        closed = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.CLOSED,
            program_name="Medically Tailored Meals")
        survivor = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals")

        # Closed enrollment has ALL members; survivor has ONLY the primary.
        MemberDietaryProfile.objects.create(
            enrollment=closed, client=primary, member_name="P One",
            menu_type="Standard", status=MemberStatus.ACTIVE)
        MemberDietaryProfile.objects.create(
            enrollment=closed, client=dep_active, member_name="D Active",
            menu_type="Standard", food_allergies=["None"], status=MemberStatus.ACTIVE)
        MemberDietaryProfile.objects.create(
            enrollment=closed, client=dep_paused, member_name="D Paused",
            menu_type="Kosher", status=MemberStatus.PAUSED, pause_locked=True)
        MemberDietaryProfile.objects.create(
            enrollment=survivor, client=primary, member_name="P One",
            menu_type="Standard", status=MemberStatus.ACTIVE)

        created = _create_missing_carried_profiles(survivor, closed)
        self.assertEqual(created, 2)  # both dependents, not the already-present primary

        a = survivor.member_profiles.get(client=dep_active)
        self.assertEqual(a.status, MemberStatus.ACTIVE)          # Active carried -> Active
        self.assertEqual(a.menu_type, "Standard")                # dietary carried
        self.assertEqual(a.food_allergies, ["None"])
        self.assertEqual(a.kitchen_meal_type, "")                # rule RESULT not copied

        p = survivor.member_profiles.get(client=dep_paused)
        self.assertEqual(p.status, MemberStatus.PAUSED)          # Paused NEVER revived
        self.assertTrue(p.pause_locked)                          # pin carried

        # Idempotent: a second run creates nothing.
        self.assertEqual(_create_missing_carried_profiles(survivor, closed), 0)


class BackfillVerifiedAtTest(TestCase):
    """An enrollment that reached Verified (per the StageEvent audit) but never
    had verified_at stamped is healed from the event time -- so a later case
    change carries service instead of bouncing to Pending Verification. Reverted
    pending_verification / terminal rows are left alone."""

    def _evt(self, enr, to_stage, when):
        from .models import StageEvent
        se = StageEvent.objects.create(
            entity_type="enrollment", enrollment=enr,
            from_stage="", to_stage=to_stage, source="manual")
        StageEvent.objects.filter(pk=se.pk).update(entered_at=when)  # auto_now_add override

    def test_stamps_serving_but_unstamped_and_skips_others(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household, MemberStatus,
        )
        now = timezone.now()
        hh = Household.objects.create(name="HH")
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="B")

        # Serving, verified in the audit log, but verified_at NULL -> should heal.
        serving = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals", verified_at=None)
        vtime = now - timezone.timedelta(days=10)
        self._evt(serving, EnrollmentStage.VERIFIED, vtime)

        # Reverted to pending_verification (reached verified once) -> left alone.
        reverted = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
            program_name="Medically Tailored Meals", verified_at=None)
        self._evt(reverted, EnrollmentStage.VERIFIED, now - timezone.timedelta(days=5))

        # Never reached verified -> left alone.
        never = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals", verified_at=None)

        call_command("backfill_verified_at", "--apply", stdout=StringIO())

        serving.refresh_from_db(); reverted.refresh_from_db(); never.refresh_from_db()
        self.assertEqual(serving.verified_at, vtime)      # healed from the event time
        self.assertTrue(serving.is_family_verified)
        self.assertIsNone(reverted.verified_at)           # reverted: untouched
        self.assertIsNone(never.verified_at)              # no evidence: untouched


class ReconcileOnHoldClosedCasesTest(TestCase):
    """Close an on_hold enrollment whose internal-service case has ended, but
    leave a hold with a still-open internal-service case alone."""

    def _client(self, first):
        from .models import Client
        return Client.objects.create(client_id=str(uuid.uuid4()), first_name=first, last_name="X")

    def _isc(self, client, status):
        from .models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus
        return Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=status,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals")

    def test_closes_ended_hold_and_keeps_open_hold(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import (
            CaseStatus, EnrollmentStage, EnrollmentVerification, Household,
        )
        hh = Household.objects.create(name="HH")

        # Ended: on_hold + only a CLOSED internal-service case -> should close.
        c_ended = self._client("Ended")
        ended_case = self._isc(c_ended, CaseStatus.CLOSED)
        ended = EnrollmentVerification.objects.create(
            client=c_ended, household=hh, case=ended_case,
            stage=EnrollmentStage.ON_HOLD, program_name="Medically Tailored Meals")

        # Still open: on_hold + an OPEN internal-service case -> leave on hold.
        c_open = self._client("Openish")
        open_case = self._isc(c_open, CaseStatus.OPEN)
        held = EnrollmentVerification.objects.create(
            client=c_open, household=hh, case=open_case,
            stage=EnrollmentStage.ON_HOLD, program_name="Medically Tailored Meals")

        call_command("reconcile_onhold_closed_cases", "--apply", stdout=StringIO())

        ended.refresh_from_db(); held.refresh_from_db()
        self.assertEqual(EnrollmentStage(ended.stage), EnrollmentStage.CLOSED)   # ended -> closed
        self.assertEqual(EnrollmentStage(held.stage), EnrollmentStage.ON_HOLD)   # open case -> untouched


class SyncHealsServiceActiveWithoutScheduledPlanTest(TestCase):
    """sync_active_calendars must pick up a SERVICE_ACTIVE enrollment that has a
    kitchen but NO scheduled plan (cancelled plan / never created) -- previously
    invisible to the batch -- and rebuild it so it isn't stranded off POs."""

    def test_sync_rebuilds_service_active_with_cancelled_plan(self):
        from datetime import timedelta

        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Household, Kitchen, KitchenStatus, MemberDeliverySchedule,
            MemberDietaryProfile, MemberStatus, OrderSchedule, OrderStatus,
            ScheduleStatus,
        )
        from .services.orders import sync_active_calendars

        today = timezone.localdate()
        k = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        hh = Household.objects.create(name="HH")
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="S", last_name="A")
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE, kitchen=k,
            program_name="Medically Tailored Meals", delivery_weekdays=["mon", "thu"])
        mp = MemberDietaryProfile.objects.create(
            enrollment=enr, client=c, member_name="S A", status=MemberStatus.ACTIVE)
        MemberDeliverySchedule.objects.create(
            enrollment=enr, member_profile=mp, member_name="S A",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, kitchen=k,
            status=ScheduleStatus.CANCELLED,                 # no SCHEDULED plan
            starts_on=today, ends_on=today + timedelta(days=60))

        sync_active_calendars()
        self.assertGreater(
            OrderSchedule.objects.filter(
                enrollment=enr, status=OrderStatus.SCHEDULED,
                anticipated_delivery_date__gte=today).count(), 0)


class DetectPoDropsDependentInheritsHouseholdTest(TestCase):
    """A dependent (no own enrollment) inherits the household PRIMARY's
    enrollment for the drop reason -- so a held household's dependents read
    on_hold, not a false off_boarded. A genuinely enrollment-less served client
    stays off_boarded."""

    def test_dependent_inherits_primary_reason(self):
        from datetime import timedelta

        from .models import (
            Client, DeliveryOrder, DeliveryOrderStatus, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, Kitchen,
            KitchenStatus, MemberDietaryProfile, MemberStatus, PurchaseOrder,
            PurchaseOrderStatus,
        )
        from .services.po_blockers import detect_po_drops

        today = timezone.localdate()
        last_monday = today - timedelta(days=today.weekday()) - timedelta(days=7)
        k = Kitchen.objects.create(name="K", status=KitchenStatus.ACTIVE)
        hh = Household.objects.create(name="HH")
        po = PurchaseOrder.objects.create(status=PurchaseOrderStatus.DRAFT)

        def served(c):
            DeliveryOrder.objects.create(
                purchase_order=po, member=c, kitchen=k,
                expected_delivery_date=last_monday,
                status=DeliveryOrderStatus.PENDING)

        primary = Client.objects.create(client_id=str(uuid.uuid4()), first_name="P", last_name="H")
        dep = Client.objects.create(client_id=str(uuid.uuid4()), first_name="D", last_name="H")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        # Primary holds the household enrollment, currently ON HOLD.
        penr = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.ON_HOLD, kitchen=k,
            program_name="Medically Tailored Meals")
        MemberDietaryProfile.objects.create(
            enrollment=penr, client=primary, member_name="P H", status=MemberStatus.ACTIVE)
        # Dependent has NO enrollment of their own -- only a profile on the
        # household enrollment.
        MemberDietaryProfile.objects.create(
            enrollment=penr, client=dep, member_name="D H", status=MemberStatus.ACTIVE)
        served(primary); served(dep)

        # Genuinely enrollment-less served client (no household / no primary).
        gone = Client.objects.create(client_id=str(uuid.uuid4()), first_name="G", last_name="X")
        served(gone)

        rows = {r["client_id"]: r for r in detect_po_drops(today)["dropped"]}
        self.assertEqual(rows[str(dep.client_id)]["reason"], "on_hold")       # inherited
        self.assertFalse(rows[str(dep.client_id)]["urgent"])
        self.assertEqual(rows[str(gone.client_id)]["reason"], "off_boarded")  # genuine


class SyncHouseholdCarriesPriorProfileTest(TestCase):
    """sync_household_members must carry a roster member's dietary INFORMATION
    forward from their most recent prior enrollment profile (governing-case
    change), not re-create it blank. Service STATUS is NOT carried -- it's owned
    by the scope rules (Household<->Individual), so it stays at the default."""

    def test_carries_prior_dietary_and_status(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, MemberDietaryProfile, MemberStatus,
        )
        from .serializers import sync_household_members

        hh = Household.objects.create(name="HH")
        primary = Client.objects.create(client_id=str(uuid.uuid4()), first_name="P", last_name="H")
        dep = Client.objects.create(client_id=str(uuid.uuid4()), first_name="D", last_name="H")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)

        # Closed prior enrollment: dependent was ACTIVE with a full profile.
        old = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.CLOSED,
            program_name="Medically Tailored Meals")
        MemberDietaryProfile.objects.create(
            enrollment=old, client=dep, member_name="D H", status=MemberStatus.ACTIVE,
            menu_type="Kosher", dietary_restrictions=["Low Sodium"],
            food_allergies=["Peanuts"], other_dietary_restrictions="No shellfish",
            general_verification_notes="prefers soft foods")

        # New live enrollment: dependent is only on the roster, no profile yet.
        new = EnrollmentVerification.objects.create(
            client=primary, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            program_name="Medically Tailored Meals", supersedes=old)
        MemberDietaryProfile.objects.create(
            enrollment=new, client=primary, member_name="P H", status=MemberStatus.ACTIVE)

        sync_household_members(primary, enrollment=new)

        dep_profile = MemberDietaryProfile.objects.get(enrollment=new, client=dep)
        # INFORMATION is carried forward...
        self.assertEqual(dep_profile.menu_type, "Kosher")
        self.assertEqual(dep_profile.food_allergies, ["Peanuts"])
        self.assertEqual(dep_profile.other_dietary_restrictions, "No shellfish")
        self.assertEqual(dep_profile.general_verification_notes, "prefers soft foods")
        # ...but STATUS is NOT (left to the scope rules; stays the model default).
        self.assertEqual(dep_profile.status, MemberStatus.PENDING)


class ClientMigrationMergeTest(TestCase):
    """merge_migrated_client: consolidate a Unite Us person-migration duplicate
    (old id has our service state; new id has the imported cases) onto the NEW
    surviving client, reconciling enrollment.client == enrollment.case.client."""

    def test_merge_moves_service_state_and_reconciles_case_link(self):
        from .models import (
            Case, CaseStatus, CaseType, Client, ClientTag, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
            MemberDietaryProfile, MemberStatus,
        )
        from .services.client_migration import merge_migrated_client, resolve_client

        # OLD: our internal service state (enrollment + household + profile + tag).
        old = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="AMALIA", last_name="TINSLEY",
        )
        old_hh = Household.objects.create(name="OLD HH")
        HouseholdMember.objects.create(household=old_hh, client=old, is_primary=True)

        # NEW (canonical): the imported cases live here.
        new = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="AMALIA", last_name="TINSLEY",
        )
        new_hh = Household.objects.create(name="NEW HH")
        HouseholdMember.objects.create(household=new_hh, client=new, is_primary=True)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=new, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
        )
        enr = EnrollmentVerification.objects.create(
            client=old, household=old_hh, case=case, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        prof = MemberDietaryProfile.objects.create(
            enrollment=enr, client=old, member_name="AMALIA TINSLEY",
            status=MemberStatus.ACTIVE,
        )
        tag, _ = ClientTag.objects.get_or_create(name="Need Review")
        old.tags.add(tag)
        old_id = str(old.client_id)

        summary = merge_migrated_client(old, new)
        self.assertTrue(summary["merged"])

        # Old client + its now-empty household are gone.
        self.assertFalse(Client.objects.filter(client_id=old_id).exists())
        self.assertFalse(Household.objects.filter(pk=old_hh.pk).exists())

        # Enrollment moved onto NEW, re-homed under the survivor's household.
        enr.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(str(enr.client_id), str(new.client_id))
        self.assertEqual(enr.household_id, new.household_membership.household_id)
        # THE CORE FIX: enrollment.client == enrollment.case.client again.
        self.assertEqual(str(enr.client_id), str(enr.case.client_id))

        # Member profile carried; tag unioned; alias stamped + resolvable.
        prof.refresh_from_db()
        self.assertEqual(str(prof.client_id), str(new.client_id))
        self.assertTrue(new.tags.filter(name="Need Review").exists())
        self.assertTrue(new.tags.filter(name="Migrated").exists())
        self.assertEqual(new.migrated_from_id, old_id)
        # A migration event is recorded on the survivor's timeline (old -> new).
        from .models import TimelineEventType
        ev = new.timeline_events.filter(event_type=TimelineEventType.CLIENT_MIGRATED).first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.metadata.get("old_id"), old_id)
        self.assertEqual(ev.metadata.get("new_id"), str(new.client_id))
        self.assertEqual(str(resolve_client(old_id).client_id), str(new.client_id))

    def test_merge_is_noop_for_same_client(self):
        from .models import Client
        from .services.client_migration import merge_migrated_client

        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Solo")
        out = merge_migrated_client(c, c)
        self.assertFalse(out["merged"])


class VerificationScopeOpenGoverningCaseTest(TestCase):
    """The Verification page (require_internal_service_primary) is gated LIVE on
    the household primary's OPEN internal-service (governing) case: when it
    closes, the whole household -- primary AND dependents -- drops off, even if
    verified."""

    def _scope(self):
        from .models import Client
        from .portal.views_members import (
            require_internal_service_primary, verification_scope_q,
        )
        ids = require_internal_service_primary(
            Client.objects.filter(verification_scope_q())
        ).distinct().values_list("client_id", flat=True)
        return {str(i) for i in ids}

    def test_household_drops_when_primary_governing_case_closes(self):
        from .models import (
            Case, CaseStatus, CaseType, Client, ClientStage, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember,
        )

        primary = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Prim", last_name="Ary",
            lifecycle_stage=ClientStage.PENDING_VERIFICATION,
        )
        dep = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Dep", last_name="Endent",
            lifecycle_stage=ClientStage.PENDING_VERIFICATION,
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=primary, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
        )
        # Verified household enrollment (so we also prove a VERIFIED client drops).
        EnrollmentVerification.objects.create(
            client=primary, household=hh, case=case, stage=EnrollmentStage.VERIFIED,
            verified_at=timezone.now(),
        )

        # OPEN governing case -> both primary and dependent are on the page.
        scope = self._scope()
        self.assertIn(str(primary.client_id), scope)
        self.assertIn(str(dep.client_id), scope)

        # Close the primary's governing case -> the whole household drops.
        case.case_status = CaseStatus.CLOSED
        case.save(update_fields=["case_status"])
        scope = self._scope()
        self.assertNotIn(str(primary.client_id), scope)
        self.assertNotIn(str(dep.client_id), scope)


class WilliamsburgKitchenGuardTest(TestCase):
    """Williamsburg lead-source households can only ever be served by the
    Williamsburg kitchen: the assignment popup lists only that kitchen, and any
    attempt to assign a different one is rejected."""

    def test_options_restricted_and_assignment_guarded(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, Kitchen, KitchenStatus, MemberDietaryProfile,
            MemberStatus,
        )
        from .portal.views_members import assign_kitchen_to_household
        from .services.kitchens import kitchen_options
        from .services.williamsburg import WILLIAMSBURG_KITCHEN_NAME

        wburg = Kitchen.objects.create(name=WILLIAMSBURG_KITCHEN_NAME, status=KitchenStatus.ACTIVE)
        Kitchen.objects.create(name="Some Other Kitchen", status=KitchenStatus.ACTIVE)

        c = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Willy", last_name="Burg",
            is_williamsburg=True,
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        enr = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
        )
        MemberDietaryProfile.objects.create(
            enrollment=enr, client=c, member_name="Willy Burg",
            menu_type="Kosher", status=MemberStatus.ACTIVE,
        )

        # Popup lists ONLY the Williamsburg kitchen.
        opts = kitchen_options(enr)
        self.assertEqual({k["name"] for k in opts["kitchens"]}, {WILLIAMSBURG_KITCHEN_NAME})

        # Assigning any other kitchen is rejected centrally.
        other = Kitchen.objects.get(name="Some Other Kitchen")
        with self.assertRaises(ValueError):
            assign_kitchen_to_household(enr, c, other, cadence="mon_thu")

        # A Williamsburg household NEVER appears in the bulk-assignment population
        # (neither boxes nor meals) -- the single source both bulk flows use.
        from .models import ProductTypeKind
        from .portal.views_members import _awaiting_enrollments
        for kind in (ProductTypeKind.MEALS, ProductTypeKind.BOXES):
            ids = {e.client_id for e in _awaiting_enrollments(kind)}
            self.assertNotIn(c.client_id, ids)


class PausedHouseholdGoverningCaseSwitchTest(TestCase):
    """A new governing case must NOT auto-resume a PAUSED (On Hold) household:
    it stays On Hold and the client is flagged 'Need Review'."""

    def _meals_case(self, client, *, opened_day):
        from datetime import timedelta
        from .models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus

        now = timezone.now()
        return Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=90),
            program_name="Medically Tailored Meals",
            case_created_at=now + timedelta(days=opened_day),
            date_opened=now + timedelta(days=opened_day),
        )

    def test_case_switch_keeps_on_hold_and_flags_need_review(self):
        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, Kitchen, KitchenStatus,
            MemberDeliverySchedule, MemberDietaryProfile, MemberStatus,
            ScheduleStatus,
        )
        from .services.lifecycle import (
            advance_enrollment, replace_enrollment_for_case_change,
        )

        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Hold", last_name="Ing",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        kitchen = Kitchen.objects.create(name="ENG", status=KitchenStatus.ACTIVE)
        old_case = self._meals_case(client, opened_day=1)
        live = EnrollmentVerification.objects.create(
            client=client, household=hh, case=old_case, kitchen=kitchen,
            stage=EnrollmentStage.SERVICE_ACTIVE, program_name="Medically Tailored Meals",
            delivery_weekdays=["mon", "thu"], verified_at=timezone.now(),
        )
        member = MemberDietaryProfile.objects.create(
            enrollment=live, client=client, member_name="Hold Ing",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        MemberDeliverySchedule.objects.create(
            enrollment=live, member_profile=member, member_name="Hold Ing",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, status=ScheduleStatus.SCHEDULED,
        )
        # Pause the household.
        advance_enrollment(live, EnrollmentStage.ON_HOLD, force=True)

        # New governing case switches in.
        new_case = self._meals_case(client, opened_day=30)
        new_enr = replace_enrollment_for_case_change(client, new_case)
        enr = new_enr or live
        enr.refresh_from_db()

        # It must NOT have resumed to Service Active -- stays On Hold + flagged.
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.ON_HOLD)
        self.assertTrue(client.tags.filter(name="Need Review").exists())


class ResumeRevalidationTest(TestCase):
    """Resume-from-On-Hold re-validates the CURRENT governing case: blocks with
    no open case / invalid authorization, routes to Kitchen Assignment when the
    kitchen can't serve, Service Active when it can, and Pending Verification when
    never verified -- recording every check + the reason on the resume event."""

    def setUp(self):
        self.agent = Agent.objects.create(name="Res", agent_code="944", group="CS")
        access = AccessToken()
        access["agent_id"] = str(self.agent.id)
        access["agent_code"] = self.agent.agent_code
        access["agent_name"] = self.agent.name
        access["agent_group"] = self.agent.group
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def _held(self, *, verified=True, with_case=True, ends_days=90,
              with_kitchen=True, held_from=None):
        from datetime import timedelta
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, Household, HouseholdMember, Kitchen,
            KitchenStatus, ServiceAuthorizationStatus,
        )
        from .services.lifecycle import advance_enrollment

        now = timezone.now()
        held_from = held_from or EnrollmentStage.SERVICE_ACTIVE
        client = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="R", last_name="M",
        )
        hh = Household.objects.create(name="HH")
        HouseholdMember.objects.create(household=hh, client=client, is_primary=True)
        case = None
        if with_case:
            case = Case.objects.create(
                case_id=str(uuid.uuid4()), client=client,
                case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
                service_authorization_status=ServiceAuthorizationStatus.APPROVED,
                service_authorization_approval_starts_at=now - timedelta(days=1),
                service_authorization_approval_ends_at=now + timedelta(days=ends_days),
            )
        kitchen = (
            Kitchen.objects.create(name="ENG", status=KitchenStatus.ACTIVE)
            if with_kitchen else None
        )
        enr = EnrollmentVerification.objects.create(
            client=client, household=hh, case=case, kitchen=kitchen,
            stage=held_from, verified_at=(now if verified else None),
        )
        advance_enrollment(enr, EnrollmentStage.ON_HOLD, force=True)
        enr.refresh_from_db()
        return client, enr

    def _resume(self, client):
        return self.api.post(
            f"/api/portal/members/{client.client_id}/resume/",
            {"reason": "agent note"}, format="json",
        )

    def test_unverified_resumes_to_pending_verification(self):
        from .models import EnrollmentStage
        client, enr = self._held(verified=False)
        resp = self._resume(client)
        self.assertEqual(resp.status_code, 200, resp.content)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.PENDING_VERIFICATION)

    def test_no_open_case_blocks(self):
        from .models import EnrollmentStage
        client, enr = self._held(with_case=False)
        resp = self._resume(client)
        self.assertEqual(resp.status_code, 400, resp.content)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.ON_HOLD)

    def test_expired_authorization_blocks(self):
        from .models import EnrollmentStage
        client, enr = self._held(ends_days=-1)  # window ended yesterday
        resp = self._resume(client)
        self.assertEqual(resp.status_code, 400, resp.content)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.ON_HOLD)

    def test_kitchen_cannot_serve_goes_to_kitchen_assignment(self):
        from .models import EnrollmentStage
        client, enr = self._held(with_kitchen=False)
        resp = self._resume(client)
        self.assertEqual(resp.status_code, 200, resp.content)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.KITCHEN_ASSIGNMENT)

    def test_kitchen_serves_resumes_active_and_records_checks(self):
        from .models import EnrollmentStage, StageEvent
        client, enr = self._held(with_kitchen=True)
        resp = self._resume(client)
        self.assertEqual(resp.status_code, 200, resp.content)
        enr.refresh_from_db()
        self.assertEqual(EnrollmentStage(enr.stage), EnrollmentStage.SERVICE_ACTIVE)
        ev = (
            StageEvent.objects.filter(
                enrollment=enr, to_stage=EnrollmentStage.SERVICE_ACTIVE
            )
            .order_by("-entered_at")
            .first()
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.metadata.get("resume_reason"), "agent note")
        self.assertTrue(ev.metadata.get("resume_checks", {}).get("kitchen_can_serve"))
        self.assertTrue(ev.metadata.get("resume_checks", {}).get("authorization_valid"))


class CaseSwitchDietaryCarryTest(TestCase):
    """A governing-case replacement that REUSES a placeholder survivor must carry
    the VERIFIED source's menu/dietary onto it (overwrite) so a switch can't reset
    e.g. Halal -> Standard. A survivor with its OWN data keeps it (blanks-only)."""

    def _pair(self, *, source_menu, target_menu, target_verified):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            MemberDietaryProfile, MemberStatus,
        )
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="D", last_name="C")
        hh = Household.objects.create(name="HH")
        src = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.CLOSED,
            verified_at=timezone.now(),
        )
        tgt = EnrollmentVerification.objects.create(
            client=c, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
            verified_at=(timezone.now() if target_verified else None),
        )
        MemberDietaryProfile.objects.create(
            enrollment=src, client=c, member_name="D C", menu_type=source_menu,
            status=MemberStatus.ACTIVE,
        )
        MemberDietaryProfile.objects.create(
            enrollment=tgt, client=c, member_name="D C", menu_type=target_menu,
            status=MemberStatus.ACTIVE,
        )
        return c, src, tgt

    def test_placeholder_survivor_takes_verified_source_menu(self):
        from .models import MemberDietaryProfile
        from .services.lifecycle import _carry_dietary_profiles
        c, src, tgt = self._pair(source_menu="Halal", target_menu="Standard", target_verified=False)
        _carry_dietary_profiles(tgt, src, overwrite=True)
        mp = MemberDietaryProfile.objects.get(enrollment=tgt, client=c)
        self.assertEqual(mp.menu_type, "Halal")

    def test_own_verified_survivor_keeps_its_menu(self):
        from .models import MemberDietaryProfile
        from .services.lifecycle import _carry_dietary_profiles
        c, src, tgt = self._pair(source_menu="Halal", target_menu="Standard", target_verified=True)
        # overwrite=False (survivor had its own verification) -> blanks-only, keeps Standard
        _carry_dietary_profiles(tgt, src, overwrite=False)
        mp = MemberDietaryProfile.objects.get(enrollment=tgt, client=c)
        self.assertEqual(mp.menu_type, "Standard")

    def test_carried_verification_timeline_relabel(self):
        from .models import EnrollmentStage, TimelineEventType
        from .services.timeline import stage_timeline_fields
        et, title = stage_timeline_fields(EnrollmentStage.VERIFIED, trigger="case_replaced")
        self.assertEqual(et, TimelineEventType.VERIFICATION_COMPLETED)
        self.assertEqual(title, "Verification Data Carried over new enrollment")
        # A genuine verification keeps the normal label.
        _et, title2 = stage_timeline_fields(EnrollmentStage.VERIFIED)
        self.assertEqual(title2, "Verification Completed")


class SplitHouseholdPendingLeakTest(TestCase):
    """A member with their OWN live (verified) enrollment must NOT read as Pending
    Verification because a household-mate (split into their own case) has a pending
    enrollment. A TRUE dependent (no own enrollment) still inherits the household's
    pending so a shared verification moves everyone together."""

    def test_verified_member_not_pending_due_to_sibling(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember,
        )
        from .services.lifecycle import governing_pending_enrollment

        hh = Household.objects.create(name="HH")
        a = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Ann", last_name="V")
        b = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Bob", last_name="P")
        HouseholdMember.objects.create(household=hh, client=a, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=b, is_primary=False)
        # A: own verified + serving. B: own pending (split into their own case).
        EnrollmentVerification.objects.create(
            client=a, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
            verified_at=timezone.now(),
        )
        EnrollmentVerification.objects.create(
            client=b, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        self.assertIsNone(governing_pending_enrollment(a))       # not dragged pending by B
        self.assertIsNotNone(governing_pending_enrollment(b))    # B is genuinely pending

    def test_true_dependent_still_inherits_household_pending(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember,
        )
        from .services.lifecycle import governing_pending_enrollment

        hh = Household.objects.create(name="HH")
        p = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Pri", last_name="M")
        d = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Dep", last_name="E")
        HouseholdMember.objects.create(household=hh, client=p, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=d, is_primary=False)
        # Shared household enrollment (on the primary), pending. Dependent d has
        # NO own enrollment -> must still inherit the pending state.
        EnrollmentVerification.objects.create(
            client=p, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        self.assertIsNotNone(governing_pending_enrollment(d))


class MakePrimaryReHomesOrdersTest(TestCase):
    """ensure_primary_of_own_household must re-home the member's PROTECT'd order
    schedules with their enrollments and never delete a household that still
    holds a relative's enrollment/orders (the ProtectedError regression)."""

    def _order(self, enr, hh):
        from .models import OrderSchedule
        return OrderSchedule.objects.create(
            enrollment=enr, household=hh, household_group_code="HG-TEST",
        )

    def test_reanchor_moves_orders_and_keeps_shared_household(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, OrderSchedule,
        )
        from .serializers import ensure_primary_of_own_household

        hh = Household.objects.create()
        a = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Ann", last_name="A")
        b = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Bob", last_name="B")
        HouseholdMember.objects.create(household=hh, client=a, is_primary=False)
        enr_a = EnrollmentVerification.objects.create(
            client=a, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        enr_b = EnrollmentVerification.objects.create(
            client=b, household=hh, stage=EnrollmentStage.PENDING_VERIFICATION,
        )
        self._order(enr_a, hh)  # A's PROTECT'd order pins the old household open
        self._order(enr_a, hh)

        ensure_primary_of_own_household(a)  # must NOT raise ProtectedError

        a.refresh_from_db()
        new_hh = a.household_membership.household
        self.assertTrue(a.household_membership.is_primary)
        self.assertNotEqual(new_hh.pk, hh.pk)
        # A's orders followed A to the new household.
        self.assertEqual(OrderSchedule.objects.filter(household=new_hh).count(), 2)
        # The old household is preserved because B's enrollment still lives there.
        self.assertTrue(Household.objects.filter(pk=hh.pk).exists())
        self.assertTrue(hh.enrollment_verifications.filter(pk=enr_b.pk).exists())

    def test_reanchor_still_deletes_truly_empty_household(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember,
        )
        from .serializers import ensure_primary_of_own_household

        hh = Household.objects.create()
        a = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Sol", last_name="O")
        HouseholdMember.objects.create(household=hh, client=a, is_primary=False)
        EnrollmentVerification.objects.create(
            client=a, household=hh, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        ensure_primary_of_own_household(a)
        # Nothing else lived in the old household -> it is cleaned up.
        self.assertFalse(Household.objects.filter(pk=hh.pk).exists())


class CaseReplacementReanchorsDependentTest(TestCase):
    """A governing-case REPLACEMENT for a household DEPENDENT must re-anchor them
    into their OWN household (the same end-state the verification wizard reaches),
    while preserving the verification/kitchen/cadence/status carry that
    replace_enrollment_for_case_change already performs."""

    def _meals_case(self, client, *, opened_day):
        from datetime import timedelta
        from .models import Case, CaseStatus, CaseType, ServiceAuthorizationStatus
        now = timezone.now()
        return Case.objects.create(
            case_id=str(uuid.uuid4()), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            service_authorization_approval_starts_at=now,
            service_authorization_approval_ends_at=now + timedelta(days=90),
            program_name="Medically Tailored Meals",
            case_created_at=now + timedelta(days=opened_day),
            date_opened=now + timedelta(days=opened_day),
        )

    def test_dependent_reanchored_and_carry_preserved(self):
        from .models import (
            Client, DeliveryCadence, EnrollmentStage, EnrollmentVerification,
            Household, HouseholdMember, Kitchen, KitchenStatus,
            MemberDeliverySchedule, MemberDietaryProfile, MemberStatus,
            OrderSchedule, ScheduleStatus,
        )
        from .services.lifecycle import replace_enrollment_for_case_change

        hh = Household.objects.create(name="Shared")
        primary = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Pri", last_name="M")
        dep = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Dep", last_name="E")
        HouseholdMember.objects.create(household=hh, client=primary, is_primary=True)
        HouseholdMember.objects.create(household=hh, client=dep, is_primary=False)
        kitchen = Kitchen.objects.create(name="ENG", status=KitchenStatus.ACTIVE)
        old_case = self._meals_case(dep, opened_day=1)
        live = EnrollmentVerification.objects.create(
            client=dep, household=hh, case=old_case, kitchen=kitchen,
            stage=EnrollmentStage.SERVICE_ACTIVE, program_name="Medically Tailored Meals",
            delivery_weekdays=["mon", "thu"], verified_at=timezone.now(),
        )
        prof = MemberDietaryProfile.objects.create(
            enrollment=live, client=dep, member_name="Dep E",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        MemberDeliverySchedule.objects.create(
            enrollment=live, member_profile=prof, member_name="Dep E",
            delivery_days_cadence=DeliveryCadence.MON_THU, meals_per_day=3,
            prod_per_delivery=0, meals_boxes_total=12, status=ScheduleStatus.SCHEDULED,
        )
        OrderSchedule.objects.create(enrollment=live, household=hh, household_group_code="HG-X")
        new_case = self._meals_case(dep, opened_day=30)

        new_enr = replace_enrollment_for_case_change(dep, new_case)
        self.assertIsNotNone(new_enr)
        new_enr.refresh_from_db()

        # Carry preserved.
        self.assertEqual(str(new_enr.case_id), str(new_case.case_id))
        self.assertIsNotNone(new_enr.verified_at)
        self.assertEqual(EnrollmentStage(new_enr.stage), EnrollmentStage.SERVICE_ACTIVE)
        self.assertEqual(new_enr.kitchen_id, kitchen.pk)

        # Re-anchored into their own household; orders + enrollment followed.
        dep.refresh_from_db()
        mem = dep.household_membership
        self.assertTrue(mem.is_primary)
        self.assertNotEqual(mem.household_id, hh.pk)
        self.assertEqual(new_enr.household_id, mem.household_id)
        self.assertEqual(OrderSchedule.objects.filter(household=mem.household).count(), 1)
        # Shared household preserved (its real primary is untouched).
        self.assertTrue(Household.objects.filter(pk=hh.pk).exists())
        self.assertTrue(
            HouseholdMember.objects.filter(client=primary, household=hh, is_primary=True).exists()
        )

    def test_primary_client_replacement_is_not_reanchored(self):
        from .models import (
            Client, EnrollmentStage, EnrollmentVerification, Household,
            HouseholdMember, Kitchen, KitchenStatus, MemberDietaryProfile,
            MemberStatus,
        )
        from .services.lifecycle import replace_enrollment_for_case_change

        hh = Household.objects.create(name="Own")
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Pri", last_name="Solo")
        HouseholdMember.objects.create(household=hh, client=c, is_primary=True)
        kitchen = Kitchen.objects.create(name="ENG2", status=KitchenStatus.ACTIVE)
        old_case = self._meals_case(c, opened_day=1)
        live = EnrollmentVerification.objects.create(
            client=c, household=hh, case=old_case, kitchen=kitchen,
            stage=EnrollmentStage.SERVICE_ACTIVE, program_name="Medically Tailored Meals",
            delivery_weekdays=["mon", "thu"], verified_at=timezone.now(),
        )
        MemberDietaryProfile.objects.create(
            enrollment=live, client=c, member_name="Pri Solo",
            menu_type="Standard", status=MemberStatus.ACTIVE,
        )
        new_case = self._meals_case(c, opened_day=30)
        new_enr = replace_enrollment_for_case_change(c, new_case)
        self.assertIsNotNone(new_enr)
        c.refresh_from_db()
        # Already primary -> stays in the same household (no-op re-anchor).
        self.assertEqual(c.household_membership.household_id, hh.pk)


class ListUnmergedMigrationsCommandTest(TestCase):
    """list_unmerged_migrations: a torn enrollment.client != case.client with a
    MATCHING dob is a migration; a DIFFERENT dob is REVIEW (not a migration)."""

    def _pair(self, *, old_dob, new_dob, last):
        from datetime import date
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification,
        )
        old = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="A", last_name=last, date_of_birth=old_dob,
        )
        new = Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="A", last_name=last, date_of_birth=new_dob,
        )
        case = Case.objects.create(
            case_id=str(uuid.uuid4()), client=new, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
        )
        EnrollmentVerification.objects.create(
            client=old, case=case, stage=EnrollmentStage.SERVICE_ACTIVE,
        )
        return old, new

    def test_dob_split(self):
        from datetime import date
        from io import StringIO
        from django.core.management import call_command

        mig_old, mig_new = self._pair(old_dob=date(1990, 1, 1), new_dob=date(1990, 1, 1), last="Same")
        rev_old, rev_new = self._pair(old_dob=date(1990, 1, 1), new_dob=date(2015, 5, 5), last="Diff")

        out = StringIO()
        call_command("list_unmerged_migrations", stdout=out)
        text = out.getvalue()
        # Migration pair appears under MIGRATIONS; review pair flagged separately.
        self.assertIn(str(mig_old.client_id), text)
        self.assertIn("MIGRATIONS", text)
        self.assertIn(str(rev_old.client_id), text)
        self.assertIn("REVIEW", text)

        ids_out = StringIO()
        call_command("list_unmerged_migrations", "--ids-only", stdout=ids_out)
        ids = ids_out.getvalue()
        self.assertIn(str(mig_old.client_id), ids)          # migration id printed
        self.assertNotIn(str(rev_old.client_id), ids)       # review id excluded


class DetectApiMigrationTest(TestCase):
    """detect_api_migration + identity_matches_for_merge: API-based migration
    detection (no extension) and the strict auto-merge identity gate."""

    class _FakeApi:
        def __init__(self, mapping):
            self._m = mapping  # requested_id -> returned data.id
        def get_person(self, person_id, include="addresses"):
            return {"data": {"id": self._m.get(str(person_id), str(person_id))}}

    def test_detect_flags_redirected_id(self):
        from .models import Client
        from .services.client_migration import detect_api_migration
        old = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="B")
        new_id = str(uuid.uuid4())
        api = self._FakeApi({str(old.client_id): new_id})
        self.assertEqual(detect_api_migration(api, old), new_id)

    def test_detect_none_when_same_id(self):
        from .models import Client
        from .services.client_migration import detect_api_migration
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="B")
        api = self._FakeApi({})  # returns same id -> no migration
        self.assertIsNone(detect_api_migration(api, c))

    def _with_medicaid(self, client, member_id):
        from .models import Insurance, InsurancePlanType, RecordStatus
        Insurance.objects.create(
            client=client, plan_type=InsurancePlanType.MEDICAID,
            external_member_id=member_id, status=RecordStatus.ACTIVE, is_primary=True,
        )
        return client

    def test_gate_matches_same_person(self):
        from datetime import date
        from .models import Client
        from .services.client_migration import identity_matches_for_merge
        dob = date(1990, 5, 1)
        old = self._with_medicaid(Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ann", last_name="Lee", date_of_birth=dob), "MID123")
        new = self._with_medicaid(Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="ann", last_name="LEE", date_of_birth=dob), "mid123")
        self.assertTrue(identity_matches_for_merge(old, new))

    def test_gate_rejects_twin_same_dob_diff_name(self):
        from datetime import date
        from .models import Client
        from .services.client_migration import identity_matches_for_merge
        dob = date(2015, 3, 3)
        old = self._with_medicaid(Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Ava", last_name="Kim", date_of_birth=dob), "M1")
        new = self._with_medicaid(Client.objects.create(
            client_id=str(uuid.uuid4()), first_name="Mia", last_name="Kim", date_of_birth=dob), "M2")
        self.assertFalse(identity_matches_for_merge(old, new))

    def test_gate_rejects_when_medicaid_missing(self):
        from datetime import date
        from .models import Client
        from .services.client_migration import identity_matches_for_merge
        dob = date(1980, 1, 1)
        old = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Sam", last_name="Roe", date_of_birth=dob)
        new = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Sam", last_name="Roe", date_of_birth=dob)
        self.assertFalse(identity_matches_for_merge(old, new))  # no medicaid on file


class ResolveCaselessPreviousEnrollmentsTest(TestCase):
    """The backlog resolver flags no-prior-case placeholders, backfills the
    single-candidate ones, and leaves 2+-candidate rows untouched for review."""

    def _case(self, client, *, status, day=0, closed_day=None):
        from datetime import timedelta
        from .models import Case, CaseType, ServiceAuthorizationStatus
        now = timezone.now()
        return Case.objects.create(
            case_id=uuid.uuid4(), client=client,
            case_type=CaseType.INTERNAL_SERVICE, case_status=status,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
            program_name="Medically Tailored Meals",
            case_created_at=now + timedelta(days=day),
            date_opened=now + timedelta(days=day),
            case_closed_at=(now + timedelta(days=closed_day)) if closed_day is not None else None,
        )

    def test_flags_backfills_and_leaves_ambiguous(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import (
            CaseStatus, Client, EnrollmentStage, EnrollmentVerification,
        )

        def _prev_and_survivor(client, surv_case):
            prev = EnrollmentVerification.objects.create(
                client=client, stage=EnrollmentStage.CLOSED,
                close_reason="case_replaced", case=None,
            )
            surv = EnrollmentVerification.objects.create(
                client=client, stage=EnrollmentStage.SERVICE_ACTIVE,
                case=surv_case, supersedes=prev,
            )
            return prev, surv

        # A: no distinct prior case -> FLAG as misinformation.
        a = Client.objects.create(client_id=str(uuid.uuid4()), first_name="A", last_name="A")
        a_prev, _ = _prev_and_survivor(a, self._case(a, status=CaseStatus.OPEN, day=10))

        # B: exactly one prior case -> BACKFILL onto the row + survivor.previous_case.
        b = Client.objects.create(client_id=str(uuid.uuid4()), first_name="B", last_name="B")
        b_prior = self._case(b, status=CaseStatus.CLOSED, day=1)
        b_prev, b_surv = _prev_and_survivor(b, self._case(b, status=CaseStatus.OPEN, day=10))

        # C: two prior cases -> AMBIGUOUS, untouched.
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="C", last_name="C")
        self._case(c, status=CaseStatus.CLOSED, day=1)
        self._case(c, status=CaseStatus.CLOSED, day=2)
        c_prev, _ = _prev_and_survivor(c, self._case(c, status=CaseStatus.OPEN, day=10))

        out = StringIO()
        call_command("resolve_caseless_previous_enrollments", "--apply", stdout=out)

        a_prev.refresh_from_db(); b_prev.refresh_from_db()
        b_surv.refresh_from_db(); c_prev.refresh_from_db()
        # A flagged, still caseless.
        self.assertTrue(a_prev.hidden_misinformation)
        self.assertIsNone(a_prev.case_id)
        # B backfilled to its single prior case; survivor tracks previous_case.
        self.assertEqual(str(b_prev.case_id), str(b_prior.case_id))
        self.assertEqual(str(b_surv.previous_case_id), str(b_prior.case_id))
        self.assertFalse(b_prev.hidden_misinformation)
        # C untouched + listed as ambiguous.
        self.assertIsNone(c_prev.case_id)
        self.assertFalse(c_prev.hidden_misinformation)
        self.assertIn(str(c.client_id), out.getvalue())

    def test_resolve_ambiguous_by_close_match(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import (
            CaseStatus, Client, EnrollmentStage, EnrollmentVerification,
        )

        # Survivor case created on day 10. Two prior candidates: one CLOSED on
        # day 10 (the case the survivor directly replaced) and one closed on day 5.
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="D", last_name="D")
        replaced = self._case(c, status=CaseStatus.CLOSED, day=2, closed_day=10)
        self._case(c, status=CaseStatus.CLOSED, day=1, closed_day=5)
        prev = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.CLOSED, close_reason="case_replaced", case=None,
        )
        surv = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.SERVICE_ACTIVE,
            case=self._case(c, status=CaseStatus.OPEN, day=10), supersedes=prev,
        )

        # Without the flag it stays ambiguous (no write).
        call_command("resolve_caseless_previous_enrollments", "--apply", stdout=StringIO())
        prev.refresh_from_db()
        self.assertIsNone(prev.case_id)

        # With the flag it binds the close-matching candidate.
        call_command(
            "resolve_caseless_previous_enrollments", "--apply",
            "--resolve-ambiguous-by-close-match", stdout=StringIO(),
        )
        prev.refresh_from_db(); surv.refresh_from_db()
        self.assertEqual(str(prev.case_id), str(replaced.case_id))
        self.assertEqual(str(surv.previous_case_id), str(replaced.case_id))

    def test_bind_override_resolves_ambiguous(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import (
            CaseStatus, Client, EnrollmentStage, EnrollmentVerification,
        )

        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="E", last_name="E")
        pick = self._case(c, status=CaseStatus.CLOSED, day=1)
        self._case(c, status=CaseStatus.CLOSED, day=2)
        prev = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.CLOSED, close_reason="case_replaced", case=None,
        )
        surv = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.SERVICE_ACTIVE,
            case=self._case(c, status=CaseStatus.OPEN, day=10), supersedes=prev,
        )

        call_command(
            "resolve_caseless_previous_enrollments", "--apply",
            f"--bind={prev.pk}={pick.case_id}", stdout=StringIO(),
        )
        prev.refresh_from_db(); surv.refresh_from_db()
        self.assertEqual(str(prev.case_id), str(pick.case_id))
        self.assertEqual(str(surv.previous_case_id), str(pick.case_id))


class PurchaseOrderPreviewHelpersTest(TestCase):
    """The PO preview exposes each member's snapshot ZIP + allergy labels so the
    Generate PO popup can filter/highlight by ZIP and allergy."""

    def test_schedule_zip_and_allergy_labels(self):
        from types import SimpleNamespace
        from api.services.purchase_orders import _schedule_zip, _schedule_allergy_labels

        s = SimpleNamespace(
            delivery_address="123 Main St, Apt 4, Brooklyn NY 11211",
            allergies=["peanut", "none", "peanut"],
        )
        self.assertEqual(_schedule_zip(s), "11211")
        labels = _schedule_allergy_labels(s)
        self.assertEqual(len(labels), 1)  # de-duped + 'none' dropped
        self.assertNotIn("none", [x.lower() for x in labels])
        # No address -> no zip; no allergies -> empty list.
        self.assertEqual(_schedule_zip(SimpleNamespace(delivery_address="")), "")
        self.assertEqual(_schedule_allergy_labels(SimpleNamespace(allergies=[])), [])


class BulkAssignFiltersTest(TestCase):
    """The bulk-assign popup filters keep a household when ANY member matches the
    menu type / each selected allergy and the delivery ZIP starts with the typed
    prefix, then cap to `limit` households."""

    def _enr(self, *, zip_code, members):
        from .models import (
            Address, Client, EnrollmentStage, EnrollmentVerification,
            MemberDietaryProfile, MemberStatus,
        )
        c = Client.objects.create(client_id=str(uuid.uuid4()), first_name="H", last_name="H")
        addr = Address.objects.create(client=c, type="temporary", zip=zip_code)
        e = EnrollmentVerification.objects.create(
            client=c, stage=EnrollmentStage.KITCHEN_ASSIGNMENT,
            program_name="Medically Tailored Meals", delivery_address=addr,
        )
        for i, (menu, allergies) in enumerate(members):
            mc = c if i == 0 else Client.objects.create(
                client_id=str(uuid.uuid4()), first_name=f"M{i}", last_name="H",
            )
            MemberDietaryProfile.objects.create(
                enrollment=e, client=mc, member_name=f"M{i}", menu_type=menu,
                food_allergies=allergies, status=MemberStatus.ACTIVE,
            )
        return e

    def test_filters_and_limit(self):
        from api.portal.views_members import _bulk_filter_enrollments
        e1 = self._enr(zip_code="11211", members=[("Kosher", []), ("Standard", ["peanut"])])
        e2 = self._enr(zip_code="10001", members=[("Standard", [])])
        e3 = self._enr(zip_code="11215", members=[("Halal", ["shellfish"])])
        enrolls = [e1, e2, e3]

        # Menu type: any member on Kosher -> only e1.
        self.assertEqual(
            [e.pk for e in _bulk_filter_enrollments(enrolls, {"menu_type": "Kosher"})],
            [e1.pk],
        )
        # "all" is treated as no menu filter.
        self.assertEqual(len(_bulk_filter_enrollments(enrolls, {"menu_type": "all"})), 3)
        # ZIP prefix 112 -> e1 + e3.
        self.assertEqual(
            {e.pk for e in _bulk_filter_enrollments(enrolls, {"zip": "112"})},
            {e1.pk, e3.pk},
        )
        # Allergy peanut -> only e1 (a member carries it).
        self.assertEqual(
            [e.pk for e in _bulk_filter_enrollments(enrolls, {"allergies": ["peanut"]})],
            [e1.pk],
        )
        # Combined menu + zip.
        self.assertEqual(
            [e.pk for e in _bulk_filter_enrollments(enrolls, {"menu_type": "Halal", "zip": "112"})],
            [e3.pk],
        )
        # Limit caps households (order preserved).
        self.assertEqual(len(_bulk_filter_enrollments(enrolls, {"limit": 2})), 2)
        # No filters -> all.
        self.assertEqual(len(_bulk_filter_enrollments(enrolls, {})), 3)


class PurgeHiddenMisinformationEnrollmentsTest(TestCase):
    """Purges flagged caseless terminal placeholders, re-points supersession
    chains past them, and never touches case-backed / non-terminal rows."""

    def test_purge_repoints_chain_and_guards(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import (
            Case, CaseStatus, CaseType, Client, EnrollmentStage,
            EnrollmentVerification, MemberDietaryProfile, MemberStatus,
            ServiceAuthorizationStatus,
        )

        client = Client.objects.create(client_id=str(uuid.uuid4()), first_name="P", last_name="Urge")
        case = Case.objects.create(
            case_id=uuid.uuid4(), client=client, case_type=CaseType.INTERNAL_SERVICE,
            case_status=CaseStatus.OPEN,
            service_authorization_status=ServiceAuthorizationStatus.APPROVED,
        )

        # Chain: O (older, kept) <- F (flagged placeholder) <- S (active survivor).
        older = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.CLOSED, case=case,
        )
        flagged = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.CLOSED, close_reason="case_replaced",
            case=None, hidden_misinformation=True, supersedes=older,
        )
        survivor = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.SERVICE_ACTIVE, case=case, supersedes=flagged,
        )
        # A cascade child on the flagged row.
        MemberDietaryProfile.objects.create(
            enrollment=flagged, client=client, member_name="P", menu_type="Standard",
            status=MemberStatus.ACTIVE,
        )
        # A flagged row that is NOT eligible (still SERVICE_ACTIVE) -> must survive.
        live_flagged = EnrollmentVerification.objects.create(
            client=client, stage=EnrollmentStage.SERVICE_ACTIVE,
            case=None, hidden_misinformation=True,
        )

        call_command("purge_hidden_misinformation_enrollments", "--apply", stdout=StringIO())

        # Flagged terminal placeholder gone; its cascade child gone.
        self.assertFalse(EnrollmentVerification.objects.filter(pk=flagged.pk).exists())
        self.assertFalse(MemberDietaryProfile.objects.filter(enrollment_id=flagged.pk).exists())
        # Chain re-pointed: survivor now supersedes the older kept enrollment.
        survivor.refresh_from_db()
        self.assertEqual(survivor.supersedes_id, older.pk)
        # Guards: older (case-backed) + non-terminal flagged both survive.
        self.assertTrue(EnrollmentVerification.objects.filter(pk=older.pk).exists())
        self.assertTrue(EnrollmentVerification.objects.filter(pk=live_flagged.pk).exists())


class MenuCarryRegressionAuditGuardTest(TestCase):
    """list_menu_carry_regressions must only flag a survivor that is the member's
    LIVE enrollment. A disregarded/closed placeholder that supersedes the now-
    active (correct) enrollment must NOT be reported (the IZABELLE false positive)."""

    def _enr(self, client, stage, menu, *, supersedes=None, close_reason=""):
        from .models import EnrollmentVerification, MemberDietaryProfile, MemberStatus
        e = EnrollmentVerification.objects.create(
            client=client, stage=stage, supersedes=supersedes, close_reason=close_reason,
        )
        MemberDietaryProfile.objects.create(
            enrollment=e, client=client, member_name=client.first_name,
            menu_type=menu, status=MemberStatus.ACTIVE,
        )
        return e

    def test_live_reset_flagged_but_disregarded_placeholder_not(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import Client, EnrollmentStage

        # Real regression: ACTIVE Standard survivor supersedes a closed Kosher source.
        a = Client.objects.create(client_id=str(uuid.uuid4()), first_name="Real", last_name="Reg")
        src_a = self._enr(a, EnrollmentStage.CLOSED, "Kosher", close_reason="case_replaced")
        self._enr(a, EnrollmentStage.SERVICE_ACTIVE, "Standard", supersedes=src_a)

        # False positive: DISREGARDED Standard placeholder supersedes the ACTIVE
        # Kosher enrollment (member's real menu is correctly Kosher).
        b = Client.objects.create(client_id=str(uuid.uuid4()), first_name="False", last_name="Pos")
        active_kosher = self._enr(b, EnrollmentStage.SERVICE_ACTIVE, "Kosher", close_reason="case_replaced")
        self._enr(b, EnrollmentStage.DISREGARDED, "Standard", supersedes=active_kosher)

        out = StringIO()
        call_command("list_menu_carry_regressions", stdout=out)
        text = out.getvalue()
        self.assertIn(str(a.client_id), text)      # genuine regression is flagged
        self.assertNotIn(str(b.client_id), text)   # disregarded placeholder is NOT
