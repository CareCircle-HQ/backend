# Known issue: orphaned delivery orders on members with NO internal-service case

Status: **to investigate (data integrity)**. Surfaced auditing the Data page
"No Case Created" bucket — those members showed a Delivery Status ("ready for
delivery" / "cancelled") despite having no case.

## Symptom
A member is classified **No Case Created** (no internal-service case, own or
household) yet has **DeliveryOrders tied to Purchase Orders** — i.e. they were
put into POs and served. On the Data page this shows up as `no_case` rows with a
non-blank `current_delivery_status`.

## Root cause
Their **internal-service (food) case was deleted/removed** from the system (e.g.
a Unite Us re-sync or cleanup), but the **DeliveryOrders were left behind
(orphaned)**. `_derive_delivery` reads the member's own delivery orders
regardless of case, so the delivery status still shows. Their eligibility /
navigation cases usually survive — only the internal-service case is gone.

Local snapshot: **79 members**, **587 orphaned DeliveryOrders**. Of the 79:
74 have only an eligibility case, 2 eligibility+navigation, 3 have zero cases;
**none** have an internal-service case.

NOTE: Eligibility (eligible/ineligible) on no_case members is NOT part of this
bug — eligibility is screening/coverage-derived and expected for every member.
Only the DELIVERY data on a no-case member is the anomaly.

## Detection query
The Data page read model now BLANKS delivery fields for no-case members (so the
Data page no longer shows delivery status on a No Case Created row). Detect the
orphans authoritatively from the live tables instead:
```python
from api.models import Client, Case, CaseType, DeliveryOrder
served = set(DeliveryOrder.objects.values_list("member_id", flat=True))
with_is = set(Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
              .values_list("client_id", flat=True))
orphans = served - with_is  # have delivery orders but NO internal-service case
```

## Suggested action
- **Ops/data:** investigate why the internal-service case was deleted while
  delivery orders remained; either restore/relink the case or clean up the
  orphaned DeliveryOrders.
- **Data page (optional):** we could blank the delivery fields when a member has
  no internal-service case, but leaving them visible usefully surfaces the
  anomaly. Deferred pending the ops decision.

## Affected members (local snapshot; re-run the detection query on prod)
- 95ecf498-5a0b-4e30-ac91-dfb24106799a
- 31ff307c-1d7e-4508-96f2-134f65b64dd4
- 5601b8a8-abfb-401d-923e-2c22d522965f
- 6145200a-9227-4e8e-aed2-ec6f3070188d
- c3c81cc2-6728-4a37-9866-7d572463271b
- aeaead65-8b97-4e58-975a-a332200ef51a
- af10d372-fb54-4540-9a7f-ba50cd05016e
- 1a83f17d-2e24-4cab-994c-7afe2f78705e
- 577afda2-694d-4571-af64-263e5800fb64
- c3aca4df-d417-456d-90fc-485dfecf3765
- 9c91fde5-e45a-45a4-bc39-845bd827dc70
- 581da84f-78e2-4b03-a105-5611ac00c5b6
- 58f2f618-3040-4493-8ae6-8a1e238773cf
- fd1dba19-1fa7-4a7e-a325-6b6ae6716178
- f1ca457a-30ff-4e2c-b2b9-f9e167d225f9
- 623dc3a5-2711-4a51-8b69-611a3a42f18e
- c3012b4a-81b6-4017-a6b0-a7e082da675e
- 69278ff4-f91e-4cbf-b6ec-9954a500a464
- 16d59c22-f7f1-48a9-a85b-155211d21f48
- 75effb2e-e4d7-4048-ade3-d09821b8b4ac
- 3e33a495-fc1a-4ec8-82ec-daed7fa1ad09
- db6ed47b-089c-469f-88b8-32b47dd88aef
- 9ee25d40-65bd-4e5c-a617-52afbcd52698
- 3f21805e-afc4-4455-b2f8-19b87b3ab6c6
- cdbf269a-7e66-4fad-b1c8-b9e7b43827bb
- be354c32-774c-47c3-90e0-74394b6fe9a8
- 6432b379-785b-4a2a-9d0b-23d14fbcdb6a
- dd3deacf-37f0-4025-8ead-0820714661af
- 5eda8435-ced1-47a7-903b-9ef6537b1c1d
- 79fce169-16fd-4a3c-9f1f-e8b5036191ba
- 956aec14-6bbe-4986-afea-ba10ee95894a
- db23600a-6372-4350-b683-6efcfc8eec86
- 07878d47-96de-4f5a-a492-cf40812f97d4
- 73c67e6d-c96c-4d00-8780-474d1fb610ff
- cefd3317-60a5-43c4-9585-83cf2acf40a8
- 4a6da1ab-c56f-4fd8-8241-fd7700fb3308
- c35a8111-39a6-4c27-bbf0-e988fab9827a
- 643d6163-e6ba-44f3-88bc-dd4d87ba9c61
- a6da3feb-b51e-48b2-b23d-221ba9e6a1ec
- 31b7df6b-2bf6-4771-9d54-dd37b769ddd9
- d3a6e2f3-4e15-4a53-8e46-ec1938f1d324
- b099b94a-a54d-4c02-9997-b0368c0e2bb5
- 110e6427-9eb4-4e97-ad7a-4d4e206f6d95
- 88181e8e-b320-4b90-ade0-e1d7bca3dedb
- 7de8f770-cafe-4f85-a8b1-457dfa394dd1
- 019d0d4d-b94d-4320-a4b9-8ae70a60b1f0
- f18e2edd-d797-4a7a-812e-fc0cba449ac2
- 4941bb53-77b8-4fd1-9392-fc5d390ec8a5
- f86e3e1f-68a8-4a38-acda-135c47e848c1
- 903cd917-5462-475a-b4bf-1430584d462a
- 55620822-05b1-47b9-a43f-7d276d978b71
- 3005eb95-1c84-4f2f-adcf-95ed74c8bff6
- 6d1882da-be84-45c7-8184-b30144a520e8
- 77764fc3-a523-46d8-9be6-0c72515d56cf
- 6bf99c6a-fdc9-478d-babc-e1b836ab2f9f
- cf804321-4588-4ff2-acd1-209dc38a7431
- 156bcf38-19bf-461d-a265-e47c31c8d3fc
- 4cbb51b7-da0e-48ec-9eb7-f5330d9732b3
- ef00d4c4-db97-4dde-9311-8a94f89ff32e
- 8bdb0b8f-40ef-483c-ae1a-11801c202027
- 4a66fe9e-1b44-4302-b0d8-68e77b6ebb40
- c70595a0-4498-4e4d-8afa-b28777dfc50f
- 3a02691c-2496-4f1b-b8ca-979f0a0008aa
- 4678d09e-6ffd-423c-a772-29df5f1ac4e0
- 5cc041ee-9e0c-40ef-85d7-f66c5b112da3
- 8ae31d84-b871-403e-8245-48eaab6d2830
- f73c5b03-c448-48ec-b746-02dfdc6b81a2
- e3341a3a-0b25-40d8-b6ff-4da61a7ef373
- c792f3fc-af79-41f1-91b5-3283c11bc38f
- 71f013e9-f01a-4b99-b98b-dbb81d0ff933
- 68e3114b-42b0-49da-bbe6-2124df3e19d4
- 76b040e4-c9b9-44e5-931d-8615855e9d5c
- 57a2b330-b0be-43a2-9e60-de84fad3c94f
- e97d2e63-28c9-456f-be96-d9d9e789646e
- 090efddb-2d66-48d7-97a5-94c5b438efcb
- 758051c5-c6a1-470b-8358-c75e7b69fb55
- d98cc766-e7d0-4771-a538-211f9c8a7d2e
- 16a96722-036a-42c1-be48-df5fb91145fc
- d1f51796-93f6-49b7-9f99-f8bd29e04bcd
