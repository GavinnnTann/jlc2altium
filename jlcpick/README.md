# jlcpick

Shortlist JLCPCB/LCSC parts by stock **and** by whether a symbol, footprint
and 3D model already exist — then import them and check the footprint
against the datasheet.

The premise: picking a part is not slow because searching is slow. It is slow
because a part that looks fine on paper turns out to have no library entry,
and you lose an afternoon drawing a symbol. So asset availability is a
first-class filter here, not an afterthought.

```
$ jlcpick search 3.3V LDO --package SOT-23 --limit 4

LCSC     MPN             PACKAGE    LIB    STOCK    PRICE   ASSETS  DESCRIPTION
-------  --------------  ---------  -----  -------  ------  ------  --------------------------------------------
C347376  XC6206P332MR    SOT-23     ext    1774668  0.0204  full    -25℃~+80℃@(Ta) 1 240mV@(50mA) 250mA 3.3V ...
C347386  XC6219B332MR    SOT-23-5   ext    721898   0.0381  full    -20℃~+60℃ 1 15uA 280mV@(100mA) 3.3V 300mA...
C5446    XC6206P332MR-G  SOT-23-3L  basic  405951   0.1181  full    -40℃~+85℃@(Ta) 1 1uA 200mA 3.3V 6V Fixed ...
C347215  HT7533S         SOT-23     ext    313992   0.035   full    -40℃~+85℃ 1 100mA 100mV@(1mA) 3.3V 30V 3u...

4 result(s) - price at qty 100
```

Search words are ANDed, and each one can match the description, MPN or
package, so `"3.3V LDO 300mA"` and `3.3V LDO 300mA` mean the same thing.
Results are sorted by stock. Stock and prices come from your catalogue
snapshot, so they will differ from the run above.

## Install

The jlc2altium `install.ps1` does this. By hand:

```bash
pip install -e .
pip install easyeda2kicad          # only for the KiCad import backend
python ../tools/fetch_jlcparts.py  # jlcparts catalogue (split zip) -> ~/.cache/jlcpick/cache.sqlite3
export JLCPICK_DB=~/.cache/jlcpick/cache.sqlite3
```

`fetch_jlcparts.py` also indexes the catalogue (stock, category,
subcategory), which takes searches from about 15 s to about 1 s. For a
catalogue downloaded some other way, run `python ../tools/fetch_jlcparts.py --index-only`.

The catalogue reader introspects the schema and understands both the older
`components` table and the current "source-db-v2" `jlc_components` table
(text categories, `library_type` = base/expand, a `present` flag).

## Commands

| Command | Does |
|---|---|
| `search` | parametric search with asset availability |
| `probe` | asset check for specific LCSC codes |
| `geom` | normalised footprint geometry, mm, Y-up |
| `verify` | self-checks, plus diff against a datasheet land pattern |
| `template` | emit the datasheet JSON to fill in |
| `import` | pull parts into an Altium (default) or KiCad library |

Every command takes `--json`.

## Verification

Self-checks need no datasheet and catch the failures that waste a board spin:

- duplicate pad numbers (silently shorted nets)
- pads split across both copper layers — a flipped footprint that left copper behind
- zero-size pads
- pad numbering gaps
- silkscreen printed over solderable area

Land-pattern comparison needs the datasheet, and splits the work on purpose:
reading a table out of a PDF is a language task, measuring pads and diffing
numbers is a deterministic one. Mixing them gives you a confident sentence
instead of a number.

```bash
jlcpick template > part.json       # fill from the RECOMMENDED LAND PATTERN table
jlcpick verify C2040 --datasheet part.json
```

## Architecture

```
catalog.py   jlcparts SQLite (schema is introspected, not hardcoded)
             + jlcsearch.tscircuit.com live fallback
easyeda.py   EasyEDA component API -> does a symbol/footprint/3D exist?
             parses footprints to mm, Y-up, origin-centred; paced requests,
             falls back to lceda.cn on HTTP 403
verify.py    land-pattern maths and the diff engine
convert.py   LCSC2Altium (through tools/lcsc2altium_cli.py) / easyeda2kicad
cli.py       argparse, --json everywhere
```

### The Altium backend

Writing `.SchLib`/`.PcbLib` from scratch means implementing OLE compound
documents with reverse-engineered binary records inside. You would get 90%
working and then lose a month on pin electrical types, parameter records,
unique IDs and the symbol-to-footprint link.

[LCSC2Altium](https://github.com/Gort-Power/LCSC2Altium) already did that,
including the EasyEDA Y-down to Altium Y-up flip and origin computation from
converted geometry rather than EasyEDA's bbox. `import --backend altium`
shells out to the program named by `JLCPICK_LCSC2ALTIUM`; jlc2altium's
`tools/lcsc2altium_cli.py` is that wrapper (merges into existing libraries,
caches EasyEDA responses, names footprints by EasyEDA's footprint title).

`--backend kicad` uses easyeda2kicad instead, if you work in KiCad.

## Tests

```bash
python3 tests/test_geometry.py        # land-pattern maths, self-checks
python3 tests/test_easyeda_parsing.py # EasyEDA record format, offline
python3 tests/test_catalog.py         # search filters on a synthetic catalogue
```

All run without network. The parsing test encodes EasyEDA's record field
order — if EasyEDA changes it, that test fails first.

## Limits

- No schematic capture. Parts and libraries only.
- Local catalogue stock is as old as your snapshot; re-check with `--live`
  before ordering.
- Fine-pitch, RF and WLCSP parts are often missing from EasyEDA or present
  with geometry you should not trust. Verify those against the datasheet
  every time.

## Credits

Stands on [jlcparts](https://github.com/yaqwsx/jlcparts),
[easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py),
[LCSC2Altium](https://github.com/Gort-Power/LCSC2Altium), and
[jlcsearch](https://jlcsearch.tscircuit.com).

MIT.
