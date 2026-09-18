# Housing programmes still needed for internal-service cases

**Generated from the local production clone.** Compares the 26 priced items in
`Simplified_Billable_Items_Pricing` against the `ActiveProgram` table, matching on
the item parsed out of each programme name.

**10 programmes are needed: 6 created, 4 reclassified.** Until they exist, an
assessor can recommend those interventions and no internal-service case can be
opened for them — they are priced but unorderable.

---

## How the match was made

A housing programme name is three parts separated by ` - ` (space-hyphen-space,
never a bare hyphen, or `De-humidifier` shears in half):

```
Home Remediation - Air Conditioner - Manhattan
|____ family ___|  |____ item ____|  |_ borough _|
```

The middle part is the item, and it is what the price sheet calls a **billing
category**. Three of those labels do not match our programme names exactly, so the
comparison uses the mapping already recorded in
`api/services/assessment_forms.py` rather than the label text:

```
price sheet                 programme table
Handrails                -> Hand Rails                 (a space)
Air Filtration Devices   -> Air Filtration Device      (a plural)
Doors & Cabinet Handles  -> Doors and Cabinet Handles  (& vs and)
```

A naive label match would have reported these three as missing and quietly created
duplicates.

---

## Current state

```
PRICE CATEGORY             PROGRAMME ITEM               BK    MN    QN
Accessibility Ramps        -- none --                   ---   ---   ---
Pathways                   -- none --                   ---   ---   ---
Doors & Cabinet Handles    Doors and Cabinet Handles    EXT   EXT   EXT
Grab Bars                  Grab Bars                    INT   INT   EXT
Bathroom Facilities        Bathroom Facilities          INT   INT   INT
Non-skid Surfaces          Non-skid Surfaces            INT   INT   INT
Handrails                  Hand Rails                   INT   INT   INT
Air Conditioner            Air Conditioner              INT   INT   INT
Air Filtration Devices     Air Filtration Device        INT   INT   INT
De-humidifier              De-humidifier                INT   INT   INT
Heater                     Heater                       INT   INT   INT
Humidifier                 Humidifier                   INT   INT   INT
```

Boroughs are Brooklyn, Manhattan and Queens — the only three in the table today.

---

## 1. Create — 6 programmes that do not exist at all

Family: **Home Accessibility and Safety Modification**
Category: **Internal Services** · Case type: **housing**
Service type: **`environmental_modifications_accessibility`**

```
Home Accessibility and Safety Modification - Accessibility Ramps - Brooklyn
Home Accessibility and Safety Modification - Accessibility Ramps - Manhattan
Home Accessibility and Safety Modification - Accessibility Ramps - Queens
Home Accessibility and Safety Modification - Pathways - Brooklyn
Home Accessibility and Safety Modification - Pathways - Manhattan
Home Accessibility and Safety Modification - Pathways - Queens
```

Both are legitimate **2.1 Home Accessibility and Safety Modifications** services
under the 1115 waiver, whose list explicitly includes *"Accessibility ramps"* and
*"Widening of doorways and pathways"*. They are not stray options on the form.

They are also not hypothetical: the combined assessment you supplied
(case `A000196`) **already recommends a Modular/portable ramp**, and there is no
case that can be opened for it.

**Priced items that would become orderable:**

| Programme item | Priced options | Vendor price |
|---|---|---|
| Accessibility Ramps | Modular/portable ramp | $876.75 |
| | Threshold ramp | $666.75 |
| Pathways | Threshold reducer | $456.75 |

---

## 2. Reclassify — 4 programmes that exist but are EXTERNAL

An external programme is rejected by `CaseSerializer` as
`CaseType.EXTERNAL_SERVICE`, so a case opened against one never reaches the CRM.

```
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Brooklyn
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Manhattan
Home Accessibility and Safety Modification - Doors and Cabinet Handles - Queens
Home Accessibility and Safety Modification - Grab Bars - Queens
```

**`Grab Bars - Queens` is the one to look at first.** Brooklyn and Manhattan are
internal and Queens is not, which is almost certainly an oversight rather than a
decision — grab bars are the single most-recommended item on the form, and a Queens
member's grab bar currently cannot become a case while the same item in Brooklyn
can.

**Doors and Cabinet Handles** has three priced options (Lever door handle $430.50,
D-ring cabinet pull $262.50, Loop cabinet handle $262.50) and an intervention group
on the assessment form, so the form can recommend something no case can be opened
for.

Each needs `case_category` set to **Internal Services**, `case_type` to
**housing**, and `service_type` to
**`environmental_modifications_accessibility`** — matching the migration `0268`
pattern.

---

## 3. Noted, but NOT proposed

**`Kitchen Cabinet or Sinks`** exists for all three boroughs and is external. It is
a 2.1 waiver service, but it has **no price row and no intervention on the
assessment form**, so nothing can recommend it and nothing could be billed for it.
Making it internal alone would achieve nothing — it needs a price and a form option
first, which is a decision rather than a gap.

**Electric door openers** is in the waiver's 2.1 list and has no programme, no
price and no form option. Mentioned only so the absence is on record.

---

## Why this matters now

The pricing table, the assessment form's intervention catalogue and the programme
table are meant to be the same list seen three ways. Two of the three already agree
exactly — all 26 priced items match a form option, in both directions. The
programme table is the one that lags, and every row it lacks is an intervention an
assessor can recommend, a vendor can be paid for, and no case can be opened
against.

## Suggested implementation

A single data migration in the shape of `0268_seed_home_accessibility_programs.py`:

1. `update_or_create` the 6 new programmes as Internal Services / housing /
   `environmental_modifications_accessibility`
2. `update` the 4 external rows to the same values
3. call `clear_program_domain_cache()` afterwards, or the classification cache will
   keep rejecting them until the next restart

Then re-run this comparison: every row should read `INT / INT / INT`.

**Check production separately before deploying.** This analysis is from the local
clone, and the programme table is edited in production by agents — the external
rows may differ there.
