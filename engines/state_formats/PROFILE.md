# State format profile

A **state format profile** is a JSON config that teaches Flagly how to detect and parse one state's budget PDF layout. Adding a second state should mean a new profile file + registry entry — not a new hardcoded parser fork.

## Fields

| Field | Purpose |
|-------|---------|
| `id` | Stable id used in detection (`state_niger`) |
| `jurisdiction` | Human/jurisdiction label (`niger_state`) |
| `label` | Display name |
| `detection` | Structural signals (phrases, regexes, min hits). Never use filename. |
| `section_markers.start` | Substrings that open the project-detail section |
| `section_markers.stop` | Substrings that end parsing (optional) |
| `columns` | Ordered logical columns + header label fragments found in the PDF |
| `amount_column` | Which logical column is the approved-year amount (e.g. `approved_2026`) |
| `patterns` | Regexes for admin code, location code, economic code |
| `row_rules` | Skip totals, reject fragment lines, wrap-fold behaviour |

## Parser contract

The generic state parser (`engines/state_formats/parser.py`):

1. Runs `pdftotext -layout`
2. Finds the section from `section_markers`
3. Records which amount/header columns it detected
4. Emits unified Flagly rows (`description`, `amount`, `location`, `mda_code`, `mda_name`, …) via `_finalize_df`

## Adding another state

1. Copy `profiles/niger_v1.json` → `profiles/<state>_v1.json`
2. Retune `detection` and `section_markers` against a sample of that state's PDF
3. Register the profile id in `engines/state_formats/__init__.py`
4. Add a text fixture under `tests/fixtures/` and a detection/parse test
