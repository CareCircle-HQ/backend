# Company Status refactor (Data page) — WORKING NOTES

Re-checking each of the six Data-page Company Statuses against the intended
business algorithm, one by one. **Nothing here is implemented yet** — this is the
agreed design + open questions to resume from.

Buckets are evaluated in priority order (first match wins):
`No Case → Closed → Unable → Paused → Active → Pending`.
Implementation lives in `api/services/enrollment_analytics.py::_company_status`.

---

## 1. ACTIVE  — reviewed (NOT yet implemented)

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

## 2. PENDING — TODO (re-check)
## 3. UNABLE TO BE SERVICED — TODO (re-check)
## 4. PAUSED — TODO (re-check)
## 5. CLOSED — TODO (re-check)
## 6. NO CASE CREATED — TODO (re-check)

(Left to review together, same format: intended algorithm vs. implemented, impact,
open questions.)
