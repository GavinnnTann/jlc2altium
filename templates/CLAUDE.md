# Hardware design agent — project instructions

Copy this file into the root of a hardware project. It is read by the agent as
project memory and doubles as the human runbook. Toolkit: jlc2altium
(installed at `$JLC2ALTIUM_HOME`).

**Division of labour.** The designer picks parts and does placement and
routing. The agent does part shortlisting, library work, schematic capture
from an agreed netlist, verification, and pre-fab auditing.

---

## 1. Hard rules

These are not style preferences. Each one maps to a board that came back dead.

### Never assert a specification you have not read

Catalogue data (stock, price, package, LCSC code) is fact: report it directly.
Anything electrical (efficiency, dropout, Rds(on), Iq, accuracy, tempco,
derating) needs a datasheet URL **and page number**, or an explicit
"not checked". Values from an LCSC listing are labelled as listing values.

> Wrong: "the TPS62840 does 90% at 1 mA"
> Right: "datasheet p.9 fig 8-3 shows ~89% at 1 mA, 3.3 Vout, 3.7 Vin"
> Also right: "haven't checked light-load efficiency, it's on p.9"

### The designer picks the part

Present 3–6 candidates with trade-offs. Never silently select, and never
narrow to one option without saying what was dropped and why.

### The agent does not make RF, power-path, or thermal decisions

Matching networks, antenna keepout, ground stitching, return paths, stackup,
copper for thermal relief, inductor and bulk-capacitor choice. The agent reads
datasheets and reference designs aloud and flags discrepancies. It does not
choose. Treat any agent output here as a question, not an answer.

### Verify every imported footprint

`jlcpick verify` on everything. Fine-pitch, WLCSP, RF, and anything with a
thermal pad also gets a datasheet land-pattern comparison. No exceptions.

### Re-check stock live before ordering

The local catalogue is as old as its download. `jlcpick search --live` before
committing.

### Calibration

Treat every generated circuit, footprint and netlist as a first draft from a
junior, never a deliverable. The audit in Stage 6 exists because a drawing
that looks right can still be wired wrong.

---

## 2. Setup (once per machine)

Windows, Altium Designer installed. From the jlc2altium repo:

```powershell
.\install.ps1 -FetchDb
```

It clones altium-mcp and LCSC2Altium, builds the Python environments,
registers the `altium` MCP server for Claude Code (user scope), installs the
skills (`jlcpick`, `altium-capture`, `altium-script`), puts `jlcpick`,
`jlc-query`, `lcsc2altium-cli`, `altium-call`, `capture` on PATH and sets
`JLCPICK_DB`, `JLCPICK_LCSC2ALTIUM`, `JLC2ALTIUM_HOME`. Restart Claude Code
afterwards so the MCP server and skills load.

Refresh the parts catalogue monthly: `fetch-jlcparts`.

---

## 3. Tool inventory

| Tool | Scope | Needs Altium open |
|---|---|---|
| `jlcpick search/probe` | part shortlisting, EasyEDA asset availability | no |
| `jlc-query` | parametric search of the catalogue (units, ranges) | no |
| `jlcpick geom/verify` | footprint geometry, self-checks, datasheet diff | no |
| `lcsc2altium-cli` | LCSC codes → `.SchLib`/`.PcbLib` with 3D, merged | no |
| `capture` | place, wire, check, audit, ERC, render sheets from a spec | yes |
| altium-mcp symbol/footprint creators | parts EasyEDA lacks | yes |
| altium-mcp `check_schematic` | drafting problems in saved sheets | no |
| altium-mcp `get_erc_violations`, `get_project_components` | compile, ERC, BOM view | yes |
| altium-mcp `get_outjob_settings` | fab-output audit | no |
| altium-mcp `run_output_jobs` | Gerbers/BOM/CPL | yes |
| altium-mcp `run_altium_script` | anything not wrapped (DelphiScript sandbox) | yes |

---

## 4. Workflow

### Stage 1 — Architecture (conversation, no tooling)

Block diagram, rails, current budget, interface list. Lock target MPNs for the
hero parts. Being wrong here is cheapest.

### Stage 2 — Shortlist

```bash
jlcpick search "3.3V buck 2A" --package QFN --min-stock 500 --qty 100 --json
jlc-query --subcat "Power Inductors" --attr "Inductance=2.2uH" --where "Current - Saturation(Isat)>=6A" --sort area
```

Present a table: MPN, LCSC, basic/ext, stock, unit price at build quantity,
package, asset score, one-line trade-off; then datasheet URLs; then a
recommendation. Prefer a `full`-asset part (symbol + footprint + 3D) over a
marginally better part you would have to draw. If the designer wants compact
boards, rank smaller packages first and state the derating cost.

### Stage 3 — Import

```bash
lcsc2altium-cli --3d --lcsc C2040,C165948 --schlib Libraries/Board.SchLib --pcblib Libraries/Board.PcbLib
```

Footprints are named after EasyEDA's footprint title, so identical names mean
identical geometry. For parts EasyEDA lacks, use altium-mcp's
`create_symbols_batch` / `create_footprints_batch`.

### Stage 4 — Verify

```bash
jlcpick verify C2040
jlcpick template > C2040.datasheet.json   # fill from the RECOMMENDED LAND PATTERN table
jlcpick verify C2040 --datasheet C2040.datasheet.json
```

A `fail` on `pitch` means the footprint is for a different package. Duplicate
pad numbers are only acceptable for shield/shell tabs meant to be one net.

### Stage 5 — Schematic capture

Agree the netlist first (net → pins), then capture with the `altium-capture`
skill: a spec file, `capture setup`, `capture build all`. Placement on the
sheet is generated; tidy it by hand if wanted.

### Stage 6 — Netlist audit (highest-value agent task)

```
capture <spec> audit     # Altium's compiled netlist must MATCH the spec
capture <spec> erc       # 0 errors, 0 warnings
```

Then verify the netlist **pin by pin** against the datasheet pin tables and
reference designs: decoupling on the right pins, bootstrap, feedback divider,
soft-start, pull-ups on the right rail, strapping pins at the correct levels,
NC pins actually unconnected.

### Stage 7 — Layout (designer's)

The agent helps at the margins: `get_net_connections`, `place_components`,
`check_placement`, `check_orientation`, `route_track` / `check_routing` for
simple runs, `run_drc`. Placement and routing decisions stay with the designer.

### Stage 8 — Pre-fab audit

`get_project_info` (nothing unsaved), `get_erc_violations`, `get_board_summary`,
`get_component_primitives` (flipped footprints), `get_outjob_settings`.

### Stage 9 — Generate and order

`run_output_jobs`. JLC BOM columns: `Comment`, `Designator`, `Footprint`,
`LCSC` (map from the `LCSC Part Number` parameter). CPL: `Designator`, `Mid X`,
`Mid Y`, `Layer`, `Rotation`. Log every rotation JLC corrects in
`docs/cpl-rotations.md` and bake it into the export.

---

## 5. Known failure modes

- `run_altium_script` closes dialogs owned by X2.EXE while it waits, yours
  included. Close your Altium dialogs before running scripts.
- Altium window screenshots can be stale: render sheets with `capture render`.
- A sheet resized by script keeps its old size until reopened (false
  off-sheet ERC warnings); `capture erc` reopens sheets first.
- Land patterns differ per vendor for the same nominal package. Follow the
  datasheet for the specific part, never the package name.
- EasyEDA footprints are community-contributed; that is what Stage 4 is for.
- The jlcparts catalogue changes its schema from time to time; if
  `jlcpick search` finds nothing after a refresh, run it with `-v` and read
  the `catalogue schema:` line.
