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

## PHASE 1 — the Assessment Order

Planned in **`docs/housing-assessment-order-plan.md`**. An agent runs a wizard on
the member profile, once per member, producing an Assessment Order a vendor
executes at the home: questionnaires signed on site into a locked PDF, plus
documents, proof images and findings. The findings then drive the Home Remediation
work orders, which link back to the order.

Two things there that differ sharply from the food side, and are worth carrying in
mind before reading the rest of this document:

- the order is **per MEMBER, not per case**, and there is no request/approve cycle;
- it is **append-only** -- "keep the record, do not rebuild". No reconcile, no
  supersede-and-replace. The food side's rebuild machinery is what forked 149
  enrollments across three families, so the absence is deliberate.

Six open questions are listed there; Q1 (one order per member vs one ACTIVE per
member) and Q2 (what "locked after each signature" means technically) change the
schema, so they want settling before implementation.

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

### STEP 2 DONE -- housing resolution (`api/services/housing.py`)

```
housing_service_case(client)       the governing assessment, or None
housing_assessment_cases(client)   the member's OWN EEA cases, most-governing first
housing_work_orders(client)        the Home Expense Assistance/Repairs cases
is_assessment_case / is_work_order_case / is_housing_case
has_open_housing_case(client)
```

- `GOVERNING_SERVICE_TYPES` is an **allowlist** holding only EEA, rather than
  excluding HEAR. A housing service added later therefore cannot start governing
  by default; it has to be admitted deliberately.
- **No household fallback**, unlike food's `governing_service_case_for_display`.
  A dwelling is one property, so the case belongs to the primary; a dependent
  resolving to a housing case would be wrong. Tested.
- `is_work_order_case` is its own predicate, not "housing and not assessment": a
  housing case resolving to NEITHER service type is a classification gap worth
  seeing, not something silently treated as a work order.
- Classification uses our curated program mapping FIRST (an agent can correct it
  in Settings > Programs), then falls back to the case's own `service_type`
  string as sent by Unite Us, matched against the choice labels.

### STEP 2b DONE -- the verification can never touch a housing case

Three paths could bind a food verification to a housing case. Step 1 closed the
picker only:

1. `has_open_internal_service_case` gates ENTRY to the wizard and counted any
   internal-service case, so a HOUSING-ONLY member passed it. It also feeds the
   verify button, `can_request_verification` and the exports.
2. `MemberVerificationCreateView` takes `case_id` from the REQUEST BODY and
   checked only `case_type == INTERNAL_SERVICE`.
3. `governing_internal_case` (164 call sites) was never scoped -- and it is the
   FALLBACK the wizard binds to when no `case_id` is posted. So with 1 and 2
   closed, an approved assessment (newest, highest-ranked) still became the
   "governing" case and the verification was written against it.

What found #3 was an API-level test asserting no `EnrollmentVerification` ends up
bound to a housing case. It failed on the first run. Testing the helpers alone
would have passed and shipped the hole.

### UI DONE

```
stage bar        F / H badge per governing case; housing chip = amber + House,
                 "EEA" with the full name on hover
Cases tab        same F / H badge; amber EEA chip before the scope label;
                 new "Home Remediation" tab (housing WORK ORDERS, matched on
                 service_type_code -- a case_type filter cannot separate them,
                 both are internal_service); house-with-wrench icon
```

What earns a row on the bar differs BY TYPE, and this was got wrong once before
being corrected: FOOD keeps its non-governing OPEN cases, because they carry
"Duplicated", "Conflicting" and "Reauthorization - Waiting" -- a Conflicting row
is how an agent SEES two competing food cases, the precondition of the fork loop
that rewrote 149 enrollments. HOUSING shows only its governing assessment,
because its non-governing cases are work orders (9 for the first member) and
they buried everything else.

### Both entry points verified (2026-09-16)

**Extension**, against localhost: MIRIAM ISRAEL (`fad1f448`) landed with 1
assessment + 9 work orders + live food service, resolving as one governing case
per type and a single FOOD enrollment.

**CSV import**: a hand-made file proved the classification end to end -- the
Home Remediation and Dwelling rows imported and classified as work order /
assessment, an `External Services` row was REJECTED, and no enrollment was
created for either housing case. Note the first two attempts "passed" for the
wrong reason; see the five-gate note in `AGENTS.md`.

Correction to an earlier claim in this document: reclassifying the programs is
**not** what opened the CSV importer, because those cases never came through it.
The gate that had been rejecting them on every other path is `CaseSerializer`'s
`EXTERNAL_SERVICE` backstop. The housing cases existed in Unite Us since August
(`case_created_at`) but were only STORED once the programs became Internal
Services (`added_to_system_at = 2026-09-16`).

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

**CLOSED: the operator states re-assessment cases never occur**, so a member never
holds two live assessments and the situation does not arise. Housing therefore
uses `governing_case_key` UNCHANGED -- "the same rules we did for food". Recorded
in `api/services/housing.py` as known behaviour so nobody "fixes" it blindly.

One observed consequence worth knowing, from the CSV test: authorization favour
ranks above recency, so an assessment with a **blank** auth status (rank 0) loses
to any approved one, even one created five days earlier. KAMARI GREENE's real
assessment has a blank auth status.

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
