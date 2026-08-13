# Reauthorization / Service Extension — Governing-Case Plan

## Goal

Screener agents pre-create **`Reauthorization:` internal-service cases in
advance**, so a household ends up with **two open + approved cases at once**: the
one currently serving, and a reauthorization meant to **extend** the same service
into a **future authorization window**.

Today's governing rule (`governing_case_key`: approved → open → most-recently
created) makes the newer reauth case win **immediately**, which closes the live
enrollment and forks a new one *now* — disrupting active service even though the
reauth window hasn't started.

We want the reauth to be **detected and parked** as a scheduled extension, keep
serving on the current case, and **take over only when its window becomes
effective** — preserving both enrollments for history.

---

## Decisions (locked)

1. **Defer-and-extend ONLY when the reauth is the same product kind (meals→meals,
   boxes→boxes) AND same scope (household/individual)** as the current serving
   case. A different kind/scope reauth is a genuine switch → today's immediate
   behavior.
2. **Gap between windows → pause all members** during the gap.
3. **Overlapping windows → the reauth does not govern until the 1st case's window
   ends.**
4. **Switch at reauth-start** (unified with #3 below).
5. Same as #3.
6. Governance stays on the current case during deferral, so PO / eligibility read
   the correct (current) window automatically.
7. Emit a **timeline event** when the deferred extension takes over.

**Model:** **two enrollments** — the current serving one, plus a second in a new
**non-serving `SCHEDULED_EXTENSION`** ("Reauthorization – Waiting") stage. We keep
both to preserve history (linked via `supersedes`).

**Gap state:** at the current window end (`E1`), the current enrollment moves to
**`service_complete`** (members paused); it becomes `closed` when its underlying
case is closed. The reauth activates at its start (`S2`).

**Unified switch point:** with current window `[S1, E1]` and reauth `[S2, E2]`,
the reauth becomes governing at **`max(E1, S2)`**:
- Overlap (`S2 < E1`) → waits until `E1`.
- Contiguous (`S2 ≈ E1`) → switches at the boundary.
- Gap (`S2 > E1`) → current → `service_complete` + members paused at `E1`; reauth
  activates at `S2`.

**"Reauth Attention" tag — intervention-only:** a `ClientTag` named
**"Reauth Attention"** (get-or-create, default color). Applied to the primary
**only when the handoff needs a human**: a **gap** between windows, or a
**kind/scope mismatch** reauth. Auto-cleared after a clean switch.

---

## What we already have

- **`ActiveProgram.to_extend`** (migration `0194`) — classifies a `program_name`
  as a reauthorization/extension. Seeded True for internal-service
  `Reauthorization: …` programs.
- **`Case.effective_authorization_window()`** → `(start, end)`, null-tolerant
  (falls back to the request window). The basis for all date comparisons.
- **`governing_case_key`** (`api/services/lifecycle.py`) — the single governing
  selection chokepoint.
- **`replace_enrollment_for_case_change`** — the fork trigger, called on every
  case-save path via `reconcile_internal_service_authorization` (ext live write +
  CSV import both flow through here).
- The existing **Authorization Expired** concept keyed on the window end — the
  moment a reauth is meant to take over.
- `reconcile_superseded_live_enrollments` — the double-live safety net; the new
  waiting stage must stay compatible with it (never a serving duplicate).

---

## Design

### 1. Classify the case — `Case.is_extension`
- New `BooleanField(default=False)`, **derived on save** from a matching
  `ActiveProgram` with `to_extend=True` (match by `program_name`), mirroring the
  existing `household_type` / `case_type` derive-on-save pattern.
- One-time **backfill** command for existing cases.

### 2. New non-serving stage — `EnrollmentStage.SCHEDULED_EXTENSION`
- "Reauthorization – Waiting". A verified household's reauth enrollment parks here
  until its window is effective.
- **Must be added to every inert/excluded surface** so it can never serve:
  - Purchase Orders (`SERVICE_EXCLUDED_*` / PO builders)
  - Distribution matrix
  - Delivery calendar sync (`sync_delivery_calendars` / `rebuild_delivery_calendar`)
  - **Verification queue + Members-Pending-Verification report** (it is already
    verified, NOT awaiting verification — do not pollute that list)
  - Any "governing/serving enrollment" helpers (`_governing_enrollments`,
    `_primary_enrollment`) as appropriate.
- Carries the full member roster + dietary/verification data at creation (already
  verified), so activation is a clean promotion.

### 3. Detection & parking (at case save)
Hook into `reconcile_internal_service_authorization` (covers ext + import). When
an **approved** internal-service case arrives that is:
- `is_extension = True`, AND
- **same product kind + same scope** as the current serving enrollment, AND
- window **starts in the future** (`S2 > today`),

then **do not switch**. Instead:
- Ensure a `SCHEDULED_EXTENSION` enrollment exists bound to the reauth case
  (create it, carrying roster/dietary/verification; link `supersedes` → current).
- Leave the current enrollment governing + serving.
- If this handoff **needs attention** (gap `S2 > E1`, or kind/scope mismatch that
  we chose NOT to defer), apply the **"Reauth Attention"** tag.

### 4. Governance = date-aware candidate filtering
- Rather than rewrite `governing_case_key` semantics everywhere, **exclude a
  dormant future extension from the governing candidate set** while a current case
  is active (given `today`). The reauth enters the candidate pool only at
  `max(E1, S2)`.
- This keeps PO / eligibility / program-status reading the current case during the
  deferral (decision #6) for free.

### 5. Activation & gap handling (date-driven)
A **daily** re-evaluation (see #6) performs the calendar-day transitions:
- **At `E1`** (current window end) while reauth not yet active:
  - Current enrollment → **`service_complete`**, **all members paused**.
  - (It becomes `closed` when its case closes — normal path.)
- **At `max(E1, S2)`** (reauth effective):
  - Promote the `SCHEDULED_EXTENSION` enrollment → `service_active`, carrying
    kitchen/cadence/dietary/`verified_at`/dependents (reuse the carry helpers +
    the reuse-path scope-pause fix), rebuild its delivery calendar.
  - Close the old enrollment (supersedes link preserved for history).
  - **No re-verification.**
  - Emit timeline event: *"Service extended via reauthorization case X."*
  - **Auto-clear** the "Reauth Attention" tag on a clean switch.

### 6. Re-evaluation timing
- **Detection/parking** runs inside `reconcile_internal_service_authorization`
  (ext + nightly import).
- **A daily celery-beat task + management command** walks every client with a
  `SCHEDULED_EXTENSION` enrollment and applies the pause-at-`E1` / activate-at-`S2`
  transitions on the correct calendar day (so it fires even on days with no case
  update). The management command doubles as the manual/backfill tool.

---

## Edge cases

- **Already-active / backdated reauth** at import (`S2 ≤ today`): switch
  immediately ONLY when the current case's window has also ended (nothing left to
  protect). If a backdated reauth OVERLAPS a still-active current window, it is
  still deferred until the current window ends (`max(E1, S2)`) so the current
  service is never cut short. Both the governing filter and the daily activation
  task use the same `max(E1, S2)` switch point, so an ext save, a CSV import, and
  the daily task all agree.
- **Different kind or scope** → not an extension; immediate switch as today, and
  flag "Reauth Attention".
- **Multiple stacked reauths** → the daily task picks the next `max(E1, S2)`
  boundary; only one waiting enrollment serves at a time.
- **Reauth case closed/cancelled before it activates** → discard the waiting
  enrollment (or close it) so it never activates.
- **Current case closed early (before `E1`)** → reauth activation logic must still
  honor `max(E1, S2)`; a gap opens → pause + attention tag.

---

## Phased implementation

1. **Model + backfill** — `Case.is_extension` (derive on save) + backfill command.
2. **Waiting stage + exclusions** — add `SCHEDULED_EXTENSION`; wire it into every
   inert surface (PO, matrix, calendar, verification queue/report, governing
   helpers). Regression tests that it never serves.
3. **Governance filter** — date-aware exclusion of dormant future extensions from
   the governing candidate set; parking branch in
   `reconcile_internal_service_authorization`.
4. **Activation + daily task** — pause-at-`E1` / activate-at-`max(E1,S2)`;
   celery-beat task + management command; timeline event.
5. **Reauth Attention tag** — get-or-create `ClientTag`; apply on gap / mismatch;
   auto-clear on clean switch.
6. **Tests** — deferral (no early switch), overlap (wait to `E1`), gap (pause +
   tag), contiguous (clean handoff), kind/scope mismatch (immediate + tag),
   backdated reauth (immediate), history preserved (both enrollments +
   supersedes), waiting stage excluded from PO/matrix/calendar/verification.

---

## Decided details
- **"Reauth Attention" `ClientTag` color: Red.**
- **Gap-pause label: "Reauthorization"** — a member paused during a window gap
  surfaces this label (distinct from the generic Paused chip) so it's clear the
  pause is a reauthorization handoff, not an agent pause.

- **Waiting `SCHEDULED_EXTENSION` enrollment is visible on the member Programs
  tab, read-only**, showing its scheduled start, own authorization window, and
  planned kitchen + cadence.
- **`ScheduleStatus.WAITING`**: a parked reauth gets DISPLAY-only `WAITING`
  delivery schedules mirroring the current kitchen/cadence. `WAITING` never
  generates PO occurrences (every occurrence/PO path filters `status=SCHEDULED`);
  at activation the `WAITING` rows are dropped and a fresh `SCHEDULED` plan +
  calendar are rebuilt from the live kitchen/cadence. Only OPEN reauth cases are
  parked (a closed/cancelled reauth is never parked and is discarded if found).
