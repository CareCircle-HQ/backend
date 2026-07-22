"""One-off data fix (2026-07-22): resolve the 9 blank-org internal-service
cases that back a verification enrollment, per explicit per-client instructions.

For each client we DELETE the listed case id(s). For every enrollment whose
governing case is being deleted, its ``EnrollmentVerification.case`` would
otherwise be nulled (FK is on_delete=SET_NULL) -- we REASSIGN the ones listed in
``REASSIGN`` to a replacement Met Council case first, and leave the rest null
(that is the instructed outcome for those members).

Dry-run by default (prints exactly what WOULD change); pass --apply to commit.
Everything runs in a single transaction and is idempotent: re-running after a
successful apply is a no-op (missing case ids are skipped, already-reassigned
enrollments are left as-is). Safe to delete this command once it has been run on
prod.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Case, Client, EnrollmentVerification

# client_id -> case_id(s) to DELETE.
PLAN = {
    "1d11102a-514d-4b8b-910f-1aaa134d6434": [
        "f027f9ff-d410-44d7-9afd-4e63ae7f9801",
    ],
    "47a37740-b13a-4ebb-bcc3-556da4883539": [
        "0954b0f4-3f2a-4c19-996e-7651045ca628",
        "93baa02a-87f2-40da-b546-c3bf92c660d1",
        "9d3c556c-b60e-44bb-b26f-450b93e83529",
    ],
    "489c8850-ebcc-4e45-be71-a807521230ff": [
        "0a7fccfd-1dc0-4ab2-a8fa-a8c5d5ff835f",
        "4764eda9-c194-4389-9b6d-df69033d640a",
        "b78574cf-2243-425b-9d19-f92c1889f35f",
    ],
    "5d8b45bc-c432-4c32-a7e2-e96d6eb1dd3a": [
        "6dd560d7-27ec-4184-92c6-933b98eaaf9f",
    ],
    "748dcc06-3740-4129-9dc8-cfc8c219c6a5": [
        "636f3d2e-927e-4bda-a553-81ae2de14b30",
    ],
    "752c5f40-48fe-473c-87f3-97d1cd76525a": [
        "1eaa558e-e478-425d-9d74-05d89ca6cc34",
    ],
    "9c6409c3-6c8d-48ec-a8df-403f6545a2c7": [
        "cdee668e-48cf-4e49-9787-86613cb19dd7",
    ],
    "af9e4acd-e10e-471c-8c5d-d96af9547b41": [
        "9a58e3d6-6303-4688-aec1-e2c01675e51b",
        "99d1e0a2-c82b-4950-bec3-f0fc91fcecde",
    ],
    "b5ad1a24-2b92-493e-a018-33b5f9dc65a4": [
        "12ffe436-0138-46d7-858b-2a7942f3ef76",
        "c0723b04-213c-48c2-892f-e7f47cc3a659",
    ],
}

# Enrollment governing-case reassignment: the enrollment currently backed by
# <old_case_id> is moved to <new_case_id> BEFORE the old case is deleted. There
# is a one-enrollment-per-case unique constraint, so any OTHER (stale) enrollment
# already sitting on <new_case_id> is deleted first -- the moved enrollment is
# the one we keep.
REASSIGN = {
    # client 752c5f40: keep the VERIFIED enrollment (on 1eaa558e) and move it onto
    # the kept Met Council case 38c6b9ca, dropping the stale pending enrollment
    # that currently occupies 38c6b9ca.
    "1eaa558e-e478-425d-9d74-05d89ca6cc34": "38c6b9ca-299f-4022-95fb-59a46be5c1e9",
}


class Command(BaseCommand):
    help = (
        "One-off 2026-07-22 fix for the 9 enrollment-backed blank-org cases. "
        "Dry-run by default; pass --apply to commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually apply. Without this the command only previews.",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        all_case_ids = [cid for cids in PLAN.values() for cid in cids]

        # 1) Preflight validation -- every case id must exist and belong to the
        # stated client; the reassignment targets must exist. Any mismatch is a
        # hard stop (never guess on a prod data fix).
        problems = []
        for client_id, case_ids in PLAN.items():
            if not Client.objects.filter(pk=client_id).exists():
                problems.append(f"client {client_id} does not exist")
            for cid in case_ids:
                c = Case.objects.filter(pk=cid).first()
                if c is None:
                    self.stdout.write(f"  note: case {cid} already absent (skip)")
                elif str(c.client_id) != client_id:
                    problems.append(
                        f"case {cid} belongs to {c.client_id}, not {client_id}"
                    )
        for old, new in REASSIGN.items():
            if not Case.objects.filter(pk=new).exists():
                problems.append(f"reassignment target {new} does not exist")
        if problems:
            self.stderr.write(self.style.ERROR("Preflight failed -- aborting:"))
            for p in problems:
                self.stderr.write(f"  - {p}")
            return

        # 2) Preview the enrollment fate for each to-delete case.
        self.stdout.write("Enrollment handling:")
        for e in EnrollmentVerification.objects.filter(
            case_id__in=all_case_ids
        ).order_by("client_id"):
            target = REASSIGN.get(str(e.case_id))
            if target:
                self.stdout.write(
                    f"  enrollment {e.pk} (client {e.client_id}): "
                    f"REASSIGN case {e.case_id} -> {target}"
                )
            else:
                self.stdout.write(self.style.WARNING(
                    f"  enrollment {e.pk} (client {e.client_id}): "
                    f"case {e.case_id} DELETED -> governing case set to NULL"
                ))

        # Stale enrollments occupying a reassignment target (must be removed to
        # satisfy the one-enrollment-per-case unique constraint).
        stale = EnrollmentVerification.objects.filter(
            case_id__in=REASSIGN.values()
        ).exclude(case_id__in=REASSIGN.keys())
        for e in stale:
            self.stdout.write(self.style.WARNING(
                f"  stale enrollment {e.pk} (client {e.client_id}) on target "
                f"case {e.case_id} will be DELETED before reassignment"
            ))

        present = Case.objects.filter(pk__in=all_case_ids)
        self.stdout.write(
            f"\nWill delete {present.count()} of {len(all_case_ids)} listed "
            f"case(s) across {len(PLAN)} client(s)."
        )

        if not apply:
            self.stdout.write(self.style.WARNING(
                "Dry run -- no changes made. Re-run with --apply to commit."
            ))
            return

        with transaction.atomic():
            # Reassign first so the SET_NULL on delete can't touch these rows.
            reassigned = 0
            stale_deleted = 0
            for old, new in REASSIGN.items():
                # Drop any OTHER enrollment already sitting on the target case.
                sd, _ = (EnrollmentVerification.objects
                         .filter(case_id=new).exclude(case_id=old).delete())
                stale_deleted += sd
                reassigned += EnrollmentVerification.objects.filter(
                    case_id=old
                ).update(case_id=new)

            deleted, _ = Case.objects.filter(pk__in=all_case_ids).delete()

        self.stdout.write(self.style.SUCCESS(
            f"Deleted {stale_deleted} stale enrollment(s); reassigned "
            f"{reassigned} enrollment(s); deleted case rows (+children): {deleted}."
        ))

        # 3) Recompute the acquisition funnel for every touched client.
        from api.services.lifecycle import recompute_client_stage

        recomputed = 0
        for client in Client.objects.filter(pk__in=list(PLAN.keys())):
            try:
                recompute_client_stage(client)
                recomputed += 1
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  stage recompute failed for {client.pk}: {exc}")
        self.stdout.write(self.style.SUCCESS(
            f"Recomputed lifecycle stage for {recomputed} client(s)."
        ))
