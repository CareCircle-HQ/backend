"""Build up the master/lookup catalog (ProgramMainCategory -> Program ->
Service) from saved Screening, Assessment, and Case records.

These helpers are deduped (so each unique value is stored once) and are called
best-effort from the serializers: a catalog hiccup must never break the
underlying client/case/screening save, so callers wrap them in try/except.
"""

import logging
import re
import uuid

from django.utils.text import slugify

from api.models import Program, ProgramMainCategory, Service

logger = logging.getLogger(__name__)

# Matches a trailing parenthetical main category, e.g.
# "Clinically Appropriate Meals (Food)" -> ("Clinically Appropriate Meals", "Food").
_CATEGORY_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<category>[^()]+)\)\s*$")


def _clean(value):
    """Normalize a catalog value that may be a plain string or a dict with a
    ``name``/``code`` key into a trimmed string."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("code") or ""
    return (value or "").strip() if isinstance(value, str) else ""


def _split_name_and_category(name):
    """Split a program label into (program_name, main_category_or_None).

    The screening program name usually embeds its main category in a trailing
    parenthetical, e.g. "Clinically Appropriate Meals (Food)" means the program
    belongs to the "Food" main category. If we can't extract one, the category
    is ``None`` and the program is left without a relationship.
    """
    match = _CATEGORY_RE.match(name)
    if not match:
        return name, None
    base = match.group("name").strip()
    category = match.group("category").strip()
    if not base or not category:
        return name, None
    return base, category


def upsert_main_categories(names):
    """Store unique ProgramMainCategory rows from Screening results."""
    for raw in names or []:
        name = _clean(raw)
        if name:
            ProgramMainCategory.objects.get_or_create(name=name)


def upsert_program(name):
    """Get-or-create a master Program by name (auto-UUID for new rows).

    If the name carries a trailing "(Main Category)" the parenthetical is parsed
    out, stored as a ProgramMainCategory, and linked to the program. When no
    category can be extracted the program is stored without a relationship.
    """
    name = _clean(name)
    if not name:
        return None
    program_name, category_name = _split_name_and_category(name)
    # Tolerate pre-existing duplicate Programs with the same name (the name
    # column isn't unique): pick the first rather than letting get_or_create
    # raise MultipleObjectsReturned.
    program = Program.objects.filter(name=program_name).order_by("pk").first()
    if program is None:
        program = Program.objects.create(
            name=program_name, program_id=uuid.uuid4()
        )
    if category_name:
        category, _ = ProgramMainCategory.objects.get_or_create(name=category_name)
        if program.main_category_id != category.pk:
            program.main_category = category
            program.save(update_fields=["main_category"])
    return program


def upsert_programs(names):
    """Store unique Programs from an Assessment's eligible_services."""
    for raw in names or []:
        upsert_program(raw)


def _unique_service_code(name):
    """Generate a unique slug code for a new Service (code is unique/required)."""
    base = (slugify(name) or uuid.uuid4().hex)[:80]
    code = base
    suffix = 1
    while Service.objects.filter(code=code).exists():
        tail = f"-{suffix}"
        code = base[: 80 - len(tail)] + tail
        suffix += 1
    return code


def upsert_service_from_case(service_type, program_name):
    """Store a unique Service (by name) from a Case's service_type and link it
    to the master Program identified by the case's program_name."""
    name = _clean(service_type)
    if not name:
        return None
    program = upsert_program(program_name)
    service = Service.objects.filter(name=name).first()
    if service is None:
        service = Service.objects.create(code=_unique_service_code(name), name=name)
    if program and service.program_id != program.pk:
        service.program = program
        service.save(update_fields=["program"])
    return service
