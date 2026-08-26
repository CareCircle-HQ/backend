from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = 'api'

    def ready(self):
        # TEMP DEBUG (remove after root-causing the false-verification incident):
        # when TRACE_SYSTEM_VERIFICATION=1, log a full stack trace the instant an
        # EnrollmentVerification is saved with verified_at SET but verified_by
        # NULL and verified_at transitioning None -> value (a "system"
        # verification being created). Pinpoints the origin code path during an
        # `import_csv --type cases` run. No-op unless the env var is set.
        import os

        if not os.getenv("TRACE_SYSTEM_VERIFICATION"):
            return

        import logging
        import traceback

        from django.db.models.signals import pre_save

        logger = logging.getLogger("trace.system_verification")

        def _trace_system_verification(sender, instance, **kwargs):
            try:
                if instance.verified_at is None or instance.verified_by_id is not None:
                    return
                was_none = True
                if not instance._state.adding and instance.pk:
                    old = (
                        sender.objects.filter(pk=instance.pk)
                        .values_list("verified_at", flat=True)
                        .first()
                    )
                    was_none = old is None
                if not was_none:
                    return  # already had verified_at -- not a new stamp
                logger.warning(
                    "SYSTEM verified_at stamped (verified_by NULL): enr=%s client=%s "
                    "stage=%s adding=%s\n%s",
                    instance.pk, instance.client_id, instance.stage,
                    instance._state.adding,
                    "".join(traceback.format_stack(limit=30)),
                )
            except Exception:  # noqa: BLE001 - tracing must never break a save
                pass

        from api.models import EnrollmentVerification

        pre_save.connect(
            _trace_system_verification,
            sender=EnrollmentVerification,
            dispatch_uid="trace_system_verification",
        )
