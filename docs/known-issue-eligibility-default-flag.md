# Known issue: "Eligibility" is a default flag, not an affirmative determination

Status: **to fix later**. Surfaced auditing the Data page. Not blocking — the Data
page mirrors the Members-page value — but the "eligible" count is misleading and
should be reworked into a real "qualified" signal.

## Problem
`eligibility` is computed purely from the client's lifecycle stage
(`api/portal/serializers.py::get_eligibility`):
```python
"ineligible" if client.lifecycle_stage == ClientStage.INELIGIBLE else "eligible"
```
So **"eligible" is the DEFAULT** — every member who has NOT been actively flagged
`INELIGIBLE` reads "eligible", including brand-new members with **no cases and no
evaluation at all**. It does NOT mean the member was assessed and approved.

- `ineligible` = meaningful: the member was actively gated off (expired/missing
  Medicaid, wrong plan type, out-of-range ZIP/state, out of orbit).
- `eligible` = "not flagged ineligible" = a default that over-counts.

### Evidence (local snapshot, No Case Created bucket)
Of 24,060 `no_case + eligible` members: 23,945 have insurance on file, 23,816
have social coverage, 17,466 were screened, 5,933 have an eligibility assessment
-- BUT 6,552 have neither screening nor assessment, and 102 are bare records with
no coverage at all. So the label lumps vetted-with-coverage members together with
never-evaluated ones.

## Interim mitigation (DONE)
The Data page **hides the "By Eligibility" breakdown when filtered to No Case
Created** (where the default "eligible" is most misleading). See
`frontend/src/app/pages/DataPage.tsx`. The eligibility filter/column still exists
for parity with the Members page.

## Proposed real fix (LATER)
Introduce a derived **"Qualified"** signal that reflects an affirmative
determination rather than the absence of a flag, e.g.:
- not `INELIGIBLE`, AND
- has valid Medicaid + valid social coverage, AND
- has a screening (and/or eligibility assessment).

Expose that on the Data page (filter + breakdown) instead of / alongside the raw
`eligibility` flag, and consider relabeling the raw value (e.g. "Not Ineligible")
so it isn't read as "approved".
