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

## The lifecycle (as given)

### Assessment

```
Dwelling Assessment CASE imported
      |
      v  agent completes the wizard
DispatchOrder(assessment)   status = PENDING SCHEDULE
      |
      v  scheduled with the vendor
                            status = CONFIRMED      * fires a vendor calendar
      |                                               event + reminder
      v  visit done, evidence gathered
                            status = PENDING SUBMISSION
      |
      v  submission GATE satisfied (see below)
                            status = SUBMITTED      -> answers LOCK here
      |
      v  agent uploads to Unite Us and records it
                            status = UPLOADED
```

### Remediation

Once the assessment is **UPLOADED**, the screener creates one or more Home
Remediation cases in Unite Us. Each imported case **automatically creates a
remediation order under that member's assessment order**, so every Home Remediation
case is linked to its Dwelling Assessment.

```
Home Remediation CASE imported
      |
      v  automatic
DispatchOrder(remediation, parent=the assessment order)
                            status = PENDING SCHEDULE
      |                     the vendor sees it IMMEDIATELY and schedules it
      v
                            status = CONFIRMED  -> calendar event + reminder
      v
                            status = PENDING SUBMISSION
      v
                            status = SUBMITTED
      v
                            status = UPLOADED
```

Identical chain for both kinds, which is the argument for one `DispatchOrder` with
a `kind` rather than two models.

**CONFIRMED: one status field, a linear chain** — not three independent flags. Plus
a full transition history, "the same details we did for the food cases".

### History — extend `StageEvent`, do not build a parallel log

`StageEvent` is already an append-only audit log of stage transitions with a
**nullable-FK + `entity_type` discriminator**, and its docstring says it "mirrors
the nullable-FK pattern used by Answer". It spans Client and Enrollment today. So
the dispatch order joins it:

```
StageEntityType   += DISPATCH_ORDER
StageEvent        += dispatch_order FK (nullable)

existing fields, unchanged and exactly what is wanted:
    from_stage / to_stage      the transition
    source                     auto | manual
    actor                      WHICH user did it
    note / metadata            why, and any detail
    entered_at                 when
```

Extending it rather than adding `DispatchStageEvent` matters for a reason beyond
tidiness: **"show me everything that happened to this member"** stays one query. A
parallel log would need every history view, export and report to know about a
second table -- and today already showed what duplicated definitions cost, when
fixing the unmapped-program metric left the management command printing the old
answer.

A `TimelineEvent` type per dispatch transition is the second half, since that is
what the member profile's History tab reads. `TimelineEventType` already follows
exactly this pattern for the food stages -- one granular type per stage, "so the
History tab reads each transition distinctly instead of a pile of generic rows".
Worth following that precedent: `dispatch_scheduled`, `dispatch_confirmed`,
`dispatch_submitted`, `dispatch_uploaded`, rather than one lumpy `dispatch` type.

## The submission GATE

> "any order can't be submitted if the order is missing the vendor signature, the
> member signature and they need to upload at least one photo per finding."

`PENDING SUBMISSION -> SUBMITTED` requires **all three**:

| Requirement | Note |
|---|---|
| **Vendor signature** | |
| **Member signature** | A second signer — the member, at their own home |
| **≥ 1 photo per FINDING** | Per finding, not per order |

### This changes the schema

My earlier sketch hung proofs off the order. **The gate is per-finding, so a proof
must be attachable to a finding** — otherwise "at least one photo per finding" is
not expressible:

```
DispatchProof
    order     FK
    finding   FK, NULLABLE   <- general site photos have no finding;
                                the GATE only counts the ones that do
```

Enforce it server-side in one place — a `can_submit(order)` returning the missing
items, used by both the API and the UI. If the UI checks and the endpoint does not,
it will be bypassed; that is precisely how the food verification endpoint still
accepted a housing case after the picker had been fixed.

Worth deciding: is a finding with **no** photo a blocker, or can a finding be
marked "no photo applicable"? A hard rule with no escape hatch tends to produce a
junk photo rather than compliance.

## RESOLVED — the CRM DISPLAYS status; the vendor page PRODUCES it

> "the crm will show the status the vendor page produce. we will implement just
> after we do the structure for those orders"

This settles the scope tension below, and settles it better than the agent-
attestation compromise I proposed. The build order is:

```
1. the ORDER STRUCTURE          <- THIS TASK
     models, the creation wizard, CRM read-only display, upload tracking
2. the VENDOR PAGE              <- immediately after
     scheduling, check-in, signatures, photos -> the statuses the CRM shows
```

So the vendor portal is **next**, not deferred indefinitely, and that changes two
things:

- **No agent-attestation path is needed.** `submitted_via` can be dropped. The CRM
  never authors evidence or status — it displays what the vendor produced, which
  keeps Q2's locking rule pure instead of carving an exception into it on day one.
- **Orders sitting at PENDING SCHEDULE / CONFIRMED is the expected state**, not a
  gap. Nothing can legitimately reach SUBMITTED until step 2 ships, and that is
  correct rather than incomplete.

The one consequence worth stating: the **upload tracking has nothing to track until
step 2 ships**, because there is no submitted evidence to upload. It should still be
built now — it is part of the structure, and building it later means revisiting the
same models — but do not expect the queue to show anything yet.

The section below is kept because it is still the right analysis of *why* the CRM
cannot author a submission. Only its conclusion changed.

## The signatures, and why the CRM cannot close an order

Both signatures are captured **at the member's home**, by people who have no way to
log in until the vendor portal exists. So in a CRM-only Phase 1:

**an order cannot legitimately reach SUBMITTED at all.**

That is not a reason to stop — it is a reason to be explicit about what Phase 1 is:

| Phase 1 (CRM only) | Needs the portal |
|---|---|
| Order created by the wizard | Vendor confirms their own appointment |
| Scheduling and CONFIRMED | Vendor signature captured on site |
| Findings recorded | Member signature captured on site |
| Documents + photos attached by an agent | Questionnaire answers authored by the vendor |
| The Unite Us upload record | |

So this task takes an order to **CONFIRMED** and stops there, and the vendor page
supplies the tail. That is the resolution above: the tail waits, which is clean,
and it is affordable precisely because the vendor page is the NEXT task rather than
a distant one.

Recorded for the future: if that ordering ever slips and an agent has to close
orders from paper, then `submitted_via = agent_attested | vendor_signed` must be
added BEFORE the first such order, not after. Retrofitted, it cannot distinguish
the paper-era rows from digitally signed ones, and the signature guarantee becomes
retroactively worthless.

## ⚠️ Legacy: work orders that predate all of this

The auto-creation rule assumes the assessment order exists first. Real data does
not:

```
MIRIAM ISRAEL (fad1f448)   1 assessment case + 9 Home Remediation cases,
                           all imported before this feature existed
```

Three members currently hold housing cases, and none has an assessment order. So
the import needs a rule for a remediation case whose member has no assessment
order:

- **skip** — create no remediation order (they stay plain cases, as today);
- **create an orphan** — a remediation order with no parent; or
- **backfill** — create the assessment order first, in whatever state.

**DECIDED: skip at import, then ADOPT on verification.**

> "I agree, we will save the home remediations cases and rebuild it when the
> dwelling case get verified"

So the linking rule has two entry points, not one:

| When | Member HAS an assessment order | Member has NONE |
|---|---|---|
| **A Home Remediation case imports** | create its remediation order immediately | keep the case, create nothing — it waits |
| **An assessment order is created** (the dwelling case is verified) | — | **adopt** every unlinked Home Remediation case the member holds |

That covers the real data. MIRIAM's nine cases sit unlinked today; the moment her
dwelling case is verified and her assessment order exists, all nine are adopted
under it.

**Calling it "adopt" rather than "rebuild" on purpose.** Nothing is recomputed or
rewritten — the cases already exist and are untouched; they simply gain a
remediation order and a parent. That keeps rule 5 intact ("keep the record, do not
rebuild") and avoids reaching for the food side's rebuild/replace vocabulary, which
is the machinery that forked 149 enrollments. The distinction is worth preserving
in the code's naming too: `adopt_unlinked_remediation_cases(order)`, not
`rebuild_*`.

Two details to settle when implementing:

- **Is adoption idempotent?** It must be — running it twice must not create two
  remediation orders for one case. A unique constraint on
  `(case, kind=remediation)` gives that for free, and is cheaper than remembering
  to check.
- **Does adoption reach CLOSED remediation cases?** Four of MIRIAM's nine are
  closed. Adopting them records history honestly; skipping them avoids dispatching
  work nobody will do. Leaning: adopt them, but never in a schedulable status —
  their order starts at the terminal end, as a record.

## The wizard

Launched from a button on the member profile, once per member. On completion the
order exists at `PENDING SCHEDULE` and **the vendor sees it on their dashboard**.

### Step 1 — who and where

| Field | Notes |
|---|---|
| Member full name | Display only |
| Phone + type | Type is a dropdown: **mobile / landline** |
| Email | Optional |
| **Address** | Show the member's PRIMARY address, and also offer Google autocomplete |
| Address notes | Free text |

**Google Places already exists — reuse it.** `HouseholdTab.tsx` autocompletes the
delivery street through a BACKEND proxy (`/places/autocomplete/`,
`/places/details/`), so `GOOGLE_MAP_KEY` stays server-side. No new dependency and no
key in the browser; the same two endpoints serve this wizard.

**CONFIRMED — captured on the order, and verified ONCE.**

> "the address must be captured on the order, same address for all orders,
> remember we only verify the address one time on the dwelling assessment"

So the address is verified once, on the **assessment** order, and every remediation
order under it uses that same address. Since remediation orders are children
(`parent` FK), they **inherit** rather than copy: one verified address per
assessment, and no way for a child to drift from it.

That also settles the risk for free — a later edit to the client's primary address
cannot rewrite where a completed assessment happened, because the order holds its
own.

Store the structured result (`place_id`, formatted address, lat/lng) rather than a
string: it gives the vendor something to navigate to, and makes any future
service-area check possible without re-geocoding.

⚠️ One thing to decide when building: if the member genuinely **moves**, the
existing assessment keeps its address (correct — that is where the assessment
happened), but the new dwelling needs a new assessment. That is the `secondary`
dwelling label from Q1 doing its job, and the remediation orders under the OLD
assessment must keep pointing at the OLD address.

### Step 2 — the dispatch

| Field | Notes |
|---|---|
| **Referral type** | `Mobility` / `Ventilation` / `Combined` |
| **Assign to vendor** | Dropdown of all vendor companies |
| **Member availability** | At least **3 days**, each with a time window. NOT the appointment |
| **Consent to call and text** | So the vendor may contact the member |
| **ECM case billed?** | Confirmation checkbox |
| Other notes | Free text |

#### Referral type is functional, not a label

It selects which questionnaire the member is asked later, and `Combined` means
**both**. So the questionnaire model needs a template/type concept from the start,
even though questionnaires themselves ship with the vendor page. Storing
`referral_type` as a plain string and inferring later would repeat the
`case_category`-vs-`case_type` confusion the housing work already untangled.

#### Vendor companies — a new model, not `DeliveryCompany`

There is no vendor model that fits. `Provider` is the Unite Us organization
("normalized provider/organization from the source system"), and `DeliveryCompany`
is explicitly "a delivery company/vendor that transports meal orders".

A housing vendor inspects and remediates homes; it is a different relationship with
different people and different work. Overloading `DeliveryCompany` would put
"companies that drive meals" and "companies that assess dwellings" in one table and
make every existing delivery query ambiguous.

⚠️ One consequence for the vendor page that follows: `api/partner/` auth binds its
principal to a `delivery_company`. Serving housing vendors on that surface means
generalizing the partner principal — worth knowing NOW, while the model is being
named, rather than discovering it when the portal is built.

#### Vendor users: admin-provisioned, then self-managed

> "vendors will be added by the admin in the crm settings. we will provide one
> admin user so they can add more users on their side. we can also reset the admin
> user password."

So the model is **bootstrap-and-delegate**:

```
CRM Settings  ->  create Vendor (the company)
              ->  create ONE vendor ADMIN user        <- we provision this
                        |
                        v  in the vendor portal (next task)
                  the vendor admin creates their own staff users
CRM Settings  ->  reset the vendor admin's password   <- we retain this
```

This means **vendor users exist as a model in THIS task**, not the next one: CRM
Settings has to create the company and its admin user, so the user model and the
password-reset path are part of the structure even though nobody can log in until
the portal ships.

Three things to settle while building it:

- **Vendor users are not CRM users.** They must not be `Agent`s, and they must not
  be able to reach CRM endpoints. The `api/partner/` host isolation is the right
  answer, but the principal needs generalizing (above).
- **Scoping is the security boundary.** Every vendor query must be filtered to that
  vendor's own orders. The partner API already establishes this — its principal
  exists "to carry the company every query must be scoped to" — so follow that
  exactly rather than relying on filters at each call site.
- **Password reset needs an audit trail.** "We can reset the admin user password"
  is a support action on an external account; who did it and when should be
  recorded, the same as any other privileged action.

#### Availability: the rule is 3 DAYS, not 3 windows

"a minimum of 3 available days with time frame for each" — so validation counts
**distinct dates**, not rows. Three windows on one Tuesday does not satisfy it, and
a naive `len(windows) >= 3` check would accept it.

```
DispatchAvailabilityWindow      many per order
    date / start_time / end_time
```

And it is explicitly **not** the appointment — the vendor picks from these. So the
order needs both: the member's offered availability (many) and the agreed
appointment (one, on `DispatchVisit`).

#### Consent: VERBAL, recorded, and GDPR-shaped

> "this is a verbal consent to send sms and call as gdpr required"

Verbal consent given by the member to the agent, for the vendor to contact them.
Under GDPR the burden is to **demonstrate** consent, so the record has to answer:
who consented, to what, when, and on what basis.

```
consent_to_call / consent_to_text     what they agreed to
consent_method                        verbal          <- explicit, not implied
consent_captured_at / _by             when, and which agent heard it
```

Two booleans behind one checkbox, because "SMS and call" are separable and a
member may later object to one. That distinction cannot be reconstructed later if
only a single flag is stored.

**GDPR also requires that consent can be WITHDRAWN**, and as easily as it was
given. Worth deciding now whether withdrawal is a status on this record (append a
withdrawal rather than flipping the boolean, so the history survives) or handled
through the Client's existing consent machinery. Recording only the grant leaves
no place to put a later refusal.

This is **narrower than the Client's existing consent** (`consent_accepted`,
`consent_status`, `consented_at`), which is consent to the programme.
Vendor-contact consent is its own fact and belongs on the order.

#### "ECM case billed?" — a checkbox for now, DEFERRED

> "we will decide later when we get the case details or the invoice from united us"

So: a plain confirmation checkbox in this task, with the data-driven version
deferred until the Unite Us case details or invoice show whether "billed" is
derivable at all.

Worth keeping in view: a checkbox alone records only that someone clicked. If it
later turns out the CRM can show the member's ECM case and its billing state, the
agent should be confirming something they can SEE rather than recall. That is a UI
change on the same field, so nothing here forecloses it.

## VOID and resubmit — the correction path

> "lets let the vendor have the option to void an assessment that they submitted
> and resubmit the new assessment, but the system should keep the voided copy as
> well."

This answers the "no correction path" problem directly, and answers it the right
way: **not by unlocking, but by superseding.** A submitted assessment stays exactly
as signed; a correction is a NEW submission, and the voided one is kept.

### It makes SUBMISSION the versioned thing

Locking (Q2) says answers cannot change after submission. Void-and-resubmit says a
correction must be possible. Both hold if the **submission** — not the order — is
what gets versioned:

```
DispatchSubmission           many per order, append-only
    sequence                 1, 2, 3...
    state                    active | voided
    submitted_at / submitted_by
    voided_at / voided_by / void_reason
    pdf_s3_key / pdf_sha256  the IMMUTABLE snapshot of what was signed
    signatures               vendor + member, belonging to THIS submission
```

Exactly one submission is `active` per order. Voiding does not delete or edit
anything — it marks that submission voided and lets a new one be built. The old
PDF, its hash and its signatures survive untouched, which is what "keep the voided
copy" has to mean for a signed record to be worth anything.

### What the PDF is for

Findings and photos live on the **order** and describe the dwelling; they can be
corrected after a void. The **PDF is the snapshot** — an immutable record of what
the findings and answers looked like at the moment someone signed. So after a void:

- the vendor fixes the wrong finding,
- re-signs, producing submission #2 with a new PDF and new signatures,
- and submission #1 still shows exactly what was originally attested.

That is why the `sha256` matters: it proves the voided copy has not been quietly
edited to match the new story.

### Three things to settle

**1. Does voiding return the order to `PENDING SUBMISSION`?** It should — the order
is once again awaiting a valid submission, and the gate (two signatures, a photo
per finding) must be satisfied again. Otherwise a voided order sits in a status
that claims work is finished.

**2. ⚠️ Can a vendor void something already UPLOADED to Unite Us?** This is the
sharp edge. If the evidence has been uploaded and the vendor then voids it, Unite
Us holds a document the CRM now considers wrong, and nothing in the CRM will say
so. The upload record needs a **superseded** state and the order needs to surface
"re-upload required", or the two systems drift silently — which is precisely the
class of bug that had four members being double-delivered for a month.

**3. Is there a limit, and who can see the voids?** No limit is fine, but the CRM
should show the void history — `submission #2 (previous voided: wrong room
measured)` — rather than only the current one. A void with no visible reason looks
like a system glitch to whoever finds it later.

### Note the asymmetry, deliberately

The **vendor** can void their own submission. **CRM users still cannot edit
anything** — Q2 is untouched. The correction path belongs to the party who signed,
which is the only party whose correction means anything.

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
Vendor                   a housing vendor company (NOT DeliveryCompany)
    added in CRM Settings; we provision ONE admin user and can reset its password

VendorUser               NOT an Agent, and never reaches CRM endpoints
    is_admin             the bootstrap user; creates their own staff in the portal

DispatchOrder            the unit of dispatched work
    kind                 assessment | remediation
    parent               FK self -- remediation orders hang off their assessment
    client               FK
    dwellings            JSON {"primary": <case_id>, "secondary": <case_id>}
    case                 the Unite Us case this order serves
    status               pending_schedule -> confirmed -> pending_submission
                         -> submitted -> uploaded
    vendor               FK Vendor
    created_by           the agent who ran the wizard

    -- step 1: captured ON the order, not read live from the client --
    contact_phone / phone_type        mobile | landline
    contact_email                     optional
    address_*                         formatted + place_id + lat/lng
    address_notes                     verified ONCE on the assessment;
                                      remediation orders INHERIT via parent

    -- step 2 --
    referral_type                     mobility | ventilation | combined
    consent_to_call / consent_to_text  two booleans, one checkbox in the UI
    consent_method                    verbal (GDPR: demonstrable)
    consent_captured_at / consent_captured_by
    ecm_billed_confirmed              the human sign-off
    notes

DispatchAvailabilityWindow  many per order -- >= 3 DISTINCT DATES
    date / start_time / end_time

DispatchVisit            the appointment and the visit itself
    scheduled_for / confirmed_at / started_at / completed_at
    calendar_event_ref   what was pushed to the vendor's calendar (portal phase)

DispatchSubmission       many per order, append-only -- ONE active at a time
    sequence / state     active | voided
    submitted_at / submitted_by
    voided_at / voided_by / void_reason
    pdf_s3_key / pdf_sha256      the immutable snapshot of what was signed

DispatchSignature        belongs to a SUBMISSION, not the order
    signer_role          vendor | member      -- the gate needs BOTH
    signed_at / signed_by_name / image or PDF ref

DispatchFinding          an inspection result; may spawn remediation orders
DispatchProof            an image        S3 + sha256   (mirrors DeliveryOrderProof)
    finding              FK NULLABLE -- the GATE counts photos PER FINDING
DispatchDocument         a document      S3 + sha256
DispatchQuestionnaire    the signed form + its answers      (portal phase)

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
