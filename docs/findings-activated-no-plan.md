# Findings: "activated but no delivery plan" family (work in progress)

Investigation thread started from the Data page `review` filter. Saved
mid-investigation to pick up later. Status: several fixes shipped, several open.

## Root cause (established)

`create_member_delivery_schedules` (api/services/delivery.py):
1. SAVES `enrollment.delivery_weekdays` FIRST,
2. then aborts with `return []` if no servable member exists (PENDING / INACTIVE /
   OUT_OF_ORBIT / PAUSED are all excluded),
3. while the CALLER still advances the enrollment to `service_active`.

Result: enrollment with kitchen + weekdays stamped but zero
`MemberDeliverySchedule` rows and zero calendar occurrences. Both heal paths then
asked the missing PLAN for the cadence (`current_household_cadence`), so nothing
repaired it. The Data page showed a cadence (derived from `delivery_weekdays`)
while the member page showed none.

## Fixes shipped this session (on `dev`, NOT deployed)

- `2e6f806` Relatives inherit a household case only if they are a member profile
  on that enrollment. Stopped uncovered relatives (e.g. ELLIANNA BROASTER)
  inheriting a case that isn't theirs -> `no_case`.
- `4c347e5` `rebuild_delivery_calendar` bootstraps a missing plan from the
  enrollment's own `delivery_weekdays` when there's no superseded predecessor,
  via new `cadence_matching_weekdays` (EXACT set match against the Cadence
  settings table -- never a guess). `sync_delivery_calendars` already selected
  these households; it just couldn't fix them before.
- `12bd53c` Timeline event "Not returned to service" (Needs Review) emitted by
  `_carry_service_and_activate` whenever a member ends a case replacement in a
  non-servable status. Replaces a bare `except: pass` + unchecked outcome.
  Also shipped: error message now says "Programs tab" (the visible label)
  instead of "Household tab" (the component name).
- `08f7bdb` + frontend `3cf0ceb2` Pause now allowed from PENDING and INACTIVE;
  `pause_prior_status` field (migration 0260) restores the pre-pause status on
  unpause so pause+unpause can't activate a member past the gates.
- `d623c985` Executive dashboard tooltips (45) explaining each calculation.

## Verified effects (local clone)

- `sync_delivery_calendars` ran: 15 member plans created across 14 households,
  424 occurrences added. `rebuild_enrollment_analytics` followed.
- Review bucket: 30 -> 14 afterwards.
- 93 stranded `service_active` enrollments (kitchen, no plan) -> 14 healed; the
  other 79 mostly have no servable member (66 out_of_orbit, others
  inactive/paused) or no cadence to infer (2-3).

## OPEN ITEMS (not done, by design or not yet)

1. `replace_enrollment_for_case_change` copies `status=mv.status` verbatim onto
   the new enrollment, including terminal INACTIVE -- same defect as the
   household-split copy, different path. 4 households sit in exactly JENII's
   state; ~20 came through a replacement carrying a non-servable status.
   Decision deferred: apply the same "terminal -> PENDING" mapping here?
   Nothing creates INACTIVE anymore (the cancel flow was deleted in c1548d0),
   so it's a finite fossil population.
2. OMAR OLGUINGONZALEZ-type: non-primary member whose profile stayed `pending`
   when the household was activated (household-scope case). Third path into
   "household active, member left behind". The PENDING->ACTIVE promotion only
   exists inside `_carry_service_and_activate` and kitchen assignment; a
   normal activation path that skips it leaves the member stranded. Not yet
   root-caused.
3. 53 relatives who ARE profiles on a household enrollment whose case scope
   still says `individual` (stale scope -- likely the CASE is wrong, not the
   attribution). Includes LAVON PAYNE receiving 26 delivery occurrences under
   LEGEND MARTIN's individual-scope authorization -- possible billing angle.
4. `reconcile_enrollment_calendar` early-returns on a meals<->boxes `requeue`
   before reaching the bootstrap -- a household mid-switch skips the heal.
   Didn't affect the 22 investigated but worth checking whether it strands
   others.
5. Data-page `_cadence_for_client` falls back to `delivery_weekdays`, so an
   unplanned household shows a cadence that doesn't exist. Would make the Data
   page agree with the member page, but changes the cadence filter -- user's
   call.
6. Tooltip audit: text checked against the code and it MATCHES
   (delivered="previously" -> `in_any_po` = any non-cancelled PO line;
   has_active_delivery = upcoming SCHEDULED occurrence OR live DeliveryOrder).
   Two wording soft spots if precision matters: "Inactive Members (Case
   Exists)" glosses Needs Review as one of the five; "will return to Inactive"
   in the unpause dialog relies on pause_prior_status being populated
   (legacy paused rows fall through to the meal-rule path).
7. Rebuild-run caveat noted earlier: mid-rebuild the read model is a mix of old
   + new logic, and the dashboard "as of" reads max refreshed_at, which looks
   current while the table is only partly current. Could stamp rebuild
   start/finish to show "rebuild in progress".
8. Still outstanding from before: the stuck "Prepare Members for PO" job at
   ~53% (ImportRun row likely RUNNING forever, blocks the feature), the
   deleted "Not Being Served" drill-down (endpoint still live -- could move to
   CS tab), Williamsburg clients on non-Williamsburg kitchens, and the DCA
   partner credential that needs rotating.

## Deployment state at time of writing

`dev` is ~14 backend / ~7 frontend commits ahead of prod, migrations 0254-0260.
None of the above fixes are live until `dev` is merged to `main` and deployed.
