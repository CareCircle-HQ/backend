# AI Query Agent — natural language over CRM data

**Status:** design agreed, including Q1-Q3. No code written yet, deliberately --
Phase 0 (the glossary) is a conversation about definitions, not a coding task.

## Goal

Let a management user ask a question in English and get a verifiable answer, on
screen and as a CSV. The question that started this:

> *"Can you show me households with open IS cases that are active but have some
> additional household members that are paused?"*

That is not a toy example -- it is an anomaly detector for exactly the class of
problem we spent 2026-09-14/15 diagnosing by hand (EVAN AKALLOO paused on a
relative's active enrollment; NAVITA and SKYLAR paused with no case of their own).
**This tool is a self-service version of the SQL that investigation required.**

## The hard part is semantics, not SQL

The example question contains a landmine: **"active" is ambiguous in our own
schema.**

```
Client.lifecycle_stage == 'active'
EnrollmentVerification.stage == 'service_active'
EnrollmentAnalytics.company_status == 'active'
MemberDietaryProfile.status == 'active'
```

Four meanings, four different answers. This is not hypothetical: on 2026-09-14 we
found TWO different definitions of "governing case" in this codebase differing by
~443 cases, and `active_enrollment()` resolving to a CLOSED enrollment for a member
who was being served.

A management user who gets a confidently wrong number makes a decision on it.
**That is a worse failure than the query erroring.** So the foundation is a
GLOSSARY -- each business term defined once, in code, mapped to one query fragment
-- and the agent may only use terms from it. Anything else: "I can't express that."

## Phase 0 in progress -- the glossary, VERIFIED against the schema

Every term below was checked against the live schema and production data rather
than accepted as written. Three of the first eight were wrong or unanswerable,
which is the entire argument for doing this before any code.

### Verified as stated

| Term | Definition | Check |
|---|---|---|
"active" | enrollment `stage = service_active` | read model `stage` has it (14,831 rows) |
"On Hold" | enrollment `stage = on_hold` | confirmed (4,087 rows) |
"team" | `analytics.team` | populated 54,535 / 76,426 |
"no delivery plan" | no `delivery_schedules` with `status = scheduled` | as used by `diagnose` + health metrics |
"servable" | member status NOT IN `SERVICE_EXCLUDED_MEMBER_STATUSES` | existing constant -- reuse it, do not restate it |
"household" | group by `household_id` | read model is member-grain, so this is an aggregation |

### Corrected

**"Ineligible" is NOT an enrollment stage.** There is no such stage
(`pending_validation, validated, pending_verification, verified,
kitchen_assignment, service_active, service_complete, closed, on_hold, cancelled,
disregarded, scheduled_extension`). It lives on `Client.lifecycle_stage` -- and
there are TWO values, which the code gates on TOGETHER:

```python
if (client.lifecycle_stage or "") in (ClientStage.NOT_ELIGIBLE, ClientStage.INELIGIBLE):
```
(`serializers.py` -- "the hard-ineligible off-ramp, or the LEGACY not_eligible denial")

Three candidate definitions, three different answers:

```
read_model.eligibility == 'ineligible'              21,542   <- what the Data page shows
lifecycle_stage == 'ineligible'                     21,694
lifecycle_stage IN ('ineligible','not_eligible')    22,393   <- what the CODE gates on
```

**~850 members of spread on one word.** Per Q2 (same data as the Data page) the
glossary uses `read_model.eligibility`, and this divergence is documented rather
than discovered later by a manager comparing two screens.

**"Individual" collides with itself.** Two distinct meanings, both real here:

| meaning | maps to | seen in |
|---|---|---|
grain -- one row per member | no household grouping | "show me individuals with..." |
case scope | `Case.household_type = 'individual'` | EVAN and LIAM each holding one, which caused this week's 149-enrollment fork loop |

So the glossary needs separate words -- "per member" for the grain,
"individual-scope case" / "household-scope case" for the case attribute -- and the
interpretation line must show which was used, because the words genuinely overlap.

**"open IS case" -- WHICH governing case?** The codebase has two definitions
differing by ~443 cases. The read model uses
`governing_service_case_for_display(client)`, plus inheritance: a caseless member
BORROWS the household primary's case (unless `inherit_blocked`). The glossary
therefore pins to the read model's `case_*` columns.

> KNOWN DIVERGENCE: the Executive dashboard uses the OTHER definition
> (`governing_internal_case_ids`). The agent will disagree with it. Documented on
> purpose -- somebody will otherwise report it as an agent bug.

### Blocked -- need one read-model column each

`company_status` is deliberately COARSE and collapses exactly the distinctions
these terms need (`enrollment_analytics._company_status`):

```python
if (parity.get("out_of_orbit") or parity.get("out_of_range") ...):
    return "unable"     # both -> one bucket
if (parity.get("paused") or member_status == "nutritionist_paused" ...):
    return "paused"     # paused + nutritionist_paused -> one bucket
```

| Term | Wanted | Blocked because |
|---|---|---|
"Out of Orbit" | member status `out_of_orbit` (313) | folded into `company_status = unable` |
"Out of Range" | member status `out_of_range` (291) | folded into `company_status = unable` |
"paused" (precise) | member status `paused` (1,479) | `company_status = paused` is 1,793 -- a different, wider set |
"individual-scope case" | `case.household_type` | not stored in the read model at all |

**Proposed: two columns on `EnrollmentAnalytics`.**

1. `member_status` -- the raw `MemberDietaryProfile.status`. Note the builder
   ALREADY computes it and passes it into `_company_status`; it simply is not
   persisted. One line in the row dict plus a migration.
2. `case_household_type` -- individual vs household scope.

Between them these unlock the motivating question, the whole
out-of-orbit / out-of-range / nutritionist-paused family, and the case-scope
questions behind EVAN, NAVITA and SKYLAR. Without them, four of the terms
management just asked for cannot be expressed at all.

Cheap, and precedented: the `medicaid_id` fix on 2026-09-15 was the same shape
(one builder line, then a rebuild).

### The lesson from doing this

Of the first eight terms: **five verified, one wrong (`Ineligible`), one ambiguous
with itself (`Individual`), and four blocked on missing columns.** Had the agent
been built first, each of those would have surfaced as a confidently wrong answer
to a manager instead of a line in a table.

## Decisions taken

### 1. The model emits a validated query IR, never SQL

| Approach | Verdict |
|---|---|
LLM writes SQL | rejected -- hallucinated joins/columns, unbounded cost, PHI exposure, unverifiable, and the schema is 260+ migrations deep |
LLM picks from fixed filters | insufficient -- cannot express the household-level quantifier the example needs |
**LLM emits typed JSON IR, we compile it** | **chosen** |

The IR is a Pydantic model, validated BEFORE anything executes -- which is
precisely what Pydantic AI is good at. A malformed or out-of-glossary IR is
rejected and retried, not run. Sketch of the example question:

```
scope: household
household_must_have:
  - open internal-service case
  - enrollment stage = service_active        # glossary pins "active"
members_where_any:
  - is_primary = false
  - status = paused
return: household, members, which are paused
```

Because the IR is structured we can render it back in English for the user to
confirm or correct -- which is the human-in-the-loop moment, and the reason AG-UI
fits.

### 2. Query the read model, not the OLTP schema

`EnrollmentAnalytics` is already a semantic layer: ~76k rows, one per member,
denormalised, described in its own docstring as "arbitrary-field filtering +
exports for the data team", carrying `household_id`, `is_primary`,
`company_status`, `stage`, case fields, dates, `team`. It already powers the Data
page AND its export, so its semantics are exercised daily.

Two consequences:

- **It is member-grain.** Household questions need `GROUP BY household_id ...
  HAVING`, so the IR needs QUANTIFIERS (`any` / `all` / `count`) over the members
  of a household. That is the one real extension.
- **It is periodically refreshed** -- observed 26h stale on 2026-09-15. Every
  answer MUST carry "as of <refreshed_at>". Without that stamp the tool will
  eventually state yesterday's truth as today's, and trust dies at the first
  instance.

### 3. The model never sees member rows

```
model receives:   question + glossary/schema (+ the current IR, for follow-ups)
model returns:    an IR
Django returns:   rows -> straight to the UI, never back through the model
```

- **Compliance:** results never leave the account.
- **Cost:** ~2-4k tokens per question regardless of result size; a 5,000-row
  answer costs the same as a 3-row one.
- **Accuracy:** the model cannot miscount or mis-summarise rows it never saw.

The trade-off -- no "summarise these results" -- is accepted; see the follow-up
taxonomy for how analysis is handled when wanted.

### 4. Output: table on screen, clean CSV, visible interpretation

Agreed:

- **Table/cards on screen**, plus a one-line summary COMPUTED FROM THE DATA (not
  generated), e.g. `19 households · 2 repairable now · 17 need a member returned
  to service`. Reads like prose, cannot hallucinate.
- **The interpretation is always shown**: `Read "active" as: enrollment stage =
  service_active [change]`. That single line is what makes the tool trustworthy,
  because it exposes the one thing that can silently be wrong.
- **Export the FULL result set**, not the visible page, with a higher cap than the
  on-screen limit (the existing Data export already streams 76k rows).
- **Nothing but data in the CSV.** No question, no interpretation, no preamble
  row. We spent 2026-09-15 making that export machine-readable (normalised dates,
  Medicaid ID, Team); a header preamble would undo it. The question goes in the
  FILENAME and the audit record.

One compiler, two renderers:

```
IR --> compiler --> queryset --+--> paginated JSON      -> table on screen
                              +--> stream_csv_response  -> full CSV export
```

Reusing `stream_csv_response` / `default_filename` means the download is provably
the same query as the screen, just unpaginated.

### 5. Why prose was rejected as the default

A confident paragraph is believed more than a table. Demonstrated during the
2026-09-15 incident with the ASSISTANT as the failure case: two fluent, wrong
diagnoses (the analytics rebuild caused the 502s; a worker timeout would have
killed the 900s requests). What corrected both was numbers that could be checked
-- `ActiveEnterTimestamp`, `urt == rt`, "zero tracebacks". Tables preserve that
corrective mechanism; prose removes it.

### 6. Follow-ups refine the IR, not the data

"Ask questions about the data already shown" is a first-class requirement, and it
works at the IR level:

```
turn 1:  scope=household, any_member{status=paused}, stage=service_active
turn 2:  ...same IR + kitchen=Hicksville          ("of those, only Hicksville")
turn 3:  ...same IR, group_by=kitchen             ("how many per kitchen")
```

The current IR IS the AG-UI shared state (`useCoAgent`). The model sees the
glossary + current IR + new question and emits a new IR -- so refinement costs
nothing extra and the "no rows in context" property survives.

Three kinds of follow-up, three handlers:

| Kind | Example | Handling |
|---|---|---|
Refinement | "only Hicksville", "sort by oldest", "count per team" | new IR; no data to the model |
Drill-down on ONE row | "why is EVAN paused?" | route to `ai_explain` (see ai_member_explanations_plan.md) -- that is exactly its job |
Analysis of the set | "what do these have in common?" | aggregates only (counts, group-bys), never rows |

This makes the two AI features converge rather than compete: the query agent finds
WHICH members, `explain_member` says WHY for one of them.

### 7. Stack: Pydantic AI + AG-UI + CopilotKit, on Bedrock

Same technology as ext-v2, and the integration is first-class: AG-UI is
CopilotKit's protocol and Pydantic AI supports it natively (`AGUIAdapter`), with
`AGUIAdapter.run_stream()` documented for non-Starlette frameworks (Django
included).

**Bedrock for production**, decided. The reasoning matters, because an earlier
version of this argument was WRONG:

> "The model never sees member data, so the provider choice is less critical."

That is true of the RESULTS but not of the QUESTION. A user will type *"show me
Evan Akalloo's household"* -- **the question itself is PHI.** So the provider does
handle PHI, and needs in-account hosting or a BAA. The AWS BAA already exists
(confirmed 2026-09-14) and Bedrock is HIPAA-eligible, so it is the path of least
legal friction -- and it matches the decision already taken in
`ai_member_explanations_plan.md`.

Model quality is not the constraint: mapping a sentence to a typed IR given a
glossary is a narrow task, and validation catches malformed output anyway. A
cheaper mid-tier model is likely sufficient.

Concrete commitments:

- Enable Bedrock model access in **us-east-2** (per-region, per-model, console).
  CHECK THIS FIRST -- if the preferred model is unavailable there it means a
  cross-region inference profile, better known in week one than in Phase 2.
- IAM: `bedrock:InvokeModel` (+ `InvokeModelWithResponseStream`) scoped to
  specific model ARNs, on the AGENT service's role -- not the web role.
- Keep the provider behind ONE function, mirroring `_call_llm` in
  `api/services/ai_explain.py` ("the single provider-swappable point"). Prototype
  against anything using SYNTHETIC questions with no real names; switch to Bedrock
  for production.

### 8. The agent runs as a separate ASGI process

```
Next.js + CopilotKit  --AG-UI/SSE-->  agent service (uvicorn, Pydantic AI)
                                           | internal HTTP, service-token auth
                                           v
                                      Django /api/internal/query/  (IR in, rows out)
```

**Do NOT host the SSE stream in gunicorn.** Production runs `gthread` with
`9 workers x 2 threads = 18 concurrent requests`, and an SSE stream holds a thread
for its entire lifetime. Ten open agent panels would consume ten of eighteen slots
permanently -- the SAME failure mode fixed twice on 2026-09-14/15 (the Unite Us
fan-out, and CallTools presence polled every 10s).

Suggested placement: **same EC2 box, separate systemd unit, separate uvicorn
process**, nginx routing `/agent/` to its own socket. No new infrastructure, and
agent traffic cannot starve CRM traffic. Mirror the existing patterns, including
`ExecReload=/bin/kill -s HUP $MAINPID` (see deploy/gunicorn.service -- a missing
ExecReload made every deploy a small outage).

**SSE caveat:** nginx buffers by default, which breaks streaming. That location
needs `proxy_buffering off`, or the UI appears frozen and then dumps everything at
once.

## Phasing -- value before any AI

| Phase | What | Why this order |
|---|---|---|
**0** | **Glossary + IR schema.** The 30-50 terms management actually asks about, each pinned to one query fragment | The unglamorous step that decides whether any of it can be trusted |
**1** | **IR compiler + guards + audit**, as an internal endpoint, driven by HAND-WRITTEN IRs. No LLM | Ships value alone (saved/shareable queries) and is fully unit-testable |
**2** | **Pydantic AI agent**: question -> IR, AG-UI endpoint, golden-set evals | The AI becomes the easy part once the target is typed |
**3** | **CopilotKit UI**: interpretation chips, result table, freshness stamp, CSV export | |
**4** | Aggregates/trends, saved questions, scheduled digests | |
**5** | Optional narrative over aggregates; converge with `ai_explain` for drill-down | |

**Phase 1 before Phase 2 is the important call.** With a tested compiler, a wrong
answer can only come from a wrong INTERPRETATION -- which the UI shows the user. If
both are built together, you cannot tell which layer lied.

## Guardrails

- **Management-only**, matching the existing `DataExportView` gate.
- **Read-only database role.** Not "we don't generate writes" -- the connection
  CANNOT write.
- **Hard row cap** + `LIMIT` injection on screen; a higher, explicit cap for export.
- **`statement_timeout`** on the query role (the web path already sets 30s).
- **`EXPLAIN` cost check** before execution -- reject absurd plans rather than run
  them.
- **Audit every question**: user, question text, IR, row count, duration. Both a
  compliance record and the best possible dataset for what people actually ask.

## Evaluation: a golden set seeded from real investigations

We have an unusually good starting set -- questions answered by hand on
2026-09-14/15 where the TRUE answers are known:

```
households active with a paused additional member       -> finds EVAN, NAVITA, SKYLAR
service_active + kitchen but no live delivery plan      -> 19, of which 2 servable
members served on someone else's live enrollment        -> 1,078 (1 with own closed enr)
households where two members each hold an open IS case  -> 4
stranded households with NO delivery cadence            -> 2 (enr 15942, 12175)
enrollments showing more than one "primary" member      -> 6 before the fix
```

Each becomes a `question -> expected IR` test. That is how the glossary stays
honest as it grows.

## DECISIONS TAKEN (Q1-Q3 resolved)

### Q1 -- The agent will NEVER write. READ-ONLY, permanently.

This is a constraint to design INTO the system, not a phase-1 limitation:

- the database role is **read-only** -- not "we don't generate writes", the
  connection cannot write,
- no action/tool surface exists for mutations, so there is nothing to
  accidentally expose later,
- the human-in-the-loop step is therefore about CONFIRMING THE INTERPRETATION
  ("I read 'active' as enrollment stage = service_active"), not authorising a
  change.

It removes the entire risk class that produced the worst bug found this week
(`replace_enrollment_for_case_change` forking 149 enrollments across three
families because one ownership guard was missing).

### Q2 -- Same data as the Data page

Confirmed: query `EnrollmentAnalytics`, the read model the Data page and its
export already use. So "as of <refreshed_at>" is the contract, and the existing
`biz-read-model-stale` alarm (>12h) already protects it -- a stale read model now
pages somebody rather than quietly answering with yesterday's truth.

No OLTP querying, which keeps the join surface tiny and the guardrails
precautionary rather than load-bearing.

### Q3 -- Sizing: 2 uvicorn workers

The audience is much smaller than the agent count suggests:

```
240 active agents
 21 Management + 16 is_manager  ->  ~21-30 eligible users
```

And async makes idle SSE connections nearly free -- which is the whole reason for
not hosting them in gunicorn:

```
gunicorn gthread : one open panel = ONE THREAD of 18
uvicorn async    : one open panel = a coroutine + a socket (a few KB)
```

Even with every eligible user holding a panel open, that is ~21 mostly-idle
sockets. Realistic peak -- 8 managers asking a question every 30s -- is
0.27 questions/sec, each costing one Bedrock call (1-3s) plus one read-model query
(50-500ms).

**2 workers is for zero-downtime reloads, not throughput.**

The real ceilings are elsewhere, and neither is CPU:

1. **Bedrock quotas** -- per-model, per-region requests/min and tokens/min. Check
   the number when enabling model access in us-east-2; it is the one hard limit.
2. **Keeping queries short** -- the row cap, `EXPLAIN` check and
   `statement_timeout` are what make "50-500ms" true even for a pathological
   question.

## Architecture: one codebase, two servers

Because the agent is read-only and shares the CRM's definitions, the cleanest
deployment is NOT a separate service talking to Django over internal HTTP (an
earlier draft of this plan said that). It is the SAME repo served by a second
server process:

```
uvicorn agent_asgi:app     async SSE + Pydantic AI; shares api/ models + glossary
gunicorn backend.wsgi      unchanged, serves the CRM
```

| | internal HTTP hop | same codebase under uvicorn |
|---|---|---|
glossary / query logic | duplicated or proxied | **one definition, shared** |
service authentication | needs a service token | not needed |
gunicorn slots consumed | yes, briefly | **none** |
caveat | -- | Django ORM calls need `sync_to_async` |

The shared-glossary column decides it: **semantic drift is this design's biggest
risk**, and one codebase makes drift impossible by construction. Wrapping the
short, synchronous ORM call in `sync_to_async` is a small, known cost.

Still true from the earlier draft: nginx routes `/agent/` to its own socket with
`proxy_buffering off` (nginx buffers by default, which breaks SSE), and the unit
gets `ExecReload=/bin/kill -s HUP $MAINPID` -- a missing ExecReload is what made
every CRM deploy a small outage until 2026-09-15.

## Risks

- **Semantic drift.** The glossary must be the ONLY definition of each term; if
  view code and glossary disagree, the tool lies confidently. Mitigation: the
  glossary compiles to the same helpers the portal uses, and the golden set fails
  when they diverge.
- **Over-trust.** Mitigated by visible interpretation, computed (not generated)
  summaries, and tables over prose.
- **Stale read model** presented as current. Mitigated by the mandatory "as of"
  stamp and the existing `biz-read-model-stale` alarm.
- **Cost of unbounded questions.** Mitigated by row caps, `EXPLAIN` checks and
  statement timeouts -- not by hoping the model is sensible.
- **Question text is PHI.** Mitigated by Bedrock in-account under the existing
  BAA, and by not logging question text anywhere outside the audited store.
