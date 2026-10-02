# kicad-component-sync

Fetches parametric component data from distributor APIs (DigiKey) and writes it
into KiCad symbol libraries and CSV BOMs.

Fill in a Part number; `Voltage`, `Operating Temperature` and `Package` get
filled in for you. Ships a CLI and a Python package built on two pluggable
abstractions, so adding a distributor or a file format means one new module and
one registry line — no change to the CLI.

| Abstraction | Implementations |
| --- | --- |
| `BaseProvider` — where data comes from | `DigiKeyProvider` |
| `BaseProcessor` — what file is enriched | `CSVProcessor`, `KiCadSymProcessor` |

## Install

```bash
git clone git@github.com:while-one/kicad-component-sync.git
cd kicad-component-sync
python3 -m pip install -e ".[dev]"

export DIGIKEY_CLIENT_ID=...
export DIGIKEY_CLIENT_SECRET=...
```

Credentials can also be passed per-invocation with `--client-id` /
`--client-secret`.

## CLI

```bash
# Validate only: print the proposed diff, write nothing
python -m component_sync.cli lib.kicad_sym --provider digikey --dry-run

# Apply
python -m component_sync.cli lib.kicad_sym -p digikey

# CSV BOM
python -m component_sync.cli bom.csv -p digikey
```

Or use the wrapper, which reports the target, credential status and mode before
running:

```bash
./sync-kicad lib.kicad_sym --dry-run
```

Exit status is `0` on success, `1` on an expected failure, `130` on interrupt.
A run that could not resolve some parts exits non-zero in write mode so it can
gate a CI job.

### Reviewing a large library

A run over a real library reports a few hundred changes spanning very different
levels of risk, so the report leads with counts and then groups by how much
attention each change deserves:

| Group | Meaning |
| --- | --- |
| `NEEDS REVIEW` | overwrites a value that already exists — someone chose that text |
| `VALUE CONVENTION` | the deliberate `Value` standardisation, called out on its own |
| `SAFE ADDS` | fills an empty field; nothing is lost if the value is wrong |
| `UNRESOLVED` | parts that did not resolve, which need an action of their own |

Within a group, changes are listed **per part** so a symbol's fields stay
adjacent and its name is printed once:

```
NEEDS REVIEW  (5)
  Description 1   Datasheet 1   Digikey 1   Voltage Rating 1   Temperature Max 1

  ~ GRM21BC80J226ME51K
        Description '' -> '22 µF ±20% 6.3V Ceramic Capacitor X6S 0805 (2012 Metric)'
        Voltage Rating '10V' -> '6.3V'
        Temperature Max '+85 C' -> '+105 C'
```

A part needing changes in two groups is listed in each, since the groups answer
different questions. Long values are abbreviated by eliding the **middle**: two
DigiKey URLs for one part share a long prefix and differ only in the trailing
product identifier, so eliding the tail would render both sides as the same
string and hide the change. `--width N` sets the limit.

Field selection keeps a review focused. Filters are applied **before** any edit
is planned, so a filtered field is never written even transiently:

```bash
# Just the SI convention: 264 changes become 9
python -m component_sync.cli -n --only Value lib.kicad_sym

# Keep descriptions, skip the noisy rewrite of your own prose
python -m component_sync.cli -n --skip Description lib.kicad_sym

# 'Yageo' -> 'YAGEO' is a real edit but never a useful review decision
python -m component_sync.cli -n --ignore-case lib.kicad_sym
```

`--component` narrows a run to individual components, matched case-insensitively
against both the symbol name and the manufacturer part number, so it does not
matter which of the two your library uses:

```bash
# One component, by symbol name or by part number
python -m component_sync.cli -n --component 1028 lib.kicad_sym
python -m component_sync.cli -n --component RC0402FR-0736K5L lib.kicad_sym

# Several at once
python -m component_sync.cli -n --component 'GRM155,RX0402' lib.kicad_sym
```

The filter is applied **before any provider lookup**, so an excluded component
costs no request: `--component 1028` spends 2 requests rather than the ~60 a
full run needs. That matters at DigiKey's 1000/day quota, and a filtered-out
component is never listed as unresolved. A narrowed run states its scope, so a
quiet report is not mistaken for a clean one:

```
DRY RUN  lib.kicad_sym
  filter: 1 component(s) in scope
```

`--only` takes precedence over `--skip`. A run that filtered something out says
so in its footer rather than silently reporting less:

```
  filters     only: manufacturer  ignore-case
  held back   Datasheet, Description, Digikey, Package, Value  (excluded by the filters above)
```

Colour is used only when writing to a terminal, so redirecting to a file yields
plain text. `NO_COLOR` is honoured and `--no-color` forces it off. `--width N`
sets the maximum width of a rendered value, since some datasheet URLs run past
250 characters.

## The KiCad workflow

**KiCad 10 exposes no Python API for the Schematic or Symbol editor.** There is a
`pcbnew` module for the PCB editor, but no `eeschema` module and no public API
for reading or writing symbols in an open editor. Verified on kicad-10.0.6:

- `import pcbnew` works, but exposes only PCB-side schematic *parity* types
  (`SCH_SYMBOL_T` and friends) — not live editor objects.
- `import eeschema` fails: no such module.

So there is no in-editor "press a button and the fields fill in" plugin. The
supported workflow operates on the saved file:

```bash
# 1. Symbol Editor: create the symbol, fill in Part, save (Ctrl+S)
# 2. Enrich the saved library
python -m component_sync.cli ~/path/to/mylib.kicad_sym
# 3. Symbol Editor: Tools -> Refresh Symbol Libraries
# 4. Schematic:     Tools -> Update Symbols from Library
```

`component_sync/plugin.py` exists as a best-effort bridge that reaches for the
editor through the wx application object. Treat it as experimental: it depends
on KiCad internals that could not be verified without an interactive GUI
session.

## Matching: what the key actually is

There is no fuzzy matching. The join is string equality on one field.

**Lookup key** — the manufacturer part number:

| Input | Source of the MPN |
| --- | --- |
| `.csv` | the column whose header matches `mpn`, `part`, `part number`, or `manufacturer part number` |
| `.kicad_sym` | the `Part` property of each `(symbol …)`, falling back to `MPN` |

**Exact-match filter** — DigiKey's search endpoint returns *approximate* matches,
so searching one part can return a neighbouring one. Results are filtered down
to the product whose `ManufacturerProductNumber` equals your key (compared after
stripping and lowercasing). If nothing matches exactly it is reported as *not
found* rather than silently enriched with the wrong part's data.

**Write key** — the field *name* selects the destination: same-named CSV column
or same-named `(property …)` block. Existing fields are replaced in place, new
ones appended.

## Safety

- **Dry run never writes.** Validates every MPN, prints a diff, logs missing
  parts, leaves the file byte-for-byte identical. Tests assert on file bytes.
- **Atomic writes.** Mutations go to a `tempfile.NamedTemporaryFile` in the
  destination directory, are `fsync`ed, then moved over the target with
  `os.replace`. An interrupted run cannot leave a truncated BOM or library.
- **Formatting preservation.** The `.kicad_sym` processor parses the file into an
  S-expression tree but applies changes as *byte-span splices*. Comments, tab
  indentation and property ordering survive exactly. A real run against a
  58-symbol library produced 30 added lines and **zero** modified lines.
- **No regex on structure.** `component_sync/sexpr.py` is a hand-written
  recursive-descent parser; structure is never interpreted by regular
  expression.

## Caching

A bill of materials names the same part many times over. In this project 172
component instances resolve to only 56 distinct part numbers, so every processor
owns a `LookupCache` and one request is made per distinct part, not per row:

```
Cache       : cache: 116 reused, 56 requested (+102 value hits, +14 miss hits, 0 failed)
```

The rules are deliberately conservative:

- **Misses are cached too.** An unstocked part repeats just as much as a stocked
  one, and re-querying it costs the same request.
- **Keyed on provider *identity*, not its name.** Two instances of the same class
  pointed at different distributor accounts would legitimately return different
  data, so they never share entries.
- **Only outer whitespace and case are normalised.** `PCA85162T/Q900/1Y` and
  `ATS2D3G NC LFG` keep their internal separators; stripping them would invent a
  part number that does not exist.
- **Transient faults are never cached.** A transport or server error is retried
  on the next occurrence. Caching it would permanently drop a part that is in
  fact available, and its symbol would silently never gain its fields.

## Managed fields

Field names follow the project's existing convention: ranges are written as
discrete `Min`/`Max` pairs, never as one free-text field.

| Written field | Source |
| --- | --- |
| `Value` | derived from the type parameter, for **discrete passives only** (see below) |

Properties this tool **adds** are written with `(hide yes)` and parked at
`(at 0 0 0)`, so they never appear on the schematic. Properties that already
exist keep whatever visibility the author gave them: an edit splices only the
value and never rewrites the surrounding block.
| `Manufacturer`, `Description`, `Datasheet`, `Package` | vendor record |
| `Digikey` | the product URL exactly as the API returned it |
| `Temperature Min` / `Temperature Max` | a range such as `-55/+150 C` or `-55 C / +85 C` |
| `Voltage Min` / `Voltage Max` | a range such as `2.65 V to 3.6 V` or `1.8V-5.5V` |
| `Voltage Rating` | a lone voltage such as `16 VDC` |
| `Operating Temperature` | a lone temperature such as `85 C` |

Never written: `Reference`, `Footprint` and `Part`. `Part` is the lookup key,
and the first two are the designer's decisions.

A lone value is kept verbatim rather than forced into a `Min` or `Max` that
would misrepresent it. Both bounds are emitted **only** when the source actually
contained two values.

### `Value` is derived, not invented

`Value` is a human-readable label, and it is exactly the quantity a distributor
publishes as a parameter. The project uses **SI notation with an explicit unit
everywhere** — there is one rendering path, no options and no per-type variants:

| Parameter present | Vendor input | Derived |
| --- | --- | --- |
| `Capacitance` | `0.1 uF` | `100 nF` |
| `Resistance` | `10 kOhms` | `10 kOhm` |
| `Resistance` | `4.7 kOhms` | `4.7 kOhm` |
| `Resistance` | `0 Ohms` | `0 Ohm` |
| `Inductance` | `560 nH` | `560 nH` |
| `Frequency` | `27.12 MHz` | `27.12 MHz` |

Notes on the convention:

- **No unit symbols.** `kOhm` and `uF`, never `kΩ` or `µF`. These are plain-text
  fields, and `Ω` is awkward to type and easy to mangle.
- **Largest fitting prefix**, so 1000 nF becomes `1 uF` and 1000 pF becomes
  `1 nF`.
- **Resistance is not special-cased.** R-notation (`10K`, `4K7`) is a real and
  defensible alternative, but mixing the two styles in one library is what
  makes a BOM hard to read, so everything uses SI.
- **Resistance in ohms stays `Ohm`, not `R`**, matching how distributors write
  it and avoiding ambiguity with the `R` reference designator.

When a part exposes none of those parameters — an IC, connector or switch —
**nothing is written** and the existing `Value` is left untouched. Those symbols
conventionally carry the part number as their value, and overwriting it would
destroy them.

### Value is derived only for discrete passives

Having a `Capacitance`, `Resistance`, `Inductance` or `Frequency` parameter is
**not** enough on its own. The NFC reader `PN7160A1HN/C100E` publishes
`Frequency = 13.56MHz`, and the cellular module `BG95M3LA-64-SGNS` publishes its
supported bands; deriving from parameters alone would replace their part numbers
with `13.56 MHz` and a frequency list.

Derivation is therefore gated on the distributor's own category taxonomy —
`Capacitors`, `Resistors`, `Inductors, Coils, Chokes`,
`Crystals, Oscillators, Resonators`. Everything else, including
`RF and Wireless` and `Integrated Circuits (ICs)`, keeps its existing `Value`
and gains only package, ranges and links.

## Verified against the live API

The DigiKey integration is checked against **captured production responses**,
stored in `component_sync/tests/fixtures/digikey_search.json`, not against
invented payloads. That distinction matters more than it sounds: the provider was
originally written against a hand-guessed response shape, and its 374 lines of
tests all passed while the code was unable to read a single real part.

| Bug | Symptom |
| --- | --- |
| `GET /products/v4/search/{mpn}` | The real endpoint is `POST /products/v4/search/keyword`. Every request 404'd, so **every part in the library reported as missing** |
| `Parameter` / `Value` | Real keys are `ParameterText` / `ValueText`, so `raw_parameters` was always empty — no `Value`, no ranges, ever |
| `Description` read as a string | It is an object; the library received a Python dict repr |
| `Datasheet` | The field is `DatasheetUrl`, and is often protocol-relative `//host/...` |
| `Package` | Not a top-level field; it lives in the `Package / Case` parameter |
| `"Voltage - Supply"` | Punctuation normalisation produced `voltage___supply`, matching nothing |
| micro prefix | DigiKey sends U+00B5 `µ`; only ASCII `u` was recognised, so `0.1 µF` became `100 mF` |
| `-` placeholders | Inapplicable parameters were written as real values |

Run against the real library this now reports **264 changes and 4 unresolved
parts**, against 24 unresolved before, with the file left byte-identical.

### URLs are never synthesised

`Digikey` is written only when the API returns a `ProductUrl`. A hand-built URL
would be indistinguishable from a real one in the library, so a missing value is
reported as missing instead.

There is deliberately **no `Mouser` handling**. This tool queries DigiKey and has
no Mouser API, so it cannot produce a Mouser link and does not try. Existing
Mouser links are preserved exactly as they are.

## Rate limits

DigiKey's **Product Information** API allows **120 requests per minute** and
**1000 per day**. The daily allowance is the binding constraint: a full run over
this library spends roughly 70 of it, so the quota runs out after about 14 runs
and **resets at midnight UTC**. Both windows are enforced by the provider:

- Requests are paced to about 109/minute, so a run never provokes a burst
  refusal in the first place.
- A **per-minute burst** is retried automatically, honouring the `Retry-After`
  header, because a burst clears within seconds.
- An **exhausted daily quota** stops the run immediately with the reset time.
  Every remaining request would be refused too, so continuing would only repeat
  the message and bury the one fact that matters. Nothing is written, and the
  exit status is `2`.

A quota refusal is reported as its own category, kept apart from unstocked
parts, because the two call for opposite responses:

| Category | Meaning |
| --- | --- |
| `NOT QUERIED` | the provider refused or failed; **re-running may well succeed** |
| `UNRESOLVED` | the distributor genuinely has no such part; fix the MPN |

A per-part provider fault no longer aborts the run, so one bad part does not
throw away the results for the other 56.

## Providers: data and sourcing are different things

Distributors are not interchangeable, and a single `--provider` choice between
them wrongly implies they are. Two roles exist:

| Role | Contributes | Providers |
| --- | --- | --- |
| **data** | component values: `Value`, voltage and temperature bounds, `Package`, `Manufacturer`, `Description`, `Datasheet` | DigiKey |
| **source** | purchasing links only | Mouser |

`--provider` offers **data** providers only. Sourcing providers run in sequence
*after* the data provider, and are skipped automatically when unconfigured, so
having `MOUSER_API_KEY` set is enough to enable the Mouser pass. `--no-source`
turns it off.

### Why Mouser cannot be a data provider

Its Search API publishes **no parametric data at all**. The only two
`ProductAttributes` names that appear on any part — across every search endpoint
in both API versions — are `Packaging` and `Standard Pack Qty`. There is no
`Resistance`, `Capacitance`, `Inductance`, `Frequency`, `Voltage` or
`Operating Temperature`, and no product-detail endpoint exists that exposes more.

The words do appear in the response, but only as prose inside `Description` and
`Category` ("Thick Film Resistors - SMD"). Parsing a value out of a sentence is
exactly the kind of guess that produced the `1e400 → 1 F` and `36k5` errors
earlier, so it is not done.

### Sourcing links are added, never substituted

A sourcing provider may only **add** a link. If the file already holds a value
for that field, the link is dropped: **46 of the 57** existing `Mouser` values in
this library are `mou.sr` short links to a different reseller, and those are the
author's curation, not an error to correct. The rule is scoped to contributed
links specifically, so it never blocks the data provider from fixing a genuinely
wrong `Value`.

Two further rules keep a wrong link from being written:

- **URLs are used exactly as returned.** Mouser returns a URL carrying the locale
  of the *API account* rather than of the reader, plus a `?qs=` tracking
  parameter. Rewriting it would mean constructing a URL, and a fabricated URL is
  indistinguishable from a real one in a symbol library.
- **An ambiguous part number is refused, not guessed.** Manufacturer part numbers
  are unique only within a manufacturer, so a short or numeric number can match
  unrelated products. Mouser returns **six** products all numbered `1028`: side
  cutting pliers, conduit fittings, an eInk display, a battery holder, punches
  and a printer shaft. The `Manufacturer` already in the file disambiguates it;
  where that fails, the ambiguity is reported. Taking the first result would
  attach a link to *pliers* to a battery-holder symbol.

Distributor naming of the same manufacturer also differs ("Murata" versus
"Murata Electronics", "YAGEO" versus "Yageo"), so the hint is compared after
stripping case and punctuation, accepting a division as its parent. This is
string comparison, not a vendor table, so it has limits: `C&K` and `C and K` are
not recognised as the same company, and such a part stays reported as ambiguous,
which is the safe direction to fail in.

### Two Mouser behaviours a naive client gets wrong

- A rejected API key returns **HTTP 200** with `Errors` populated. Checking
  `status_code` alone reads a rejected key as success.
- A part Mouser cannot find is **silently dropped** from a batch, with an empty
  `Errors` array. A miss is only detectable by comparing the requested part
  numbers against those returned.

In this library the sourcing pass currently proposes **no** changes: all 57
symbols that have a `Part` already carry a `Mouser` link. It earns its place for
new symbols and parts added later.

## Adding a provider

```python
from component_sync.models import ComponentData
from component_sync.providers.base import BaseProvider
from component_sync.providers.factory import ProviderFactory

class MouserProvider(BaseProvider):
    name = "mouser"

    def authenticate(self) -> None: ...
    def fetch_component_data(self, mpn: str) -> ComponentData: ...

ProviderFactory.register("mouser", MouserProvider)
```

`--provider` choices are generated from the registry, so the new key appears in
the CLI automatically.

## Known limitations

- **The daily quota is real.** 1000 requests/day, resetting at midnight UTC.
  A full run costs about 70. See *Rate limits* above.
- **The cache is per-run, not on disk.** Starting a second run re-requests
  everything. Persisting responses across runs would go stale, and a stale price
  or stock figure is worse than a slow run.
- **MPNs must be exact.** Some libraries store `Part` values that are not clean
  orderable part numbers, for example `RV-3028-C7 32.768kHz 1ppm TA QA` (MPN
  plus description). These miss, and are reported under *Parts not found*, which
  is where the author discovers them. The tool deliberately does not guess: every
  normalisation that would "fix" this one breaks a real part elsewhere.
  `RC0402JR-7D0RL` is a genuine miss — DigiKey returns 200 with no products.

## Development

```bash
python -m pytest -q                     # 304 tests
python -m mypy --strict component_sync  # clean
python -m ruff check component_sync     # clean
```

All HTTP is mocked with `unittest.mock`; the suite runs offline.

Range parsing is covered by `component_sync/tests/test_ranges.py`, which pins the
separator styles and units that vendors actually use.

## License

MIT
