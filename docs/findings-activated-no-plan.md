# Findings: "activated but no delivery plan" family + the 2026-09-14 incident

Two threads from one session:
1. an investigation started from the Data page `review` filter, and
2. a production incident (site-wide slowness) hit part-way through.

Everything below is shipped unless it sits under OPEN ITEMS.

---

# Part 1 -- "activated but no delivery plan"

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
while the member page showed none -- which is why it stayed invisible.

## Shipped

- `2e6f806` Relatives inherit a household case only if they are a member profile
  on that enrollment. Stopped uncovered relatives (e.g. ELLIANNA BROASTER)
  inheriting a case that isn't theirs -> `no_case`.
- `4c347e5` `rebuild_delivery_calendar` bootstraps a missing plan from the
  enrollment's own `delivery_weekdays` when there's no superseded predecessor,
  via new `cadence_matching_weekdays` (EXACT set match against the Cadence
  settings table -- never a guess; 8 of these were Tue-Only, which the old
  weekday derivation would have collapsed to once_a_week and delivered on the
  wrong day). `sync_delivery_calendars` already selected these households; it
  just couldn't fix them before.
- `12bd53c` Timeline event "Not returned to service" (Needs Review) emitted by
  `_carry_service_and_activate` whenever a member ends a case replacement in a
  non-servable status. Replaces a bare `except: pass` whose OUTCOME was never
  checked. Keyed off the resulting STATUS, not just whether the call raised --
  the real case (JENII DAVIDSON) raised nothing at all.
  Also: the error message now says "Programs tab" (the label agents see) rather
  than "Household tab" (the component name), which had sent an agent hunting for
  a tab that does not exist.
- `08f7bdb` + frontend `3cf0ceb2` Pause allowed from PENDING and INACTIVE;
  `pause_prior_status` (migration 0260) restores the pre-pause status on unpause,
  so pause+unpause cannot activate a member past the kitchen/nutritionist gates.
- `d623c985` Executive dashboard tooltips (~45) explaining each calculation.

## Verified effects (production clone, then applied to prod)

- `sync_delivery_calendars`: 15 member plans created across 14 households,
  424 occurrences added, 124 removed.
- Review bucket: 30 -> 14.
- 93 stranded `service_active` enrollments (kitchen, no plan) -> 14 healed. Of
  the 79 untouched, 91% have no servable member (66 out_of_orbit, others
  inactive/paused) and 2-3 have no cadence to infer. Both are correct refusals.

## Why members go INACTIVE and never come back

Nothing in the current code creates INACTIVE except a one-off roster import
(`hold_pending_closure_from_file --cancel`). The household cancel/reactivate flow
that used to set it was DELETED in `c1548d0` (2026-07-27) -- and the reactivate
half went with it, which is why there was no route back until the "Return this
member to service" checkbox was added. It is a finite fossil population, not a
growing one.

JENII DAVIDSON is the worked example: cancelled 2026-07-29 by an agent (two days
after that flow was deleted), then on 08-03 a governing-case replacement copied
the terminal status onto a NEW enrollment and activated it in the same second.

---

# Part 2 -- Incident 2026-09-14: site-wide slowness

Reported as slow saves from the extension and a slow members list. Neither
touches Unite Us; both were **starved of gunicorn workers**.

## Cause

`POST /api/portal/members/<id>/refresh-uniteus/` (the member page's "Refresh from
Unite Us", no case_id) ran the whole daily pull INLINE, and `DailyPull.execute`
loops over EVERY active credential, reprocessing the same person on each:

```
for cred in creds:          # ~104 active, ALL on one provider
    for cid in client_ids:  # the ONE member being refreshed
        self._process_person(cid)   # full pull: person + cases + coverage + notes
```

Measured production runs: **707s, 473s, 841s, 908s** (up to 15 minutes per click).

**Corrected mechanism.** The first theory was "dead credentials each cost a 30s
timeout". Probing proved the opposite: 98 of ~104 credentials are ALIVE. The cost
was ~98 healthy credentials each doing a COMPLETE, redundant re-pull of the same
member. 97 of those 98 passes were pure waste.

This failure was already known: `/api/uniteus/pull/` carries a comment describing
it exactly and is gated behind `UNITEUS_ONDEMAND_SYNC_ENABLED` (unset in prod, so
that endpoint returns 503 and was NOT the source). The gate was never applied to
the member-page refresh, which had the identical inline fan-out.

## Shipped (deployed via PR #304 / `269bf01`, gunicorn restarted 13:13 EDT)

- `d02264f` Both on-demand paths pin to ONE credential (`credential_id`), exactly
  as the case-scoped path already did -- which is why refreshing a single CASE
  was fast while refreshing the MEMBER was not. The nightly cron keeps the
  fan-out: different sessions can see different people, which is its purpose.
  Also: a 401 now retires the credential (previously `_mark_expired` was only
  reachable from the token-REFRESH path, so a revoked-but-fresh-looking token
  failed for ever and stayed ACTIVE). A 403 deliberately does NOT retire -- it
  means "this session cannot see THAT record", and retiring on it would kill
  working sessions. The probe confirmed this distinction matters: 6 credentials
  raised UniteUsAuthExpired via 403 and correctly stayed active.
- `c714365` `emit_timeline_event` adopts the winner's row when it loses the
  dedupe_key race (was: `IntegrityError` on `unique_timeline_dedupe_key` aborting
  a member's import). The savepoint is required, not decorative -- on Postgres an
  IntegrityError poisons the surrounding transaction.
- nginx: explicit `default_server` catch-all. Unknown-Host probes get `444`
  instead of reaching Django; `GET /` is answered by nginx with `ok`, so the ALB
  health check no longer needs a free gunicorn worker. Previously `/` was served
  by Django's root view, so worker exhaustion would have failed the health check
  and pulled the only target out of service -- turning slowness into a full
  outage. Verified: real host 200, private IP `ok`, unknown host 000.

## Things that were NOT wrong (checked, ruled out)

- `DisallowedHost` errors in the gunicorn log: benign. Django converts
  SuspiciousOperation to a 400; the traceback pointed at `PartnerHostMiddleware`
  only because it is the first caller of `get_host()`. The Host values were the
  ALB's own public IPs -- external probes hitting the load balancer by IP.
- The credential pool: healthy (98 alive). An earlier suggestion to bulk-retire
  the 33 credentials not captured in 7 days would have destroyed working
  sessions. Do not do that.

---

# OPEN ITEMS

1. `replace_enrollment_for_case_change` copies `status=mv.status` verbatim onto
   the new enrollment, including terminal INACTIVE -- same defect as the
   household-split copy, different path. 4 households sit in exactly JENII's
   state; ~20 came through a replacement carrying a non-servable status.
   Decision deferred: apply the same "terminal -> PENDING" mapping here?
2. OMAR OLGUINGONZALEZ-type: non-primary member whose profile stayed `pending`
   when the household activated (household-scope case). A third path into
   "household active, member left behind". The PENDING->ACTIVE promotion exists
   only in `_carry_service_and_activate` and kitchen assignment; an activation
   path that skips both strands the member. Not yet root-caused.
3. 53 relatives who ARE profiles on a household enrollment whose case scope still
   says `individual` (stale scope -- likely the CASE is wrong, not the
   attribution). Includes LAVON PAYNE receiving 26 delivery occurrences under
   LEGEND MARTIN's individual-scope authorization -- a possible billing /
   authorization question, not just a display bug.
4. `reconcile_enrollment_calendar` early-returns on a meals<->boxes `requeue`
   before reaching the bootstrap, so a household mid-switch skips the heal.
   Didn't affect the 22 investigated; worth checking whether it strands others.
5. Data-page `_cadence_for_client` falls back to `delivery_weekdays`, so an
   unplanned household still displays a cadence that does not exist. Removing the
   fallback makes the Data page agree with the member page but changes the
   cadence filter -- needs a decision.
6. Move the on-demand Unite Us refresh OFF the request path (Celery + Redis are
   already running for CSV imports). Pinning to one credential took it from ~900s
   to ~1s, but it is still synchronous.
7. Give that refresh a BOUNDED credential fallback (the agent's own, then 2-3
   others) instead of one-or-nothing. Today, if the single chosen credential is
   dead the agent gets "reconnect" even though another would have worked -- a
   deliberate trade, but a regression in resilience.
8. `ImportRun` observability: counts are written only in `finalize()`, so a run
   shows `0 / RUNNING` throughout and a killed process leaves the row RUNNING for
   ever. This is very likely the mechanism behind the stuck "Prepare Members for
   PO" job at ~53%, which still blocks that feature.
9. Rebuild caveat: mid-rebuild the read model mixes old and new logic, and the
   dashboard "as of" reads max(refreshed_at), which looks current while the table
   is only partly current. Could stamp rebuild start/finish.
10. Tooltip wording soft spots (text otherwise verified against the code):
    "Inactive Members (Case Exists)" glosses Needs Review as one of the five;
    the unpause dialog's "will return to Inactive" relies on
    `pause_prior_status` being set, and legacy paused rows fall through to the
    meal-rule path.
11. Older items: the deleted "Not Being Served" drill-down (endpoint still live
    -- could move to the CS tab), Williamsburg clients on non-Williamsburg
    kitchens, and the DCA partner credential that needs rotating (its secret
    appeared in a transcript).
12. The ALB health check now proves nginx is alive, not Django. Correct for a
    single instance; revisit if a second instance is ever added.

---

# Deployment state (2026-09-14, end of session)

Backend: **deployed**. `main` = `269bf01` (merge of `dev`, PR #304), including
both incident hotfixes. Migrations through `0260` applied (`showmigrations`
confirms `0259` and `0260` as `[X]`).

Frontend: **verify**. `3cf0ceb2` (pause from Pending/Inactive) and `d623c985`
(dashboard tooltips) are frontend-only. Their backend support is live; the UI is
not until the frontend is deployed.

Not yet proven in production: an on-demand "Refresh from Unite Us" after the
13:13 restart. Expect 1-3s instead of 700-900s. The query to check:

```
python manage.py shell -c "
from django.utils import timezone
from datetime import timedelta
from api.models import ImportRun
for r in ImportRun.objects.filter(source='uniteus', started_at__gte=timezone.now()-timedelta(hours=1)).order_by('-started_at')[:5]:
    s=(r.finished_at-r.started_at).total_seconds() if r.finished_at else None
    print(str(r.started_at)[11:19], s, r.status, r.triggered_by)
"
```
