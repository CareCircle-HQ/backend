# "Not eligible for wrong opened case"

Marking a member Ineligible when their **governing case is on a service the member
was never qualified for**, on both the extension save and the CSV import, and
restoring them when it is corrected.

Nothing in this document is implemented yet. It exists because the rules turned out
to interact in ways worth agreeing before writing code — in particular, a third of
the members in scope **cannot be judged at all**, and the one design decision that
looked obvious to me was wrong.

---

## 1. The governing screening, and the governing assessment

Two new "governing" concepts, deliberately shaped like the existing governing case:

> **The governing screening** is the member's MOST RECENT screening that lists
> services. **The governing assessment** is their MOST RECENT assessment that lists
> services. Anything earlier is INVALID — superseded the moment a newer one is
> recorded.

This is stricter than a union of a member's history, and it is the correct reading:
a new eligibility assessment is a re-determination, not an addition. If the newest
assessment does not name meals, the member is not eligible for meals, whatever a
previous one said.

⚠️ **I proposed the opposite first, and was wrong.** My reasoning was that of 58
members flagged on the latest assessment, 22 had an earlier one that DID permit
their case — so using the latest "created 22 false positives". That inverted the
rule: those 22 are not false positives, they are members whose eligibility was
withdrawn by a later assessment and whose case was never corrected. They are the
exact population this feature exists to find. Recorded here because the same
argument will look persuasive again.

The practical consequence is that the tracker's display rule and this gate agree,
which is worth having: an agent looking at the Service Tracker sees the same
eligibility the gate judged them on.

---

## 2. What each rule contributes

Measured on the local clone (a refreshed production copy), over members with at
least one LIVE internal-service case — `open`, `managed`, `pending_authorization` or
`off_platform`. 3,000 sampled of 17,056; the `~` figures scale to the full set.

```
live internal-service cases                         17,056

  A1  no assessment at all              34.8%   ~5,941   NOT JUDGED
  A2  latest assessment has no ECM       0.3%      ~45
  A3  latest assessment PERMITS it      62.0%  ~10,580   ok
  A4  latest permits a DIFFERENT food    1.7%     ~295   INELIGIBLE
  A5  latest names NO food service       1.2%     ~198   INELIGIBLE

  B1  no screening at all                4.7%     ~795   NOT JUDGED
  B2  latest screening includes Food    94.2%  ~16,072   ok
  B3  latest screening lacks Food        0.9%     ~147   INELIGIBLE
```

Distinct members, not rows:

```
  judgeable (has an assessment)                     ~11,075

  ineligible by rule 2, the assessment                 ~494
  ineligible by rule 1, the screening                  ~113
  by both                                               ~96
  by EITHER -- the population this creates             ~511

  screening-only (the assessment says ok)               ~17
```

**~511 members** would be marked Ineligible on the next import.

---

## 3. ⚠️ A third of the members cannot be judged

`A1` is the largest number in this document by two orders of magnitude: **~5,941
members with a live internal-service case have no eligibility assessment at all.**

They must be **skipped**, not flagged. A gate that treats "no assessment" as "not
qualified" would mark a third of the book Ineligible and stop their deliveries on
the next import.

`B1` (~795 with no screening) is the same problem, smaller.

This is the single most important line in the rule:

> No governing assessment → no verdict. Silence is not evidence.

---

## 4. The rule

```
reason: "Not eligible for wrong opened case"

FIRES when ALL hold:
  1  the member has a GOVERNING internal-service case
  2  its service_type is one we can judge:
       Medically Tailored Meals          <- MEALS
       Produce Prescription/Voucher      <- FOOD_PRESCRIPTION
  3  a GOVERNING ASSESSMENT exists                      (else: no verdict)
  4  that assessment does NOT name the service the governing case is on

CLEARS when any of 1-4 stops holding:
  - the wrong case is closed, or a correct one becomes governing
  - a new assessment names the service
  - the case is no longer a food service we judge
```

Both spellings of every eligibility result count — `Medically Tailored Meals (MTM)
(Food)` and the bare `Medically Tailored Meals (MTM)`, and the same for vouchers.
The bare forms appear on a few dozen older records and mean the same thing.

**Both services may be permitted at once, and then neither case is wrong.** The
check is against *what the assessment permits*, never against a single expected
answer. No member in a 600-strong sample had both — so the shortcut "it is one or
the other" would pass every test and fail the first time it mattered.

### Open questions

- **A5 (~198): the latest assessment names NO food service at all.** Distinct from
  A4, where a *different* food service is named. Is a food case wrong because the
  newest assessment was housing-only, or is that assessment simply incomplete?
- **B3 (~147): the latest screening does not include Food.** Only ~17 of these are
  not already caught by the assessment rule. Worth a separate reason string, or
  fold into one?
- **A2 (~45): no ECM in the latest assessment.** A different fault — no
  qualification at all rather than the wrong case. Currently a tracker warning.

---

## 5. Where it would hook in

`eligibility.evaluate_client` gains a fourth gate beside the three that exist
(medical insurance, medicaid type, address range). That is the whole integration:

- `reconcile_client_eligibility` already runs on **both** the extension save and the
  CSV import (`csv_import.py:909`, `views_members.py:274`).
- It already writes the member note, the timeline event and
  `Client.ineligible_reasons`.
- It already **auto-restores** a previously-ineligible member when the gates pass
  again, so "make eligible when the case is corrected" needs no new code.

⚠️ **It also stops future deliveries.** `reconcile_client_eligibility` truncates the
schedule and puts the member On Hold so no new PO includes them. For ~511 members
that is a real operational event, not a label — which is why a dry-run command that
lists exactly who is affected should come before the gate is switched on.

Note `apply_out_of_range_ineligibility` is deliberately **sticky** and never
auto-reverses. This gate must NOT copy that: a corrected case has to restore the
member automatically, which is what a normal gate in `evaluate_client` does.

---

## 6. Verifying

```
python manage.py check
python manage.py test api.tests --noinput --parallel 4
```

Migrations are disabled under the test runner, so a data migration cannot be
asserted on in the suite — run it against the local clone.

The read model is served from a replica; pin any verification query to the primary
with `.using('default')`, or it answers from a lagging copy.
