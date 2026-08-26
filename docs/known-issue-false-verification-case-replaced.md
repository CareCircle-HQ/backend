# Incident: members falsely marked Verified + advanced past verification

Status: **root fix applied** (`reconcile_member_stages`); **remediation available**
(`revert_falsely_verified_enrollments`). Surfaced 2026-08-21 (Friday): "many
members marked Verification Completed with false Requested/Completed dates, moved
to Kitchen Assignment, skipping the verification wizard AND the nutritionist step."

## Root cause (confirmed on a prod clone, 2026-08-25)
The dominant Friday path was the **`reconcile_member_stages`** management command
-- NOT the daily import and NOT the Meal-Inputs importer. Its decision table
**conflated AUTHORIZATION with VERIFICATION**: for an APPROVED internal-service
case it called ``_set_verified_at()`` (stamped ``verified_at = now``, leaving
``verified_by`` NULL) and advanced the enrollment to Kitchen Assignment /
Service Active -- inferring "verified" purely from the Unite Us authorization,
with no real verification and no nutritionist sign-off. On 8/21, 101 of 108
system-verified rows carried its note "Reconcile: approved but delivery data
incomplete."

Secondary/older origins (same shape -- ``verified_at`` set, ``verified_by`` NULL,
no nutritionist): the bulk file imports -- "Imported from Meal Inputs verification
sheet" (~1,164, mostly 6/29), "Imported from LIST 3", "LIST 2 import", "Active
Members import", "Williamsburg exception". The case-replacement carry
(``_carry_service_and_activate``) then PROPAGATES any of these forward on later
imports (it only checks ``verified_at`` is non-null, not that it was real).

Full prod-clone scope: ~1,645 clients are system-verified + advanced +
no-nutritionist; 50 currently live via the case_replaced trigger, ~99 from the
8/21 reconcile run.

## Root fix (APPLIED)
`api/management/commands/reconcile_member_stages.py`: authorization is no longer
treated as verification. An enrollment that is NOT already verified
(``verified_at`` is NULL) now stays at **Pending Verification** regardless of an
approved authorization; only an ALREADY-verified enrollment is advanced, and the
command never stamps a fresh ``verified_at``. Validated on the clone: the 12
never-verified + approved rows stay Pending (were previously stamped + pushed to
Kitchen Assignment); the 871 already-verified advance legitimately.

NOTE (still open): the carry (``_carry_service_and_activate``) will still
propagate a PRE-EXISTING system-only verification. A carry-level guard is risky
(a ``verified_at``-only household is treated as verified across many legit
flows -- gating on ``verified_by`` broke 16 tests). Preventing the origin
(reconcile_member_stages, and reviewing the bulk importers) is the durable fix.

## Remediation
`python manage.py revert_falsely_verified_enrollments --since 2026-08-21 [--list]`
(dry-run) -> `--apply`. Signature: ``verified_by`` NULL AND
``nutritionist_approved_at`` NULL AND advanced stage. Reverts to Pending
Verification (clears the false verification + kitchen/cadence, pulls future
deliveries, recomputes lifecycle). Spares real / grandfathered-nutritionist
verifications. ALWAYS scope with ``--since`` -- without it, historical bulk-import
verifications (e.g. 6/29 Meal Inputs) also match. Serving rows held back unless
``--include-serving``.
