import uuid

from django.test import TestCase

from .models import Client, Insurance, RecordStatus
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
