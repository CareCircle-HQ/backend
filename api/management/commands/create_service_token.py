"""Create (or rotate) a long-lived API token for a non-interactive service user.

The extension ships this token and sends it as `Authorization: Token <key>`,
so end users never have to log in.

Examples:
    python manage.py create_service_token
    python manage.py create_service_token --username ext-service --rotate
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token

User = get_user_model()


class Command(BaseCommand):
    help = "Create or rotate a static API token for a service user."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            default="ext-service",
            help="Service account username (default: ext-service).",
        )
        parser.add_argument(
            "--staff",
            action="store_true",
            help="Grant is_staff so the account can also use the admin import UI.",
        )
        parser.add_argument(
            "--rotate",
            action="store_true",
            help="Delete any existing token and issue a fresh one.",
        )

    def handle(self, *args, **options):
        username = options["username"]
        user, created = User.objects.get_or_create(username=username)

        if created:
            # Service account: no usable password, login only via token.
            user.set_unusable_password()
            user.is_active = True

        if options["staff"]:
            user.is_staff = True
        user.save()

        if options["rotate"]:
            Token.objects.filter(user=user).delete()

        token, _ = Token.objects.get_or_create(user=user)

        self.stdout.write(self.style.SUCCESS("Service user ready."))
        self.stdout.write(f"  username : {user.username}")
        self.stdout.write(f"  created  : {created}")
        self.stdout.write(f"  is_staff : {user.is_staff}")
        self.stdout.write(self.style.MIGRATE_HEADING("  API token:"))
        self.stdout.write(f"  {token.key}")
        self.stdout.write("")
        self.stdout.write("Use it as:  Authorization: Token " + token.key)
