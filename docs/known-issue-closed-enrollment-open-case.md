# Known issue: terminal enrollment on an OPEN internal-service case (service limbo)

Status: **to fix** (lifecycle). Surfaced while auditing the Data page Company
Status filter. This is NOT a Data-page bug — the Data page correctly flags these
members; the root cause is in the enrollment lifecycle.

## Symptom
A member has an **open + (usually) approved** internal-service (meal/box) case,
but their governing **enrollment is in a terminal stage (`closed`/`cancelled`)**
and they are **not in any PO / have no kitchen** → they are not being served
despite an active, approved case. In the timeline these members often show
`Governing Case Changed` (+ sometimes a spurious `Service Reactivated`, see the
separate reactivation fix).

## Root cause (from prod data)
The typical history:
1. Earlier internal-service case(s) were **denied → closed**. The closure
   full-stop **closed the member's enrollment**.
2. Later a **new open + approved** internal-service case arrived and became the
   governing case.
3. The enrollment on/for that new governing case was **left `closed`** — the
   lifecycle did not resume (or re-create) an enrollment onto the new open,
   approved governing case.

So the reactivation/governing-case-change path does not resume a terminal
enrollment when a new open+approved governing case supersedes a denied/closed
one. Verified members should have been resumed to Kitchen Assignment → Service
Active; unverified members should have been re-queued for verification.

## Detection query
```python
from api.models import EnrollmentAnalytics as EA
EA.objects.filter(case_type="internal_service", case_status="open",
                  stage__in=["closed", "cancelled"])
```
(Read-model shortcut. Authoritative check: client has an OPEN internal-service
case but their active/governing EnrollmentVerification is closed/cancelled.)

## Required action to get them into service
- **Verified + approved (needs RESUME):** resume/re-drive the enrollment on the
  open approved case → Kitchen Assignment → Service Active (the reactivation path
  should do this automatically).
- **Unverified + approved (needs RE-VERIFICATION):** run the verification wizard
  on the new open case, then kitchen assignment.
- **Denied auth:** correctly `unable` (expected — not part of the bug to fix,
  listed for completeness).

## Proposed fix location
`api/services/lifecycle.py` — the reconcile / governing-case-change path
(`reconcile_internal_service_authorization`, `_carry_service_and_activate`, and
the reactivation branch). When a new OPEN + APPROVED governing internal-service
case supersedes a prior denied/closed one, resume the member's terminal
enrollment (or create a fresh one) instead of leaving it `closed`.

## Affected members (local snapshot; re-run the query on prod for the live set)
Total: **26** — open internal-service case + terminal enrollment.

### Verified + approved — need RESUME (should be serving)
- 36f530a0-f99a-4d7f-a31f-aed32a821c4c
- e9c530ce-2572-48b5-b52e-de8723d31ff6
- faff53d1-21c6-4257-82fd-587f575736e6
- 1d11102a-514d-4b8b-910f-1aaa134d6434  (currently `active` via in_any_po)
- 0b91195c-93d6-4f45-bb31-c0f06146d96f  (currently `active` via in_any_po)
- 59569a8a-9b65-4487-9098-b2841e322e39  (currently `active` via in_any_po)
- 947bd920-1b9d-47f9-8e20-b47a4da6bbe3  (currently `active` via in_any_po)
- db4ba05c-1798-4ae4-9d56-61248216d3c4  (currently `active` via in_any_po)

### Unverified + approved — need RE-VERIFICATION
- a016f8f4-9327-4cd9-9f2c-1cf91a39a9e7
- eb929069-f8cd-4ed9-bdd8-ccba12062a4d
- fbd5fc1a-f3bb-4ddd-9e81-03d8aacad9f9
- c1e33e4a-049c-42d6-aae1-f93697b121a1
- afee0aff-899d-4b5e-9583-4107a7707482
- 223fc4e0-8fc5-4f19-89c8-a87412adb71d
- 1df44a12-0526-4309-9c13-161090b15b4e
- d74eb8f9-6d08-42ce-962d-c295b985082f

### Denied / never-requested auth — `unable` (expected, listed for completeness)
- d4040c96-c22c-417f-a887-a10d9fa5f307  (denied)
- 9553adb3-c3ce-4072-bd8b-1419e322b081  (denied)
- 69e2f2e3-ed5a-42aa-aa8d-1f436fe9efb7  (never_requested)
- dfee396a-3853-47ae-a145-927f586f6647  (denied)
- 533a0c8e-183b-410c-a0cc-8fe42cd1f2be  (never_requested)
- bef6e3ca-ebc7-42e1-ac91-2c04f8eb08df  (denied)
- 3ebfb548-ae80-4af7-9183-ea5c971797e1  (denied)
- da22674a-5ef9-4015-aaba-b8f5c2f88c4f  (denied)
- 3c80fc75-450f-4397-9685-007e6e3dc8ff  (denied)
- 7c6f28c4-fe29-407a-acf9-a7bf07aed43b  (denied)
