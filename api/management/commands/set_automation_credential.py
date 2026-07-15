"""Flag ONE captured Unite Us credential as the dedicated automation session.

Background jobs (the exports automation) prefer the ``for_automation`` credential
so a server-side token refresh never rotates -- and logs out -- a real agent's
live browser session. Point this at a Unite Us SERVICE ACCOUNT that's been logged
in via the extension (so its session is captured), then flag it here.

    # by employee id (x-employee-id of the service account)
    python manage.py set_automation_credential --employee-id <uuid>
    # or by row pk
    python manage.py set_automation_credential --pk 42
    # clear the flag entirely (fall back to newest-active selection)
    python manage.py set_automation_credential --clear

Setting one automatically clears the flag on all others (only one at a time).
Uses .update() so it never decrypts the stored tokens.
"""
from django.core.management.base import BaseCommand, CommandError

from api.models import UniteUsCredential, UniteUsCredentialStatus


class Command(BaseCommand):
    help = "Flag one Unite Us credential as the dedicated automation session."

    def add_arguments(self, parser):
        parser.add_argument("--employee-id", default="", help="x-employee-id of the credential to flag.")
        parser.add_argument("--pk", type=int, default=None, help="UniteUsCredential row pk to flag.")
        parser.add_argument("--clear", action="store_true", help="Clear for_automation on all credentials.")

    def handle(self, *args, **opts):
        if opts["clear"]:
            n = UniteUsCredential.objects.filter(for_automation=True).update(for_automation=False)
            self.stdout.write(self.style.SUCCESS(f"Cleared for_automation on {n} credential(s)."))
            return

        qs = UniteUsCredential.objects.all()
        if opts["pk"]:
            qs = qs.filter(pk=opts["pk"])
        elif opts["employee_id"]:
            qs = qs.filter(employee_id=opts["employee_id"].strip())
        else:
            raise CommandError("Provide --employee-id, --pk, or --clear.")

        # values_list avoids selecting (and decrypting) the token columns.
        rows = list(qs.values_list("pk", "employee_id", "provider_id", "status"))
        if not rows:
            raise CommandError("No matching credential found.")
        if len(rows) > 1:
            raise CommandError(
                f"Matched {len(rows)} credentials; narrow with --pk. "
                f"pks: {[r[0] for r in rows]}"
            )
        pk, employee_id, provider_id, status = rows[0]
        if status != UniteUsCredentialStatus.ACTIVE:
            self.stdout.write(self.style.WARNING(
                f"Note: credential #{pk} status is '{status}', not ACTIVE -- "
                "automation will skip it until it's active/refreshed."
            ))

        # Exactly one automation credential: clear all, then set this one.
        UniteUsCredential.objects.filter(for_automation=True).exclude(pk=pk).update(
            for_automation=False
        )
        UniteUsCredential.objects.filter(pk=pk).update(for_automation=True)
        self.stdout.write(self.style.SUCCESS(
            f"Flagged credential #{pk} (provider={provider_id} employee={employee_id}) "
            "as the automation session."
        ))
