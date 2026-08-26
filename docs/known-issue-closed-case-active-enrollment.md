# Known issue: active enrollment mis-bound to a CLOSED case

Status: **partially fixed** (non-active stale enrollments now terminalized);
**deeper case remains**.

## What was fixed
`reconcile_internal_service_authorization` now enforces a per-enrollment
invariant (`_terminalize_closed_case_enrollments`): when a case closes but a
newer OPEN internal-service case keeps the client active, any **non-active**
enrollment still non-terminal on the closed case is terminalized (CLOSED if the
stage allows it, else CANCELLED). Runs at the case-save chokepoint (ext + CSV
import) after the governing-case carry, only on a partial close (an open case
remains). This clears the stale/parallel secondary enrollments a closed case used
to leave behind (~63 cleaned on the 8/26 snapshot; the out-of-orbit "any current
enrollment" filter no longer surfaces those).

## What remains
~1,080 clients have their **ACTIVE** (governing/serving) enrollment sitting on a
**closed** case while a separate OPEN case exists — mostly `validated` (594) and
`on_hold` (472). The invariant deliberately **skips the active enrollment**:
closing it blindly breaks legitimately-served members (see
`CsvImportRulesTest.test_cases_import_defers_reconcile_until_full_picture`, where
a kitchen_assignment survivor's `case` still points at the old closed case while
it's served via the open governing case).

The root cause is that `_bind_governing_case_to_serving_enrollment` /
`replace_enrollment_for_case_change` don't repoint a `validated` / `on_hold`
enrollment onto the new open governing case — so the survivor's `enrollment.case`
stays stale-closed. Fixing it means teaching the governing-case switch to
repoint (or reopen) those pre-service / paused enrollments, then the invariant
would terminalize any true leftover. Deferred: it touches the core case-switch
carry and needs its own regression coverage.
