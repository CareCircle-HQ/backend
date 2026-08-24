"""Import a delivery company's Proof-of-Delivery report (CSV): match each row to
its DeliveryOrder, update status/delivered_at, and fetch the photo URLs into S3
as DeliveryOrderProof rows. See docs/proof_of_delivery_plan.md.

    # dry run (match + parse only, no writes / no image fetch)
    python manage.py import_delivery_pod --company QARI --file "tmp/import/Delivery Report_08.17.26_QARI_ENG_PHS.csv"

    # apply (update orders + download images to S3)
    python manage.py import_delivery_pod --company QARI --file <path> --apply

    # process a file already uploaded to S3
    python manage.py import_delivery_pod --company USP --key imports/<uuid>/report.csv --apply
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Import a delivery company's proof-of-delivery report (CSV)."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True,
                            help="Delivery company name (substring) or id.")
        src = parser.add_mutually_exclusive_group(required=True)
        src.add_argument("--file", help="Local path to the report CSV.")
        src.add_argument("--key", help="S3 key of an already-uploaded report CSV.")
        parser.add_argument("--apply", action="store_true",
                            help="Write changes + fetch images (default: dry run).")
        parser.add_argument("--no-fetch", action="store_true",
                            help="Match/update only; do not download images.")

    def handle(self, *args, **opts):
        from api.models import (
            DeliveryCompany, ImportRun, ImportRunStatus,
        )
        from api.services import import_storage
        from api.services.pod_import import POD_SOURCE, run_pod_import_from_bytes

        company = self._resolve_company(DeliveryCompany, opts["company"])
        apply = bool(opts["apply"])
        fetch = not opts["no_fetch"]

        # Load the CSV bytes (local file or S3).
        if opts["file"]:
            path = opts["file"]
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            if not os.path.exists(path):
                raise CommandError(f"File not found: {path}")
            with open(path, "rb") as fh:
                data = fh.read()
            source_report = os.path.basename(path)
        else:
            tmp = import_storage.download_to_temp(opts["key"])
            with open(tmp, "rb") as fh:
                data = fh.read()
            source_report = os.path.basename(opts["key"])

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== POD import ({'APPLY' if apply else 'DRY RUN'}) ==="
        ))
        self.stdout.write(f"  company={company.name} ({company.pk})")
        self.stdout.write(f"  file={source_report}\n")

        run = ImportRun.objects.create(
            source=POD_SOURCE, status=ImportRunStatus.RUNNING,
            triggered_by="manual", export_type="delivery_pod",
            original_filename=source_report[:255],
        )
        try:
            importer = run_pod_import_from_bytes(
                data=data, delivery_company=company, source_report=source_report,
                apply=apply, fetch=fetch,
            )
        except Exception as exc:
            run.status = ImportRunStatus.FAILED
            run.error_log = str(exc)[:5000]
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error_log", "finished_at"])
            raise CommandError(str(exc))

        s = importer.stats
        run.status = ImportRunStatus.COMPLETED
        run.finished_at = timezone.now()
        run.processed_count = s.get("rows", 0)
        run.progress_total = s.get("rows", 0)
        run.created_count = s.get("proofs_created", 0)
        run.updated_count = s.get("orders_updated", 0)
        run.skipped_count = s.get("unmatched", 0) + s.get("proofs_deduped", 0)
        run.error_count = s.get("images_failed", 0) + s.get("row_errors", 0)
        run.stats = {"delivery_pod": s}
        run.error_log = "\n".join(importer.errors[:200])
        run.save()

        for k in ("rows", "matched", "unmatched", "member_mismatch", "orders_updated",
                  "proofs_created", "proofs_deduped", "images_expired", "images_failed"):
            self.stdout.write(f"  {k:16s}: {s.get(k, 0)}")
        if importer.errors:
            self.stdout.write(self.style.WARNING(
                f"\n  {len(importer.errors)} issue(s); first 10:"))
            for e in importer.errors[:10]:
                self.stdout.write(f"    - {e}")
        self.stdout.write(self.style.SUCCESS(
            f"\n{'APPLIED' if apply else 'DRY RUN'} — ImportRun #{run.pk}"))

    @staticmethod
    def _resolve_company(DeliveryCompany, token):
        token = (token or "").strip()
        c = DeliveryCompany.objects.filter(pk=token).first() if _looks_uuid(token) else None
        if c is None:
            matches = list(DeliveryCompany.objects.filter(name__icontains=token)[:5])
            if not matches:
                raise CommandError(f"No delivery company matching '{token}'.")
            if len(matches) > 1:
                names = ", ".join(f"{m.name} ({m.pk})" for m in matches)
                raise CommandError(f"Ambiguous company '{token}': {names}")
            c = matches[0]
        return c


def _looks_uuid(s):
    import uuid
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, AttributeError, TypeError):
        return False
