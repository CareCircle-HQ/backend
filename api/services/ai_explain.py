"""AI-assisted member history explanation (prototype).

Turns a member's raw event history (StageEvents + TimelineEvents) plus their
current lifecycle / enrollment / case state into a grounded, human- AND
AI-agent-readable explanation: a short narrative of what happened, per-event
annotations, and a machine-readable list of recommended next actions.

Design principles (see the brainstorm notes):
* NEVER rewrite the source events. The audit log is immutable / create-once.
  AI output is additive and lives elsewhere (this prototype just prints it;
  production will persist it to ``TimelineEvent.metadata['ai_explanation']`` and
  a per-member summary row).
* GROUNDED: the model is given ONLY the structured facts gathered here and is
  instructed to use nothing else and to say "unknown" rather than guess. This
  is the primary hallucination guard for a benefits/clinical CRM.
* PROVIDER-SWAPPABLE: ``_call_llm`` is the single integration point. It uses the
  Anthropic API when ``ANTHROPIC_API_KEY`` is set; swapping to AWS Bedrock (the
  production target, since infra is already on AWS/boto3) is a change to this
  one function.
"""

import json
import os

from django.utils import timezone

# --- Configuration (env-tunable, mirrors the rest of the settings) ----------
# Direct Anthropic API for the prototype. For production, point this at Bedrock
# so member data stays inside the AWS account (see _call_llm).
_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
_MAX_TOKENS = int(os.getenv("AI_EXPLAIN_MAX_TOKENS", "1500"))


def _iso(value):
    """ISO-8601 (or empty) for any datetime/date/None."""
    return value.isoformat() if value else ""


def _actor_label(actor):
    """Readable name for a StageEvent's user actor (may be None)."""
    if actor is None:
        return ""
    for attr in ("get_full_name", "name", "username", "email"):
        val = getattr(actor, attr, None)
        val = val() if callable(val) else val
        if val:
            return str(val)
    return str(actor)


# ---------------------------------------------------------------------------
# 1. Fact gathering -- the ONLY information the model is allowed to reason over
# ---------------------------------------------------------------------------
def gather_member_facts(client):
    """Build a structured, JSON-serializable snapshot of everything known about
    ``client`` that's relevant to explaining their history. Pure reads; no LLM.

    Returns a dict with: the member's current state, their enrollments, their
    internal-service/other cases, and the merged, chronologically-ordered event
    stream (StageEvents + TimelineEvents)."""
    from api.models import (
        Case,
        EnrollmentVerification,
        MemberDietaryProfile,
        StageEvent,
        TimelineEvent,
    )

    facts = {
        "generated_at": _iso(timezone.now()),
        "member": {
            "client_id": str(client.pk),
            "name": f"{client.first_name or ''} {client.last_name or ''}".strip(),
            "lifecycle_stage": client.lifecycle_stage,
            "ineligible_reasons": list(getattr(client, "ineligible_reasons", []) or []),
        },
        "enrollments": [],
        "cases": [],
        "member_profiles": [],
        "events": [],
    }

    for e in EnrollmentVerification.objects.filter(client=client).order_by("opened_at"):
        facts["enrollments"].append({
            "id": e.pk,
            "stage": e.stage,
            "program_name": e.program_name,
            "renewal_number": e.renewal_number,
            "case_id": str(e.case_id) if e.case_id else None,
            "supersedes_id": e.supersedes_id,
            "opened_at": _iso(e.opened_at),
            "stage_at": _iso(e.stage_at),
            "verified_at": _iso(e.verified_at),
            "nutritionist_approved_at": _iso(e.nutritionist_approved_at),
            "closed_at": _iso(e.closed_at),
        })

    for c in Case.objects.filter(client=client).order_by("date_opened"):
        facts["cases"].append({
            "case_id": str(c.case_id),
            "case_type": c.case_type,
            "case_status": c.case_status,
            "service_authorization_status": c.service_authorization_status,
            "program_name": c.program_name,
            "date_opened": _iso(c.date_opened),
            "case_closed_at": _iso(c.case_closed_at),
        })

    for m in MemberDietaryProfile.objects.filter(client=client):
        facts["member_profiles"].append({
            "id": m.pk,
            "status": m.status,
            "eligibility_paused": getattr(m, "eligibility_paused", None),
            "enrollment_id": m.enrollment_id,
        })

    # Merge both event logs into one chronological stream -- this is the spine of
    # the narrative. Each row is tagged with its log so the model can weight the
    # curated timeline vs the raw stage audit.
    events = []
    for se in StageEvent.objects.filter(client=client).select_related("actor").order_by("entered_at"):
        events.append({
            "at": _iso(se.entered_at),
            "log": "stage_event",
            "enrollment_id": se.enrollment_id,
            "transition": f"{se.from_stage or '-'} -> {se.to_stage}",
            "source": se.source,
            "actor": _actor_label(se.actor),
            "note": se.note or "",
        })
    # StageEvents attached to the member's enrollments but logged against the
    # enrollment entity (entity_type='enrollment') may not carry client -- include
    # them via the enrollment ids too.
    enr_ids = [e["id"] for e in facts["enrollments"]]
    if enr_ids:
        for se in (
            StageEvent.objects.filter(enrollment_id__in=enr_ids)
            .exclude(client=client)
            .select_related("actor")
            .order_by("entered_at")
        ):
            events.append({
                "at": _iso(se.entered_at),
                "log": "stage_event",
                "enrollment_id": se.enrollment_id,
                "transition": f"{se.from_stage or '-'} -> {se.to_stage}",
                "source": se.source,
                "actor": _actor_label(se.actor),
                "note": se.note or "",
            })
    for te in TimelineEvent.objects.filter(client=client).order_by("occurred_at"):
        events.append({
            "at": _iso(te.occurred_at),
            "log": "timeline_event",
            "enrollment_id": te.enrollment_id,
            "event_type": te.event_type,
            "title": te.title,
            "subtitle": te.subtitle,
            "badge": te.badge_text,
            "source": te.source,
            "actor": te.actor,
        })
    events.sort(key=lambda r: r["at"] or "")
    facts["events"] = events
    return facts


# ---------------------------------------------------------------------------
# 2. Prompt construction
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are a case-history analyst for a food-benefits CRM. You explain what "
    "happened to a member so a care agent (or an automated agent) can resolve "
    "their situation. You are given a structured JSON snapshot of the member's "
    "current state and their full, chronologically ordered event history.\n\n"
    "STRICT RULES:\n"
    "- Use ONLY the facts in the provided JSON. Do not invent dates, reasons, "
    "names, or policies. If something is not determinable from the facts, say "
    "'unknown' or 'not recorded'.\n"
    "- Prefer the exact wording of event notes when citing a reason.\n"
    "- Be concise and specific. Reference dates and enrollment/case ids.\n\n"
    "Respond with a single JSON object of this shape:\n"
    "{\n"
    '  "summary": "2-4 sentence plain-English narrative of what happened and the current state",\n'
    '  "timeline": [{"at": "<iso>", "explanation": "one line: what this event means and why it happened"}],\n'
    '  "current_situation": "one sentence: where the member stands right now",\n'
    '  "recommended_actions": [{"action": "short imperative", "rationale": "why", "target": "enrollment/case id if any"}],\n'
    '  "open_questions": ["anything a human must verify that the facts do not settle"],\n'
    '  "confidence": "high | medium | low"\n'
    "}"
)


def build_member_summary_prompt(facts):
    """Return ``(system_prompt, user_prompt)`` for the per-member summary +
    per-event explanation call, grounded in ``facts``."""
    user = (
        "Here is the member snapshot and event history as JSON. Explain it per "
        "the rules and output the JSON object.\n\n```json\n"
        + json.dumps(facts, indent=2, default=str)
        + "\n```"
    )
    return _SYSTEM_PROMPT, user


# ---------------------------------------------------------------------------
# 3. LLM call -- the single provider-swappable integration point
# ---------------------------------------------------------------------------
def llm_available():
    """True when an Anthropic key is configured AND the SDK is importable."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _call_llm(system_prompt, user_prompt):
    """Send the prompt to the model and return the raw text response.

    Prototype uses the direct Anthropic API. PRODUCTION: replace the body with a
    Bedrock ``invoke_model`` (anthropic.messages format is supported on Bedrock),
    reusing the existing boto3 setup, so member data never leaves AWS."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")


def explain_member(client):
    """Gather facts, build the prompt, and (when the LLM is configured) return
    the parsed explanation.

    Returns a dict: ``{"facts", "system_prompt", "user_prompt", "explanation",
    "raw", "llm_called"}``. When no LLM is configured, ``explanation``/``raw``
    are None and the caller can inspect the prompt (dry-run)."""
    facts = gather_member_facts(client)
    system_prompt, user_prompt = build_member_summary_prompt(facts)
    result = {
        "facts": facts,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "explanation": None,
        "raw": None,
        "llm_called": False,
    }
    if not llm_available():
        return result
    raw = _call_llm(system_prompt, user_prompt)
    result["raw"] = raw
    result["llm_called"] = True
    try:
        result["explanation"] = json.loads(raw)
    except (ValueError, TypeError):
        # Model didn't return clean JSON -- surface the raw text so the caller
        # can still use it / we can tighten the prompt.
        result["explanation"] = None
    return result
