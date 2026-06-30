"""Diagnose why a client's lifecycle_stage is what it is (read-only).

Prints the stored stage, the early-funnel inputs, every case (type / status /
authorization) and every governing enrollment, then the freshly *derived* stage
so we can see whether the stored value is stale or genuinely correct.

    python manage.py diagnose_client <client_id>
    python manage.py diagnose_client <client_id> --recompute   # write the fix

No writes happen unless --recompute is passed.
"""

from django.core.management.base import BaseCommand

from api.models import CaseType, Client
from api.services.lifecycle import (
    _derive_early_funnel,
    _governing_enrollments,
    _primary_enrollment,
    derive_client_stage,
    governing_case_key,
    recompute_client_stage,
)


class Command(BaseCommand):
    help = "Read-only diagnosis of a client's lifecycle_stage."

    def add_arguments(self, parser):
        parser.add_argument("client_id", help="Client UUID.")
        parser.add_argument(
            "--recompute", action="store_true",
            help="Apply recompute_client_stage and save the derived stage.",
        )

    def handle(self, *args, **options):
        cid = options["client_id"]
        client = Client.objects.filter(pk=cid).first()
        if not client:
            self.stderr.write(self.style.ERROR(f"No client with id {cid}"))
            return

        self.stdout.write(self.style.MIGRATE_HEADING("1) Client"))
        self.stdout.write(f"  client_id       : {client.pk}")
        self.stdout.write(f"  name            : {client.first_name} {client.last_name}")
        self.stdout.write(f"  lifecycle_stage : {client.lifecycle_stage}  (stored)")

        # --- Cases -----------------------------------------------------------
        cases = list(client.cases.all())
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n2) Cases ({len(cases)})"))
        for c in cases:
            self.stdout.write(
                f"  - {c.case_id}\n"
                f"      case_type   : {c.case_type}\n"
                f"      case_status : {c.case_status}\n"
                f"      service_type: {c.service_type!r}\n"
                f"      program_name: {c.program_name!r}\n"
                f"      auth_status : {c.service_authorization_status!r} "
                f"({c.service_authorization_status_label!r})\n"
                f"      provider    : {c.provider_name or c.originating_provider_name!r}\n"
                f"      date_opened : {c.date_opened}"
            )
        internal = [c for c in cases if c.case_type == CaseType.INTERNAL_SERVICE]
        if internal:
            gov = max(internal, key=governing_case_key)
            self.stdout.write(self.style.SUCCESS(
                f"  governing internal-service case: {gov.case_id} "
                f"(auth={gov.service_authorization_status!r})"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "  No INTERNAL_SERVICE case -> client cannot leave the early funnel "
                "via case authorization."
            ))

        # --- Enrollments -----------------------------------------------------
        enrollments = _governing_enrollments(client)
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n3) Governing enrollments ({len(enrollments)})"
        ))
        for e in enrollments:
            self.stdout.write(
                f"  - {e.pk}  stage={e.stage}  stage_at={e.stage_at}  "
                f"case={getattr(e, 'case_id', None)}"
            )
        primary = _primary_enrollment(client)
        self.stdout.write(
            f"  primary enrollment: "
            f"{primary.pk if primary else '(none)'}"
            + (f"  stage={primary.stage}" if primary else "")
        )

        # --- Derived stage ---------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("\n4) Stage derivation"))
        early = _derive_early_funnel(client)
        derived = derive_client_stage(client)
        self.stdout.write(f"  early-funnel stage : {early}")
        self.stdout.write(f"  derived stage      : {derived}")
        self.stdout.write(f"  stored stage       : {client.lifecycle_stage}")
        if str(derived) != str(client.lifecycle_stage):
            self.stdout.write(self.style.WARNING(
                "  -> STORED IS STALE. Re-run with --recompute to fix this client."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "  -> stored matches derived; the stage is computed correctly."
            ))

        # --- Optional write --------------------------------------------------
        if options["recompute"]:
            new_stage = recompute_client_stage(client, save=True)
            self.stdout.write(self.style.SUCCESS(
                f"\n5) recompute_client_stage applied -> {new_stage}"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "\nRead-only. Re-run with --recompute to apply the derived stage."
            ))
