# Housing Programs — plan and status

## Goal

Process housing programs, i.e. bring housing cases into the CRM. The three
Dwelling Assessment / SOW Development programs are the housing service **we
deliver**, so they are Internal Services — of a different TYPE.

**The rule, from the operator:** one governing internal-service case **per type**.
If type = Food, everything behaves exactly as it does today. If type = Housing, a
new workflow applies (to be designed).

Everything else stays as it is: the 7 programs under the active
`ProgramMainCategory` "Housing" (Asthma Remediation, Tenancy Sustaining Services,
Rent/Temporary Housing Rent Payment Assistance, ...) are referrals **out** and
remain External.

## DONE (2026-09-15/16)

| Migration | What |
|---|---|
`0261` | `ActiveProgram.CaseType` += `HOUSING` (alongside `FOOD`, `TRANSPORTATION`) |
`0262` | the 3 programs: `External Services` -> `Internal Services`, `food` -> `housing`, `Food` -> `Housing` |
`0263` | `ActiveProgram.ServiceType` += `ENVIRONMENTAL_EXPOSURE_ASSESSMENT` |
`0264` | the 3 programs: `service_type = environmental_exposure_assessment` |

Also:

- **`ProgramMainCategoryAdmin`** gained `is_active` in `list_display`,
  `list_filter` and `list_editable`. That flag marks a program area as being
  processed and is where the available types live -- "Housing" is currently the
  ONLY active category of 46.
- **Meals/Boxes metrics now exclude non-food types.** `UnmappedProgramNames` and
  `ServiceTypeBlankWithCase` are about meal/box reporting; a housing program has no
  product kind BY DESIGN. Before the exclusion the 3 Dwelling programs alone put
  `UnmappedProgramNames` at 2 -- an alarm that could never clear.
  `unmapped_program_identifiers()` now has ONE definition, shared by `collect()`
  and the management command (the command's private copy had already failed to
  pick up the exclusion).

### The switch that turned import ON

Worth being explicit, because it is not obvious from the diff:

```python
case_in_import_scope(service_type, program_name):
    scope = meal/box subtype  OR  program's category in _IN_SCOPE_CASE_CATEGORIES
_IN_SCOPE_CASE_CATEGORIES = {internal service(s), eligibility, reauthorization,
                             care management, navigation, screening}
```

`External Services` is NOT in scope, so the importer **silently skipped** these
programs. Reclassifying them to `Internal Services` is what turns housing case
import on. Verified both ways: the Dwelling programs are now in scope, and
`Tenancy Sustaining Services` still is not.

One housing case already exists and proves the path end to end:

```
KAMARI GREENE | case_type=internal_service | status=open
program_name  = 'Dwelling Assessment & SOW Development ... - Brooklyn'
service_type  = 'Environmental Exposure Assessment'
```

### A decision, with its cost pinned by a test

`Environmental Exposure Assessment` was deliberately NOT added to
`api.serializers.INTERNAL_SERVICE_SUBTYPES`. That frozenset means "IS our meal/box
service": it forces `CaseType.INTERNAL_SERVICE` regardless of program AND drives
`purge_out_of_scope_cases`, so a housing subtype there would blur its meaning.

Cost: a housing case arriving with a **blank or unmatched `program_name`** is out
of scope and gets skipped. That is the one scenario that would justify revisiting.

## REQUIRED NEXT — one governing case PER TYPE

**This must land before housing volume arrives.** Governing-case resolution is
currently per MEMBER, not per type, so a member holding both a Food case and a
Housing case puts them in direct competition. That is exactly the shape that forked
**149 enrollments across three families** in the week of 2026-09-14 (see
`replace_enrollment_for_case_change`, commit `62fc4a5`): two open internal-service
cases, each reconcile rewriting the other's enrollment, unbounded churn.

Since our own members are the ones getting housing services, Food + Housing on one
member is not an edge case -- it is the expected state.

### Call sites that assume ONE internal-service case per member

To be mapped precisely before choosing an approach. Known starting points:

```
api/portal/serializers.py   governing_service_case_for_display(client)
api/services/lifecycle.py   governing_case_key()
                            replace_enrollment_for_case_change()
                            _full_stop_close_out()
                            _primary_enrollment()
api/services/orders.py      active_enrollment()
api/services/enrollment_analytics.py  build_row() -- case_* columns, company_status
api/serializers.py          derive_case_type() / _CATEGORY_TO_CASE_TYPE
```

### Design question, undecided

How does a `Case` carry its type at query time?

- **Derive** from `program_name` via `ActiveProgram` on each call. No migration,
  but `derive_case_type_from_active_program` is an UNCACHED query per call, and
  governing-case resolution runs everywhere -- including the 76k-row analytics
  rebuild. Would need caching.
- **Store** a discriminator on `Case` (e.g. `service_domain`), populated on import
  and backfilled. Costs a migration; makes the type filterable, indexable, and
  available to the read model and the AI query agent.

Leaning **store**, for the same reason `EnrollmentAnalytics` exists at all.

### Also to settle

1. **Do housing members need an enrollment and a service lifecycle at all**, or
   are these cases tracking-and-reporting only? Everything downstream depends on
   this. If they do get enrollments, they must not enter kitchen assignment,
   delivery calendars or Purchase Orders.
2. **`DeliveryGapsNoPlan`** requires `kitchen__isnull=False`, so a housing
   enrollment without a kitchen will not appear -- confirm that holds once the
   workflow exists.
3. **The housing workflow itself**: what are the stages, what does "service
   active" mean for a dwelling assessment, and what closes it?

## Waiting on

**Example clients with housing cases**, to design the workflow against real data
rather than assumptions. Two things to check the moment they arrive:

- does any of them ALSO hold a food case? That makes the per-type competition live
  rather than theoretical,
- what stages, statuses and authorization fields do their cases carry, and do they
  fit the existing enrollment stages or need their own.
