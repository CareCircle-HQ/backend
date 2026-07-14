"""Normalize every stored address ZIP to 5 digits.

Addresses often arrive as ZIP+4 ("55401-1234"), with stray spaces, or with a
trailing country segment. Delivery coverage, kitchen routing and the
``AllowedZipCode`` match all key on the FIRST 5 DIGITS, so this command rewrites
:class:`~api.models.Address.zip` to just those 5 digits.

Safe by default: it only REPORTS what it would change. Pass ``--apply`` to write.

    python manage.py normalize_zip_codes            # dry run (no writes)
    python manage.py normalize_zip_codes --apply     # persist the fixes
    python manage.py normalize_zip_codes --apply --include-doctor  # also Client.doctor_zip

A value with FEWER than 5 digits (e.g. "5540") can't be made 5 digits, so it is
left untouched and listed as "unfixable" for manual review.
"""

import re

from django.core.management.base import BaseCommand

from api.models import Address, Client

BATCH = 1000


def normalize_zip(raw):
    """Return the first 5 digits of a ZIP string.

    ("55401-1234" -> "55401", " 55401 " -> "55401", "554011234" -> "55401").
    Returns:
      * the 5-digit string when at least 5 digits are present;
      * "" when the input is blank (nothing to do);
      * None when digits exist but there are fewer than 5 (unfixable).
    """
    if raw is None:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        # No digits at all -- treat blank/garbage as "nothing to normalize"
        # rather than unfixable, so it isn't flagged for manual review.
        return ""
    if len(digits) < 5:
        return None
    return digits[:5]


class Command(BaseCommand):
    help = "Normalize all stored address ZIP codes to 5 digits."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Persist the changes. Without it, the command only reports.",
        )
        parser.add_argument(
            "--include-doctor", action="store_true",
            help="Also normalize Client.doctor_zip (attestation doctor address).",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Cap the number of rows scanned per model (for testing).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        limit = options["limit"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            "Normalizing address ZIP codes to 5 digits "
            f"({'APPLY' if apply else 'DRY RUN'})"
        ))

        self._normalize_model(
            Address, "zip", apply, limit, label="Address.zip",
        )
        if options["include_doctor"]:
            self._normalize_model(
                Client, "doctor_zip", apply, limit,
                label="Client.doctor_zip",
            )

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDry run only -- no changes written. Re-run with --apply to persist."
            ))

    def _normalize_model(self, model, field, apply, limit, label):
        qs = model.objects.exclude(**{field: ""}).order_by("pk")
        if limit:
            qs = qs[:limit]

        changed = unfixable = 0
        pending = []
        examples = []

        for obj in qs.iterator(chunk_size=BATCH):
            current = getattr(obj, field) or ""
            new = normalize_zip(current)
            if new is None:
                unfixable += 1
                if len(examples) < 10:
                    examples.append(f"{current!r} (pk={obj.pk})")
                continue
            if new != current:
                changed += 1
                if len(examples) < 10:
                    examples.append(f"{current!r} -> {new!r}")
                if apply:
                    setattr(obj, field, new)
                    pending.append(obj)
                    if len(pending) >= BATCH:
                        model.objects.bulk_update(pending, [field])
                        pending.clear()

        if apply and pending:
            model.objects.bulk_update(pending, [field])

        self.stdout.write(
            self.style.SUCCESS(
                f"{label}: {changed} {'fixed' if apply else 'to fix'}"
            )
            + (f", {unfixable} unfixable (<5 digits)" if unfixable else "")
        )
        for ex in examples:
            self.stdout.write(f"    {ex}")
