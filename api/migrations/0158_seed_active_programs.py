"""Seed / refresh the ActiveProgram table (Settings > Active Programs) with the
current Program Name -> Case Category list.

Source: the operator-provided export in ``tmp/import/programs-service_type.csv``
(the programs we have in the Settings table today). Embedded here as a literal so
the seed is reproducible in every environment -- the tmp CSV is NOT present in
production and a migration must never depend on it.

Idempotent: upserts by ``program_name`` (same semantics as
``manage.py import_program_pipelines``), so re-running never duplicates a row.
Duplicate program names in the source (e.g. "Housing & Shelter" listed several
times) are collapsed to a single row. ``is_for_household`` is set explicitly
because a data migration uses the historical model, which does NOT run the
model's ``save()`` override that normally derives it.
"""

import csv
import io

from django.db import migrations

# Program Name,Case Category -- verbatim from the operator's export, with the
# ``\u2019`` mojibake ("Selfhelp\u00e2\u20ac\u2122s") normalized to a plain
# apostrophe so it matches the other "Selfhelp's ..." rows and never creates a
# curly/straight-quote duplicate.
PROGRAMS_CSV = """Program Name,Case Category
Boro Park Jewish Community Council,External Services
Care Management Services,Care Management
Clinically Appropriate Meals - (Household) High-Risk Children Under the Age of 18 - Brooklyn,Internal Services
Clinically Appropriate Meals - (Household) High-Risk Children Under the Age of 18 - Manhattan,Internal Services
Clinically Appropriate Meals - (Household) High-Risk Children Under the Age of 18 - Queens,Internal Services
Clinically Appropriate Meals - (Household) Pregnant / Postpartum - Brooklyn,Internal Services
Clinically Appropriate Meals - (Household) Pregnant / Postpartum - Manhattan,Internal Services
Clinically Appropriate Meals - (Household) Pregnant / Postpartum - Queens,Internal Services
Clinically Appropriate Meals - Other Eligible Populations - Brooklyn,Internal Services
Clinically Appropriate Meals - Other Eligible Populations - Manhattan,Internal Services
Clinically Appropriate Meals - Other Eligible Populations - Queens,Internal Services
Clinically Appropriate Meals - Pregnant / Postpartum - Brooklyn,Internal Services
Clinically Appropriate Meals - Pregnant / Postpartum - Manhattan,Internal Services
Clinically Appropriate Meals - Pregnant / Postpartum - Queens,Internal Services
Cooking Supplies- Kitchenware - Brooklyn,External Services
Cooking Supplies- Kitchenware - Manhattan,External Services
Cooking Supplies- Kitchenware - Queens,External Services
Cooking Supplies- Microwave - Brooklyn,External Services
Cooking Supplies- Microwave - Manhattan,External Services
Cooking Supplies- Microwave - Queens,External Services
Cooking Supplies- Refrigerator - Brooklyn,External Services
Cooking Supplies- Refrigerator - Manhattan,External Services
Cooking Supplies- Refrigerator - Queens,External Services
Dwelling Assessment & Statement of Work (SOW) Development - Modifications and Remediation Service - Brooklyn,External Services
Dwelling Assessment & Statement of Work (SOW) Development - Modifications and Remediation Service - Manhattan,External Services
Dwelling Assessment & Statement of Work (SOW) Development - Modifications and Remediation Service - Queens,External Services
Enhanced Care Management - Application Fees Level 2 Only - Brooklyn,Other
Enhanced Care Management - Application Fees Level 2 Only - Manhattan,Other
Enhanced Care Management - Application Fees Level 2 Only - Queens,Other
Enhanced Care Management - Care Management Level 2 Only - Brooklyn,Care Management
Enhanced Care Management - Care Management Level 2 Only - Manhattan,Care Management
Enhanced Care Management - Care Management Level 2 Only - Queens,Care Management
Enhanced Care Management - Eligibility Assessment Level 2 Only - Brooklyn,ELIGIBILITY
Enhanced Care Management - Eligibility Assessment Level 2 Only - Manhattan,ELIGIBILITY
Enhanced Care Management - Eligibility Assessment Level 2 Only - Queens,ELIGIBILITY
Enhanced Care Management - Follow Up Level 2 Only - Brooklyn,Other
Enhanced Care Management - Follow Up Level 2 Only - Manhattan,Other
Enhanced Care Management - Follow Up Level 2 Only - Queens,Other
Family Connect (WYNYC),External Services
Food pantry,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) High-Risk Children Under the Age of 18 - Brooklyn,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) High-Risk Children Under the Age of 18 - Manhattan,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) High-Risk Children Under the Age of 18 - Queens,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) Pregnant / Postpartum - Brooklyn,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) Pregnant / Postpartum - Manhattan,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) Pregnant / Postpartum - Queens,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - Other Eligible Populations - Brooklyn,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - Other Eligible Populations - Manhattan,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - Other Eligible Populations - Queens,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - Pregnant / Postpartum - Brooklyn,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - Pregnant / Postpartum - Manhattan,External Services
Fresh Produce and Nonperishable Groceries: Pantry Stocking - Pregnant / Postpartum - Queens,External Services
Health Home Serving Adults Care Management Program,External Services
Help 365,External Services
Home Accessibility and Safety Modification - Bathroom Facilities - Brooklyn,External Services
Home Accessibility and Safety Modification - Bathroom Facilities - Manhattan,External Services
Home Accessibility and Safety Modification - Bathroom Facilities - Queens,External Services
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Brooklyn,External Services
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Manhattan,External Services
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Queens,External Services
Home Accessibility and Safety Modification - Grab Bars - Brooklyn,External Services
Home Accessibility and Safety Modification - Grab Bars - Manhattan,External Services
Home Accessibility and Safety Modification - Grab Bars - Queens,External Services
Home Accessibility and Safety Modification - Hand Rails - Brooklyn,External Services
Home Accessibility and Safety Modification - Hand Rails - Manhattan,External Services
Home Accessibility and Safety Modification - Hand Rails - Queens,External Services
Home Accessibility and Safety Modification - Kitchen Cabinet or Sinks - Brooklyn,External Services
Home Accessibility and Safety Modification - Kitchen Cabinet or Sinks - Manhattan,External Services
Home Accessibility and Safety Modification - Kitchen Cabinet or Sinks - Queens,External Services
Home Accessibility and Safety Modification - Non-skid Surfaces - Brooklyn,External Services
Home Accessibility and Safety Modification - Non-skid Surfaces - Manhattan,External Services
Home Accessibility and Safety Modification - Non-skid Surfaces - Queens,External Services
Home Remediation - Air Conditioner - Brooklyn,External Services
Home Remediation - Air Conditioner - Manhattan,External Services
Home Remediation - Air Conditioner - Queens,External Services
Home Remediation - Air Filtration Device - Brooklyn,External Services
Home Remediation - Air Filtration Device - Manhattan,External Services
Home Remediation - Air Filtration Device - Queens,External Services
Home Remediation - De-humidifier - Brooklyn,External Services
Home Remediation - De-humidifier - Manhattan,External Services
Home Remediation - De-humidifier - Queens,External Services
Home Remediation - Heater - Brooklyn,External Services
Home Remediation - Heater - Manhattan,External Services
Home Remediation - Heater - Queens,External Services
Home Remediation - Humidifier - Brooklyn,External Services
Home Remediation - Humidifier - Manhattan,External Services
Home Remediation - Humidifier - Queens,External Services
Hope and Healing Family Center Brooklyn Diaper Bank,External Services
Housing Transition and Navigation Services - Brooklyn,External Services
Housing Transition and Navigation Services - Queens,External Services
Interborough Developmental & Consultation Center -Health Home Programs,External Services
JCCA - East Flatbush Community Partnership,External Services
Medically Tailored Meals (MTM) - (Household) High-Risk Children Under the Age of 18 - Brooklyn,Internal Services
Medically Tailored Meals (MTM) - (Household) High-Risk Children Under the Age of 18 - Manhattan,Internal Services
Medically Tailored Meals (MTM) - (Household) High-Risk Children Under the Age of 18 - Queens,Internal Services
Medically Tailored Meals (MTM) - (Household) Pregnant / Postpartum - Brooklyn,Internal Services
Medically Tailored Meals (MTM) - (Household) Pregnant / Postpartum - Manhattan,Internal Services
Medically Tailored Meals (MTM) - (Household) Pregnant / Postpartum - Queens,Internal Services
Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn,Internal Services
Medically Tailored Meals (MTM) - Other Eligible Populations - Manhattan,Internal Services
Medically Tailored Meals (MTM) - Other Eligible Populations - Queens,Internal Services
Medically Tailored Meals (MTM) - Pregnant / Postpartum - Brooklyn,Internal Services
Medically Tailored Meals (MTM) - Pregnant / Postpartum - Manhattan,Internal Services
Medically Tailored Meals (MTM) - Pregnant / Postpartum - Queens,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) High-Risk Children Under the Age of 18 - Brooklyn,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) High-Risk Children Under the Age of 18 - Manhattan,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) High-Risk Children Under the Age of 18 - Queens,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) Pregnant / Postpartum - Brooklyn,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) Pregnant / Postpartum - Manhattan,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) Pregnant / Postpartum - Queens,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Other Eligible Populations - Brooklyn,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Other Eligible Populations - Manhattan,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Other Eligible Populations - Queens,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Pregnant / Postpartum - Brooklyn,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Pregnant / Postpartum - Manhattan,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Pregnant / Postpartum - Queens,Internal Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) High-Risk Children Under the Age of 18 - Brooklyn,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) High-Risk Children Under the Age of 18 - Manhattan,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) High-Risk Children Under the Age of 18 - Queens,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) Pregnant / Postpartum - Brooklyn,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) Pregnant / Postpartum - Manhattan,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) Pregnant / Postpartum - Queens,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Other Eligible Populations - Brooklyn,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Other Eligible Populations - Manhattan,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Other Eligible Populations - Queens,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Pregnant / Postpartum - Brooklyn,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Pregnant / Postpartum - Manhattan,External Services
Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Pregnant / Postpartum - Queens,External Services
Navigation Services - Eligibility Assessment and Navigation to existing resources (Level 1) - FFS Members - Brooklyn,ELIGIBILITY
Navigation Services - Eligibility Assessment and Navigation to existing resources (Level 1) - FFS Members - Manhattan,ELIGIBILITY
Navigation Services - Eligibility Assessment and Navigation to existing resources (Level 1) - FFS Members - Queens,ELIGIBILITY
Navigation Services - Eligibility Assessment and Navigation to existing resources (Level 1) - MCO Members - Brooklyn,ELIGIBILITY
Navigation Services - Eligibility Assessment and Navigation to existing resources (Level 1) - MCO Members - Manhattan,ELIGIBILITY
Navigation Services - Eligibility Assessment and Navigation to existing resources (Level 1) - MCO Members - Queens,ELIGIBILITY
Nutritional Counseling and Education - Counseling and Education: Individual (Initial assessment) - Brooklyn,External Services
Nutritional Counseling and Education - Counseling and Education: Individual (Initial assessment) - Manhattan,External Services
Nutritional Counseling and Education - Counseling and Education: Individual (Initial assessment) - Queens,External Services
Nutritional Counseling and Education: Individual (Reassessment) - Brooklyn,External Services
Nutritional Counseling and Education: Individual (Reassessment) - Manhattan,External Services
Nutritional Counseling and Education: Individual (Reassessment) - Queens,External Services
Nutritional Counseling and Education; Group (2 or more individuals) - Brooklyn,External Services
Nutritional Counseling and Education; Group (2 or more individuals) - Manhattan,External Services
Nutritional Counseling and Education; Group (2 or more individuals) - Queens,External Services
NY1115 Annual HRSN Screening,SCREENING
NY1115 Annual HRSN Screening- CCB Navigator,SCREENING
NY1115 Member Navigation,Care Management
NY1115 Rescreening - FFS Members - Brooklyn,SCREENING
NY1115 Rescreening - FFS Members - Manhattan,SCREENING
NY1115 Rescreening - FFS Members - Queens,SCREENING
NY1115 Rescreening - MCO Members - Brooklyn,SCREENING
NY1115 Rescreening - MCO Members - Manhattan,SCREENING
NY1115 Rescreening - MCO Members - Queens,SCREENING
NYC Groceries to Go,External Services
PHS SNAP (General),External Services
Private Transportation - Care Management - Brooklyn,External Services
Private Transportation - HRSN Services Only,External Services
Public Transportation - Care Management - Brooklyn,External Services
Public Transportation - HRSN Services Only - Brooklyn,External Services
Public Transportation - HRSN Services Only - Queens,External Services
Reauthorization: Clinically Appropriate Meals - (Household) High-Risk Children Under the Age of 18 - Brooklyn,Internal Services
Reauthorization: Clinically Appropriate Meals - (Household) High-Risk Children Under the Age of 18 - Manhattan,Internal Services
Reauthorization: Clinically Appropriate Meals - (Household) High-Risk Children Under the Age of 18 - Queens,Internal Services
Reauthorization: Clinically Appropriate Meals - (Household) Pregnant / Postpartum - Brooklyn,Internal Services
Reauthorization: Clinically Appropriate Meals - (Household) Pregnant / Postpartum - Manhattan,Internal Services
Reauthorization: Clinically Appropriate Meals - (Household) Pregnant / Postpartum - Queens,Internal Services
Reauthorization: Clinically Appropriate Meals - Other Eligible Populations - Brooklyn,Internal Services
Reauthorization: Clinically Appropriate Meals - Other Eligible Populations - Manhattan,Internal Services
Reauthorization: Clinically Appropriate Meals - Other Eligible Populations - Queens,Internal Services
Reauthorization: Clinically Appropriate Meals - Pregnant / Postpartum - Brooklyn,Internal Services
Reauthorization: Clinically Appropriate Meals - Pregnant / Postpartum - Manhattan,Internal Services
Reauthorization: Clinically Appropriate Meals - Pregnant / Postpartum - Queens,Internal Services
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) High-Risk Children Under the Age of 18 - Brooklyn,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) High-Risk Children Under the Age of 18 - Manhattan,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) High-Risk Children Under the Age of 18 - Queens,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) Pregnant / Postpartum - Brooklyn,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) Pregnant / Postpartum - Manhattan,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - (Household) Pregnant / Postpartum - Queens,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - Other Eligible Populations - Brooklyn,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - Other Eligible Populations - Manhattan,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - Other Eligible Populations - Queens,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - Pregnant / Postpartum - Brooklyn,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - Pregnant / Postpartum - Manhattan,Reauthorization
Reauthorization: Fresh Produce and Nonperishable Groceries: Pantry Stocking - Pregnant / Postpartum - Queens,Reauthorization
Reauthorization: Medically Tailored Meals (MTM) - (Household) High-Risk Children Under the Age of 18 - Brooklyn,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - (Household) High-Risk Children Under the Age of 18 - Manhattan,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - (Household) High-Risk Children Under the Age of 18 - Queens,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - (Household) Pregnant / Postpartum - Brooklyn,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - (Household) Pregnant / Postpartum - Manhattan,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - (Household) Pregnant / Postpartum - Queens,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - Other Eligible Populations - Brooklyn,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - Other Eligible Populations - Manhattan,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - Other Eligible Populations - Queens,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - Pregnant / Postpartum - Brooklyn,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - Pregnant / Postpartum - Manhattan,Internal Services
Reauthorization: Medically Tailored Meals (MTM) - Pregnant / Postpartum - Queens,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) High-Risk Children Under the Age of 18 - Brooklyn,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) High-Risk Children Under the Age of 18 - Manhattan,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) High-Risk Children Under the Age of 18 - Queens,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) Pregnant / Postpartum - Brooklyn,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) Pregnant / Postpartum - Manhattan,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - (Household) Pregnant / Postpartum - Queens,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Other Eligible Populations - Brooklyn,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Other Eligible Populations - Manhattan,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Other Eligible Populations - Queens,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Pregnant / Postpartum - Brooklyn,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Pregnant / Postpartum - Manhattan,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Boxes - Pregnant / Postpartum - Queens,Internal Services
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) High-Risk Children Under the Age of 18 - Brooklyn,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) High-Risk Children Under the Age of 18 - Manhattan,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) High-Risk Children Under the Age of 18 - Queens,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) Pregnant / Postpartum - Brooklyn,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) Pregnant / Postpartum - Manhattan,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - (Household) Pregnant / Postpartum - Queens,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Other Eligible Populations - Brooklyn,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Other Eligible Populations - Manhattan,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Other Eligible Populations - Queens,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Pregnant / Postpartum - Brooklyn,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Pregnant / Postpartum - Manhattan,Reauthorization
Reauthorization: Medically Tailored or Nutritionally Appropriate Food Prescriptions: Voucher - Pregnant / Postpartum - Queens,Reauthorization
Reauthorization: Nutritional Counseling and Education - Counseling and Education: Individual (Initial assessment) - Brooklyn,Reauthorization
Reauthorization: Nutritional Counseling and Education - Counseling and Education: Individual (Initial assessment) - Manhattan,Reauthorization
Reauthorization: Nutritional Counseling and Education - Counseling and Education: Individual (Initial assessment) - Queens,Reauthorization
Reauthorization: Nutritional Counseling and Education: Individual (Reassessment) - Brooklyn,Reauthorization
Reauthorization: Nutritional Counseling and Education: Individual (Reassessment) - Manhattan,Reauthorization
Reauthorization: Nutritional Counseling and Education: Individual (Reassessment) - Queens,Reauthorization
Reauthorization: Nutritional Counseling and Education; Group (2 or more individuals) - Brooklyn,Reauthorization
Reauthorization: Nutritional Counseling and Education; Group (2 or more individuals) - Manhattan,Reauthorization
Reauthorization: Nutritional Counseling and Education; Group (2 or more individuals) - Queens,Reauthorization
Selfhelp's Case Management Programs,External Services
Selfhelp's Naturally Occurring Retirement Communities,External Services
SNAP (Supplemental Nutrition Assistance Program),External Services
Tenancy Sustaining Services - Housing Recertification and Renewals - Brooklyn,External Services
"Tenancy Sustaining Services - Rights, Education, Eviction Prevention, Dispute Resolution, Risk Intervention, and Legal Services - Brooklyn",External Services
The Menu,External Services
Utility Assistance - Brooklyn,External Services
NYC Benefits,External Services
CORE,External Services
Health Home Program,External Services
Selfhelp's Older Adult Centers,External Services
New Horizon Counseling Center - Health Home Care Management,External Services
Housing & Shelter,External Services
Private Transportation - HRSN Services Only - Brooklyn,External Services
Selfhelp's Holocaust Survivor Programs,External Services
"Job readiness - The Alex House Project, Inc",External Services
Selfhelp's Home Care Training Program,External Services
Housing Transition and Navigation Services - Manhattan,External Services
Selfhelp's Alzheimer's Resource Program (SHARP),External Services
WIC Made Easy,External Services
"Original Pentecostal Apostolic Church, Inc",External Services
Community Food Pantry,External Services
"IMMIGRATION, SNAP.",External Services
Food and Nutrition Program,External Services
Public Transportation - HRSN Services Only,External Services
Home-Delivered Meals,External Services
Diabetes Nutrition Education,External Services
Housing Program,External Services
Reauthorization: Public Transportation - Care Management - Manhattan,External Services
Holistic Parenting Classes,External Services
Island Harvest Food Bank,External Services
Continuous Access Center,External Services
Nutrition- Food Pantry,External Services
Addiction Services,External Services
Job Readiness,External Services
Public Health Solutions Services,External Services
NYP/PHS Queens Program,External Services
"CAMBA's Health Link Program, A Health Home Program",External Services
Maranatha S.D.A. Church Food Pantry,External Services
Project Dignity,External Services
High School Equivalency/GED Program (Women's Only Program),External Services
Resource Referral Program,External Services
IHOPE,External Services
Public Transportation - HRSN Services Only - Manhattan,External Services
Brooklyn Care Coordination,External Services
"Tenancy Sustaining Services - Rights, Education, Eviction Prevention, Dispute Resolution, Risk Intervention, and Legal Services - Queens",External Services
Senior Services,External Services
Utility Assistance - Manhattan,External Services
Pre-Tenancy Services - Negotiating Lease Agreements - Brooklyn,External Services
"Home Care Services- Brooklyn, Manhattan, Queens, Bronx",External Services
Queens Vocational Services,External Services
Pre-Tenancy Services - Navigating and Completing Housing Application - Brooklyn,External Services
Pre-Tenancy Services - Tenant Screening Assistance and Tenant Interviews - Queens,External Services
"Tenancy Sustaining Services - Financial Education, Literacy and Resources - Brooklyn",External Services
New Horizon Counseling Center- Outpatient Mental Health,External Services
NFP The Rockaways,External Services
Interborough Developmental & Consultation Center CCBHC - Mental Health,External Services
VAP - General Civil Legal Services (NY),External Services
Mercy Haven Inc Food Pantry,External Services
Reentry Services,External Services
Utility Assistance - Queens,External Services
Utility Setup - Back Payments - Brooklyn,External Services
Tenancy Sustaining Services - Housing Recertification and Renewals - Queens,External Services
"Disability Benefits (SSI/SSDI) Evaluation, Application Support, Appeal Representation",External Services
"Interborough Developmental & Consultation Center - Multihealth Services, Dba. SLA Associates",External Services
Pre-Tenancy Services - Negotiating Lease Agreements - Queens,External Services
Healthcare Access,External Services
Workforce Development & Re-Entry at BronxConnect (ONLY For Formerly Incarcerated or Detained),External Services
Pre-Tenancy Services - Negotiating Lease Agreements - Manhattan,External Services
Senior Community Connection Project,External Services
Pre-Tenancy Services - Navigating and Completing Housing Application - Queens,External Services
"Tenancy Sustaining Services - Rights, Education, Eviction Prevention, Dispute Resolution, Risk Intervention, and Legal Services - Manhattan",External Services
NYC Health Department Family Wellness Suite - East Harlem,External Services
Reauthorization: Public Transportation - HRSN Services Only - Manhattan,External Services
Northwell Health - Military Liaison Services,External Services
Public Transportation - Care Management - Queens,External Services
Reauthorization: Housing Transition and Navigation Services - Queens,External Services
The Bowery Mission's Residential Programs (Make Progress),External Services
Day Program,External Services
Pre-Tenancy Services - Tenant Screening Assistance and Tenant Interviews - Manhattan,External Services
NYC Benefits (Non SCN),External Services
Lifeline Free Government Phones,External Services
Public Transportation - Care Management,External Services
Help 365 Bronx,External Services
Early Head Start - Family Child Care Network,External Services
Bridge Street Development Corporation,External Services
Mercy Haven,External Services
The River Fund New York,External Services
Homeless Veteran's Reintegration Program,External Services
In-home personal care,External Services
National Diabetes Prevention Program (NDPP),External Services
Health and Wellness,External Services
"""


def _parse_rows():
    """Yield unique (program_name, case_category) from the embedded CSV.

    Deduped by the case-folded, trimmed program name so exact duplicates in the
    source (e.g. "Housing & Shelter" listed three times) collapse to one row.
    First occurrence wins.
    """
    seen = set()
    reader = csv.reader(io.StringIO(PROGRAMS_CSV))
    header = True
    for row in reader:
        if header:
            header = False
            continue
        if not row:
            continue
        name = (row[0] or "").strip()
        category = (row[1] or "").strip() if len(row) > 1 else ""
        if not name or not category:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        yield name, category


def seed_active_programs(apps, schema_editor):
    ActiveProgram = apps.get_model("api", "ActiveProgram")
    for name, category in _parse_rows():
        # Both classifications are set EXPLICITLY (the historical model does not
        # run ActiveProgram.save(), and update_or_create must also apply them to
        # any pre-existing row):
        #   * case_type = "food" -- these are all food-domain programs (every
        #     internal-service program today is food; the meal/box programs move
        #     to Product as food products).
        #   * is_for_household -- household vs individual, derived from the name
        #     exactly as the model's save() does ("(Household)" pathway rows).
        ActiveProgram.objects.update_or_create(
            program_name=name,
            defaults={
                "case_category": category,
                "case_type": "food",
                "is_for_household": "household" in name.casefold(),
            },
        )


def noop_reverse(apps, schema_editor):
    # Seed data; leave the rows in place on reverse.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0157_client_ineligible_reasons_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_active_programs, noop_reverse),
    ]
