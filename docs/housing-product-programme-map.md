# Product <-> programme map (housing)

Generated from the local production clone. Every priced product, the category
it belongs to, and the exact `ActiveProgram` row a case would be opened
against.

A housing programme name is three parts separated by ` - `:

```
Home Remediation - Air Conditioner - Manhattan
|____ family ___|  |____ item ____|  |_ borough _|
```

The middle part is what links a product to a programme. For **Home
Remediation** it is the product-level item; for **Home Accessibility** it is the
CATEGORY, so several products share one programme.

## Bathroom Facilities

*Home Accessibility and Safety Modification* · 3 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Shower chair | $456.75 | `Home Accessibility and Safety Modification - Bathroom Facilities - <borough>` | internal | internal | internal |
| Bath bench | $509.25 |  |  |  |  |
| Raised toilet seat | $509.25 |  |  |  |  |

## Non-skid Surfaces

*Home Accessibility and Safety Modification* · 3 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Non-skid bath mat | $325.50 | `Home Accessibility and Safety Modification - Non-skid Surfaces - <borough>` | internal | internal | internal |
| Non-slip adhesive strips | $404.25 |  |  |  |  |
| Non-slip tape | $404.25 |  |  |  |  |

## Grab Bars

*Home Accessibility and Safety Modification* · 4 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Grab bar at toilet | $498.75 | `Home Accessibility and Safety Modification - Grab Bars - <borough>` | internal | internal | **EXTERNAL** |
| Grab bar at tub | $498.75 |  |  |  |  |
| Grab bar at shower | $498.75 |  |  |  |  |
| Floor-to-ceiling safety pole | $693.00 |  |  |  |  |

## Doors & Cabinet Handles

*Home Accessibility and Safety Modification* · 3 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Lever door handle | $430.50 | `Home Accessibility and Safety Modification - Doors and Cabinet Handles - <borough>` | **EXTERNAL** | **EXTERNAL** | **EXTERNAL** |
| D-ring cabinet pull | $262.50 |  |  |  |  |
| Loop cabinet handle | $262.50 |  |  |  |  |

## Accessibility Ramps

*Home Accessibility and Safety Modification* · 2 product(s)

**No programme exists for this category in any borough.**

| Product | Vendor price | Programme |
|---|---:|---|
| Modular/portable ramp | $876.75 | — |
| Threshold ramp | $666.75 | — |

## Handrails

*Home Accessibility and Safety Modification* · 2 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Interior staircase handrail | $666.75 | `Home Accessibility and Safety Modification - Hand Rails - <borough>` | internal | internal | internal |
| Hallway handrail | $666.75 |  |  |  |  |

## Pathways

*Home Accessibility and Safety Modification* · 1 product(s)

**No programme exists for this category in any borough.**

| Product | Vendor price | Programme |
|---|---:|---|
| Threshold reducer | $456.75 | — |

## Air Filtration Devices

*Home Remediation* · 2 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| HEPA air purifier | $693.00 | `Home Remediation - Air Filtration Device - <borough>` | internal | internal | internal |
| Portable air filtration unit | $693.00 |  |  |  |  |

## De-humidifier

*Home Remediation* · 1 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Dehumidifier (portable) | $693.00 | `Home Remediation - De-humidifier - <borough>` | internal | internal | internal |

## Humidifier

*Home Remediation* · 2 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Portable humidifier | $561.75 | `Home Remediation - Humidifier - <borough>` | internal | internal | internal |
| Cool mist humidifier | $666.75 |  |  |  |  |

## Air Conditioner

*Home Remediation* · 2 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Window air conditioner | $1323.00 | `Home Remediation - Air Conditioner - <borough>` | internal | internal | internal |
| Portable air conditioner | $903.00 |  |  |  |  |

## Heater

*Home Remediation* · 1 product(s)

| Product | Vendor price | Programme name | Brooklyn | Manhattan | Queens |
|---|---:|---|---|---|---|
| Portable space heater | $509.25 | `Home Remediation - Heater - <borough>` | internal | internal | internal |

---

# Missing: products with no internal-service programme

**10 of 26 priced products** cannot become an
internal-service case today.

### Grab Bars

Products affected: Grab bar at toilet, Grab bar at tub, Grab bar at shower, Floor-to-ceiling safety pole

**Reclassify from External to Internal Services:**

```
Home Accessibility and Safety Modification - Grab Bars - Queens
```

### Doors & Cabinet Handles

Products affected: Lever door handle, D-ring cabinet pull, Loop cabinet handle

**Reclassify from External to Internal Services:**

```
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Brooklyn
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Manhattan
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Queens
```

### Accessibility Ramps

Products affected: Modular/portable ramp, Threshold ramp

No programme exists in any borough. **Create three:**

```
Home Accessibility and Safety Modification - Accessibility Ramps - Brooklyn
Home Accessibility and Safety Modification - Accessibility Ramps - Manhattan
Home Accessibility and Safety Modification - Accessibility Ramps - Queens
```

### Pathways

Products affected: Threshold reducer

No programme exists in any borough. **Create three:**

```
Home Accessibility and Safety Modification - Pathways - Brooklyn
Home Accessibility and Safety Modification - Pathways - Manhattan
Home Accessibility and Safety Modification - Pathways - Queens
```

## What each row needs

Every gap above is in the **Home Accessibility** family, so they all take the same
values -- the Home Remediation side is already complete:

```
case_category = Internal Services
case_type     = housing
service_type  = environmental_modifications_accessibility
```

Matching migration `0268_seed_home_accessibility_programs.py`. Call
`clear_program_domain_cache()` afterwards, or the classification cache keeps
refusing them until the next restart.

## Verified against the clone

Every name listed under "Reclassify" exists verbatim and is currently
`External Services`. Every name under "Create" does NOT exist -- so neither list
would produce a duplicate.

Re-check against PRODUCTION before acting: agents edit the programme table there,
so the external rows may differ.
