# Incident: members falsely marked Verified + advanced to Kitchen Assignment (case_replaced)

Status: **remediation available** (revert command); **root fix pending** (lifecycle).
Surfaced 2026-08-21 (Friday): "many members marked Verification Completed with
false Requested/Completed dates, moved to Kitchen Assignment, skipping the
verification wizard AND the nutritionist step."

## Root cause
It was NOT the Meal-Inputs importer. It was the regular **CSV/Unite Us import**
replacing governing cases. When a governing internal-service case is replaced,
`api/services/lifecycle.py::_carry_service_and_activate` carries the prior
enrollment's verification forward and ladders the new enrollment
`pending_verification -> verified -> kitchen_assignment`.

The carry's guard only checks that `verified_at` is **non-null**
(`if not new_enr.verified_at: return False`) -- it does NOT check that the
verification was **real**. A member whose `verified_at` is only a **system stamp**
(`verified_by` NULL, never a real wizard verification, `nutritionist_approved_at`
NULL) therefore gets propagated into Kitchen Assignment on every case replacement,
skipping verification + nutritionist. A large batch of case replacements in one
import mass-advances these never-really-verified members. StageEvents show
`metadata.trigger == "case_replaced"`, `source == "auto"`, all stamped the import
date; `verified_by` NULL, `nutritionist_approved_at` NULL.

Confirmed on the local snapshot: of the clients with a system-verified enrollment
dated 8/21, **none had ever had a real `verified_by`** on any enrollment.

## Detection / remediation
`python manage.py revert_falsely_verified_enrollments --since 2026-08-21 [--list]`
(dry-run) -> `--apply` to revert to Pending Verification (clears the false
verification fact + carried kitchen/cadence, pulls future deliveries, recomputes
lifecycle). Serving rows (SERVICE_ACTIVE/ON_HOLD/SERVICE_COMPLETE) are held back
unless `--include-serving` (reverting mid-delivery is disruptive -- review first).

Signature: `stage_events.metadata.trigger = "case_replaced"` AND
`verified_by IS NULL` AND `nutritionist_approved_at IS NULL`. This deliberately
spares REAL verifications (real `verified_by`) and grandfathered
nutritionist-approved households.

## Proposed root fix (LATER)
In `_carry_service_and_activate` (and the carry), do NOT treat a system-only
verification as sufficient to advance past verification. Options:
- gate the ladder-forward on a REAL verification (`verified_by` set) OR a genuine
  nutritionist sign-off, not merely `verified_at` non-null; and
- never enter KITCHEN_ASSIGNMENT without `nutritionist_approved_at` unless the
  household was already serving (prior_was_serving).
Until then, re-run the revert command after imports that replace many cases.
