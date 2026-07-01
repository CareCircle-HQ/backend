# Expose the Celery app so `celery -A backend` and `@shared_task` work, and so
# tasks are registered when Django starts. Guarded so the project still runs
# (runserver / manage.py) in environments where Celery isn't installed yet.
try:
    from .celery import app as celery_app

    __all__ = ("celery_app",)
except ModuleNotFoundError:  # celery not installed (e.g. minimal local env)
    __all__ = ()
