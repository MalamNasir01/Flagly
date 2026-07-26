# Budget Red Flag Identification Tool Documentation

## Overview

Flagly is a Nigerian Budget Red Flag Scanner for accountability journalists and civic auditors. It ingests federal or state budget files and returns a risk scored report of suspicious line items.

## Seven extracted parameters

Every line item should carry:

1. `mda_code`   administrative unit code
2. `mda_name`   ministry / MDA name
3. `project_code`   project or ERGP style code
4. `project_name`   project description
5. `project_status`   ONGOING or NEW when present
6. `amount`   approved allocation in naira
7. `expenditure_code`   economic / expenditure code when present

Where a field cannot be extracted, return null and attach a soft data quality note.

## Flag engines

### Inflated amount

- Hard ceiling: any single line item at or above ₦1,000,000,000 is HIGH.
- Category relative IQR: within each project category, flag amounts above Q3 + 3×IQR as HIGH and above Q3 + 1.5×IQR as MEDIUM. Attach category median and bounds as evidence.

### Vague location

Phrase match (case insensitive) includes: selected locations, multiple lots, various states, nationwide, geopolitical zone, senatorial zone, selected states, selected LGAs, various locations, across the country.

If any phrase appears and no specific state or LGA is extracted, flag vague location. Elevate to HIGH at or above ₦5,000,000.

### Duplicate matching

Use RapidFuzz `token_set_ratio`. Similarity 95 to 100 is HIGH. Similarity 85 to 94 is MEDIUM. Pair both items and expose the matched counterpart.

### MDA mandate mismatch

Compare project category against `/data/mda_mandates.json`. When the project category does not intersect the MDA scope, fire HIGH mandate mismatch. Seed includes FRMA style examples (stadium, school, nursing school under a road agency).

### Ghost projects (multi year)

When two or more budget years are uploaded, fuzzy match project names at 90+. Recurrence across three or more consecutive years with ONGOING status is HIGH. Two year recurrence is MEDIUM.

## Composite risk score

Score 0 to 100 combining:

- Number of flags triggered
- Severity weight (HIGH = 3, MEDIUM = 1)
- Amount weighting (scaled log of naira amount)

Bands: above 60 HIGH, above 25 MEDIUM, below 25 LOW.

## Reference sources for investigators

- BPP Price Intelligence
- ICPC CEPTI
- tracka.ng
- Open Treasury

Findings are starting points for investigation, not proof of wrongdoing.
