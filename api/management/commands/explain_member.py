"""Prototype: AI-explain a single member's history.

Gathers the member's structured facts + full event stream, builds the grounded
prompt, and -- when an Anthropic key is configured -- calls the model and prints
the explanation (narrative + per-event annotations + recommended actions).

With NO key configured it runs as a dry run: it prints the exact prompt that
WOULD be sent, so you can inspect / tune the context before wiring the provider.

    python manage.py explain_member <client_id>
    python manage.py explain_member <client_id> --show-prompt   # also print the prompt
    python manage.py explain_member <client_id> --facts-only     # just the gathered JSON
"""
import json

from django.core.management.base import BaseCommand, CommandError

from api.models import Client
from api.services import ai_explain


class Command(BaseCommand):
    help = "AI-explain a single member's history (prototype; dry-runs without an LLM key)."

    def add_arguments(self, parser):
        parser.add_argument("client_id", type=str, help="Client UUID to explain.")
        parser.add_argument(
            "--show-prompt", action="store_true",
            help="Also print the system + user prompt that is/would be sent.",
        )
        parser.add_argument(
            "--facts-only", action="store_true",
            help="Only gather and print the structured facts JSON (no prompt, no LLM).",
        )

    def handle(self, *args, **opts):
        client = Client.objects.filter(pk=opts["client_id"]).first()
        if client is None:
            raise CommandError(f"No client with id {opts['client_id']}")

        if opts["facts_only"]:
            facts = ai_explain.gather_member_facts(client)
            self.stdout.write(json.dumps(facts, indent=2, default=str))
            return

        result = ai_explain.explain_member(client)
        name = result["facts"]["member"]["name"] or client.pk

        if opts["show_prompt"] or not result["llm_called"]:
            self.stdout.write(self.style.MIGRATE_HEADING("\n=== SYSTEM PROMPT ==="))
            self.stdout.write(result["system_prompt"])
            self.stdout.write(self.style.MIGRATE_HEADING("\n=== USER PROMPT ==="))
            self.stdout.write(result["user_prompt"])

        if not result["llm_called"]:
            self.stdout.write(self.style.WARNING(
                "\nNo LLM configured (set ANTHROPIC_API_KEY and `pip install anthropic`). "
                "Printed the prompt above as a dry run."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== EXPLANATION: {name} ==="))
        if result["explanation"] is not None:
            self.stdout.write(json.dumps(result["explanation"], indent=2))
        else:
            self.stdout.write(self.style.WARNING(
                "Model did not return valid JSON. Raw response:"
            ))
            self.stdout.write(result["raw"] or "(empty)")
