# Enrollment lifecycle & governing-case rules

The rules the meal/box (internal-service) enrollment lifecycle MUST obey, and
the checklist to keep them true when the model changes (new stages, new cases,
new import paths). Read this before touching `EnrollmentVerification` stages,
governing-case selection, enrollment creation, or the delivery calendar / PO.

Most of the production incidents we cleaned up (two live enrollments, empty
Nutritionist queue, members missing from POs, wrong kitchen) trace back to
violating one of these rules. Keep them intact.

---

## 0. The core invariant

**A client has AT MOST ONE live (non-terminal, non-disregarded) internal-service
enrollment at any time.** Everything else is `CLOSED` / `CANCELLED` history.

Two live enrollments for one client is ALWAYS a bug. It causes: duplicate PO
lines, a stale enrollment that logistics/nutrition work independently, the
Nutritionist queue hiding the real row, and wrong kitchen/cadence.

---

## 1. Stage buckets (the taxonomy every rule keys off)

Every `EnrollmentStage` falls into exactly one bucket. Rules are written against
the BUCKET, not the individual stage — so a new stage MUST be added to the right
bucket (see the checklist in §7).

| Bucket | Stages (today) | Meaning |
|---|---|---|
| **Pre-service** | `pending_validation`, `validated`, `pending_verification`, `verified`, `kitchen_assignment` | In the funnel, not yet delivering |
| **Serving** | `service_active`, `on_hold`, `service_complete` | Delivering / paused-from-delivering / finished |
| **Terminal** | `closed`, `cancelled` | Dead history — never on a PO, never blocks |
| **Dismissed** | `disregarded` | Dismissed verification, kept only for history |

Sub-distinctions used by specific rules:
- **Verified-or-beyond** = `verified`, `kitchen_assignment` + all *serving* — "has a
  verification fact." Used so a fresh pre-verification row never masks a
  more-advanced enrollment.
- **Early pre-service** = `pending_validation`, `validated`, `pending_verification`
  — no progress worth preserving.

Where the buckets live in code (keep in sync):
- `_TERMINAL_STAGES` — `api/services/lifecycle.py`
- `_PRIOR_SERVING_STAGES` — `api/services/lifecycle.py` (the "serving" bucket)
- `SERVICE_EXCLUDED_ENROLLMENT_STAGES` — `api/models.py` (excluded from POs/calendar sweep: on_hold, kitchen_assignment, service_complete, closed, cancelled)
- `_KITCHEN_ASSIGNABLE_STAGES` — `api/portal/views_members.py`
- `_PRE_VERIFICATION` (in `active_enrollment`) — `api/portal/serializers.py`
- `_TERMINAL_OR_SERVING` (in the verification-create guardrail) — `api/serializers.py`
- `ENROLLMENT_TRANSITIONS` (the allowed stage-transition map) — `api/services/lifecycle.py`

---

## 2. Governing internal-service case

- The **governing case** is chosen by `governing_case_key`
  (`api/services/lifecycle.py`): most favorable authorization first (an approval
  beats a denial regardless of dates), then OPEN over closed/cancelled, then
  **most-recently-created**.
- If agents open several cases by mistake, the **most-recently-created OPEN case
  governs**, and the single enrollment follows it — **until a case closes**, then
  governing is re-derived among the remaining open cases and the enrollment
  rebinds to whatever governs.
- The single enrollment always represents the governing case; a non-governing
  open case does NOT get its own second live enrollment.

---

## 3. Governing-case CHANGE (client already has a live enrollment `L`)

`replace_enrollment_for_case_change` (`api/services/lifecycle.py`), invoked only
from `reconcile_internal_service_authorization` (which runs on **case save /
import**, NOT on kitchen assignment):

| Situation | Action | Result |
|---|---|---|
| `L` already on the governing case | no structural change; project auth | 1 live |
| Governing changed, **L pre-service, same kind** | **rebind** `L.case = G`, keep verification/nutritionist progress | 1 live |
| Governing changed, **L pre-service, kind changed** (meals↔boxes) | close `L`; one new `pending_verification` for `G` | 1 live |
| Governing changed, **L serving, same kind** | carry service (close old, new supersedes, kitchen/cadence carried → Service Active) | 1 live |
| Governing changed, **L serving, kind changed** | supersede `L` (closed) + new enrollment at **Kitchen Assignment** on `G` | 1 live |

**Never fork a new enrollment for a pre-service same-kind change** — rebind. Fork
only for served enrollments (to keep history) or a genuine kind change.

When a superseded/old enrollment is closed it MUST truly terminate — use
`_force_close_enrollment`; never leave it non-terminal.

---

## 4. Enrollment CREATION guardrail

**A new/renewal case must never open a SECOND live enrollment.** Every path that
creates an `EnrollmentVerification` must first check for an existing live
in-funnel enrollment and REUSE it:

- `EnrollmentVerificationSerializer.create` (`api/serializers.py`) — if the client
  already has a live enrollment that is not terminal and not serving, rebind +
  refresh it instead of creating a new row.
- Any NEW creation path (imports, wizards, scripts) MUST do the same.

New enrollments are created ONLY when the client has **no** live enrollment.

---

## 5. Delivery calendar & Purchase Order invariants

- A **terminal enrollment MUST NOT keep live (SCHEDULED) future occurrences.**
  Entering a terminal stage truncates future deliveries (`advance_enrollment`
  terminal cleanup + `_force_close_enrollment`). A stale calendar on a dead
  enrollment (a) never feeds a PO and (b) must never block the live survivor.
- **Calendar dedupe ignores terminated enrollments.**
  `_dedupe_calendar_occurrences` (`api/services/orders.py`) excludes
  `CLOSED`/`CANCELLED` enrollments — a dead row's occurrences must not stop the
  live survivor from building its own.
- **A kitchen/cadence change rebuilds the calendar immediately** via
  `reconcile_enrollment_calendar` (`api/services/orders.py`) — same per-enrollment
  reconcile the `sync_delivery_calendars` batch runs.
- **PO generation is a SEPARATE, manual step.** Building/changing the calendar
  does NOT put a member on an already-generated PO — the PO must be
  (re)generated. Cancelled orders never block re-batching.
- **Late (passed-cutoff) POs:** `backfill_late_occurrences` fills a skipped
  cadence date; it skips a member only when they already hold **as many live
  deliveries that week as their cadence prescribes** (per-cadence count, so a
  2×/week member still gets both days) and ignores CANCELLED rows.

---

## 6. Display / queue rules

- `active_enrollment` (`api/portal/serializers.py`) resolves the ONE enrollment
  that represents a client. It prefers the governing-case enrollment **but never
  demotes to a fresh pre-verification row** when a verified-or-beyond live
  enrollment exists (otherwise the Nutritionist queue / status labels break).
- The **Nutritionist queue** = households whose `active_enrollment` is `verified`
  and not yet nutritionist-approved, deduped by client, superseded rows hidden.
- A closed-case hold shows as **Closed/Inactive**, not "On Hold" (the closure
  full-stop parks the enrollment On Hold + the client Service Inactive).

---

## 7. CHECKLIST — adding a new `EnrollmentStage` (do ALL of these)

A new stage silently breaks the rules unless you classify it everywhere. When
adding a stage:

1. **Pick its bucket** (§1): pre-service, serving, terminal, or dismissed.
2. **Update the bucket constants** so it's covered:
   - terminal? → `_TERMINAL_STAGES` (lifecycle), `_TERMINAL`/terminal lists in the
     reconcile mgmt commands, and the terminal-cleanup + calendar dedupe exclusions.
   - serving? → `_PRIOR_SERVING_STAGES`, and decide PO/calendar inclusion
     (`SERVICE_EXCLUDED_ENROLLMENT_STAGES` in `models.py`).
   - pre-service? → `_PRE_VERIFICATION` (if pre-verification) and the
     verification-create guardrail's `_TERMINAL_OR_SERVING` (so it's treated as
     reusable/in-funnel), and `_KITCHEN_ASSIGNABLE_STAGES` if a kitchen can be
     assigned from it.
3. **Add its edges to `ENROLLMENT_TRANSITIONS`** (and any `_PROCESS_GATES`).
4. **`active_enrollment`**: confirm the new stage doesn't wrongly mask or get
   masked (verified-or-beyond vs pre-verification).
5. **PO / calendar**: decide if the stage delivers. If not, add it to
   `SERVICE_EXCLUDED_ENROLLMENT_STAGES`. If terminal, ensure future occurrences
   are truncated on entry and excluded from the dedupe blocker set.
6. **Governing-case change table (§3)**: decide the rebind/carry/requeue behavior
   for the new stage.
7. **Re-assert the invariant (§0)**: the new stage must not allow two live
   enrollments to coexist.
8. **Tests**: add/extend tests so the invariant + queue + calendar still hold.

> Rule of thumb: if a rule says "terminal" / "serving" / "pre-service", the new
> stage must be added to that bucket's constant — never leave a stage
> unclassified, or it will fall through every guard.

---

## 8. Where each rule is enforced (code map)

| Rule | File · function |
|---|---|
| Governing case selection | `api/services/lifecycle.py` · `governing_case_key`, `internal_service_case` |
| Governing-case change (rebind/carry/fork) | `api/services/lifecycle.py` · `replace_enrollment_for_case_change`, `_carry_service_and_activate` |
| Reconcile entry (case save/import) | `api/services/lifecycle.py` · `reconcile_internal_service_authorization` |
| Force-close a superseded row | `api/services/lifecycle.py` · `_force_close_enrollment` |
| Creation guardrail (reuse in-funnel) | `api/serializers.py` · `EnrollmentVerificationSerializer.create` |
| One-enrollment resolver / queue | `api/portal/serializers.py` · `active_enrollment` |
| Terminal calendar cleanup | `api/services/lifecycle.py` · `advance_enrollment` (terminal branch) |
| Calendar dedupe ignores dead rows | `api/services/orders.py` · `_dedupe_calendar_occurrences` |
| Per-edit calendar rebuild | `api/services/orders.py` · `reconcile_enrollment_calendar` |
| Late-PO backfill | `api/services/purchase_orders.py` · `backfill_late_occurrences` |
| Repair sweeps | `api/management/commands/reconcile_superseded_live_enrollments.py`, `reconcile_closed_enrollment_calendars.py` |
