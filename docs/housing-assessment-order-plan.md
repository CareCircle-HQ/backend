# Housing Phase 1 — the Assessment Order

Follows `docs/housing-programs-plan.md`, which covers everything up to and
including a Dwelling Assessment case landing in the CRM correctly classified. This
document is the next step: turning that case into an **Assessment Order** a vendor
can execute at the member's home.

**Status: planning. No code written.**

---

## The flow

```
screener creates the Dwelling Assessment case in Unite Us
      |
      v  extension or CSV import                          <- DONE
   case arrives, classified housing / EEA, governs housing
      |
      v  an agent runs a WIZARD on the member profile     <- THIS DOCUMENT
   ASSESSMENT ORDER created, once per member
      |
      v  a vendor attends the home
   questionnaires answered + SIGNED on site -> locked PDF
   documents, proof images, findings attached
      |
      v  findings drive new Unite Us cases
   HOME REMEDIATION cases (work orders) import back and link to the order
```

---

## Rules, as given by the operator

| # | Rule |
|---|---|
| 1 | **One assessment order per MEMBER.** There is no "request" step — the verification agent creates it once. |
| 2 | One assessment order can have **many work orders** linked (the Home Remediation cases). |
| 3 | Each assessment order has **many documents**, **many proofs** (images) and **many findings**. |
| 4 | The assessment carries **questionnaires** the vendor answers and **signs at the client's house**. These produce a **PDF, locked after each signature**. |
| 5 | A member may have **many Home Remediation cases and many Dwelling Assessment cases**. We do not rebuild anything — we **keep the record**. |

### What rule 1 changes about the design

The Assessment Order is **per member, not per case**. That is a real departure from
the food side, where `EnrollmentVerification` hangs off a case/household. It also
means:

- the work-order link is **member-scoped**, which matches `housing_work_orders()`
  as already built (it links by member, because Unite Us gives no parent
  reference on the case);
- there is **no request/approve cycle** to model — no `verification_requested`
  equivalent, no queue of pending requests. One agent action, once;
- and the natural DB shape is a `OneToOneField(Client)` … which rule 5 then
  complicates. See the open questions.

### What rule 5 changes

"Keep the record, do not rebuild" is an explicit instruction to make this
**append-only**. No reconciliation, no supersede-and-replace, no derived state
that gets recomputed. That is the opposite of how food enrollments work, and it is
the safer choice here: the food side's rebuild/replace machinery is exactly what
forked 149 enrollments across three families.

So: new cases attach, nothing is rewritten, and history stays legible.

---

## What to build on (do not reinvent)

| Need | Existing pattern |
|---|---|
| Proof images | `DeliveryOrderProof` — S3 key + `content_hash` (sha256) + many-per-parent. The hash makes re-uploads idempotent and de-dupes the same image. |
| Vendor access | `api/partner/` — a SEPARATE hostname (`PARTNER_API_HOST`), client-id/secret → JWT, and the CRM's routes do not exist on that host. Already carries delivery companies. |
| Wizard on the member page | `MemberVerificationCreateView` + the 5-step verification wizard. Same shape: one button, several steps, one atomic write. |
| File storage | `django-storages` + S3, already configured (`USE_S3`). |

Reusing the partner host for the assessment vendor is the strong option: the
isolation already exists and is already reasoned about, so a vendor credential
cannot reach CRM endpoints even by accident.

---

## Open questions — these change the schema, so worth settling first

### Q1. "One per member" — hard constraint, or one ACTIVE per member?

Rules 1 and 5 pull in different directions: one order per member, but a member may
have several Dwelling Assessment cases.

A `OneToOneField(Client)` enforces rule 1 at the database level and is
self-documenting — but it makes a second assessment **impossible**, and a dwelling
assessment is about a **property**. A member who moves house is not an exotic
scenario, and neither is an assessment that could not be completed.

Options:
- **`OneToOne`** — simplest, matches rule 1 literally, blocks any second order.
- **FK + "one active per member"** — a partial unique constraint on
  `(client, active)`. Keeps the record (rule 5), still prevents two live orders.

Recommend the second unless you are certain a member can never need a second
assessment. It satisfies both rules and costs one migration rather than a painful
one later.

**What happens when a member moves address mid-assessment?**

### Q2. What exactly does "locked after each signature" mean?

This is the most delicate part of the feature — it is a signed record about
someone's home, so tamper-evidence matters more than convenience.

Two readings:
- **One PDF per questionnaire**, locked when that questionnaire is signed; or
- **One accumulating PDF**, re-locked after each of several signatures.

And "locked" technically:
- **Recommended:** generate the PDF, store it in S3, record its `sha256`, and mark
  the row immutable. A later signature produces a **NEW version** rather than
  mutating the object. That gives a verifiable chain — any bytes change is
  detectable — and keeps every intermediate state.
- Mutating a stored PDF in place would destroy the evidence value of the
  signature, so it is worth ruling out explicitly.

**Who signs — the vendor only, or the member too? And can anything be
un-locked, by whom, with what audit trail?**

### Q3. How does a work order link back to its finding?

Rule 3 gives findings; rule 2 links work orders. But the causal link — *this*
Home Remediation case exists because of *that* finding — has no carrier: Unite Us
sends no parent reference on a case, and the work orders arrive as ordinary
imported cases.

Options:
- **Member + program matching** — a "De-humidifier" case pairs with a damp
  finding. Fragile, and silently wrong when two findings could produce the same
  device.
- **Agent links it** — a dropdown on the work order. Reliable, costs a click.
- **No link** — work orders attach to the ORDER (rule 2) but not to individual
  findings.

The last is the cheapest and may be enough. Worth knowing whether "which finding
caused this repair?" is a question anyone will actually ask.

### Q4. Does the vendor work offline?

Signing happens **in the client's house**. Basements, poor signal, and a
questionnaire that must not be lost after 40 minutes of work.

If offline capture is needed, that is a substantially larger piece: local
persistence, sync, conflict handling, and an answer to "the vendor signed at 14:02
but it arrived at 18:30". If the vendor can be assumed online, the API is
straightforward.

**Worth answering early — it is the difference between a form and a sync engine.**

### Q5. Who creates the Home Remediation cases in Unite Us?

The findings drive new cases, but do **we** create them (an API write to Unite Us)
or does an agent create them by hand, after which they import back as normal?

The current direction of travel is one-way: Unite Us → CRM. Writing back is a new
capability with its own failure modes.

### Q6. What do the questionnaires contain?

You mentioned the questions are still to come. Whether they are **fixed** or
**configurable** decides the schema: hard-coded fields, or a
question/answer/version model. Configurable is more work but survives the first
revision of the form; fixed is faster and cheaper to get right.

---

## Sketch, for discussion only

```
HousingAssessmentOrder
    client            FK (one ACTIVE per member -- Q1)
    case              FK to the governing EEA case, nullable
    status            draft -> dispatched -> in_progress -> complete
    created_by        the verification agent
    vendor            who executes it
    ... wizard answers ...

HousingAssessmentDocument        many per order   S3 + sha256
HousingAssessmentProof           many per order   S3 + sha256   (mirrors DeliveryOrderProof)
HousingAssessmentFinding         many per order   the inspection results
HousingAssessmentQuestionnaire   many per order
    signed_at / signed_by / pdf_s3_key / pdf_sha256 / locked   (Q2)

work orders: the Home Remediation CASES, linked by member today
             (housing_work_orders), or by FK if we add one (Q3)
```

Deliberately NOT included: any reconcile, rebuild or supersede step. Rule 5 says
keep the record, and the food side is the cautionary tale.

---

## Dependencies

- `docs/housing-programs-plan.md` — steps 1, 2, 2b and the UI are **done**; housing
  cases import and classify correctly, and cannot touch food service.
- The new **verification type** for housing is referenced in the flow but was
  deferred ("we will discuss later"). The Assessment Order is what the
  verification produces, so the two are the same conversation.
