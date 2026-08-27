"""Per-entity audit history (django-simple-history) with source/actor tagging.

Every tracked model gets its own ``Historical*`` table (full row snapshot per
change) plus two extra columns we add here:

- ``change_source`` — where the change came from: import | extension | admin | crm | system
- ``change_actor``  — who/what made it, e.g. ``agent:355``, ``user:alex``,
  ``system:unite-us-import``

Attribution works two ways:

1. **Server-side jobs** (daily Unite Us pull, CRM sync) wrap their writes in the
   ``change_context(...)`` context manager, which sets a thread-local.
2. **HTTP edits** (agent via the extension, or Django admin) are attributed
   lazily at history-creation time by reading the request that
   ``simple_history.middleware.HistoryRequestMiddleware`` exposes. We do this
   lazily because DRF authentication only resolves ``request.user`` inside the
   view, after request middleware has run.

Agents are NOT Django auth users, so we never bind ``history_user`` (its FK
would reject a non-User principal); the actor is recorded in ``change_actor``.
"""

import threading
from contextlib import contextmanager

from django.db import models
from django.dispatch import receiver
from simple_history.models import HistoricalRecords
from simple_history.signals import pre_create_historical_record

_local = threading.local()


class ChangeSource:
    IMPORT = "import"
    EXTENSION = "extension"
    ADMIN = "admin"
    CRM = "crm"
    SYSTEM = "system"


def set_change_context(source="", actor=""):
    _local.ctx = {"source": source or "", "actor": actor or ""}


def clear_change_context():
    if hasattr(_local, "ctx"):
        del _local.ctx


def current_change_source():
    """The active ``change_context`` source on this thread, or "".

    Lets non-history code (e.g. the ``is_new`` flag in CaseSerializer) tell that
    it's running inside an IMPORT/CRM/etc. block. HTTP writes don't set this
    thread-local (they're attributed lazily from the request), so it's "" there.
    """
    ctx = getattr(_local, "ctx", None)
    return (ctx or {}).get("source", "")


@contextmanager
def change_context(source="", actor=""):
    """Attribute all history rows created in this block to (source, actor).

    Use from server-side code that runs without an HTTP request, e.g.::

        with change_context(ChangeSource.IMPORT, "system:unite-us-import"):
            client.save()
    """
    prev = getattr(_local, "ctx", None)
    set_change_context(source, actor)
    try:
        yield
    finally:
        if prev is None:
            clear_change_context()
        else:
            _local.ctx = prev


class HistoryChangeContextBase(models.Model):
    """Extra columns added to every historical table."""

    change_source = models.CharField(max_length=20, blank=True, db_index=True)
    change_actor = models.CharField(max_length=120, blank=True)

    class Meta:
        abstract = True


def _no_history_user(instance, request):
    # Actor is recorded in change_actor; never bind the history_user FK.
    return None


def tracked_history():
    """A HistoricalRecords configured with our source/actor columns.

    Usage on a model::

        from api.history import tracked_history
        history = tracked_history()
    """
    return HistoricalRecords(
        bases=[HistoryChangeContextBase], get_user=_no_history_user
    )


def _attribution_from_request():
    """Best-effort (source, actor) from the current request, if any."""
    request = getattr(HistoricalRecords.context, "request", None)
    user = getattr(request, "user", None) if request is not None else None
    if user is None:
        return "", ""
    # An AgentUser (the agent JWT principal) is ALWAYS the extension, even when
    # it has no dialer ``agent_code`` -- agents without an extension have
    # agent_code=None (see Agent.agent_code). Keying only on agent_code
    # mis-stamped those code-less agents' writes as ADMIN, so detect the agent
    # principal via ``agent_id`` and prefer the code, falling back to the id, for
    # the actor label.
    agent_id = getattr(user, "agent_id", None)
    agent_code = getattr(user, "agent_code", None)
    if agent_id or agent_code:
        return ChangeSource.EXTENSION, f"agent:{agent_code or agent_id}"
    # A genuine Django auth user (e.g. Django admin / superuser) -- not an agent.
    if getattr(user, "is_authenticated", False):
        label = getattr(user, "username", "") or getattr(user, "pk", "")
        return ChangeSource.ADMIN, f"user:{label}"
    return "", ""


@receiver(pre_create_historical_record)
def _stamp_change_context(sender, history_instance, **kwargs):
    ctx = getattr(_local, "ctx", None)
    if ctx:
        source, actor = ctx.get("source", ""), ctx.get("actor", "")
    else:
        source, actor = _attribution_from_request()
    history_instance.change_source = source
    history_instance.change_actor = actor
    # A ``save(update_fields=[...])`` on a partially-hydrated instance can leave a
    # NOT-NULL string column unset (None) on the in-memory object -- e.g. the
    # denormalized ``governing_internal_case_status`` when only
    # ``governing_internal_case_id`` was touched. The main table is shielded by
    # ``update_fields``, but simple-history copies the WHOLE instance into the
    # history row, so that None would violate the history table's NOT-NULL
    # constraint and crash the save. Coerce such Nones to "" (the fields' intended
    # empty value) so a history write can never break a legitimate save.
    for f in history_instance._meta.fields:
        if (
            getattr(f, "empty_strings_allowed", False)
            and not f.null
            and getattr(history_instance, f.attname, "") is None
        ):
            setattr(history_instance, f.attname, "")
