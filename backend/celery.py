"""Celery application for async imports + smart diagnostics.

The worker is a separate long-running process (systemd) on the same EC2 as
gunicorn; Redis (also local to start) is the broker/result backend. Task config
lives in Django settings under the ``CELERY_`` namespace.

Run locally:
    celery -A backend worker -l info --concurrency=2
    celery -A backend beat -l info        # scheduled diagnostics (later)
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

app = Celery("backend")

# Pull CELERY_* settings from Django and auto-discover tasks.py in each app.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
