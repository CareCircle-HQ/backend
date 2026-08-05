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

    members = []
    for p in profiles:
        conditions = list(p.conditions or [])
        members.append({
            "client_id": str(p.client_id) if p.client_id else "",
            "status": p.status,
            "status_label": p.get_status_display(),
            "name": p.member_name or _full_name(p.client),
            "meal_type": p.menu_type or "",
            "conditions": conditions,
            "medications": list(p.medications or []),
            "weight": p.weight or "",
            "height": p.height or "",
            "weeks_gestation": p.weeks_gestation if "Pregnant" in conditions else None,
            "months_postpartum": p.months_postpartum if "Postpartum" in conditions else None,
            "nutrition_concern": p.general_verification_notes or "",
        })

    return {
        "primary": {
            "name": _full_name(primary),
            "dob": primary.date_of_birth.isoformat() if primary and primary.date_of_birth else "",
            "phone": primary_phone,
            "email": getattr(primary, "client_email_address", "") or "",
            "address": _primary_address(primary),
            "household_size": len(profiles),
        },
        "members": members,
    }


def _decode_signature(data_url):
    """Decode a 'data:image/png;base64,...' signature into raw bytes (or None)."""
    if not data_url or "," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None


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
            ("Meal Type", m["meal_type"]),
            ("Medical Conditions", ", ".join(m["conditions"]) or "No Restriction"),
            ("Medications", ", ".join(m["medications"])),
            ("Weight", m["weight"]), ("Height", m["height"]),
        ]
        if m["weeks_gestation"] is not None:
            rows.append(("Weeks Gestation", m["weeks_gestation"]))
        if m["months_postpartum"] is not None:
            rows.append(("Months Postpartum", m["months_postpartum"]))
        rows.append(("Primary Nutrition Concern", m["nutrition_concern"]))
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
