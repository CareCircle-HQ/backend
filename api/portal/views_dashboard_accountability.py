"""Agent Accountability dashboard -- per-agent (screener) productivity.

A single aggregate endpoint reporting, over a date window, how many:
  * Screenings              -- by ``Screening.facilitator_id`` (maps to
                               ``UniteUsAgent.employee_id``),
  * Eligibility Assessments -- by ``Assessment.created_by_id`` (maps to
                               ``UniteUsAgent.user_id``),
  * Internal Service cases  -- by ``Case.created_by_id`` (maps to
                               ``UniteUsAgent.user_id``),
each agent is responsible for.

Identity is unified through the :class:`UniteUsAgent` roster so a single person
appears once even though screenings key off a DIFFERENT Unite Us id
(``employee_id``) than cases + assessments (``user_id``). Rows whose id isn't on
the roster fall back to the raw creator name (cases/assessments) or the raw
facilitator id (screenings). Screenings that predate the ``facilitator_id``
backfill fall back to the client's most-recent internal-service case creator so
historical data isn't lost.

The date window is a named ``period`` (today/week/month/last_month/year/all) or a
custom ``start``/``end`` range -- see ``resolve_window``.
"""

from django.db.models import Count
from rest_framework.response import Response

from ..models import Assessment, Case, CaseType, Screening, UniteUsAgent
from .base import PortalAPIView, current_agent
from .views_dashboard import resolve_window


def _is_privileged(agent):
    """Accountability (per-agent performance) is a management view."""
    return bool(
        agent and (agent.group == "Management" or getattr(agent, "is_manager", False))
    )


def _agent_display(a):
    return a.name or a.email or str(a.user_id)


class AgentAccountabilityView(PortalAPIView):
    """Per-screener productivity table (screenings / eligibility assessments /
    internal-service cases) over a date window. See module docstring."""

    def get(self, request):
        agent = current_agent(request)
        if not _is_privileged(agent):
            return Response(
                {"detail": "Agent accountability access required."}, status=403
            )

        start, end = resolve_window(request)

        # --- Unite Us roster: unify the two id spaces onto one identity --------
        # cases + assessments carry the creator's ``user_id``; screenings carry
        # the facilitator's ``employee_id``. Both live on the same UniteUsAgent
        # row, so it's the natural hub to collapse a person into one bucket.
        roster = list(UniteUsAgent.objects.all())
        by_user = {str(a.user_id): a for a in roster}
        by_emp = {str(a.employee_id): a for a in roster if a.employee_id}

        buckets = {}

        def bucket(key, display, team):
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = {
                    "key": key,
                    "agent": display,
                    "team": team,
                    "screenings": 0,
                    "assessments": 0,
                    "internal_cases": 0,
                }
            return b

        def resolve_user(uid, name):
            """Resolve a ``user_id``-keyed creator (cases/assessments)."""
            a = by_user.get(str(uid)) if uid else None
            if a:
                return f"uua:{a.pk}", _agent_display(a), a.originating_team
            label = (name or "").strip()
            if not label:
                return None
            return f"name:{label.casefold()}", label, ""

        def resolve_emp(eid):
            """Resolve an ``employee_id``-keyed facilitator (screenings)."""
            a = by_emp.get(str(eid)) if eid else None
            if a:
                return f"uua:{a.pk}", _agent_display(a), a.originating_team
            if eid:
                return f"emp:{eid}", f"Unite Us user {eid}", ""
            return None

        # --- Internal-service cases created, per agent (created_by_id) ----------
        case_qs = Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
        if start is not None:
            case_qs = case_qs.filter(
                case_created_at__date__gte=start, case_created_at__date__lte=end
            )
        for r in (
            case_qs.values("created_by_id", "created_by_name")
            .annotate(n=Count("case_id"))
        ):
            res = resolve_user(r["created_by_id"], r["created_by_name"])
            if res:
                bucket(*res)["internal_cases"] += r["n"]

        # --- Eligibility assessments submitted, per agent (created_by_id) -------
        asmt_qs = Assessment.objects.all()
        if start is not None:
            asmt_qs = asmt_qs.filter(
                screen_created_at__date__gte=start, screen_created_at__date__lte=end
            )
        for r in (
            asmt_qs.values("created_by_id", "created_by_name", "provider_name")
            .annotate(n=Count("assessment_id"))
        ):
            # created_by_name is the submitter; provider_name holds the same
            # value on legacy rows imported before created_by_name existed.
            name = r["created_by_name"] or r["provider_name"]
            res = resolve_user(r["created_by_id"], name)
            if res:
                bucket(*res)["assessments"] += r["n"]

        # --- Screenings, per facilitator (facilitator_id -> employee_id) --------
        # Legacy fallback: screenings imported before the facilitator_id backfill
        # carry no facilitator, so attribute them to the client's most-recent
        # internal-service case creator (a user_id-keyed identity).
        client_creator = {}
        for r in (
            Case.objects.filter(case_type=CaseType.INTERNAL_SERVICE)
            .exclude(client_id__isnull=True)
            .values("client_id", "created_by_id", "created_by_name")
            .order_by("case_created_at")
        ):
            client_creator[r["client_id"]] = (
                r["created_by_id"],
                r["created_by_name"],
            )  # last write = most recent

        scr_qs = Screening.objects.all()
        if start is not None:
            scr_qs = scr_qs.filter(
                screen_created_at__date__gte=start, screen_created_at__date__lte=end
            )
        for r in (
            scr_qs.values("facilitator_id", "client_id")
            .annotate(n=Count("enhanced_screen_id"))
        ):
            fid = r["facilitator_id"]
            if fid:
                res = resolve_emp(fid)
            else:
                cc = client_creator.get(r["client_id"])
                res = resolve_user(cc[0], cc[1]) if cc else None
            if res:
                bucket(*res)["screenings"] += r["n"]

        rows = list(buckets.values())
        rows.sort(
            key=lambda r: (
                r["internal_cases"],
                r["assessments"],
                r["screenings"],
                r["agent"],
            ),
            reverse=True,
        )
        return Response({
            "screeners": rows,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        })
