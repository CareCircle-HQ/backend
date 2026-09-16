# Company Status — what each one actually means

The **Company Status** filter on the Data page is not a stored field. It is
computed for every member, every rebuild, by `_company_status()` in
`api/services/enrollment_analytics.py`.

There are seven values. Every member lands in exactly one.

| Status | In one line |
|---|---|
| **No Case Created** | We have no internal-service case for them at all |
| **Closed** | Their governing case is closed or cancelled |
| **Unable to Be Serviced** | The case is open, but something blocks delivery |
| **Paused** | They got through, and service is temporarily stopped |
| **Active** | We are actually delivering to them right now |
| **Pending** | The case is live and they are still moving toward service |
| **Review** | None of the above fit — a state that needs a human |

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

Two things to take from those numbers. **No Case Created dominates** -- most people
in the database were screened and never had an internal service opened, so any
"how many members..." question needs to say whether it includes them. And
**Review is tiny by design** -- it is the ladder's fallback, so a rising Review
count means members are reaching a state none of the rules answer.

---

## The single most important thing: it is a FIRST-MATCH LADDER

The checks run **in order**, and the first one that matches wins. Nothing later is
even evaluated.

```
1. No Case Created
2. Closed
3. Unable to Be Serviced
4. Paused
5. Active
6. Pending
7. Review           (the fallback -- reached only when 1-6 all miss)
```

This explains almost every "why is this member in the wrong bucket?" question:

- A member who is **paused AND has no Medicaid** shows as **Unable**, not Paused —
  because Unable is checked first.
- A member who is **being delivered to but whose case just closed** shows as
  **Closed**, not Active — Closed is checked second.
- A member with an **open case and an expired authorisation** shows as **Unable**,
  even though their enrollment may still say `service_active`.

So the ladder is not just an implementation detail. **It is the definition.**

---

## 1. No Case Created

> They hold no governing internal-service case, and they are not covered by
> someone else's.

The check is simply: is there a governing case, and is it an internal-service one?

Two fallbacks have already been tried before this point, which matters:

- the member's **own** governing case, and
- their **household primary's** case (a covered relative inherits it)

So a dependent on a parent's meals case does **not** land here — they inherit the
household's status instead. Reaching this bucket means genuinely no case anywhere.

**Biggest bucket by far** — most people in the database have been screened but
never had an internal service case opened.

> ⚠️ As of the housing work, "internal-service case" means a **FOOD** one for this
> purpose. A member whose only internal-service case is a housing assessment
> currently reads **No Case Created** here.

---

## 2. Closed

> The governing case's status is `closed` or `cancelled`.

That is the whole rule. Note what it does **not** look at: the enrollment stage,
whether deliveries are still scheduled, or whether the authorisation is still
valid. The case is the authority.

This is why a member can read **Closed** while still having deliveries on the
calendar — that mismatch is a real defect class, not a display quirk (see
`docs/findings-closed-case-committed-po.md`).

---

## 3. Unable to Be Serviced

> The case is **open**, but we cannot deliver.

Any **one** of these is enough:

| Trigger | Meaning |
|---|---|
| **Out of orbit** | No kitchen can fulfil their location |
| **Out of range** | Delivery address is outside the service area |
| **No valid Medicaid** | Insurance missing, inactive or expired |
| **No social care coverage** | Coverage missing, inactive or expired |
| **Eligibility = ineligible** | The read model's eligibility says ineligible |
| **Lifecycle off-ramp** | `lifecycle_stage` is `not_eligible` or `ineligible` |
| **Authorization denied** | The case authorisation was refused |
| **Authorization expired** | The approval **window** has lapsed |

Two of these are worth calling out, because they are the usual surprises:

**"Authorization Expired" is computed, not raw.** The check keys off the derived
`program_status`, *not* the case's `service_authorization_status` — because the raw
status **can still read "approved"** after the approval window has passed. Keying
off the raw field would leave lapsed members looking serviceable.

**The lifecycle off-ramp is deliberate.** A member parked on `not_eligible` has a
closed enrollment and is not being served, so they belong in Unable. Without this
their lingering open case would make them look **Pending** — as if they were on
their way to service, which they are not.

Consequence of the ladder: **Unable outranks Paused.** An eligibility pause (no
coverage) is Unable, never Paused.

---

## 4. Paused

> They made it through, and service is temporarily stopped, with the case still
> open.

Any one of:

| Trigger | Who stopped it |
|---|---|
| Member status **Paused** | An agent |
| Member status **Nutritionist Paused** | The nutritionist |
| Enrollment stage **on hold** | The programme |
| Program status **On Hold** | The programme |

The distinction from Unable is **intent**: Paused means *"we can serve them, just
not this week."* Unable means *"we cannot serve them at all right now."*

> ⚠️ The Data page's Company Status **collapses** the specific reason. `paused` and
> `nutritionist_paused` both read "Paused"; `out_of_orbit` and `out_of_range` both
> read "Unable". To filter on the precise reason you need the member-status field,
> which is why the query-agent glossary lists four terms as blocked on it.

---

## 5. Active

> We are **actually delivering** to them right now.

This is the strictest bucket, and intentionally so. It requires **all** of:

1. A **real completed verification** — `verified_at` is set. Deliberately *not*
   inferred from the enrollment stage, because a past incident proved the stage
   can be wrong while no verification ever happened.
2. A **currently valid authorisation** — `approved` or `not_required`. An expired
   one has already fallen into Unable above.

…**and then one of two routes to "actually serving":**

- **Being delivered** — an active delivery calendar: an upcoming scheduled
  occurrence, or a live (non-cancelled) `DeliveryOrder` already in a PO.
  Nutrition is *not* re-checked here — they are already being served, and any
  nutrition gap still shows in the nutritionist filter.
- **Pending kitchen assignment** — stage is `kitchen_assignment` **and** the
  nutritionist has signed off. Not yet delivered, so the sign-off is required.

The key idea: **Active is defined by the delivery calendar, not by the
`service_active` stage.** An "activated" member with no live deliveries is not
being served, and lands in **Review** instead. That gap is tracked separately in
`docs/company-status-review-activated-no-delivery.md`.

---

## 6. Pending

> The case is live and they are still **progressing toward** service.

Requires both:

- **A live authorisation** — `approved` or `pending`/requested, and
- **A pre-service enrollment stage** — anything *before* being served: pending
  verification, verified-and-waiting, awaiting nutritionist.

Explicitly **excluded**: `service_active` and `service_complete`. A member at
those stages is either Active (if genuinely delivering) or Review (if not) — never
Pending. Pending means *"on the way"*, and someone marked service-active is not on
the way.

---

## 7. Review

> Nothing above fitted. A human needs to look.

The fallback, and deliberately a small bucket. Two shapes reach it:

- **Activated but not delivering** — a `service_active`/`service_complete`
  enrollment with **no live delivery calendar**. Not Active (no calendar), and not
  Pending (excluded by stage). This is the main occupant.
- **An authorisation that is neither approved nor pending** — e.g.
  `never_requested`. There is nothing to progress toward and nothing blocking
  delivery in the Unable sense, so it needs a decision.

A growing Review count is a signal, not a category. It means members are reaching
a state the ladder has no answer for.

---

## Reading the ladder backwards (the practical bit)

When a member is in a bucket you did not expect, walk the ladder from the top and
find the **first** rule that matches them. That is your answer — not the rule that
describes them best.

```
"Why is my active member in Unable?"        -> check Medicaid, social care,
                                               and the AUTHORISATION WINDOW
"Why is my paused member in Unable?"        -> a coverage gap outranks a pause
"Why is my delivering member in Closed?"    -> the case closed; deliveries lag
"Why is my service_active member in Review?"-> no live delivery calendar
"Why does my housing member say No Case?"   -> only FOOD cases count here
```

---

## Where this is computed

| | |
|---|---|
| Derivation | `_company_status()` in `api/services/enrollment_analytics.py` |
| Delivery-calendar test | `_has_active_delivery()`, same file |
| Governing case | `governing_service_case_for_display()` (+ household fallback) |
| Stored on | `EnrollmentAnalytics.company_status` (the read model) |
| Refreshed by | `rebuild-enrollment-analytics`, every 4 hours |

Because it is stored in the read model, **the Data page shows the value as of the
last rebuild** — the page's "Data updated" timestamp is the truth about freshness.
A member whose coverage lapsed an hour ago may still read Active until the next
rebuild.

> ⚠️ The Executive dashboard computes some of its own cohorts independently and
> does **not** always agree with these definitions — a known divergence of roughly
> 443 cases on the "open IS case" definition. If a dashboard card and a Data page
> filter disagree, that is why.
