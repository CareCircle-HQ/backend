# "Not eligible for wrong opened case" — WITHDRAWN

⚠ **This design was wrong and must not be implemented.** The authorisation data
disproves its central premise. Kept because the reasoning that produced it is
persuasive and will be produced again.

---

## What killed it

Every population this document proposed to mark Ineligible is **approved by the
payer**:

```
meals-eligible, holding a BOX case      219    218 approved · 1 never_requested
boxes-eligible, holding a MEALS case    103    102 approved · 1 never_requested
NO food eligibility, holding either     217    212 approved · 5 never_requested
```

**532 of 539 approved.** Not one denial.

And it is not a timing artefact — the obvious defence, that the case was opened
against an older assessment:

```
case opened AFTER the latest assessment      198
NO assessment ever permitted food            116    yet the case is approved
an EARLIER assessment DID permit food        101
```

116 members hold an approved food case although **no assessment they have ever had
named a food service**, and 198 of these cases were opened *after* the assessment
that supposedly forbids them.

> **The eligibility assessment's service list does not gate which cases may be
> opened or approved.** A food-eligible member may hold meals or boxes, and members
> move between them: of the 219, 106 had a meals case at some point, 113 never did,
> and 25 hold both at once.

Had this shipped, ~511 members would have been marked Ineligible and had their
deliveries stopped — for holding cases the payer had approved.

## The mistake worth remembering

The document reasoned entirely from strings: the assessment says X, the case says Y,
therefore Y is wrong. It never asked the one question that settles it — **did the
payer approve it?** That was a single query, and it answers outright.

It also felt rigorous. It measured populations, distinguished confident cases from
ambiguous ones, warned that a third of members could not be judged, and chose the
strictest reading of the governing-assessment rule. All of that care was applied to
a premise nobody had checked.

**Before proposing another rule of this shape: check `service_authorization_status`
first.**

## What survives

Two things were right and are worth keeping:

- **The governing screening and governing assessment** — the most recent of each,
  with earlier ones invalid. That is the correct reading of a re-determination, and
  the Service Tracker uses it.
- **No governing assessment means no verdict.** ~5,941 of 17,056 members with a live
  internal-service case have no assessment at all. Any rule reading "no assessment"
  as "not qualified" would mark a third of the book ineligible.

The Service Tracker was rebuilt on this finding: its food rule now asks whether a
food case EXISTS, not which one, and the two "wrong kind of food case" alerts were
removed.
