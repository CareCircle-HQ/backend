# Housing programmes still needed for internal-service cases

**Generated from the local production clone.** Compares the 26 priced items in
`Simplified_Billable_Items_Pricing` against the `ActiveProgram` table, matching on
the item parsed out of each programme name.

**10 of the 26 priced products cannot become an internal-service case**, because
the programme their category needs is either missing or marked External.

Fixing it takes **10 programme rows: 6 created, 4 reclassified.**

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

## Every product, and whether it can become an internal-service case

`Y` = an internal programme exists for that borough. **10 of 26 products have a
gap.**

```
PRODUCT                        CATEGORY                 PROGRAMME (middle)        BK MN QN
Shower chair                   Bathroom Facilities      Bathroom Facilities        Y  Y  Y
Bath bench                     Bathroom Facilities      Bathroom Facilities        Y  Y  Y
Raised toilet seat             Bathroom Facilities      Bathroom Facilities        Y  Y  Y
Non-skid bath mat              Non-skid Surfaces        Non-skid Surfaces          Y  Y  Y
Non-slip adhesive strips       Non-skid Surfaces        Non-skid Surfaces          Y  Y  Y
Non-slip tape                  Non-skid Surfaces        Non-skid Surfaces          Y  Y  Y
Grab bar at toilet             Grab Bars                Grab Bars                  Y  Y  .   <- gap
Grab bar at tub                Grab Bars                Grab Bars                  Y  Y  .   <- gap
Grab bar at shower             Grab Bars                Grab Bars                  Y  Y  .   <- gap
Floor-to-ceiling safety pole   Grab Bars                Grab Bars                  Y  Y  .   <- gap
Lever door handle              Doors & Cabinet Handles  Doors and Cabinet Handles  .  .  .   <- gap
D-ring cabinet pull            Doors & Cabinet Handles  Doors and Cabinet Handles  .  .  .   <- gap
Loop cabinet handle            Doors & Cabinet Handles  Doors and Cabinet Handles  .  .  .   <- gap
Modular/portable ramp          Accessibility Ramps      ** NONE **                --  -- --  <- gap
Threshold ramp                 Accessibility Ramps      ** NONE **                --  -- --  <- gap
Interior staircase handrail    Handrails                Hand Rails                 Y  Y  Y
Hallway handrail               Handrails                Hand Rails                 Y  Y  Y
Threshold reducer              Pathways                 ** NONE **                --  -- --  <- gap
HEPA air purifier              Air Filtration Devices   Air Filtration Device      Y  Y  Y
Portable air filtration unit   Air Filtration Devices   Air Filtration Device      Y  Y  Y
Dehumidifier (portable)        De-humidifier            De-humidifier              Y  Y  Y
Portable humidifier            Humidifier               Humidifier                 Y  Y  Y
Cool mist humidifier           Humidifier               Humidifier                 Y  Y  Y
Window air conditioner         Air Conditioner          Air Conditioner            Y  Y  Y
Portable air conditioner       Air Conditioner          Air Conditioner            Y  Y  Y
Portable space heater          Heater                   Heater                     Y  Y  Y
```

### The two levels behave differently, which is why both are shown

* **Home Accessibility** programmes name a CATEGORY in the middle
  (`... - Bathroom Facilities - Brooklyn`), so one programme covers several
  products — three bathroom products share one programme.
* **Home Remediation** programmes name the PRODUCT-level item
  (`... - Air Conditioner - Brooklyn`), which is also our category, so the two
  levels coincide there — one programme still covers both the window and portable
  air conditioners.

Either way the question is the same: **does an internal programme exist for the
category this product sits in, in this borough?** A product with no internal
programme is priced, recommendable on the assessment form, and unorderable.

### Four categories account for all ten

```
Grab Bars                 4 products   Queens only
Doors & Cabinet Handles   3 products   all three boroughs
Accessibility Ramps       2 products   no programme in any borough
Pathways                  1 product    no programme in any borough
```

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
