# Dependent Split — Implementation Plan

Split a household **dependent** (non-primary member) into their **own** internal-service
case when they get their own case in Unite Us and an agent requests verification for them.

## Scope gate
Only fires when the verification subject is a **non-primary `HouseholdMember`** of a household
whose enrollment carries their `MemberDietaryProfile`. Members **not in a household** or
**already primary** → unchanged (today's behavior). No checkbox — auto on verification request.

## Locked decisions
1. Old profile is retained with `MemberStatus.REMOVED` (label "Removed"); excluded from service
   and exempt from `sync_household_members` roster re-add.
2. Split runs automatically when verification is requested for a detected dependent.
3. **No re-verification**: carry the verified fact (`verified_at`/`verified_by` + verified flags)
   from the household enrollment so the new enrollment is already verified.
4. Delivery address + notes always copied from the **primary**.
5. Nutritionist:
   - Already approved → copy enrollment-level (`nutritionist_approved_at/by/signature/
     signature_image/approval_pdf_key`) + per-member (`meal_plan`, `meal_plan_other`,
     `assessment_notes`, `nutritionist_pdf_key`); treat new enrollment as Nutritionist Approved.
   - Not yet approved → skip re-review, apply `Pending Nutritionist` tag, surface on a new
     Nutritionist-section page.
6. Status preserved from old profile: ACTIVE → active; paused/out-of-orbit/out-of-range/inactive
   → keep status + apply `Need Attention` tag.

## Key existing infrastructure
- `MemberVerificationCreateView` (POST /members/<id>/verification/) — creates new enrollment +
  profiles + verifies (api/portal/views_members.py ~5092-5499).
- `ensure_primary_of_own_household`, `ensure_household_with_primary` (api/serializers.py).
- `_promote_removed_member_to_own_household` (api/portal/views_members.py ~3665-3736).
- Carry helpers (api/services/lifecycle.py): `_carry_verification_fields`, `_CARRY_PROFILE_FIELDS`,
  `_create_missing_carried_profiles`, `_carry_service_and_activate`; delivery.py
  `current_household_cadence`, `create_member_delivery_schedules`.
- Tag pattern: `set_reauth_attention` / get-or-create `ClientTag`.
- Extension case save: `sidepanel/sidepanel.js` `saveCases()` POSTs `/api/cases/bulk/`, shows
  `setCaseStatus()`.

## Phases
1. **Ext warning**: backend adds `warnings` to `/api/cases/bulk/` + `/api/cases/` responses when
   the saved case's client is a non-primary household member; extension `saveCases()` displays it.
2. **`MemberStatus.REMOVED`**: enum + `SERVICE_EXCLUDED_MEMBER_STATUSES` + `sync_household_members`
   exemption + frontend chip.
3. **`split_dependent_into_own_enrollment(...)`** service: copy per-member + common (from primary)
   + nutritionist + kitchen/cadence + status; carry verified; detach old (status REMOVED +
   timeline); own household.
4. **Wire-in**: detect + auto-run in `MemberVerificationCreateView`.
5. **Tags**: `Need Attention` + `Pending Nutritionist` (get-or-create helpers).
6. **Nutritionist page**: backend list endpoint + frontend page for `Pending Nutritionist`.

## Tests
Split active / paused (+Need Attention) / nutritionist-approved (moved) vs not (+Pending
Nutritionist) / kitchen+cadence carried / old profile REMOVED survives sync / address copied from
primary / carried-verified (no re-verify). Full `MemberVerificationCreateView` integration.
