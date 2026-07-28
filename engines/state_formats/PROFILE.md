# State format profile

A **state format profile** is a JSON config that teaches Flagly how to detect and
parse one state's budget PDF layout. Adding a state means a new profile file +
draft mandates + baseline — **not** a new hardcoded parser fork.

The generic parser lives in `engines/state_formats/parser.py` and is driven by
profiles under `engines/state_formats/profiles/`. The registry
(`engines/state_formats/registry.py`) auto-discovers every `*.json` profile.

---

## Onboarding checklist (repeatable)

### 1. Confirm the PDF is text-based
```bash
pdftotext -layout path/to/budget.pdf - | head
# Must show real words. If nearly empty → scanned PDF; refuse until OCR exists.
```

### 2. Drop a profile JSON
Copy `profiles/niger_v1.json` → `profiles/<state>_v1.json` and retune:

| Field | What to set |
|-------|-------------|
| `id` | `state_<slug>` (e.g. `state_kaduna`) |
| `jurisdiction` | `<slug>_state` |
| `detection.required_phrases_any` | `"X State Government"` titles |
| `detection.location_code_re` | State location-code prefix (Niger `12…`, Kaduna `31…`) |
| `section_markers.start` | Capital-by-project section titles that are **clean** |
| `section_markers.stop` | Where to stop (exclude garbled sheets) |
| `amount_column` / `amount_token_index` | Which YoY token is the approved amount |
| `patterns.location_code` | Capture groups for location code + name |
| `row_rules` | Skip totals / fragment junk words |

No code change is required in `list_profiles` — drop the file and it is registered.

### 3. Auto-generate draft mandates
```bash
python3 -m engines.state_formats.tools.generate_mandates \
  --profile state_kaduna \
  --pdf path/to/budget.pdf
```
Writes `data/mda_mandates_states_<slug>.json` with `"reviewed": false`.

**Critical rule:** while `reviewed` is false, mandate-mismatch may fire **MEDIUM only**
(never HIGH / publishable-tier).

### 4. Run parse-quality + baseline
```bash
python3 -m engines.state_formats.tools.snapshot_baseline \
  --profile state_kaduna \
  --pdf path/to/budget.pdf
```
Writes `samples/state_<slug>_baseline.json` including parse-quality suspect counts
and top-10-by-amount.

### 5. Trust gate (automatic)
Before results can be treated as above-MEDIUM / publishable:

1. Parse-quality merge guards are clean (or within baseline tolerance), **and**
2. A baseline snapshot exists for the profile.

If either fails, the API marks the scan `provisional` / `confidence: low` with
visible warnings. Unreviewed mandates additionally surface `mandates_reviewed: false`.

### 6. Tests
- Detection fixture (profile phrases fire; other states do not).
- Parse fixture / PDF regression (item count, clean top-10, quality bound).
- Federal + Niger baselines must stay green.

### 7. Human review
Edit the draft mandates scopes/exclusions, set `"reviewed": true`, re-snapshot
if needed, then treat mandate HIGH as publishable.

---

## Profile schema

| Field | Purpose |
|-------|---------|
| `id` | Stable id used in detection (`state_niger`) |
| `jurisdiction` | Mandate / stats key (`niger_state`) |
| `label` | Display name |
| `detection` | Structural signals (phrases, regexes, min hits). Never filename. |
| `section_markers.start` | Substrings that open capital project tables (may be many) |
| `section_markers.stop` | Substrings that end parsing |
| `columns` | Logical columns + header fragments |
| `amount_column` | Logical approved-year column id |
| `amount_token_index` | Optional 0-based index among amount tokens after location (Kaduna=1). Omit for Niger last-of-4 layout. |
| `patterns` | Regexes for admin / location / economic codes |
| `row_rules` | Skip totals, reject fragment lines |

## Parser contract

1. Runs `pdftotext -layout`
2. Finds every `section_markers.start` match (resets carry state between tables)
3. Stops at `section_markers.stop`
4. Emits unified Flagly rows via `_finalize_df`
5. Attaches `parse_meta.parse_quality` (dual-verb / mid-phrase suspects)

## Refusal behaviour (format_detect)

| Class | User message |
|-------|----------------|
| Scanned PDF | Needs text-based PDF or Excel; OCR coming |
| Known unsupported state | Names the state if detectable + lists supported formats |
| Unknown | Lists what **is** supported |

Never emit a report full of n/a for an unsupported upload.
