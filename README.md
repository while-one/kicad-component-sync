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

## Managed fields

Field names follow the project's existing convention: ranges are written as
discrete `Min`/`Max` pairs, never as one free-text field.

| Written field | Source |
| --- | --- |
| `Value` | derived from the type parameter (see below) |
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

### URLs are never synthesised

`Digikey` is written only when the API returns a `ProductUrl`. A hand-built URL
would be indistinguishable from a real one in the library, so a missing value is
reported as missing instead.

There is deliberately **no `Mouser` handling**. This tool queries DigiKey and has
no Mouser API, so it cannot produce a Mouser link and does not try. Existing
Mouser links are preserved exactly as they are.

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

- **No response caching.** Every row mentioning a part triggers its own request.
  Fine for a grouped BOM (56 unique parts of 57 rows), but a *per-instance* BOM
  would issue 166 calls for 56 distinct parts and hit distributor rate limits.
- **MPNs must be exact.** Some libraries store `Part` values that are not clean
  orderable part numbers, for example `PCA85162T/Q900/1Y` (packaging suffix),
  `PN7160A1HN/C100E` (needs an underscore), or
  `RV-3028-C7 32.768kHz 1ppm TA QA` (MPN plus description). These miss, and are
  reported under *Parts not found*.

## Development

```bash
python -m pytest -q                     # 105 tests
python -m mypy --strict component_sync  # clean
python -m ruff check component_sync     # clean
```

All HTTP is mocked with `unittest.mock`; the suite runs offline.

Range parsing is covered by `component_sync/tests/test_ranges.py`, which pins the
separator styles and units that vendors actually use.

## License

MIT
