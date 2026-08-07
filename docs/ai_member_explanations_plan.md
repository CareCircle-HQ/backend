# AI Member History Explanations — Implementation Plan

## Goal

Use an LLM to turn each member's raw event history into a grounded, human- **and**
AI-agent-readable explanation, so a care agent (or an automated agent) can quickly
see **what happened to a member and what to do next** to resolve their situation.

Two granularities, both wanted:

1. **Per-event annotation** — a one-line "what this event means / why it happened"
   for individual timeline/stage events.
2. **Per-member situation summary** — a synthesized narrative across the whole
   history + a machine-readable list of recommended next actions.

This effectively productizes the manual diagnosis we did for member
`SANDRA COLON` (out-of-range hold → old auto-cancel → cancelled-reconcile →
migration to ineligible/paused).

## Design principles

- **Never rewrite source events.** `TimelineEvent` / `StageEvent` are
  append-only / create-once and the dedupe system depends on their immutability.
  AI output is **additive** and stored separately.
- **Grounded.** The model receives ONLY the structured facts we gather and is
  instructed to use nothing else and to answer "unknown" rather than guess. This
  is the primary hallucination guard for a benefits/clinical CRM.
- **Async + cached.** Generation runs in Celery (never inline in imports /
  reconciles) and is cached by an input hash so we only pay when the history
  changes.
- **Provider-swappable.** A single integration point (`_call_llm`) so we can
  swap the provider without touching the rest.

## Provider recommendation

- **Model:** Anthropic Claude — `claude-sonnet-4-5` class for quality,
  `claude-haiku` class for the cheap nightly batch. Strong at grounded
  summarization + strict JSON output ("use only these facts, else say unknown").
- **Production hosting:** run Claude via **AWS Bedrock** so member PII stays
  inside the existing AWS account/VPC (infra is already on boto3/S3 — no new
  vendor / BAA). The prototype uses the direct Anthropic API for speed of setup;
  swapping to Bedrock is a change to `_call_llm` only.
- **Config:** `ANTHROPIC_API_KEY`, optional `ANTHROPIC_MODEL`,
  `AI_EXPLAIN_MAX_TOKENS` (all `os.getenv`, mirroring existing settings).

## Current status — prototype (DONE)

Scope delivered: "prototype one member" — no DB model, no UI.

- `api/services/ai_explain.py`
  - `gather_member_facts(client)` — pure reads; merges `StageEvent` +
    `TimelineEvent` into one chronological stream plus current lifecycle /
    enrollment / case / member-profile state. **This is the grounding payload.**
  - `build_member_summary_prompt(facts)` — grounded system+user prompt; asks for
    `summary`, `current_situation`, per-event `timeline` annotations,
    `recommended_actions`, `open_questions`, `confidence`.
  - `explain_member(client)` — gather → prompt → call → parse JSON. Dry-runs
    (returns the prompt, no call) when no key is configured.
  - `_call_llm(system, user)` — **the single provider-swappable point.**
- `api/management/commands/explain_member.py`
  - `python manage.py explain_member <client_id> [--show-prompt] [--facts-only]`
  - Dry-runs and prints the exact prompt when no LLM key is set.

Verified: fact-gathering reproduces the full Sandra story; dry-run prints the
prompt cleanly.

### To run the prototype live

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
# optional: export ANTHROPIC_MODEL=claude-sonnet-4-5
python manage.py explain_member <client_id>
```

## Next phases

### Phase 1 — Persistence + caching (backend, no UI)

- **New model** `MemberSituationSummary`:
  - `client` (OneToOne), `summary` (text), `current_situation` (text),
    `recommended_actions` (JSON), `open_questions` (JSON), `confidence` (char),
    `model_name` (char), `input_hash` (char, indexed),
    `source_events_through` (datetime — newest event covered),
    `generated_at` (datetime).
- **Per-event annotations**: write to `TimelineEvent.metadata["ai_explanation"]`
  (JSON field already exists — no migration on that table). Never touch the
  human `subtitle`.
- **Input hash / cache**: hash the gathered facts (event ids + timestamps + key
  state). Skip regeneration when the hash is unchanged since the last summary.
- **Idempotent, isolated** writes (one member per transaction), mirroring the
  existing management-command patterns.

### Phase 2 — Async generation

- **Celery task** `generate_member_explanation(client_id)` (already have Celery).
- **Triggers:**
  - On-demand endpoint (for the "Explain" button) — regenerate if the input hash
    changed, else return cached.
  - **Nightly batch** (Celery beat) that pre-warms members flagged in Care
    Management / anomaly states, so the common cases are ready instantly.
- Throttle / batch to control cost; use the cheaper model for the batch pass.

### Phase 3 — API + UI

- `GET /api/portal/members/<id>/explanation/` → cached summary + actions
  (+ `stale` flag when the history moved past `source_events_through`).
- `POST .../explanation/regenerate/` → force a fresh generation.
- **UI:** "Explain this member" button on the member timeline / Care Management
  row; render the narrative, the per-event annotations inline on the timeline,
  and the recommended-actions list. Show model + generated_at for transparency.

### Phase 4 — Bedrock + hardening

- Swap `_call_llm` to Bedrock `invoke_model` (anthropic messages format is
  supported on Bedrock), reusing the boto3 setup.
- Add prompt/version tracking (store the prompt version with each summary so we
  can re-generate when the prompt changes).
- Guardrails: cap event volume per call (paginate/condense very long histories),
  log token usage, and add a feedback signal ("was this helpful?") for tuning.

## Output shape (contract)

```json
{
  "summary": "2-4 sentence narrative of what happened and current state",
  "timeline": [{"at": "<iso>", "explanation": "what this event means / why"}],
  "current_situation": "one sentence: where the member stands now",
  "recommended_actions": [
    {"action": "short imperative", "rationale": "why", "target": "enrollment/case id"}
  ],
  "open_questions": ["anything a human must verify"],
  "confidence": "high | medium | low"
}
```

The `recommended_actions` list is the key AI-agent-consumable surface — it lets an
automated agent act (e.g. "run reconcile on enrollment 10879"), not just read.

## Risks / open questions

- **Cost** at scale → caching by input hash + cheap batch model are the levers.
- **Hallucination** → strict grounding, "say unknown", cite note wording; keep
  humans in the loop for `recommended_actions` until confidence is proven.
- **PII / compliance** → Bedrock in-account hosting for production.
- **Very long histories** → may need condensing / windowing before the call.
