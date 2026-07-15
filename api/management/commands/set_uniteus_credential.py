"""DISCOVERY (temporary): upsert a fresh Unite Us credential from a token you
grabbed in the browser, so probes can run locally without the extension.

Because it saves through EncryptedTextField, the token is encrypted with THIS
environment's FIELD_ENCRYPTION_KEY -- so a locally-created credential is always
readable locally (unlike a prod-copy DB).

How to grab the values (you're already logged into Unite Us):
  1. Open app.uniteus.io, DevTools -> Network.
  2. Click any request to core.uniteus.io/v1/... and look at Request Headers.
  3. Copy: authorization (the part AFTER "Bearer "), x-provider-id, x-employee-id.

    python manage.py set_uniteus_credential \
        --provider-id <x-provider-id> \
        --employee-id <x-employee-id> \
        --access-token '<bearer token, no "Bearer " prefix>'

Then run:  python manage.py probe_uniteus_exports --limit 5
(the access token is short-lived; run the probe promptly).
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from datetime import timedelta

from api.models import UniteUsCredential, UniteUsCredentialStatus


class Command(BaseCommand):
    help = "Upsert a Unite Us credential from a browser-captured token (local probing)."

    def add_arguments(self, parser):
        parser.add_argument("--provider-id", required=True, help="x-provider-id header value.")
        parser.add_argument("--employee-id", default="", help="x-employee-id header value.")
        parser.add_argument("--access-token", required=True,
                             help="Bearer access token WITHOUT the 'Bearer ' prefix.")
        parser.add_argument("--refresh-token", default="",
                             help="Optional refresh token (not needed for a read-only probe).")
        parser.add_argument("--expires-in", type=int, default=3600,
                             help="Access-token lifetime in seconds (default 3600).")
        parser.add_argument("--for-automation", action="store_true",
                             help="Flag this as the dedicated automation credential "
                                  "(preferred by background jobs; clears the flag on others).")

    def handle(self, *args, **opts):
        token = opts["access_token"].strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise CommandError("--access-token is empty after stripping.")

        pid = opts["provider_id"].strip()
        eid = opts["employee_id"].strip()
        defaults = {
            "access_token": token,
            "refresh_token": opts["refresh_token"].strip(),
            "access_expires_at": timezone.now() + timedelta(seconds=opts["expires_in"]),
            "token_type": "Bearer",
            "status": UniteUsCredentialStatus.ACTIVE,
            "last_captured_at": timezone.now(),
        }

        # NB: do NOT use update_or_create here -- a matching row may already
        # exist (e.g. from a prod-copy DB) whose token was encrypted with a
        # DIFFERENT key; update_or_create's get() would try to decrypt it and
        # blow up. Find the pk without selecting the encrypted columns, then
        # .update() (which only ENCRYPTS on write, never decrypts).
        existing_pk = (
            UniteUsCredential.objects.filter(provider_id=pid, employee_id=eid)
            .values_list("pk", flat=True)
            .first()
        )
        if opts["for_automation"]:
            defaults["for_automation"] = True

        if existing_pk:
            UniteUsCredential.objects.filter(pk=existing_pk).update(**defaults)
            pk, created = existing_pk, False
        else:
            obj = UniteUsCredential.objects.create(provider_id=pid, employee_id=eid, **defaults)
            pk, created = obj.pk, True

        # Only one automation credential at a time.
        if opts["for_automation"]:
            UniteUsCredential.objects.filter(for_automation=True).exclude(pk=pk).update(
                for_automation=False
            )

        # Read only OUR row back to confirm it decrypts with the local key.
        cred = UniteUsCredential.objects.get(pk=pk)
        ok = bool(cred.access_token)
        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Updated'} credential #{cred.pk} "
            f"provider={cred.provider_id} employee={cred.employee_id} "
            f"decrypt_ok={ok}"
        ))
        self.stdout.write("Now run: python manage.py probe_uniteus_exports --limit 5")
