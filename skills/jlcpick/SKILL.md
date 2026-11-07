---
name: jlcpick
description: Shortlist JLCPCB/LCSC parts filtered by stock AND by whether a symbol, footprint and 3D model already exist, then import them into a KiCad or Altium library and check the footprint against the datasheet land pattern. Use when choosing parts for a board, building a BOM for JLC assembly, populating a component library, or verifying an imported footprint.
---

# jlcpick

Part selection for JLCPCB assembly, filtered by the thing that actually
decides how long a part costs you: whether EasyEDA already has a symbol,
footprint and 3D model for it.

## The rule that matters

**Never assert a specification you have not read.** Report what the catalogue
says (stock, price, package, LCSC code) as fact. For anything electrical —
efficiency, dropout, Rds(on), accuracy, temperature range — either quote the
datasheet with a URL and page, or say you have not checked. A wrong part
choice surfaces as a dead board six weeks later.

**The user picks the part.** Present a shortlist with trade-offs. Do not
silently choose for them, especially for anything in the signal path, the
power path, or RF.

## Workflow

### 1. Shortlist

```bash
jlcpick search 3.3V buck 2A --package SOT-23 --min-stock 500 --qty 100 --json
jlcpick search --category "Power Inductors" --min-stock 1000 --json
```

Search words are ANDed; each must appear in the description, MPN or
package. Keep them to what LCSC descriptions actually contain (values like
`3.3V`, `2A`, a function word like `LDO`). `--category` matches any
category or subcategory name containing the text. Few or no results usually
means too many words: drop one before concluding the part does not exist.

Useful flags:

| Flag | Why |
|---|---|
| `--basic-only` | JLC Basic parts — no extended-part setup fee |
| `--require-assets` | drop anything without symbol + footprint |
| `--require-3d` | drop anything without a 3D model too |
| `--min-stock N` | stock headroom for the build |
| `--qty N` | price tier to quote at (default 100) |
| `--live` | jlcsearch instead of the local snapshot |
| `--fast` | skip the model-URL check; faster, slightly less certain |

For parametric questions ("2.2 µH, Isat ≥ 6 A, DCR ≤ 25 mΩ, smallest
first"), query the catalogue's attribute table directly:

```bash
jlc-query --subcat "Power Inductors" --attr "Inductance=2.2uH" \
  --where "Current - Saturation(Isat)>=6A" --where "DC Resistance(DCR)<=25mOhm" \
  --show "DC Resistance(DCR)" --sort area
jlc-query --subcat "Power Inductors" --keys      # attribute names in a category
```

The `ASSETS` column is the point of this tool:

- `full` — symbol + footprint + 3D. Costs minutes.
- `no-3d` — fine unless mechanical fit matters (it does for enclosures).
- `partial` / `none` — you are drawing this one. Budget an hour or more.

Prefer a `full` part over a marginally better `none` part unless the spec
genuinely requires the latter. Say so explicitly when recommending.

### 2. Present the shortlist

A table, 3–6 candidates, with: MPN, LCSC, basic/extended, stock, unit price
at the build quantity, package, asset score, and the one-line trade-off.
Then the datasheet URL for each. Then your recommendation and why.

### 3. Import

```bash
jlcpick import C2040 C165948 --backend kicad --output ./libs/jlcpick --project-relative
jlcpick import C2040 --backend altium --schlib ./lib/Parts.SchLib --pcblib ./lib/Parts.PcbLib
```

The Altium backend shells out to `lcsc2altium-cli` (jlc2altium's
`tools/lcsc2altium_cli.py`, named by `JLCPICK_LCSC2ALTIUM`). It can also be
called directly with many parts at once:

```bash
lcsc2altium-cli --3d --lcsc C16581,C351410,C7419210 --schlib Libraries/Board.SchLib --pcblib Libraries/Board.PcbLib
```

- It merges into existing libraries: parts already there are skipped
  (`--replace` re-imports), hand edits made in Altium are kept.
- Footprints are named after EasyEDA's footprint title
  (`SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BL`), never the bare package name: two
  parts in the same package can use different EasyEDA footprints, and a
  generic name would link one part's symbol to the other's pads. The 3D body
  takes the footprint's name.
- Each symbol carries `LCSC Part Number` and `MPN` parameters for the JLC BOM.
- Every pin comes in as Passive (LCSC2Altium's choice), so ERC will not catch
  output-to-output conflicts.

### 4. Verify — do not skip this

EasyEDA footprints are community-contributed and uneven.

```bash
jlcpick verify C2040                      # self-checks, no datasheet needed
```

Self-checks catch what actually kills boards: duplicate pad numbers, pads
split across both copper layers (a flipped footprint that left copper
behind), zero-size pads, numbering gaps, silk printed over solderable area.

For the land pattern, compare against the datasheet:

```bash
jlcpick template > C2040.datasheet.json
# read the datasheet, fill the JSON, then:
jlcpick verify C2040 --datasheet C2040.datasheet.json --json
```

When filling the template:

- Use the **recommended land pattern** table, not the package-outline
  drawing. They are different tables and the body dimensions will not match
  the pads. This is the single most common mistake.
- Record `datasheet_url` and `datasheet_page` so the numbers are checkable.
- Leave a field `null` if the datasheet does not give it. Omitted fields are
  skipped, not failed. Do not estimate.
- All values in millimetres.

Report `fail` findings prominently. A `warn` on `span_inner` is usually
tolerable; a `fail` on `pitch` means the footprint is for a different
package and the board will not assemble.

### 5. Inspect geometry when something looks wrong

```bash
jlcpick geom C2040 --pads --json
```

Normalised to mm, origin at the footprint origin, Y-up.

## Setup

`install.ps1` in the jlc2altium repo does all of this. By hand:

```bash
pip install -e jlcpick                   # from the jlc2altium repo
pip install easyeda2kicad                 # for the kicad backend
python tools/fetch_jlcparts.py           # jlcparts catalogue -> ~/.cache/jlcpick/cache.sqlite3
export JLCPICK_DB=~/.cache/jlcpick/cache.sqlite3
```

The catalogue is published as a split zip (`cache.z01`..`cache.zNN` +
`cache.zip`, about 300 MB, 2.3 GB unpacked); `unzip` cannot read split
archives, which is why `fetch_jlcparts.py` exists. Refresh it monthly.

The local snapshot is as old as your download. Sweep locally, then re-check
the shortlist with `--live` before committing to an order, because stock
moves. JLC's own search API rate-limits automated callers after a request or
two; jlcsearch (`--live`) does not, but its free-text search misses parts the
local catalogue has.

`fetch_jlcparts.py` indexes the catalogue after extracting it. A catalogue
from elsewhere needs `fetch-jlcparts --index-only` once, or every search
scans 3.8 million rows (about 15 s instead of about 1 s).

EasyEDA's API (`easyeda.com`) answers HTTP 403 after ~90 quick requests. The
component cache in `~/.cache/jlcpick/easyeda` is shared by probe, verify and
import. Uncached requests are spaced out (`JLCPICK_EASYEDA_GAP`: 0.5 s for
probes, 1.5 s for imports), and a 403 switches to the `lceda.cn` host, which
serves the same library, then backs off.

## Limits

- Does not do schematic capture. Parts and libraries only.
- Does not evaluate whether a circuit is correct.
- Fine-pitch, RF and WLCSP parts are frequently absent from EasyEDA, or
  present with geometry you should not trust. Verify those against the
  datasheet without exception.
