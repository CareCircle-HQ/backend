# Closed cases with PO-committed deliveries — findings 2026-09-15

## Why this came up

`sweep_closed_case_service` (daily 04:00 on Celery beat) terminates the
enrollment of any member whose LAST internal-service case has closed, and
truncates their future deliveries. The business rule, confirmed by the operator:

> **a closed case means a closed enrollment.**

**Beat had never run on this deployment** (no `-B` on celery-worker, no beat
unit — see AGENTS.md), so the sweep had never fired and a backlog accumulated:

```
1,476 candidate members
    9 of them with deliveries still scheduled (~201 occurrences)
1,467 with nothing scheduled -- no service impact
```

Daily closure volume is 20-50 (spikes to 222), so the backlog is months of
ordinary business, NOT an anomaly. Checked explicitly: 28 closures on 09-14 sits
mid-range for the preceding three weeks.

## What was applied

All 9 with scheduled deliveries, after confirming each had **zero** open
internal-service cases:

```
python manage.py stop_closed_case_service --file <ids> --apply
```

Every one returned `closed_out: True`. Result:

| member | occurrences before | after | live delivery orders |
|---|---|---|---|
VERNETTA PARKER | 21 | **0** | 0 |
NISSON DINKELS | 42 | **0** | 0 |
MARGARITA UGORSKAYA | 23 | **0** | 0 |
ADRIANNE BROWN | 41 | **1** | 1 -> cancelled |
JOAN SPENCER | 22 | **1** | 1 -> cancelled |
FAEZA BHATTI | 1 | 1 | 1 -> cancelled |
DOMINIQUE HICKS | 1 | 1 | 1 -> cancelled |
CINDY MIZRAHI | 8 | **8** | 0 |
MAGDALENA COX | 42 | **42** | 0 |

Roughly **147 of 201 occurrences correctly truncated**, and the **4 in-flight
delivery orders cancelled** via `cancel_future_delivery_orders` — the function
documented for exactly this ("so a delivery already committed to a cut Purchase
Order doesn't still ship -- and get billed").

## Open issue 1 — 50 PO-batched occurrences resisted truncation

`MAGDALENA COX` (42) and `CINDY MIZRAHI` (8) still have future occurrences after
TWO applies. MAGDALENA's case closed **2026-08-11**, so this does not
self-correct with time.

`truncate_future_deliveries` shortens each plan window then re-syncs, and
`sync_delivery_calendar` deliberately **preserves PO-batched occurrences** (so a
cut Purchase Order is not rewritten under the kitchen). That is sound for a
mid-cycle pause. It leaves a gap when the member is leaving service entirely.

**Hypothesis (NOT confirmed):** the two that failed are the two whose enrollments
are `closed`/`disregarded`, while all five that truncated cleanly are `on_hold`.
`truncate_future_deliveries` iterates `enrollment.delivery_schedules` and
shortens windows with `ends_on >= cutoff`; an already-terminal enrollment may
have no window left to shorten, so its PO-batched occurrences simply persist.

```
truncated cleanly : on_hold, on_hold, on_hold, on_hold, on_hold
did NOT truncate  : ['closed','disregarded']  and  ['closed','on_hold','disregarded']
```

Worth verifying directly before any code change.

## Open issue 2 — member profiles stay `active` on a closed enrollment

All nine still read `profiles: ['active']`, and every reconcile returned
`'paused': False`. Since Purchase Order generation works from servable members,
this is the most likely reason PO-batched occurrences persist — and it contradicts
"closed case = no service" at the member level.

Note where the delivery-order cancellation is wired in:

```
cancel_future_delivery_orders  <-  views_members.py:4849, 4926   (member pause / removal)
                               <-  lifecycle.py:2783, 2871       (member pause paths)
                               NOT <- _full_stop_close_out       (case closure)
```

So a member individually paused gets their committed orders cancelled; a member
whose CASE closes does not. If that asymmetry is unintended, it is a standing
billing exposure on every mid-cycle closure, not a one-off — 4 orders were found
live in a sample of 9.

## What to decide

1. **Should `_full_stop_close_out` also cancel committed delivery orders?** It
   already truncates and pauses; adding `cancel_future_delivery_orders` would
   close the gap in one place. Needs a look at whether any flow depends on the
   current behaviour.
2. **Should a full-stop close-out pause the member profiles too?** That would
   make the member non-servable and stop PO generation at source, rather than
   cleaning up after it.
3. **The remaining 50 occurrences** (MAGDALENA 42, CINDY 8) need removing from
   their cut POs, or an explicit decision to leave the current week with the
   kitchen and drop later weeks.
4. **The other ~1,467 candidates** have no deliveries, so the sweep is safe for
   them — but it will cancel 1,467 enrollments in one pass, which empties work
   queues visibly. Run it watched, not at 04:00 unattended.
5. **`CLOSED_CASE_SWEEP_ENABLED`** stays OFF until 1-4 are settled. Once the
   backlog is clear the sweep handles one or two members a night, which is what
   it was designed for.

## Method note

Two of the delivery-side conclusions in this investigation were reached by reading
code and were WRONG until measured:

- "the delivery orders will be cancelled, nothing ships" — they were not; the
  close-out does not call that function.
- "the sweep does not stop the food" — too pessimistic; it stopped ~147 of 201.

Both were settled by querying before/after state rather than by argument. Do the
same for the hypothesis in Open issue 1.
