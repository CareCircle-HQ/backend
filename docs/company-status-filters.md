# Data page → Company Status — every dropdown option and its rule

The **Company Status** dropdown on the Data page is not a stored source field. It
is computed for every member on every rebuild by `_company_status()` in
`api/services/enrollment_analytics.py`, then saved to
`EnrollmentAnalytics.company_status` and filtered on exactly.

This document lists the options **in the order the dropdown shows them**, each
with the rule underneath.

Distribution on a production clone (75,457 members, 2026-09-16):

```
No Case Created         53,572   71.0%
Active                  14,875   19.7%
Closed                   3,419    4.5%
Paused                   1,803    2.4%
Unable to Be Serviced    1,514    2.0%
Pending                    262    0.3%
Review                      12    0.0%
```

**No Case Created dominates at 71%** — most people in the database were screened
and never had an internal service opened, so any "how many members…" question
needs to say whether it includes them. **Review is tiny by design** — it is the
fallback, so a rising Review count means members are reaching a state none of the
rules answer.

---

## ⚠️ Read this first: the dropdown order is NOT the evaluation order

The dropdown lists the statuses in the order an operator thinks about them. The
code decides them in a different order — a **first-match ladder**, where the first
rule that matches wins and nothing later is evaluated:

```
dropdown order                evaluation order (what actually decides)
1. Active                     1. No Case Created
2. Pending                    2. Closed
3. Unable to Be Serviced      3. Unable to Be Serviced
4. Paused                     4. Paused
5. Closed                     5. Active
6. No Case Created            6. Pending
7. Review                     7. Review
```

So a member is **not** put in the bucket that best describes them — they are put in
the **first** bucket that matches. That single fact answers nearly every "why is
this member in the wrong status?" question, and each option below records what
outranks it.

---

# 1. Active

> We are **actually delivering** to them right now.

**Rule — all of these must hold:**

| Requirement | Detail |
|---|---|
| Real verification | `verified_at` is set on the enrollment |
| Valid authorization | `approved` **or** `not_required` |
| …plus **one** of the two routes below | |

**Route A — being delivered:** a live delivery calendar. Either an upcoming
scheduled occurrence (anticipated date today or later), or a non-cancelled
`DeliveryOrder` already in a Purchase Order. Nutrition is **not** re-checked here —
they are already being served, and any nutrition gap still shows in the
nutritionist filter.

**Route B — pending kitchen assignment:** stage is `kitchen_assignment` **and** the
nutritionist has approved. Not yet delivered, so the sign-off is required.

**Two deliberate strictnesses:**
- **`verified_at`, not the stage.** An incident proved the enrollment stage can
  read verified when no verification ever happened.
- **The delivery calendar, not `service_active`.** An "activated" member with no
  live deliveries is *not* being served — they fall to **Review**.

**What outranks it:** No Case Created, Closed, Unable, Paused. So a delivering
member whose case just closed reads **Closed**, and one whose coverage lapsed reads
**Unable** — Active is only reached when none of those matched.

---

# 2. Pending

> The case is live and they are still **progressing toward** service.

**Rule — both must hold:**

| Requirement | Detail |
|---|---|
| Live authorization | `approved` **or** `pending`/requested |
| Pre-service stage | anything *before* being served — pending verification, verified-and-waiting, awaiting nutritionist |

**Explicitly excluded:** `service_active` and `service_complete`. A member at
those stages is either **Active** (genuinely delivering) or **Review** (not) — never
Pending. Pending means *"on the way"*, and someone marked service-active is not on
the way.

**What outranks it:** everything above, and this is where the lifecycle off-ramp
matters — a member parked on `not_eligible` with a lingering open case would look
Pending, as if they were heading toward service. The Unable rule catches them
first, on purpose.

---

# 3. Unable to Be Serviced

> The case is **open**, but we cannot deliver.

**Rule — any ONE of these is enough:**

| Trigger | Meaning |
|---|---|
| **Out of orbit** | No kitchen can fulfil their location |
| **Out of range** | Delivery address is outside the service area |
| **No valid Medicaid** | Insurance missing, inactive or expired |
| **No social care coverage** | Coverage missing, inactive or expired |
| **Eligibility = ineligible** | The read model's eligibility says ineligible |
| **Lifecycle off-ramp** | `lifecycle_stage` is `not_eligible` or `ineligible` |
| **Authorization denied** | The case authorization was refused |
| **Authorization expired** | The approval **window** has lapsed |

**Two that surprise people:**

**"Authorization Expired" is computed, not raw.** It keys off the derived
`program_status`, *not* the case's `service_authorization_status` — because the raw
status **can still read "approved"** after the approval window passes. Keying off
the raw field would leave lapsed members looking serviceable.

**The lifecycle off-ramp is intentional.** A member on `not_eligible` has a closed
enrollment and is not being served, so they belong here — raw Medicaid can still
read active, which is why the stage is checked too.

**What it outranks:** Paused, Active, Pending. So an eligibility pause (no
coverage) is **Unable**, never Paused — and this is the most common "wrong bucket"
complaint.

---

# 4. Paused

> They made it through, and service is **temporarily** stopped, case still open.

**Rule — any ONE of:**

| Trigger | Who stopped it |
|---|---|
| Member status **Paused** | An agent |
| Member status **Nutritionist Paused** | The nutritionist |
| Enrollment stage **on hold** | The programme |
| Program status **On Hold** | The programme |

The difference from Unable is **intent**: Paused means *"we can serve them, just
not this week."* Unable means *"we cannot serve them at all right now."*

**What outranks it:** No Case Created, Closed, Unable. A paused member who also
lacks coverage reads **Unable**.

> ⚠️ This option **collapses the reason**. `paused` and `nutritionist_paused` both
> read "Paused" here (1,803 vs 1,479 on the member-status field). To filter on who
> paused it, you need the member-status field, not this dropdown.

---

# 5. Closed

> The governing case's status is `closed` or `cancelled`.

**Rule:** that is the whole check.

Note what it deliberately ignores: the enrollment stage, whether deliveries are
still scheduled, and whether the authorization is still valid. **The case is the
authority.**

**What outranks it:** only No Case Created.

> ⚠️ This is why a member can read **Closed** while deliveries are still on the
> calendar — the status follows the case immediately, the delivery machinery does
> not. That mismatch is a real defect class, not a display quirk; see
> `docs/findings-closed-case-committed-po.md`.

---

# 6. No Case Created

> No governing internal-service case — theirs or anyone's.

**Rule:** there is no governing case, or the governing case is not an
internal-service one.

Two fallbacks have already been tried before this point, which matters:

- the member's **own** governing case, then
- their **household primary's** case — a covered relative inherits it

So a dependent on a parent's meals case does **not** land here; they inherit the
household's status. Reaching this bucket means genuinely no case anywhere.

**What outranks it:** nothing. It is checked first, so it wins over everything —
a member with no case cannot be Active, Paused or anything else.

> ⚠️ Since the housing work, "internal-service case" means a **FOOD** one for this
> purpose. A member whose only internal-service case is a housing assessment
> currently reads **No Case Created**.

---

# 7. Review

> Nothing else fitted. A human needs to look.

**Rule:** the fallback — reached only when all six rules above miss. Two shapes
arrive here:

| Shape | Why it lands here |
|---|---|
| **Activated but not delivering** | `service_active`/`service_complete` with **no live delivery calendar** — not Active (no calendar), not Pending (excluded by stage) |
| **Non-actionable authorization** | e.g. `never_requested` — nothing to progress toward, nothing blocking delivery in the Unable sense |

Only **12 members** on the clone. It is a quarantine bucket, tracked in
`docs/company-status-review-activated-no-delivery.md`.

A growing Review count is a **signal, not a category** — it means members are
reaching a state the ladder has no answer for.

---

## Blank — "Company status (all)"

No filter. Returns every member **including the 53,572 with no case**, which is
usually not what someone means by "all our members".

---

## Reading the ladder backwards

When a member is in an unexpected bucket, walk the evaluation order from the top
and find the **first** rule that matches. That is the answer.

```
"Why is my active member in Unable?"          -> Medicaid, social care, or the
                                                 AUTHORISATION WINDOW
"Why is my paused member in Unable?"          -> a coverage gap outranks a pause
"Why is my delivering member in Closed?"      -> the case closed; deliveries lag
"Why is my service_active member in Review?"  -> no live delivery calendar
"Why does my housing member say No Case?"     -> only FOOD cases count here
```

---

## Where this is computed

| | |
|---|---|
| Derivation | `_company_status()` — `api/services/enrollment_analytics.py` |
| Delivery-calendar test | `_has_active_delivery()`, same file |
| Governing case | `governing_service_case_for_display()` + household fallback |
| Dropdown options | `COMPANY_STATUSES` — `frontend/src/app/pages/DataPage.tsx` |
| Stored on | `EnrollmentAnalytics.company_status` (the read model) |
| Refreshed by | `rebuild-enrollment-analytics`, every 4 hours |

Because it is stored, **the Data page shows the value as of the last rebuild** —
the page's "Data updated" timestamp is the truth about freshness. A member whose
coverage lapsed an hour ago may still read Active until the next rebuild.

> ⚠️ The Executive dashboard computes some cohorts independently and does **not**
> always agree with these definitions — a known divergence of roughly 443 cases on
> the "open IS case" definition. If a dashboard card and a Data page filter
> disagree, that is why.
