"""Nutritionist review: shared data context + signed PDF rendering.

``nutrition_review_context`` gathers the household's review data (primary
contact + per-member health) so the drawer and the PDF stay in lockstep.
``render_nutrition_pdf`` produces the signed PDF bytes (reportlab).
"""

import base64
from io import BytesIO

from django.utils import timezone


def _full_name(client):
    if client is None:
        return ""
    return f"{client.first_name} {client.last_name}".strip()


def _primary_address(client):
    if client is None:
        return ""
    addrs = list(client.addresses.all()) if hasattr(client, "addresses") else []
    addr = next((a for a in addrs if a.type == "current"), next(iter(addrs), None))
    if addr is None:
        return ""
    parts = [
        " ".join(p for p in [addr.street, addr.unit] if p).strip(),
        addr.city,
        " ".join(p for p in [addr.state, addr.zip] if p).strip(),
    ]
    return ", ".join(p for p in parts if p)


def _member_dict(p):
    """The per-member health/review dict (shared by the drawer + the PDF)."""
    conditions = list(p.conditions or [])
    return {
        "client_id": str(p.client_id) if p.client_id else "",
        "status": p.status,
        "status_label": p.get_status_display(),
        "name": p.member_name or _full_name(p.client),
        "meal_plan": p.meal_plan or "",
        "meal_plan_other": p.meal_plan_other or "",
        "meal_type": p.menu_type or "",
        "conditions": conditions,
        "medications": list(p.medications or []),
        "weight": p.weight or "",
        "height": p.height or "",
        "weeks_gestation": p.weeks_gestation if "Pregnant" in conditions else None,
        "months_postpartum": p.months_postpartum if "Postpartum" in conditions else None,
        "on_medical_diet": p.on_medical_diet,
        "medical_diet_details": p.medical_diet_details or "",
        "assessment_notes": p.assessment_notes or "",
        "nutrition_concern": p.general_verification_notes or "",
    }


def _contact(client, phone=""):
    """Contact block for a member: name, DOB, phone, email, address."""
    return {
        "name": _full_name(client),
        "dob": client.date_of_birth.isoformat() if client and client.date_of_birth else "",
        "phone": phone or "",
        "email": getattr(client, "client_email_address", "") or "",
        "address": _primary_address(client),
    }


def nutrition_review_context(enrollment):
    """The household's review data: primary contact + per-member health."""
    primary = enrollment.client
    profiles = list(
        enrollment.member_profiles.select_related("client").all()
    )
    # The primary's captured phone lives on their own dietary profile.
    primary_phone = ""
    for p in profiles:
        if p.client_id and primary and str(p.client_id) == str(primary.pk):
            primary_phone = p.mobile_number or ""
            break

    return {
        "primary": {
            **_contact(primary, primary_phone),
            "household_size": len(profiles),
        },
        "members": [_member_dict(p) for p in profiles],
    }


def _decode_signature(data_url):
    """Decode a 'data:image/png;base64,...' signature into raw bytes (or None)."""
    if not data_url or "," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None


def _health_rows(m):
    """The ordered (label, value) rows for one member's Health section."""
    # A signed PDF exists only post sign-off, so the status reads Nutritionist
    # Approved (or Nutritionist Paused for a member the nutritionist paused).
    nutri_status = (
        "Nutritionist Paused" if m["status"] == "nutritionist_paused"
        else "Nutritionist Approved"
    )
    rows = [("Nutritionist Status", nutri_status), ("Meal Plan", m["meal_plan"])]
    if m["meal_plan"] == "Other" and m["meal_plan_other"]:
        rows.append(("Other Meal Plan", m["meal_plan_other"]))
    rows += [
        ("Meal Type", m["meal_type"]),
        ("Medical Conditions", ", ".join(m["conditions"]) or "No Restriction"),
        ("Medications", ", ".join(m["medications"])),
        ("Weight", f"{m['weight']} Lbs" if m["weight"] else ""),
        ("Height", m["height"]),
    ]
    if m["weeks_gestation"] is not None:
        rows.append(("Weeks Gestation", m["weeks_gestation"]))
    if m["months_postpartum"] is not None:
        rows.append(("Months Postpartum", m["months_postpartum"]))
    rows.append(("On Medical Diet", "Yes" if m["on_medical_diet"] else "No"))
    if m["on_medical_diet"] and m["medical_diet_details"]:
        rows.append(("Medical Diet Details", m["medical_diet_details"]))
    rows.append(("Primary Nutrition Concern", m["nutrition_concern"]))
    rows.append(("Assessment Notes", m["assessment_notes"]))
    return rows


def render_member_nutrition_pdf(profile, *, agent, signature_image="", signed_at=None):
    """Render ONE member's signed Nutrition Review PDF (Member Contact + that
    member's Health + the Nutritionist sign-off). One document per member."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
    )

    signed_at = signed_at or timezone.now()
    contact = _contact(profile.client, profile.mobile_number)
    m = _member_dict(profile)

    styles = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=styles["Heading2"], textColor=colors.HexColor("#0f766e"), spaceBefore=14, spaceAfter=6)
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=9, leading=12)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter, title="Nutrition Review",
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )

    def kv_table(rows):
        t = Table([[Paragraph(f"<b>{k}</b>", small), Paragraph(str(v or "—"), small)] for k, v in rows],
                  colWidths=[1.9 * inch, 4.6 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    flow = [
        Paragraph("Nutrition Review", styles["Title"]),
        Paragraph(f"{m['name']} · Signed {signed_at.strftime('%B %d, %Y %I:%M %p')}", small),
        Paragraph("Member Contact", h),
        kv_table([
            ("Name", contact["name"]), ("Date of Birth", contact["dob"]),
            ("Phone", contact["phone"]), ("Email", contact["email"]),
            ("Address", contact["address"]),
        ]),
        Paragraph("Health", h),
        kv_table(_health_rows(m)),
        Paragraph("Nutritionist", h),
        kv_table([
            ("Name", getattr(agent, "name", "") or ""),
            ("Credentials", getattr(agent, "title", "") or ""),
            ("Email", getattr(agent, "email", "") or ""),
            ("Signature Date", signed_at.strftime("%B %d, %Y")),
        ]),
    ]
    sig_bytes = _decode_signature(signature_image)
    if sig_bytes:
        flow.append(Spacer(1, 6))
        flow.append(Paragraph("Signature:", small))
        try:
            flow.append(Image(BytesIO(sig_bytes), width=2.4 * inch, height=0.9 * inch))
        except Exception:
            pass

    doc.build(flow)
    return buf.getvalue()


def render_nutrition_pdf(enrollment, *, agent, signature_image="", signed_at=None):
    """Render the signed Nutrition Review PDF and return its bytes."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
    )

    ctx = nutrition_review_context(enrollment)
    signed_at = signed_at or timezone.now()

    styles = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=styles["Heading2"], textColor=colors.HexColor("#0f766e"), spaceBefore=14, spaceAfter=6)
    normal = styles["BodyText"]
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=9, leading=12)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter, title="Nutrition Review",
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    flow = []

    def kv_table(rows):
        t = Table([[Paragraph(f"<b>{k}</b>", small), Paragraph(str(v or "—"), small)] for k, v in rows],
                  colWidths=[1.9 * inch, 4.6 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    flow.append(Paragraph("Nutrition Review", styles["Title"]))
    flow.append(Paragraph(f"Signed {signed_at.strftime('%B %d, %Y %I:%M %p')}", small))

    p = ctx["primary"]
    flow.append(Paragraph("Primary Member Contact", h))
    flow.append(kv_table([
        ("Name", p["name"]), ("Date of Birth", p["dob"]), ("Phone", p["phone"]),
        ("Email", p["email"]), ("Address", p["address"]),
        ("Household Size", p["household_size"]),
    ]))

    flow.append(Paragraph("Health", h))
    for m in ctx["members"]:
        flow.append(Paragraph(f"<b>{m['name']}</b>", normal))
        rows = [
            ("Status", m["status_label"]),
            ("Meal Plan", m["meal_plan"]),
        ]
        if m["meal_plan"] == "Other" and m["meal_plan_other"]:
            rows.append(("Other Meal Plan", m["meal_plan_other"]))
        rows += [
            ("Meal Type", m["meal_type"]),
            ("Medical Conditions", ", ".join(m["conditions"]) or "No Restriction"),
            ("Medications", ", ".join(m["medications"])),
            ("Weight", m["weight"]), ("Height", m["height"]),
        ]
        if m["weeks_gestation"] is not None:
            rows.append(("Weeks Gestation", m["weeks_gestation"]))
        if m["months_postpartum"] is not None:
            rows.append(("Months Postpartum", m["months_postpartum"]))
        rows.append(("On Medical Diet", "Yes" if m["on_medical_diet"] else "No"))
        if m["on_medical_diet"] and m["medical_diet_details"]:
            rows.append(("Medical Diet Details", m["medical_diet_details"]))
        rows.append(("Primary Nutrition Concern", m["nutrition_concern"]))
        rows.append(("Assessment Notes", m["assessment_notes"]))
        flow.append(kv_table(rows))
        flow.append(Spacer(1, 8))

    flow.append(Paragraph("Nutritionist", h))
    flow.append(kv_table([
        ("Name", getattr(agent, "name", "") or ""),
        ("Credentials", getattr(agent, "title", "") or ""),
        ("Email", getattr(agent, "email", "") or ""),
        ("Signature Date", signed_at.strftime("%B %d, %Y")),
    ]))
    sig_bytes = _decode_signature(signature_image)
    if sig_bytes:
        flow.append(Spacer(1, 6))
        flow.append(Paragraph("Signature:", small))
        try:
            flow.append(Image(BytesIO(sig_bytes), width=2.4 * inch, height=0.9 * inch))
        except Exception:
            pass

    doc.build(flow)
    return buf.getvalue()
