# Service Tracker — rules and actions

The rules behind the **Service Tracker** on a member's Overview tab
(`api/services/service_tracker.py`). One entry per rule: what it reads, what it
decides, what an agent is expected to do, and how many members it touches.

Figures come from the local clone (production restored 2026-09-22). "Live case"
means `open`, `managed`, `pending_authorization` or `off_platform` throughout.

**Status key** — ✅ implemented · 🟡 agreed, not built · ❔ open question

---

## 0. The records every rule reads

| | |
|---|---|
| **Governing screening** | the member's MOST RECENT screening that lists services |
| **Governing assessment** | the member's MOST RECENT assessment that lists services |
| **Governing case** | `internal_service_case()` — the existing helper |
| **Borough** | the member's **ENROLLED** `Public Health Solutions - <Borough> NY1115 Enhanced HRSN Services` coverage |

Anything earlier is **invalid**. A new assessment is a re-determination, not an
addition.

⚠️ **No governing record means NO VERDICT.** Silence is not evidence.

```
members with a live internal-service case          17,056
  ... with no assessment at all          34.8%     ~5,941    cannot be judged
  ... with no screening at all            4.7%       ~795
```

⚠️ **The borough comes from the COVERAGE, never the address.** An address-based
check produced false positives — one member had all three cases flagged because she
lives in Manhattan while her coverage, and her cases, are Brooklyn. Switching to
coverage cut mismatches from 36 to 9 per 500 members.

Cost of that choice: only an **enrolled Enhanced HRSN** row counts. Of 3,000 members
with any social care coverage, 1,799 have none — the borough check is silent for
them. 1,491 of those hold an enrolled *Screening and Navigation* plan naming a
borough, which would answer most of them if the rule were ever widened. ❔

---

## Phase 1 — the gateway ✅

```
Screening Intake  →  Eligibility Assessment  →  Core Eligibility
```

The domains a member was screened for are **derived from the service names** —
screening records store `Asthma Remediation (Housing)`, not `Housing`. Two of 25
values carry no suffix (`Pre-tenancy Services`, `Cooking Supplies`) and fall back to
a keyword.

We screen for four domains and act on **two**: Food and Housing. The others are
shown as "not served" so the list does not look incomplete.

---

## Rule 0 — Care Management ✅

**If** the governing assessment includes `Enhanced Care Management (Level 2)`
**then** the member needs **both**:

| Row | Requires |
|---|---|
| Care Management Case | `service_type = Social Service Case Management` **and** `case_type = navigation` |
| Eligibility Case | `service_type = Social Service Case Management` **and** `case_type = eligibility` |

⚠️ **The care management case has `case_type = "navigation"`.** The programme
category says Care Management; the case type says navigation. The split is exact
across all 165,908 cases, so `case_type` is the reliable discriminator.

**Action:** open the missing case in Unite Us. The row names the programme.

```
live care management case   23,818 members
live eligibility case       54,345
both                        23,091
eligibility only            31,254    ← what the second row exposes
care management only           727
```

---

## Rule 1 — Housing Program ✅

**If** the governing screening includes a **Housing** service **and** the assessment
includes ECM, **then**:

**1a.** an `Environmental Exposure Assessment` case (the dwelling case) must exist.

The row reports where the **dispatch order** actually is, not merely whether the case
exists:

| Order state | Row |
|---|---|
| no order | **to-do** — "No assessment order raised yet" |
| out of service area | **blocked** — "Out of range — 33314 is outside our service area" |
| pending schedule | **waiting** — "Sent to the vendor — awaiting scheduling" |
| confirmed | **waiting** — "Visit confirmed for 25 Sep" |
| pending submission | **waiting** — "Visited — awaiting the vendor's submission" |
| submitted / uploaded | **done** — and if no case exists, "the case still needs opening" |
| cancelled | **blocked** |

Out of range outranks everything: a withheld order cannot progress, and it is the
only state with an action attached — **correct the dwelling address** on the Details
tab and it is sent automatically.

**1b.** once the assessment is **submitted**, every case from
`case_recommendations` must exist. A recommendation is satisfied by a case for that
product **and borough in ANY status** — a closed remediation case means the work was
done, and recommending it again produces a duplicate.

Blocked recommendations keep three distinct reasons, because each needs a different
fix: *no programme for the product* · *programme missing from the table* · *External
Services, needs reclassifying*.

⚠️ **ECM alone is the trigger.** The specific housing results (mould, ventilation,
asthma, rent) are not preconditions — they are what the assessment goes on to
recommend.

---

## Rules 2 & 3 — Food Program

### What is currently built ✅

**If** the governing screening includes **Food**, the assessment includes **ECM**,
and the assessment names **any** food service, **then** one live food case is
required — **meals or boxes, either satisfies it.**

### What was removed, and why ⚠️

The rule used to demand a *specific* service and flagged the other as wrong. The
authorisation data refuted it:

```
meals-eligible, holding a BOX case      219    218 approved · 1 never_requested
boxes-eligible, holding a MEALS case    103    102 approved · 1 never_requested
NO food eligibility, holding either     217    212 approved · 5 never_requested
```

Not a timing artefact either — **198** of the third group were opened *after* the
assessment that supposedly forbids them, and **116** hold an approved food case
although no assessment they have ever had named a food service.

### The asymmetric rule 🟡

Agreed, not yet built. `Food Prescriptions (Voucher / Boxes) (Food)` is the
eligibility result that corresponds to **boxes** cases.

```
meals_eligible  =  Medically Tailored Meals (MTM) (Food) | Medically Tailored Meals (MTM)
                 | Clinically Appropriate Meals (Food)   | Clinically Appropriate Meals
boxes_eligible  =  Food Prescriptions (Voucher / Boxes) (Food)
```

| Eligibility | Meals case | Boxes case |
|---|---|---|
| meals | ✓ | ✓ *(stepping DOWN is fine)* |
| boxes only | ✗ **warn** | ✓ |
| both | ✓ | ✓ |
| neither | ❔ | ❔ |

**The asymmetry is the rule.** Meals eligibility permits stepping down to boxes;
boxes eligibility does not permit stepping up to meals. MTM is the expensive
service, so over-servicing is the risk worth catching.

**Action:** review the meals case — the member was assessed for a produce
prescription only.

```
a. MEALS eligible — either is fine        9,061
b. BOXES-ONLY + a MEALS case → WARN         101     ← all 101 approved
c. BOXES-ONLY + boxes only — fine         1,645
d. NO food eligibility at all               217
z. no assessment — no verdict             6,004
```

⚠️ **All 101 are approved and being paid today.** Approval shows the payer *paid*,
not that the member was *entitled* — stepping up to a more expensive service is
exactly what an approval would not catch but an audit would. Expect the question.

#### Evidence that the meals → boxes direction is legitimate

Of the 219 meals-eligible members holding a box case, **every one is on a `Boxes`
programme, none on `Voucher`** — a deliberate route, not scattered error. And:

```
had a meals case at some point   106     consistent with switching
NEVER had a meals case           113     opened as boxes from the start
holds a LIVE meals case too       25     both at once
```

So "they switch" describes only part of it.

#### Open questions ❔

1. **Group d (217, 212 approved)** — a food case with no food eligibility at all.
   No verdict, warn for a meals case only, or warn for any food case?
2. **Which case** — flag any live meals case, or only when it is the governing one?
   25 members hold both a live meals and a live boxes case.
3. **Severity** — error (red) or warning (amber)?

---

## The borough check ✅

Every row carries the borough its programme names, compared against the member's
coverage borough.

| | |
|---|---|
| green | matches |
| red | **mismatch** — the case is billed against the wrong borough |
| grey | could not tell |

⚠️ **Three-valued, never a boolean.** `null` means no coverage, or a programme that
names no borough (`Care Management Services` names none) — **176 of 814 rows**.
Painting "could not tell" red accuses good data of being wrong, and that is how a
check stops being believed.

**Action:** 9 genuine mismatches per 500 members — ISAK covered in Brooklyn with
Queens cases, MATTHEW and SIMON covered in Queens with Manhattan ones.

---

## Alerts — things that do not ADD UP ✅

Distinct from a to-do. A to-do says "this still needs doing"; an alert says "what is
already recorded does not make sense".

| Alert | Fires when | Severity | Action |
|---|---|---|---|
| `no_ecm_housing` | screened for Housing, no ECM | warning | check the assessment is complete |
| `no_ecm_food` | screened for Food, no ECM | warning | same |

Both exist because those members have **no track at all** — without the alert the
tracker shows nothing, which reads as "nothing to do here".

**Deliberately NOT alerts:**

- *Housing screened + ECM but no dwelling case* — 6% of members, the commonest state
  in the tracker, and already the first row of the housing track in amber.
- *The wrong kind of food case* — removed; see above.

---

## Scheduled reauthorizations ✅

A reauthorization that is **approved but has not started yet** appears **below** the
service it extends — the same service continuing, not a second one. Without it an
agent sees a window about to end with no sign the next is approved, which is when a
duplicate gets opened.

State is **waiting**, not to-do: it activates on its own date, and marking it
outstanding has an agent chasing work that is done.

Defers to `lifecycle.deferred_extension_case_ids`, which requires another approved
case of the **same product kind and scope** and, on an overlapping window, defers
until the current one ends.

---

## The header badge ✅

```
N outstanding    todo + blocked only
All done         nothing outstanding
N problems       alerts outrank the count
```

⚠️ **`waiting` is NOT counted.** Those rows are somebody else's move — an approved
reauthorization starting next month once made a fully-handled member read as
"1 outstanding".

---

## Lessons this document exists to preserve

**Check `service_authorization_status` before calling a case wrong.** The withdrawn
ineligibility design (`wrong-opened-case-ineligibility.md`) reasoned entirely from
eligibility strings and would have marked ~511 members Ineligible — stopping
deliveries for cases the payer had approved. One query would have prevented it.

**Rigour applied to an unchecked premise makes a wrong answer more convincing, not
less.** That design measured populations, separated confident from ambiguous cases,
and warned about un-judgeable members. All of it sat on a premise nobody had tested.

**"It happens" is not "it is allowed", and "it was approved" is not "it was
correct".** Both directions of that mistake appear above.
