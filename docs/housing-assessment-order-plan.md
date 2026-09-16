# Housing Phase 1 — the Assessment Order

Follows `docs/housing-programs-plan.md`, which covers everything up to and
including a Dwelling Assessment case landing in the CRM correctly classified. This
document is the next step: turning that case into an **Assessment Order** a vendor
can execute at the member's home.

**Status: planning. No code written.**

**Scope: CRM ONLY.** The vendor portal is explicitly deferred to a later task, so
nothing here depends on an external user being able to log in. That constraint has
one sharp consequence for the questionnaires -- see the scope-tension section.

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

### Q1. RESOLVED — labelled dwellings in a JSON field, not a OneToOne

> "let setup the Primary Dwelling to the governing case and lets add a secondary
> Dwelling the old Dwelling case, we dont need a one to one we can used a json
> field with labels"

So: **one order per member**, holding a JSON map of **labelled** dwelling cases —
`primary` is the governing EEA case, `secondary` the older one. A second assessment
does not need a second order; it becomes another label on the same order. That
resolves the tension between rules 1 and 5 without a partial unique constraint.

One cost worth accepting knowingly: a case id inside JSON has **no foreign-key
integrity** and is awkward to query. Nothing stops it pointing at a deleted or
wrong case, and "find every order whose primary dwelling is case X" becomes a JSON
scan rather than an index lookup.

Cheap mitigation, if you want it: keep a **real FK for the primary/governing case**
— the one the code actually resolves against — and use the JSON for the labelled
set and its history. Belt and braces, one extra column, and the JSON stays as
flexible as you intended. Worth deciding, not worth blocking on.

### Q2. RESOLVED — "locked" is an authorization rule, not a file format

> "locked mean the answers can not be modified by any crm internal users, only the
> vendor can do before they submitted the order or assessment as completed"

Much simpler than the PDF-versioning scheme I sketched. The rule is about **who may
write, and when**:

| Actor | Before the vendor submits | After submission as completed |
|---|---|---|
| **Vendor** | may edit their answers | ❌ no |
| **CRM internal users** | ❌ never | ❌ never |

So CRM users are **read-only on vendor answers at all times** — the CRM displays
the evidence, it does not author it. That is a clean separation and easy to state.

Two implementation consequences:

- **Enforce it server-side on BOTH surfaces**, not in the UI. The CRM endpoint must
  refuse writes to answers even for a manager, and the vendor endpoint must refuse
  them once `completed`. A UI-only rule would be bypassed by any direct call — the
  same lesson as the food verification, where the picker excluded housing cases but
  the endpoint still accepted one.
- **The PDF becomes a rendering of locked answers**, not the thing being locked.
  Still worth storing its `sha256` so the artifact can be proved unaltered, but the
  answers are the record.

**One thing this creates: there is now no correction path.** If a vendor submits
wrong answers, nobody can fix them — not even a manager. That is defensible for a
signed record, but it needs a deliberate answer:

- can a manager **reopen** an order, returning it to vendor-editable?
- if so, is the prior state kept (append-only, per rule 5) and audited?
- if not, is the remedy a **new** assessment order rather than an edit?

Silence here becomes a support ticket the first time a vendor fat-fingers a
finding.

**And: who signs — the vendor only, or the member too?**

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

### Q4. ANSWERED — both online and offline wanted

> "will be great if we can do online and offline."

Offline is the single most expensive requirement in this feature, so the honest
recommendation is **build online first, but design it offline-ready**, then add
local persistence as its own piece of work. Three specific things, cheap to do up
front and painful to retrofit:

| Design choice | Why it has to be decided now |
|---|---|
| **Client-generated UUIDs** for orders, answers and photos | An offline client retries. Without an id the client chose, a retry creates a DUPLICATE finding or a second signature. Idempotency has to come from the client, and the server has to honour it. |
| **`captured_at` (device) separate from `received_at` (server)** | The vendor signs at 14:02 in a basement and syncs at 18:30. Both timestamps matter, and the device clock is UNTRUSTED — it can be wrong or deliberately set. Storing one field loses the distinction permanently. |
| **Photos as their own idempotent uploads** | A questionnaire is bytes; a photo set is megabytes on a bad connection. If photos are part of the submission payload, a failed upload loses the answers too. Separate them so answers land immediately and images drain independently — the `content_hash` in `DeliveryOrderProof` already gives us upload idempotency for free. |

What is genuinely hard about offline, and should not be hidden: **conflict**. If the
same order is edited on two devices, or edited offline while a manager reopens it,
something has to win. Rule 5's append-only stance helps — keep both, mark one
superseded — but it needs stating rather than discovering.

A realistic sequencing:

1. online submission, offline-ready ids and timestamps as above;
2. offline **draft** capture (answers persisted locally, submitted on reconnect);
3. offline **photo queue** with background drain.

Step 1 alone makes the feature usable and does not foreclose 2 and 3.

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

## Naming — the DISPATCH domain

You liked "work dispatcher system", so that is the head concept: **dispatch** —
work sent out to be executed somewhere else, which comes back as evidence.

One naming hazard to avoid deliberately: **"work order" is already taken.** In this
project it means a Home Remediation case specifically, and it is used that way in
`api/services/housing.py`, the Cases tab and this plan. Promoting `WorkOrder` to
the generic head would give one term two meanings — the exact trap that made
"Rejected" ambiguous on the Executive dashboard (member eligibility vs
authorization decision) and cost a rename today. So the generic head is
**DispatchOrder**, and "work order" keeps meaning what it already means.

```
DispatchOrder            the unit of dispatched work
    kind                 assessment | remediation
    parent               FK self -- remediation orders hang off their assessment
    client               FK
    dwellings            JSON {"primary": <case_id>, "secondary": <case_id>}
    case                 the Unite Us case this order serves
    status               draft -> dispatched -> in_progress -> completed
    vendor / created_by

DispatchVisit            an appointment and the visit itself
    scheduled_for / confirmed_at / started_at / completed_at

DispatchProof            an image        S3 + sha256   (mirrors DeliveryOrderProof)
DispatchDocument         a document      S3 + sha256
DispatchFinding          an inspection result; may spawn remediation orders
DispatchQuestionnaire    the signed form + its answers

DispatchUniteUsUpload    the manual upload record -- see below
```

**Why one `DispatchOrder` with a `kind` rather than two models.** The assessment
and the remediations share everything that matters: an appointment, a check-in,
photos, documents, evidence, and an upload to Unite Us. Two parallel models means
building that layer twice, which is the structural warning below. A `kind`
discriminator plus a self-FK gives "one assessment, many remediation orders under
it" (rule 2) directly, and the shared layer comes for free.

The cost, stated plainly: a `kind` column invites `if kind == ...` branching, and
if the two diverge a lot that becomes the worse choice. Today they differ only in
what they produce -- an assessment produces findings, a remediation consumes one --
so the shared shape looks right. Worth revisiting if remediation grows its own
lifecycle.

## Tracking the Unite Us upload (manual, but recorded)

> "we will upload the documents to united us manually first. but we need to track
> the upload action manually."

So the CRM does not push anything. It keeps the **record of the human action**,
which makes "what is still waiting to go to Unite Us?" answerable instead of
living in someone's memory.

```
DispatchUniteUsUpload
    order            FK DispatchOrder
    target           what was uploaded -- document / proof / questionnaire PDF
    uploaded_by      the agent who did it in Unite Us
    uploaded_at      when they say they did it
    recorded_at      when the CRM was told          (these are NOT the same)
    uniteus_ref      optional reference/note from the Unite Us side
```

Two design notes:

- **`uploaded_at` and `recorded_at` are separate**, for the same reason
  `captured_at` and `received_at` are on the questionnaire: an agent uploads at
  10:00 and ticks the box at 16:30. Collapsing them loses which is which
  permanently, and the delivery-order work today showed how much confusion
  follows from one timestamp standing for two events (`case_created_at` being
  source data, not ingestion).
- **It is a queue, not just a log.** Evidence that exists but has no upload record
  is a work item. That is worth surfacing as a count in the CRM -- and eventually
  a health metric -- because an un-uploaded assessment is invisible to the payer.

Deliberately NOT modelled yet: any automatic push, retry or reconciliation against
Unite Us. Manual-first keeps a human between vendor-supplied evidence and the
system of record.

## ⚠️ Scope tension: with no vendor portal, who fills the questionnaires?

The portal is deferred and Phase 1 is CRM-only — but Q2 says **CRM internal users
may never modify vendor answers**. With no portal, there is nobody left who can
author them.

Three ways out, and this needs deciding before implementation:

| Option | Consequence |
|---|---|
| **A. Phase 1 has no questionnaires.** Build the order, visits, documents, proofs, findings and upload tracking. Questionnaires arrive with the portal. | Cleanest, keeps Q2 intact. The vendor's paperwork stays on paper/PDF and is attached as a DOCUMENT, which is what actually happens today. |
| **B. An agent transcribes the vendor's answers**, recorded as agent-entered rather than vendor-signed. | Contradicts Q2 unless the distinction is explicit in the data — and a transcribed answer must never be presentable as a signed one. |
| **C. Vendors get a minimal signed link** (no portal, one tokenised URL per order). | Smaller than a portal, but it is still external auth and evidence capture, i.e. most of the hard part. |

**I would suggest A.** It is the only option that does not weaken the locking rule,
it delivers everything the CRM can actually use today, and the signed-questionnaire
machinery lands with the portal that gives it a real author. The vendor's signed
paperwork is simply a `DispatchDocument` until then.

## Later phase — the VENDOR PORTAL (deferred)

> "in the future we will need to add a vendor portal, where vendors will login to
> confirm appointments for assessments and work orders. they will also check in,
> start the visit and collect photos as proof of service needed. so all those
> generated work orders and assessments need to be worked by external users to
> produce evidence we will display in the crm so it can be uploaded to unite us."

This reframes the whole feature. The Assessment Order is not a form an agent fills
in — it is **work dispatched to external people**, and the CRM's job is to display
the evidence they produce.

### What the portal needs, that nothing here has yet

| Need | Status today |
|---|---|
| **Human vendor logins** | ❌ Does not exist. `api/partner/` is MACHINE-to-machine: its principal is explicitly "not a Django user", it carries a `delivery_company` and scopes, and authenticates a client-id/secret. A portal needs vendor USERS — real people, with sessions, and an identity to attribute a signature to. |
| **Appointments** | ❌ New. Confirming an appointment for an assessment OR a work order implies a scheduling model both types share. |
| **Check-in / start visit** | ❌ New. Timestamps at minimum; possibly location, which is a privacy decision, not just a field. |
| **Photos as proof of service** | ⚠️ Partly. `DeliveryOrderProof` is the pattern (S3 + sha256), but it hangs off a delivery order. |
| **Host isolation** | ✅ Exists. `PARTNER_API_HOST` already serves partner routes on their own hostname with CRM routes absent — the right foundation, and already reasoned about. |

### The consequence worth flagging now

**Work orders become first-class work, not just imported cases.** Today a Home
Remediation case is a record that arrives from Unite Us. In the portal it is
something a vendor is assigned, confirms, attends, and produces evidence for.

So the appointment/check-in/proof model must attach to **both** an assessment order
and a work order. If the assessment order is designed as a one-off member-scoped
object with proofs bolted onto it, work orders will need a parallel set of the same
machinery later. Better to design the visit/evidence layer as something both point
at, even if only assessments use it in Phase 1.

### And a new integration direction: writing BACK to Unite Us

"…so it can be uploaded to unite us" is the first requirement to **push** data to
Unite Us. Everything today is one-way, Unite Us → CRM. That is a new capability
with its own failure modes: partial uploads, retries, duplicate evidence, and no
obvious idempotency key.

Two questions:
- is the upload **manual** (an agent reviews the evidence, then clicks upload) or
  **automatic** on completion?
- what happens when it fails — a queue and retry, or an agent-visible error?

Manual-first is safer: it keeps a human between vendor-supplied evidence and the
system of record, and it makes the failure mode a visible button rather than a
silent background task.

---

## Sketch, superseded by the Naming section above

Kept for the field-level detail; the model NAMES are the ones in
"Naming -- the DISPATCH domain".

```
HousingAssessmentOrder
    id                CLIENT-generated UUID          (offline idempotency, Q4)
    client            FK -- ONE per member
    dwellings         JSON {"primary": <case_id>, "secondary": <case_id>}   (Q1)
    primary_case      FK to the governing EEA case   (optional, for integrity/query)
    status            draft -> dispatched -> in_progress -> completed
    created_by        the verification agent
    vendor            who executes it
    ... wizard answers ...

HousingAssessmentDocument        many per order   S3 + sha256
HousingAssessmentProof           many per order   S3 + sha256  (mirrors DeliveryOrderProof)
HousingAssessmentFinding         many per order   the inspection results
HousingAssessmentQuestionnaire   many per order
    answers          writable by the VENDOR until submitted; NEVER by CRM users (Q2)
    signed_at / signed_by
    pdf_s3_key / pdf_sha256       a rendering of the locked answers
    captured_at / received_at     device time vs server time (Q4)

work orders: the Home Remediation CASES, linked by member today
             (housing_work_orders), or by FK if we add one (Q3)
```

A visit/evidence layer that BOTH an assessment order and a work order can point at
is the shape the vendor portal will need -- see that section. Designing proofs to
hang solely off the assessment order means building the same machinery twice.

Deliberately NOT included: any reconcile, rebuild or supersede step. Rule 5 says
keep the record, and the food side is the cautionary tale.

---

## Dependencies

- `docs/housing-programs-plan.md` — steps 1, 2, 2b and the UI are **done**; housing
  cases import and classify correctly, and cannot touch food service.
- The new **verification type** for housing is referenced in the flow but was
  deferred ("we will discuss later"). The Assessment Order is what the
  verification produces, so the two are the same conversation.
