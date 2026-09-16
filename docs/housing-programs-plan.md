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
`0265` | `ActiveProgram.ServiceType` += `HOME_EXPENSE_ASSISTANCE_REPAIRS` |
`0266` | the 15 `Home Remediation - <device> - <borough>` programs -> Internal Services / housing / `home_expense_assistance_repairs` / Housing |

So TWO housing services are ours now, 18 programs in total:

```
Dwelling Assessment & SOW Development - ... - {Bk,Mn,Qns}    3   Environmental Exposure Assessment
Home Remediation - {Air Conditioner, Air Filtration Device,
                    De-humidifier, Heater, Humidifier}
                  - {Bk,Mn,Qns}                             15   Home Expense Assistance/Repairs
```

Settings > Programs builds its service-type dropdown straight from
`ServiceType.choices`, so adding the enum value is what puts it in the UI -- there
is no separate list to maintain.

0266 matches the `Home Remediation - ` PREFIX rather than 15 exact names, since
they share a strict scheme. The trailing `" - "` is load-bearing: one of the 7
referral-only programs under the active Housing category is "Home Remediation
Assistance: Ventilation Improving Systems", and without it a referral programme
would silently become our own service. There is a test for that collision.

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

## Governing case per TYPE — design agreed

Answers from the operator:

1. **Housing gets its OWN enrollment model** — not `EnrollmentVerification`.
2. **Individual only, tied to the PRIMARY.** Food can be household-scoped with
   several members; a housing case cannot. Specific consequence:
   `governing_service_case_for_display` deliberately falls back to the HOUSEHOLD's
   case for a dependent so "every household member shows the same authorization
   instead of a blank" — correct for food, WRONG for housing. Housing resolution
   must use the member's own case, with no household fallback, ever.
3. **All work orders hang off the same governing housing case.**
4. **Always the governing case.** A re-assessment is not a second live case: if the
   assessment could not be performed and the authorization window expired, a NEW
   EEA case is created.
5. **Derive the type from `program_name`** via the ActiveProgram table.

### The eligible-set rule

An allowlist per type rather than a count, so "one governing case per type" falls
out by construction:

```
food     -> {medically tailored meals, produce prescription/voucher}
housing  -> {environmental exposure assessment}          <- ONLY
```

`Home Expense Assistance/Repairs` is an internal-service housing case but is
absent from the allowlist, so it can never govern — it is a WORK ORDER. The
existing `governing_case_key` ranking then applies unchanged WITHIN each type.

### The Unite Us flow

```
screener creates  Environmental Exposure Assessment (EEA)   governing housing case
      v  verification (a NEW kind — deferred)
      v  creates an ASSESSMENT ORDER
      v  vendor attends the home and inspects
      v  inspection results
creates  Home Expense Assistance/Repairs (HEAR) cases       = WORK ORDERS
```

## STILL REQUIRED — the rest

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

### Decided: derive, with an in-process cache

Derive from `program_name` via ActiveProgram. The performance objection (an
uncached query at hundreds of resolution points, including the 76k-row rebuild) is
answered by caching the small map rather than by adding a column -- see STEP 1
below.

The read model should still STORE the derived type as a column so the Data page
and the AI query agent can filter on it. Derive for logic, store for reporting.

### STEP 1 DONE -- the food-scoping guard

Every governing-case resolver was
`[c for c in client.cases.all() if c.case_type == INTERNAL_SERVICE]` with no type
filter, while `governing_case_key` ranks by authorization favour, then open, then
recency. So a freshly APPROVED housing assessment would have OUTRANKED an older
approved meals case and taken over the member's food service -- verification,
kitchen assignment, delivery calendar, Purchase Orders. The default behaviour the
moment the first housing case imported, not an edge case.

`api/services/catalog.py` now owns the derivation:

```
program_service_domain(name)  -> 'food' | 'housing' | 'transportation'
case_service_domain(case)     -> derived from case.program_name
is_food_case(case)            -> the filter every food resolver applies
non_food_program_q()          -> iexact Q for SQL-side exclusions
```

- **An UNKNOWN or blank program defaults to FOOD.** Load-bearing: every
  internal-service case predating housing is food, so an unrecognised program must
  behave exactly as today rather than silently dropping out of the food pipeline.
- The program -> type map is an in-process `lru_cache`, cleared by a
  `post_save`/`post_delete` signal in `api/apps.py`, so moving a program between
  Food and Housing in Settings takes effect without a restart.
- `non_food_program_q()` matches `iexact` per name rather than `__in`: a
  case-sensitivity near-miss fails in the DANGEROUS direction, a housing case
  slipping through as food.

Scoped: `internal_service_case`, `internal_service_cases` (the verification
picker), lifecycle's `_internal_service_cases`, the deferred-extension check, and
the prior-case lookup in the replace path. Verified on a production clone --
19,998 internal-service cases derive as food, 2 as housing, food governing cases
unchanged.

**NOT yet scoped:** the ~90 reporting/count sites (dashboards, exports). They
would include housing in totals, which is visible rather than corrupting, so they
follow once we know how housing should be counted.

### Open: does a re-assessment actually take over?

`governing_case_key` ranks authorization favour FIRST:

```
APPROVED / NOT_REQUIRED  4
PENDING                  3
DENIED / NEVER_REQUESTED 2
EXPIRED                  1
```

If a lapsed EEA's status flips to EXPIRED, the new PENDING EEA wins -- what rule 4
wants. But it may NOT flip: the analytics code already warns that "window lapsed
-> needs reauthorization; raw status can still read 'approved', so we key off the
computed program_status". If Unite Us leaves it `approved` after
`approval_ends_at` passes, the DEAD assessment (rank 4) keeps governing over the
new one (rank 3) and the re-assessment never takes over.

For food that is deliberate -- it keeps a meals case governing through a
meals->boxes switch. For housing it is a bug, so housing's ranking wants WINDOW
AWARENESS: an approved authorization whose window has passed should rank below
pending.

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
