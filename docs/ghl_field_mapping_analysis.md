# GoHighLevel Field Mapping Analysis

**Date:** June 10, 2026  
**Source:** `tmp/fields_external_crm.csv` (375 fields)  
**Purpose:** Map GHL Contact and Opportunity fields to our API models

---

## Overview

| GHL Object | Count | Our API Model | Sync Strategy |
|------------|-------|---------------|---------------|
| **contact** | ~200 | `Client` + related | Upsert on save |
| **opportunity** | ~175 | `Screening`, `Case`, `Eligibility` | Create/update per record |

---

## 1. Contact Fields (Client Data)

### 1.1 Already Mapped in `custom_fields.py`

| GHL Field ID | GHL Field Name | Our API Field | Transformer |
|--------------|----------------|---------------|-------------|
| `iWw4cIFFBCFcKGlUeffm` | Enrollment Platform Client ID | `client.client_id` | `str(client.pk)` |
| `7Y8aipXfNywpogNFsEk0` | Total Household Members | `client.total_family_members` or `client.household_size` | Use available |

### 1.2 Standard GHL Contact Fields (in `contacts.py`)

| GHL Standard Field | Our API Field | Notes |
|-------------------|---------------|-------|
| `firstName` | `client.first_name` | ✓ Implemented |
| `lastName` | `client.last_name` | ✓ Implemented |
| `name` | Combined first + last | ✓ Implemented |
| `email` | `client.client_email_address` | ✓ Implemented |
| `phone` | `client.client_phone_number` | ✓ Implemented |
| `dateOfBirth` | `client.date_of_birth` | ISO format |
| `gender` | `client.gender` | Mapped to GHL values (male/female) |
| `address1` | Primary address line1+line2 | From `client.addresses` |
| `city` | Primary address city | From `client.addresses` |
| `state` | Primary address state | From `client.addresses` |
| `postalCode` | Primary address zip | From `client.addresses` |
| `country` | Hardcoded "US" | ✓ Implemented |
| `source` | `config.CONTACT_SOURCE` | "Benefully extension" |

### 1.3 Unmapped Contact Custom Fields (Priority)

| GHL Field ID | GHL Field Name | Our API Field | Transformer Needed |
|--------------|----------------|---------------|-------------------|
| `xac7ac5fVHKyutg0mrB6` | 🚫 Enrollment Platform Client ID | `client.client_id` | Already have |
| `Y6zgE4FdPysieN3jJ55C` | Agent Code - Attestation Completed | `client.agent_code` | Direct mapping |
| `xtnpC8QzUPakgeMXldMA` | Agent Code - External Services | `client.agent_code` | Direct mapping |
| `wuXdpSPdvglOX2XaMdOT` | 🚫 Assigned Agent - Attestation Requested | `client.care_coordinator` | Direct mapping |
| `v02vvzMuoOMcD3wqS5nD` | 🚫 Assigned Agent - Attestation Complete | `client.care_coordinator` | Direct mapping |
| `wW6QENM8RU9fRzQPEU2u` | Date of First Delivery | `client.delivery_start` | Date format |
| `Yvfn5jNSITA7oDZ9qc0G` | Doctors Street Address | `client.doctors_address` | **MISSING IN API** |
| `XtwIwYRKfgaTe88T92wB` | Doctors Phone # | `client.doctors_phone` | **MISSING IN API** |
| `XJyU9CjxrH5dID7vWtcC` | Doctors Fax # | `client.doctors_fax` | **MISSING IN API** |
| `ViDnbjtmh5VhDJHby2hW` | Doctors Email | `client.doctors_email` | **MISSING IN API** |
| `VDp9dccvMPl8Yood6e9O` | Doctors Name | `client.doctors_name` | **MISSING IN API** |
| `ykUXk9fRXjVxGtQOIwIO` | HM #3 - Screening? | `screening.screen_type` | Transform: "HM #3" |
| `xTiHii7zbB9YwPHYxvPJ` | HM #7 - Eligibility? | `eligibility.screen_type` | Transform: "HM #7" |
| `wGMFGBUjQy9FiMUVOX7Y` | HM #9 - Screening? | `screening.screen_type` | Transform: "HM #9" |
| `x8571pZEntk6pNnQ92Qs` | HM #9 - Meal Category | `screening.meal_category` | **MISSING IN API** |
| `YjqeQEbuyZZGr0YApljP` | HM #7 - Meal Category | `eligibility.meal_category` | **MISSING IN API** |

### 1.4 Contact Fields with 🚫 Prefix (Internal/System)

These appear to be internal tracking fields:

| GHL Field | Purpose | Need to Map? |
|-----------|---------|--------------|
| `zneDTRPytLOixqQLAqU9` | 🚫 Eligible Services | Maybe - from `client.eligible_for` |
| `Z4tA1fpY3gt4OjLP1v9v` | 🚫 Member Status | Maybe - from `client.lifecycle_stage` |
| `WFmbE1EhsFcexEi1wOoZ` | 🚫 Verification Result | From screening/eligibility |
| `VeuV354LT2elBhJAuKIv` | 🚫 Medicaid Type Verified? | From insurance data |
| `WA3I2mbsMxNqvaxy14SY` | 🚫 Originating Team | From case/network |

---

## 2. Opportunity Fields (Screenings → Opportunities)

GHL Opportunities map to our **Screenings, Cases, and Eligibility assessments**.

### 2.1 Opportunity Naming Convention

| Our Record | GHL Opportunity Name | Pipeline Stage |
|------------|---------------------|----------------|
| `Screening` | `Screening: {screen_type} - {client_name}` | Screening |
| `Case` | `Case: {service_type} - {client_name}` | Case Management |
| `Eligibility` | `Eligibility: {eligible_status} - {client_name}` | Eligibility |

### 2.2 Core Opportunity Fields

| GHL Field | Our API Field | Notes |
|-----------|---------------|-------|
| `name` | Generated from record type + client | See above |
| `status` | `open` / `won` / `lost` | Map from status |
| `pipelineId` | Configured pipeline ID | From env |
| `stageId` | Stage based on record type | Mapping needed |
| `contactId` | `client.crm_contact_id` | From contact sync |
| `monetaryValue` | `case.authorized_amount` | Parse currency string |
| `expectedCloseDate` | `case.service_authorization_request_ends_at` | Date format |

### 2.3 Opportunity Custom Fields

| GHL Field ID | GHL Field Name | Our API Field | Transformer |
|--------------|----------------|---------------|-------------|
| `ZtubJmMvebNIoZDU4ZQ1` | Medicaid Active? | `insurance.is_active` | Insurance lookup |
| `YIA99adbI63iyFfaJcPl` | HM #7 - Enrollment Platform Client ID | `client.client_id` | Direct |
| `Yc2fe2qhIBslTcQtublT` | HM #6 - Enrollment Platform Client ID | `client.client_id` | Direct |
| `xUrnyFjlqiaK4iI7hI0U` | HM #3 - Enrollment Platform Client ID | `client.client_id` | Direct |
| `WENdRf2mogcWav03WEDP` | HM #2 - Enrollment Platform Client ID | `case.screening.enhanced_screen_id` | Screening lookup |
| `vVXgJXK0quzmm69l8aox` | HM #5 - Enrollment Platform Client ID | Related screening | Lookup required |
| `VsYvy4968SkjD2R3kefD` | HM #6 - Enrollment Platform Client ID | Related screening | Lookup required |
| `Yk0zLEy87tXyNzZtwbQn` | HM #2 - Other Restrictions | `screening.decline_reason_key` | Transform options |
| `yFWKxbGlWr2tvgf6ksLL` | HM #5 - Other Restrictions | `screening.eligible_services` | JSON array |
| `W6YZwMQfaM4hGHLEjJqf` | HM #5 - Other Restrictions | Duplicate? | Check field ID |
| `zgJWaql1i7kQ8A7ibYS8` | HM #9 - Food Allergies | **MISSING** | Need to add to API |
| `wzt3nlUv7yrdZY8Qy5Bv` | HM #8 - Food Allergies | **MISSING** | Need to add to API |
| `v5a7PN4WIQtf5CyxUYaL` | HM #2 - Other Allergies | **MISSING** | Need to add to API |
| `yA55yUbzmqQrt9YodIh1` | Date Attestation Request Sent | `screening.assigned_at` | Date format |
| `Y9TLbdPcC1jldlKiKrJF` | HM #2 - Confirm no case created | `screening.case` exists? | Boolean logic |
| `xwSFdzVSVervYfSnbsYa` | HM #4 - Confirm no case created | Related case check | Boolean logic |
| `Vj38uh3d910lY9yDCN4o` | HM #6 - Confirm no case created | Related case check | Boolean logic |
| `VfWg4y3Ym5jtNy2oK2QY` | HM #9 - Confirm no case created | Related case check | Boolean logic |
| `XjLns3DZE7bekTvc0qmu` | HM #6 - Active Insurance? | `insurance.is_active` | Insurance lookup |
| `VTuVaLmNZa6fPsyntaSY` | HM #5 - Active Insurance? | `insurance.is_active` | Insurance lookup |
| `VGtEgT8uMSbVqzIppA5k` | HM #9 - Eligibility? | `eligibility.screen_type` | Transform |
| `VA0OoK0KAobBzCxZLaJ` | HM #4 - Member Enhanced? | `eligibility.parent_screen` exists? | Boolean |
| `W4y4hQHzyTyMWTu6JBZP` | HM #3 - Eligibility? | `screening.eligible_status` | Direct |
| `xStwW0W0f0Qq6jvibH9D` | HM #7 - Member Enhanced? | Related check | Boolean |
| `w1MgWEq2umOoKWKPaFkQ` | HM #2 - Member Enhanced? | Related check | Boolean |
| `X9COcyRjnHTqMbl73WXT` | HM #5 - Screening? | `screening.screen_type` | Contains "HM #5" |
| `v3PN3UCC4uPJDbwHc83c` | HM #2 - Screening? | `screening.screen_type` | Contains "HM #2" |
| `Wb6Y82h4yC9ieCsTHGmQ` | Final Verification Complete? | `screening.verified_at` exists? | Boolean |
| `WjuizZewAHUp2W3nXZL2` | Final Verification Status | `screening.screen_status` | Map values |
| `vPtYBPakJTQyhnQBW NW` | Final Verification Note | `screening.decline_note` | Direct |
| `V7YKGHEonzDbf89qSkQA` | 🚫 General Verification Note | `screening.decline_note` | Direct |
| `x8ech2HdWGslyzLFXC9C` | 🚫 Unable to reach member? | `screening.outreach_status` | Map to boolean |
| `xavLL0N59Ct2sG8QzdzO` | Final Verification Form URL | `screening.screening_form_url` | **MISSING** |
| `wU71kYX2bmN0xr8a3pPf` | Final Verification Form URL | Same as above | Duplicate? |

### 2.4 Method of Attestation Fields

| GHL Field ID | GHL Field Name | Maps To |
|--------------|----------------|---------|
| `YzhsUM1nL5vl4CKzT0Ke` | Method of Attestation Delivery - Requested | `client.communication_channels` (initial) |
| `ZLvhmJXqjMx3YUTWwAhl` | Method of Attestation Delivery - Completed | `client.communication_channels` (final) |

---

## 3. Data Transformations Required

### 3.1 Simple Direct Mappings

```python
# String/Number fields - direct assignment
client.first_name → firstName
client.last_name → lastName
client.agent_code → customFields["Agent Code"]
client.client_id → customFields["Enrollment Platform Client ID"]
```

### 3.2 Date Formatting

```python
# Django DateField → ISO 8601
client.date_of_birth.isoformat() → dateOfBirth  # "1990-05-15"
case.service_authorization_request_ends_at → expectedCloseDate
```

### 3.3 Address Aggregation

```python
# Multiple address lines → Single line
line = " ".join([addr.line1, addr.line2]).strip() → address1
```

### 3.4 Boolean Transformations

```python
# Presence check → Boolean
screening.verified_at is not None → True/False
eligibility.parent_screen exists → True/False
```

### 3.5 Option/Enum Mapping

```python
# Our choices → GHL option values
client.lifecycle_stage: {
    "lead": "Lead",
    "prospect": "Prospect", 
    "screened": "Screened",
    "eligible": "Eligible",
    "ineligible": "Not Eligible",
    "client": "Active Client"
}

screening.screen_status → opportunity.status
screening.outreach_status == "unreachable" → unable_to_reach_member = True
```

### 3.6 JSON Array → GHL Multiple Options

```python
# Our JSON list → GHL MULTIPLE_OPTIONS format
client.eligible_for: ["meals", "transportation"]
→ customFields["Eligible Services"] = ["Meals", "Transportation"]

screening.eligible_services 
→ customFields["HM #5 - Other Restrictions"]
```

### 3.7 Currency Parsing

```python
# String amount → Number
case.authorized_amount = "$8,736.00"
→ parse to 8736.00 → monetaryValue
```

---

## 4. Missing Fields in Our API (Need to Add)

### 4.1 Doctor Information (Contact)

Currently **NOT** in `Client` model:

```python
# Add to Client model:
doctors_name = models.CharField(max_length=255, blank=True)
doctors_street_address = models.CharField(max_length=255, blank=True)
doctors_phone = models.CharField(max_length=32, blank=True)
doctors_fax = models.CharField(max_length=32, blank=True)
doctors_email = models.EmailField(blank=True)
```

**Source:** Unite Us facesheet "Doctor/PCP" section

### 4.2 Meal Category (Screening/Eligibility)

```python
# Add to Screening and Eligibility models:
meal_category = models.CharField(max_length=50, blank=True)
# Values: "Kosher", "Regular", "Special Diet", etc.
```

**Source:** Screening questions/answers

### 4.3 Food Allergies (Screening)

```python
# Add to Screening model:
food_allergies = models.JSONField(default=list, blank=True)
# Values: ["Dairy", "Gluten", "Nuts", "Shellfish", "Other"]
other_allergies = models.CharField(max_length=255, blank=True)
```

**Source:** HM screening question responses

### 4.4 Screening Form URL

```python
# Add to Screening model:
screening_form_url = models.URLField(blank=True)
# Or enrollment_platform_url
```

**Source:** Generated during screening completion

---

## 5. Proposed Implementation Order

### Phase 1: Contact Fields (Immediate)
- [ ] Map remaining contact custom fields
- [ ] Add doctor fields to Client model
- [ ] Add household member count resolver
- [ ] Test contact sync end-to-end

### Phase 2: Opportunity Infrastructure
- [ ] Create `opportunities.py` module
- [ ] Define pipeline/stage mappings
- [ ] Implement opportunity creation for Screenings
- [ ] Implement opportunity creation for Cases
- [ ] Implement opportunity creation for Eligibility

### Phase 3: Data Transformations
- [ ] Add option mapping utilities
- [ ] Add currency parsing
- [ ] Add boolean presence check helpers
- [ ] Add date formatting standardization

### Phase 4: Missing Fields
- [ ] Migration: Add doctor fields
- [ ] Migration: Add meal_category
- [ ] Migration: Add food_allergies
- [ ] Update ETL to populate new fields

---

## 6. Field Mapping Quick Reference

### Contact Custom Field IDs

```python
# api/integrations/ghl/custom_fields.py additions:

FIELD_AGENT_CODE_ATTESTATION_COMPLETED = "Y6zgE4FdPysieN3jJ55C"
FIELD_AGENT_CODE_EXTERNAL_SERVICES = "xtnpC8QzUPakgeMXldMA"
FIELD_ASSIGNED_AGENT_ATTESTATION_REQUESTED = "wuXdpSPdvglOX2XaMdOT"
FIELD_ASSIGNED_AGENT_ATTESTATION_COMPLETE = "v02vvzMuoOMcD3wqS5nD"
FIELD_DATE_OF_FIRST_DELIVERY = "wW6QENM8RU9fRzQPEU2u"

# To be added after API migration:
FIELD_DOCTORS_NAME = "VDp9dccvMPl8Yood6e9O"
FIELD_DOCTORS_STREET_ADDRESS = "Yvfn5jNSITA7oDZ9qc0G"
FIELD_DOCTORS_PHONE = "XtwIwYRKfgaTe88T92wB"
FIELD_DOCTORS_FAX = "XJyU9CjxrH5dID7vWtcC"
FIELD_DOCTORS_EMAIL = "ViDnbjtmh5VhDJHby2hW"
```

### Opportunity Custom Field IDs

```python
# api/integrations/ghl/opportunities.py:

# Core
OP_FIELD_MEDICAID_ACTIVE = "ZtubJmMvebNIoZDU4ZQ1"
OP_FIELD_HM7_ENROLLMENT_CLIENT_ID = "YIA99adbI63iyFfaJcPl"
OP_FIELD_HM6_ENROLLMENT_CLIENT_ID = "Yc2fe2qhIBslTcQtublT"
OP_FIELD_HM3_ENROLLMENT_CLIENT_ID = "xUrnyFjlqiaK4iI7hI0U"
OP_FIELD_HM2_ENROLLMENT_CLIENT_ID = "WENdRf2mogcWav03WEDP"

# Status/Verification
OP_FIELD_FINAL_VERIFICATION_COMPLETE = "Wb6Y82h4yC9ieCsTHGmQ"
OP_FIELD_FINAL_VERIFICATION_STATUS = "WjuizZewAHUp2W3nXZL2"
OP_FIELD_FINAL_VERIFICATION_NOTE = "vPtYBPakJTQyhnQBW NW"
OP_FIELD_UNABLE_TO_REACH_MEMBER = "x8ech2HdWGslyzLFXC9C"

# Restrictions/Allergies
OP_FIELD_HM9_FOOD_ALLERGIES = "zgJWaql1i7kQ8A7ibYS8"
OP_FIELD_HM8_FOOD_ALLERGIES = "wzt3nlUv7yrdZY8Qy5Bv"
OP_FIELD_HM2_OTHER_ALLERGIES = "v5a7PN4WIQtf5CyxUYaL"
OP_FIELD_HM2_OTHER_RESTRICTIONS = "Yk0zLEy87tXyNzZtwbQn"
OP_FIELD_HM5_OTHER_RESTRICTIONS = "yFWKxbGlWr2tvgf6ksLL"

# Attestation
OP_FIELD_ATTESTATION_REQUESTED_DATE = "yA55yUbzmqQrt9YodIh1"
OP_FIELD_ATTESTATION_COMPLETED_METHOD = "ZLvhmJXqjMx3YUTWwAhl"
OP_FIELD_ATTESTATION_REQUESTED_METHOD = "YzhsUM1nL5vl4CKzT0Ke"
```

---

## 7. Questions / Clarifications Needed

1. **Pipeline Structure:** What are the exact pipeline IDs and stage IDs in GHL for:
   - Screenings pipeline
   - Cases pipeline  
   - Eligibility pipeline

2. **Opportunity Naming:** Confirm format: `"{Type}: {Subtype} - {Client Name}"`

3. **Duplicate Handling:** If a Screening updates, do we:
   - Update existing opportunity?
   - Create new opportunity?
   - Archive old + create new?

4. **Meal Category Values:** What are the exact option values in GHL for:
   - `opportunity.hm_9__meal_category`
   - `contact.hm_7__meal_category`

5. **Food Allergies Options:** What are the exact MULTIPLE_OPTIONS values?

6. **System Fields (🚫):** Should we map these or are they GHL-internal?

---

## Appendix: CSV Field Count by Type

```
Total fields in CSV: 375
- contact fields: ~200
- opportunity fields: ~175

Data types:
- TEXT: ~80
- SINGLE_OPTIONS: ~60
- MULTIPLE_OPTIONS: ~50
- DATE: ~20
- CHECKBOX: ~15
- NUMERICAL: ~10
- PHONE: ~5
- LARGE_TEXT: ~5
```
