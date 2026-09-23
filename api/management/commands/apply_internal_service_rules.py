"""Preview or apply the internal-service hold rules.

    python manage.py apply_internal_service_rules            # dry run
    python manage.py apply_internal_service_rules --apply
    python manage.py apply_internal_service_rules --client <uuid>

⚠ THIS EXISTS BECAUSE THE RULES OTHERWISE HAVE NO DRY RUN. They fire from
``reconcile_client_eligibility``, which runs on the extension save and the CSV
import -- so without this command the first time anyone sees the blast radius is
after an import has already held 154 enrollments. That is the same risk I insisted on
a dry run for with ``hold_household_cases``, and then did not build for these.

The rules themselves live in ``api/services/internal_service_rules.py`` and are NOT
duplicated here: this command calls ``evaluate_governing_case`` for the verdict and
``apply_internal_service_rules`` for the write, so the preview cannot drift from what
an import will actually do.

    rule 2  no ECM in the latest assessment          HOLD
    rule 3  boxes-only eligibility + a MEALS case    HOLD
    rule 4  meals-only eligibility + a BOXES case    warning only, never held

⚠ 95% OF WHAT THESE RULES HOLD IS INDIVIDUAL-SCOPE, not household -- 149 of 154 on
the clone. That matters because a hold lives on the ENROLLMENT and drops the whole
household off every Purchase Order: for an individual case that is usually one
member, but for a household-scope case every dependent loses service over the
primary's case. The report breaks both out, and names the households with dependents
at risk.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

LIVE_CASE_STATUSES = ("open", "managed", "pending_authorization", "off_platform")


class Command(BaseCommand):
    help = "Preview or apply the internal-service programme hold rules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually hold. Without it the command only reports.",
        )
        parser.add_argument(
            "--client", default="",
            help="Limit to one client UUID.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after N affected members (for a quick look).",
        )

    def handle(self, *args, **options):
        from collections import Counter

        from api.models import Client, EnrollmentStage, HoldReason, HouseholdMember
        from api.portal.serializers import internal_service_case
        from api.services.internal_service_rules import (
            apply_internal_service_rules, evaluate_governing_case,
        )

        apply_it = options["apply"]
        limit = options["limit"]

        labels = {r.code: r.label for r in HoldReason.objects.all()}
        for needed in ("not_enhanced_member", "wrong_case_type"):
            if needed not in labels:
                self.stderr.write(self.style.ERROR(
                    f"hold reason {needed!r} is missing from the catalogue. "
                    "Run migrate first."
                ))
                return

        clients = Client.objects.filter(
            cases__case_type="internal_service",
            cases__case_status__in=LIVE_CASE_STATUSES,
        ).distinct().prefetch_related("cases", "assessments", "enrollments")
        if options["client"]:
            clients = clients.filter(client_id=options["client"])

        counts = Counter()
        affected = []
        for client in clients.iterator(chunk_size=500):
            counts["members with a live governing case"] += 1
            verdict = evaluate_governing_case(client)
            if verdict is None:
                continue
            code, reason = verdict
            governing = internal_service_case(client)
            scope = (governing.household_type or "(blank)") if governing else "-"
            counts[f"{labels.get(code, code)} · {scope}"] += 1

            # Dependents who would lose service for the PRIMARY's case being wrong.
            dependents = 0
            member = HouseholdMember.objects.filter(client=client).first()
            if member is not None and member.household_id:
                dependents = HouseholdMember.objects.filter(
                    household_id=member.household_id,
                ).exclude(client=client).count()

            already_held = any(
                e.stage == EnrollmentStage.ON_HOLD for e in client.enrollments.all()
            )
            if already_held:
                counts["  ... already On Hold, will be left alone"] += 1

            affected.append({
                "client": client,
                "code": code,
                "reason": reason,
                "scope": scope,
                "dependents": dependents,
                "already_held": already_held,
                "service_type": getattr(governing, "service_type", ""),
            })
            if limit and len(affected) >= limit:
                break

        self.stdout.write(
            f"members with a live governing case: "
            f"{counts['members with a live governing case']:,}"
        )
        self.stdout.write("")
        self.stdout.write("WOULD HOLD:")
        for key, n in sorted(counts.items()):
            if key.startswith("members with"):
                continue
            self.stdout.write(f"   {n:>5}  {key}")

        by_scope = Counter(a["scope"] for a in affected)
        self.stdout.write("")
        self.stdout.write(
            f"   total {len(affected):,} enrollment(s) — "
            + " · ".join(f"{n} {s}" for s, n in by_scope.most_common())
        )

        # ⚠ The rows that take somebody ELSE'S service away.
        at_risk = [
            a for a in affected
            if a["scope"] == "household" and a["dependents"] > 0
        ]
        if at_risk:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"{len(at_risk)} HOUSEHOLD-scope hold(s) would also stop service for "
                f"{sum(a['dependents'] for a in at_risk)} dependent(s):"
            ))
            for a in at_risk:
                c = a["client"]
                self.stdout.write(
                    f"    {c.first_name} {c.last_name}  "
                    f"+{a['dependents']} dependent(s)  {labels.get(a['code'])}"
                )

        self.stdout.write("")
        self.stdout.write("examples:")
        seen = set()
        for a in affected:
            key = (a["code"], a["scope"])
            if key in seen:
                continue
            seen.add(key)
            c = a["client"]
            self.stdout.write(
                f"    {str(c.client_id)}  {c.first_name} {c.last_name[:16]:16} "
                f"{a['scope']:11} {a['service_type'][:28]:28} "
                f"{labels.get(a['code'])}"
            )

        if not apply_it:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run. Re-run with --apply to hold them."
            ))
            return

        held = failed = 0
        problems = []
        for a in affected:
            # Per member, not one transaction over all of them: a single bad
            # transition must not roll back the rest, and a re-run resumes where
            # this stopped.
            try:
                with transaction.atomic():
                    if apply_internal_service_rules(
                        a["client"], actor_label="system: internal-service rules",
                    ):
                        held += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                problems.append((a["client"], str(exc)[:90]))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"held {held:,} · failed {failed:,}"
        ))
        if problems:
            self.stdout.write(self.style.WARNING("failures:"))
            for client, why in problems[:15]:
                self.stdout.write(f"    {client.first_name} {client.last_name}: {why}")
