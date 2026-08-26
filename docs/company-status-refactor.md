# Company Status refactor (Data page) — WORKING NOTES

Re-checking each of the six Data-page Company Statuses against the intended
business algorithm, one by one. **Nothing here is implemented yet** — this is the
agreed design + open questions to resume from.

Buckets are evaluated in priority order (first match wins):
`No Case → Closed → Unable → Paused → Active → Pending`.
Implementation lives in `api/services/enrollment_analytics.py::_company_status`.

**Household counting (Data page):** counted by the GOVERNING (primary) member
(`is_primary=True`), so each household maps to exactly ONE status and the
per-status household counts partition cleanly (sum to the total). Counting
distinct `household_id` across ALL members double-counted a household whose
members span statuses (measured: 165 mixed households, ~169 overlap). Changed in
`DataSummaryView` (`views_members.py`); test `DataSummaryHouseholdByPrimaryTest`.

---

## 1. ACTIVE  — ✅ IMPLEMENTED (Option B + active delivery calendar + verification)

**Decisions made & shipped:**
- Nutrition: **Option B** — a member on an **active delivery calendar** is Active
  regardless of nutrition (already being served; the nutrition gap still shows in
  the nutritionist filter). Only the **pending Kitchen Assignment** branch requires
  nutritionist sign-off.
- "Being delivered" = **active delivery calendar** (`_has_active_delivery`: a
  non-cancelled DeliveryOrder in a PO), NOT the `service_active` stage.
- **Verification required**: Active needs a real `verified_at`, not just the stage.

**New rule (`_company_status`, after Unable/Paused excluded):**
```python
if verified and auth in ("approved", "not_required"):
    if has_active_delivery:                                   # being delivered
        return "active"
    if stage == "kitchen_assignment" and nutrition_ok:        # pre-service + nutrition
        return "active"
# else -> pending
```

**Impact on the 2026-08-25 clone (after full rebuild):** Active 12,853 → **12,675**
(−178: 2 unverified, ~75 service_active with no live delivery, ~74 kitchen w/o
nutrition, a few reclassified); Pending 75 → 256. All 12,675 Active satisfy the
rule (12,184 being delivered + 491 kitchen+nutrition); other buckets unchanged.
Unit tests: `CompanyStatusActiveRuleTest`.

---

## 1b. ACTIVE — original review notes (kept for history)

> Note: this is the **company** "Active" rollup, distinct from the system's
> enrollment `service_active` stage.

### Intended algorithm (product)
Show members whose:
- governing internal-service case is **open**, AND
- authorization is **approved**, AND
- **verification is complete**, AND
- **nutritional intake is approved**, AND
- the member is **either**:
  - currently being delivered (**active delivery calendar**), **OR**
  - **pending kitchen assignment**

…and **exclude** anyone who is:
- Paused (explicit agent pause **or** nutritionist pause), OR
- On Hold (explicit On Hold), OR
- Out of Orbit, OR
- ineligible (insurance expired/nonexistent **or** social-care coverage expired/nonexistent).

### What's implemented TODAY (`_company_status`, line ~247)
```python
# reached only after Unable + Paused buckets are excluded (priority order)
if stage in ("service_active", "kitchen_assignment") and auth in ("approved", "not_required"):
    return "active"
```
- Governing case open ✅, authorization approved ✅, exclusions (Paused / On Hold /
  Out of Orbit / ineligible) ✅ (handled by the earlier Unable/Paused buckets).
- **Verification complete** ❌ NOT explicitly checked — only inferred from
  `stage ∈ {service_active, kitchen_assignment}` (an assumption we proved can be
  false: the reconcile_member_stages incident advanced members to
  `kitchen_assignment` with `verified_at = NULL`).
- **Nutrition approved** ❌ NOT checked at Active.
- **"Being delivered"** is approximated by the **`service_active` stage**, NOT an
  actual active delivery calendar.

### Impact if we adopt the strict rule (measured on the 2026-08-25 prod clone)
Current Active total: **12,853**.
- Would move → Pending for missing **verification** (`verified_at` NULL): **2**
  (negligible — the earlier reconcile fix + cleanup handled these).
- Would move → Pending for missing **nutrition** (`nutritionist_status ≠ approved`): **377**.
- Total movers: **377**; stays Active: **12,476**.
- Of the 377 movers, **300 are `service_active` (currently being delivered)** with
  nutrition pending; only **77 are `kitchen_assignment`**.

### ⛔ OPEN QUESTION (decide before implementing)
Adding the nutrition-approved requirement moves **~300 members who are actively
being delivered** into **Pending**. Which do we want?

- **Option A (literal rule):** nutrition-approved required for Active regardless →
  those ~300 delivered-but-not-nutrition-approved members report as **Pending**
  until their nutritionist approval is recorded.
- **Option B:** a member on an **active delivery calendar** stays **Active** even
  if nutrition isn't recorded (they're operationally being served); only the
  *pre-service* branch (pending kitchen assignment) requires nutrition.

Also decide: switch "being delivered" from the `service_active` **stage** to a real
**active delivery calendar** check (vs. keeping the stage proxy).

Verification check itself: cheap + correct, adopt regardless (catches the 2).

---

## 2. PENDING — ✅ IMPLEMENTED

**Definition:** governing case **open** AND authorization ∈ (**approved**, **pending/requested**) AND the member is still **before being served** — a PRE-service enrollment (pending verification / verified-awaiting / pending nutritionist), i.e. NOT yet `service_active`.

```python
if auth in ("approved", "pending") and stage not in ("service_active", "service_complete"):
    return "pending"
return "review"   # temporary quarantine (see below)
```

Two groups are deliberately **excluded** from Pending into a temporary
**`review`** bucket (NOT surfaced in the Data-page dropdown; tracked in
`docs/company-status-review-activated-no-delivery.md`, root cause TBD):
- **Activated but not delivering** — `service_active`/`service_complete` with no
  live delivery calendar (not Active, and past kitchen so not Pending).
- **Non-actionable auth** — e.g. `never_requested`.

Impact (clone rebuild): Pending 256 → **214**; `review` = **42**. Unit tests in
`CompanyStatusActiveRuleTest`.

## 3. UNABLE TO BE SERVICED — ✅ VERIFIED (no code change)

**Definition (matches implementation):** governing case **open** AND (auth
**denied** OR **out of orbit** OR **out of range** OR **not eligible** [no valid
Medicaid / no valid social care / hard lifecycle-ineligible off-ramp incl.
out-of-range ZIP / unsupported Medicaid plan] OR **Authorization Expired**).
Authorization Expired is kept (confirmed): approval window lapsed → no current
service, same spirit as denied. Clone: 1,946 (denied 1,018, out-of-orbit 455,
ineligible 532, expired 4).

## 4. PAUSED — ✅ VERIFIED (no code change)
Governing case **open** AND (explicit **Pause** flag OR **nutritionist paused**
OR **On Hold** [stage `on_hold` / program "On Hold"]). Clone: **1,975**
(on-hold 1,912, explicit pause 190, + nutritionist-paused). Matches definition.

## 5. CLOSED — ✅ VERIFIED (no code change)
Governing internal-service case status ∈ (**closed**, cancelled). Clone:
**3,057** (all `closed`). Matches definition.

## 6. NO CASE (CREATED) — ✅ VERIFIED (no code change)
Member has **no** governing internal-service case **AND** is **not** part of a
household (`in_household` False). A household member (dependent) with no own case
is blank `""` (excluded), NOT No Case — this is the "exclude individual members
tied to the primary case" rule. Clone: **39,003** no_case (sample: 0 with an IS
case, 0 in a household); **6,354** blank (household relatives). Matches definition.

---

## Priority note
Buckets are first-match in order `No Case → Closed → Unable → Paused → Active →
Pending → review`. So when two apply, the earlier wins — e.g. a member who is
BOTH On Hold and Out of Orbit is **Unable**, not Paused. This is intentional
(more-severe state wins); revisit if a different precedence is wanted.

## Status: all six buckets reviewed
Active ✅ / Pending ✅ (+ `review` quarantine) / Unable ✅ / Paused ✅ / Closed ✅ /
No Case ✅. Open follow-up: root-cause the `review` (activated-but-not-delivering)
group — see docs/company-status-review-activated-no-delivery.md.
