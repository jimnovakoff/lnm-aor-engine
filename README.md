# lnm-aor-engine

Parameterized parser for the **USCG District 1 Local Notice to Mariners** (weekly,
~50-page PDF, Maine → NJ). For each configured Area of Responsibility it emits a
small JSON heads-up — ATON discrepancies and hazard locations — that downstream
briefing products consume.

A weekly GitHub Action refreshes `data/<aor>.json`; consumers fetch the raw files:

```
https://raw.githubusercontent.com/jimnovakoff/lnm-aor-engine/main/data/buzzards-bay.json
https://raw.githubusercontent.com/jimnovakoff/lnm-aor-engine/main/data/narragansett-east-passage.json
https://raw.githubusercontent.com/jimnovakoff/lnm-aor-engine/main/data/index.json
```

## Consumers

| AOR id | Waters | Product |
|---|---|---|
| `buzzards-bay` | Buzzards Bay / Vineyard Sound | AuxRadio Fairhaven pre-watch briefing |
| `narragansett-east-passage` | Newport ↔ Melville sail corridor | Sail Beyond Cancer pre-sail briefing (Sally) |

## Output schema

```json
{
  "aor": "narragansett-east-passage",
  "label": "...",
  "generated_utc": "2026-06-12T11:00:00Z",
  "lnm_source": "https://www.navcen.uscg.gov/sites/default/files/pdf/lnms/LNM01442025.pdf",
  "aton":    [{ "llnr": "16215", "name": "Hog Island Channel Lighted Buoy 19",
                "status": "LT EXT", "kind": "Federal" }],
  "hazards": [{ "place": "East Passage", "categories": ["Dredging"] }]
}
```

**Deliberately omitted: narrative text and positions.** The LNM PDF's flattened
multi-column text scrambles free-text prose and coordinates beyond reliable
reconstruction; emitting them would put garbled data on navigation-safety
products. Consumers must link the reader to `lnm_source` (the full LNM) for
detail. This is a safety posture, not a parsing gap to fix.

## How it parses

Line/section-aware (ported from AuxRadio `uscg_lnm_automation.py` v3.4.8, which
replaced a text-blob matcher that produced district-wide false positives):

- **ATON**: scans `Federal/Private Discrepancies` tables (`NAME LLNR STATUS AID TYPE`),
  matching the **aid name** against AOR keywords — name-matching is required because
  AOR aids can sit under non-AOR section headers (e.g. "EEZ"). Handles literal
  `null` LLNRs and float artifacts (`16302.300000000001` → `16302.3`); excludes
  offshore wind-farm WTG aids.
- **Hazards**: splits the LNM into its alphabetical place-name sections, matches the
  **place header**, and emits place + hazard category only.
- **Excludes** guard substring traps — e.g. `little narragansett` (Stonington CT)
  would otherwise match a `narragansett bay` keyword.

## Adding an AOR

Add an entry to [aors.json](aors.json) (`id`, `label`, `keywords`, optional
`excludes`), then run:

```
python lnm_aor.py --aor <id>                      # live LNM
python lnm_aor.py --aor <id> --pdf-file x.pdf     # against a local PDF
```

Keyword tips: prefer specific multi-word place names; avoid bare words that
appear inside other names ("vineyard" matches "Vineyard Wind" turbines). The
windfarm exclusion is built in.

> Not an official USCG or USCG Auxiliary product. Always consult the published
> LNM directly for operational decisions.
