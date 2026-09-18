# Product <-> programme map (housing)

Every product in the billable-items catalogue, the category it belongs to, and
the exact `ActiveProgram` row a case would be opened against.

Generated from the programme table and the product catalogue, so the programme
names below are exact rather than retyped.

A housing programme name is three parts separated by ` - `:

```
Home Remediation - Air Conditioner - Manhattan
|____ family ___|  |____ item ____|  |_ borough _|
```

The middle part links a product to a programme, and the two families use it
differently:

* **Home Accessibility** puts the CATEGORY there, so several products share one
  programme -- the three bathroom products all map to
  `... - Bathroom Facilities - <borough>`.
* **Home Remediation** puts the product-level item there, which is also our
  category -- so one programme still covers both air conditioners.

---

## Bathroom Facilities

*Home Accessibility and Safety Modification*

Products:

* Shower chair
* Bath bench
* Raised toilet seat

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Accessibility and Safety Modification - Bathroom Facilities - <borough>` | internal | internal | internal |

## Non-skid Surfaces

*Home Accessibility and Safety Modification*

Products:

* Non-skid bath mat
* Non-slip adhesive strips
* Non-slip tape

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Accessibility and Safety Modification - Non-skid Surfaces - <borough>` | internal | internal | internal |

## Grab Bars

*Home Accessibility and Safety Modification*

Products:

* Grab bar at toilet
* Grab bar at tub
* Grab bar at shower
* Floor-to-ceiling safety pole

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Accessibility and Safety Modification - Grab Bars - <borough>` | internal | internal | **EXTERNAL** |

## Doors & Cabinet Handles

*Home Accessibility and Safety Modification*

Products:

* Lever door handle
* D-ring cabinet pull
* Loop cabinet handle

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Accessibility and Safety Modification - Doors and Cabinet Handles - <borough>` | **EXTERNAL** | **EXTERNAL** | **EXTERNAL** |

## Accessibility Ramps

*Home Accessibility and Safety Modification*

**No programme exists for this category in any borough.**

* Modular/portable ramp
* Threshold ramp

## Handrails

*Home Accessibility and Safety Modification*

Products:

* Interior staircase handrail
* Hallway handrail

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Accessibility and Safety Modification - Hand Rails - <borough>` | internal | internal | internal |

## Pathways

*Home Accessibility and Safety Modification*

**No programme exists for this category in any borough.**

* Threshold reducer

## Air Filtration Devices

*Home Remediation*

Products:

* HEPA air purifier
* Portable air filtration unit

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Remediation - Air Filtration Device - <borough>` | internal | internal | internal |

## De-humidifier

*Home Remediation*

Products:

* Dehumidifier (portable)

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Remediation - De-humidifier - <borough>` | internal | internal | internal |

## Humidifier

*Home Remediation*

Products:

* Portable humidifier
* Cool mist humidifier

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Remediation - Humidifier - <borough>` | internal | internal | internal |

## Air Conditioner

*Home Remediation*

Products:

* Window air conditioner
* Portable air conditioner

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Remediation - Air Conditioner - <borough>` | internal | internal | internal |

## Heater

*Home Remediation*

Products:

* Portable space heater

| Programme | Brooklyn | Manhattan | Queens |
|---|---|---|---|
| `Home Remediation - Heater - <borough>` | internal | internal | internal |

---

# Missing: products with no internal-service programme

**10 of 26 products** cannot become an internal-service case today.
They are recommendable on the assessment form with no case that can be opened
for them.

### Grab Bars

Products affected:

* Grab bar at toilet
* Grab bar at tub
* Grab bar at shower
* Floor-to-ceiling safety pole

**Reclassify from External Services to Internal Services:**

```
Home Accessibility and Safety Modification - Grab Bars - Queens
```

### Doors & Cabinet Handles

Products affected:

* Lever door handle
* D-ring cabinet pull
* Loop cabinet handle

**Reclassify from External Services to Internal Services:**

```
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Brooklyn
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Manhattan
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Queens
```

### Accessibility Ramps

Products affected:

* Modular/portable ramp
* Threshold ramp

No programme exists in any borough. **Create three:**

```
Home Accessibility and Safety Modification - Accessibility Ramps - Brooklyn
Home Accessibility and Safety Modification - Accessibility Ramps - Manhattan
Home Accessibility and Safety Modification - Accessibility Ramps - Queens
```

### Pathways

Products affected:

* Threshold reducer

No programme exists in any borough. **Create three:**

```
Home Accessibility and Safety Modification - Pathways - Brooklyn
Home Accessibility and Safety Modification - Pathways - Manhattan
Home Accessibility and Safety Modification - Pathways - Queens
```

## What each row needs

Every gap is in the **Home Accessibility** family -- the Home Remediation side
is already complete -- so they all take the same values:

```
case_category = Internal Services
case_type     = housing
service_type  = environmental_modifications_accessibility
```

Matching migration `0268_seed_home_accessibility_programs.py`. Call
`clear_program_domain_cache()` afterwards, or the classification cache keeps
refusing them until the next restart.

## Verified

Every name under *Reclassify* exists verbatim and is currently
`External Services`. Every name under *Create* does not exist -- so neither
list can produce a duplicate.

Checked against the local clone. Re-check against PRODUCTION before acting:
agents edit the programme table there, so the external rows may differ.
