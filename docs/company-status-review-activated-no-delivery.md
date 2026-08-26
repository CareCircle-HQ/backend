# Company Status "review" bucket — activated but not delivering (TO FIX)

Status: **temporary bucket**, excluded from Pending. Needs a real resolution.

## What lands in `company_status = "review"`
Set in `api/services/enrollment_analytics.py::_company_status` (after Active,
before it would otherwise be Pending). Two groups, deliberately kept OUT of
Pending until we decide their correct home:

1. **Activated but not delivering** — an enrollment at `service_active` /
   `service_complete` with an OPEN governing case, valid auth, verified, but
   **NO live delivery calendar** (no non-cancelled `DeliveryOrder` in a PO). The
   Active rule (Option B) requires an active delivery calendar, so these fall
   through; and they're *past* Kitchen Assignment, so they don't fit the Pending
   definition ("open + auth approved/pending + before kitchen assignment").
   These are the ~74 members flagged on 2026-08-25.

2. **Non-actionable authorization** — auth not in (approved, pending): e.g.
   `never_requested` (~5 members). Excluded from Pending per the agreed
   definition.

## Why they're in limbo
"Activated but not delivering" is a real data question: either they *should* have
a delivery calendar (a scheduling gap to heal) or they were activated in error /
their service ended without the enrollment being closed. Deciding that is the
fix; for now they're quarantined in `review` so they don't inflate Pending.

## How to pull the list for review
```python
python manage.py shell -c "
from api.models import EnrollmentAnalytics as EA
rev = EA.objects.filter(company_status='review')
print('review total:', rev.count())
print('  service_active/complete (activated-no-delivery):',
      rev.filter(stage__in=['service_active','service_complete']).count())
print('  non-actionable auth:', rev.exclude(auth_status__in=['approved','pending']).count())
for r in rev.filter(stage__in=['service_active','service_complete']).values_list('client_id', flat=True):
    print(r)
"
```

## Candidate fixes (decide later)
- **Heal deliveries**: if they should be served, (re)generate their delivery
  calendar → they become Active.
- **Close/terminalize**: if their service actually ended, close the enrollment →
  they leave the open-case buckets.
- **Keep as an explicit "attention" state** on the Data page if this is a
  recurring, actionable operational category.
