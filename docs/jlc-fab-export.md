# jlc-fab-export: JLC order package from an Altium project

`jlc-fab-export` (`fab/jlc_fab_export.py`) turns the Altium project you have
open into everything JLCPCB's order page asks for:

- the Gerber and drill files, zipped, for the PCB order;
- the BOM and CPL (pick-and-place) spreadsheets for assembly, with the JLC/LCSC
  part number on every line.

It checks the files against the board and writes a README of anything that
would stop the order. It reads Altium through altium-mcp (`tools/altium_call.py`),
so Altium must be running with the project open.

## Quick start

```powershell
jlc-fab-export --init MyBoard\MyBoard.PrjPcb                 # once per project
jlc-fab-export --config MyBoard\jlc-fab.json --make-outline  # first export of a board
jlc-fab-export --config MyBoard\jlc-fab.json                 # every export after that
```

Then read `JLC_ORDER_README.md` in the output folder. If it starts with
**NOT READY TO ORDER**, fix what it lists first. The command also exits with
status 2.

Upload in this order:

1. `<name>_Gerber.zip` to the PCB order.
2. With PCB Assembly selected, `<name>_BOM_JLC.xlsx` and `<name>_CPL_JLC.xlsx`.
3. Check every polarised part in JLC's placement preview.

| Option | Effect |
|---|---|
| `--init <PrjPcb>` | Writes `jlc-fab.json` next to the project. Seeds `docs/cpl-rotations.md` in the hardware project's root (nearest folder up with a `CLAUDE.md`) from `templates/cpl-rotations.md`. |
| `--config <json>` | Builds the package. |
| `--make-outline` | Copies the board shape onto the outline layer as 0.1 mm tracks and arcs, then **saves the PCB**. Needed once per board. Refuses to run if that layer already has free tracks or arcs, or if a document is unsaved. |
| `--skip-gerbers` | BOM, CPL and checks only. |
| `--drc` | Also runs Altium's DRC and summarises it. This writes Altium's report and marks the violations on the board. |
| `--qty N` | Number of boards for the price column (catalogue snapshot). |

## Output

Default folder: `Project Outputs for <project>\JLC\`.

| File | Contents |
|---|---|
| `<name>_Gerber.zip` | Gerber layers and Excellon drill files only, no Altium reports |
| `<name>_BOM_JLC.xlsx` | `Comment`, `Designator`, `Footprint`, `LCSC`, one row per part number, designators grouped (`C1,C2,C10`) |
| `<name>_CPL_JLC.xlsx` | `Designator`, `Mid X`, `Mid Y` (e.g. `12.500mm`), `Layer` (`Top`/`Bottom`), `Rotation` (degrees) |
| `<name>_BOM_JLC.csv`, `<name>_CPL_JLC.csv` | the same tables, for diffs between exports |
| `JLC_ORDER_README.md` | blockers, board facts, decisions for the order form, checks, warnings, edge clearance, BOM with catalogue data, excluded parts, CPL notes |
| `gerber\` | Altium's raw output, reports included |

The script also writes `JLC Fabrication.OutJob` next to the project and adds
it to the project, saving the project the first time. The OutJob is
rewritten on every run, so change the script, not the OutJob.

## Configuration: `jlc-fab.json`

Paths are relative to the JSON file.

| Key | Default | Meaning |
|---|---|---|
| `project`, `pcb` | from `--init` | the `.PrjPcb` and the `.PcbDoc` to export |
| `name` | PCB file name | prefix of the output files |
| `out_dir` | `Project Outputs for <project>/JLC` | output folder; its `gerber` subfolder is emptied on every run |
| `outjob` | `JLC Fabrication.OutJob` | the OutJob the script writes |
| `outline_layer` | `1` | mechanical layer carrying the board outline (`.GM1`) |
| `outline_width_mm` | `0.1` | line width `--make-outline` uses |
| `lcsc_parameter` | `LCSC Part Number` | component parameter holding the JLC/LCSC code (`lcsc2altium-cli` writes it) |
| `bom_part_header` | `LCSC` | BOM column name for that code |
| `exclude_designators` | `[]` | parts JLC never places (fitted by hand) |
| `dnp_words` | `["DNP"]` | a Comment containing one of these whole words is not placed |
| `rotation_log` | `docs/cpl-rotations.md` of the hardware project | rotation and centre corrections |
| `edge_clearance_mm` | `2.5` | distance below which a part is reported as too close to the board edge |
| `qty` | `5` | boards for the price column |

## What JLC places

All the data comes from Altium: the PCB for positions and footprints, and the
compiled schematic for comments, Component Kind and the LCSC parameter.

A part is left out of both the BOM and the CPL, with the reason in the README,
when:

- its Component Kind is "No BOM", a net tie, graphical or a jumper;
- its designator is in `exclude_designators`;
- its Comment contains a `dnp_words` word (`22pF DNP`; not `DNPX`).

Any other part needs an LCSC code such as `C12345`. A missing or malformed
code is a blocker.

## BOM

- **Grouping.** One row per (LCSC code, Comment, Footprint), designators in
  natural order. If one code appears with two comments or footprints, both
  rows are kept and a warning is raised.
- **Catalogue columns in the README.** Basic / Extended, SMT / THT, stock and
  unit price at `qty` boards come from the local jlcparts catalogue
  (`JLCPICK_DB`), dated by its snapshot. Stock moves: confirm it on JLC's
  BOM-matching page before paying.

## CPL

- **Position.** The middle of the part's pads, in mm from the board's
  relative origin. The Gerbers and drill files use the same origin, so they
  line up wherever the origin is (bottom-left corner is the convention).

  The pads are used rather than the footprint origin because converted
  footprints often have off-centre origins. On one board, 12 LCSC2Altium
  footprints were 0.1–0.84 mm off. The README lists every part whose origin
  is more than 0.05 mm from its pad centre.
- **Rotation.** Altium's rotation (counter-clockwise, degrees) plus the
  matching row of the rotation log.
- **Rotation log** (`templates/cpl-rotations.md`, copied into the project as
  `docs/cpl-rotations.md`): a Markdown table keyed by footprint name or LCSC
  code.
  - `Rotation` is added to the part's rotation.
  - `dX`/`dY` shift the centre in the footprint's own frame, so one row fixes
    every placement of that footprint.
  - A row with `0` marks a footprint as confirmed. The README lists every
    footprint without a row as unconfirmed.
  - After each order, log what JLC's preview corrected and what it showed
    right.
- **Bottom-side parts.** These get a warning: tools disagree on bottom-side
  rotation, so check each in the preview.

## Gerber and drill settings

| Output | Settings |
|---|---|
| Gerber | RS-274X, inch, 2:5, leading zeros suppressed, relative origin, embedded apertures |
| Layers | every copper layer in the stack (signal `.G<n>`, plane `.GP<n>`), top and bottom silkscreen, solder mask and paste, board outline `.GM<outline_layer>` |
| Not plotted | Keep-Out: hole keep-out rings there would read as cut lines |
| NC drill | Excellon, inch, 2:5, trailing zeros suppressed, relative origin; plated and non-plated holes in separate files; slots as routed paths in their own file |

The outline comes from primitives on the outline layer, not from Altium's
board shape. `--make-outline` creates them from the board shape: lines and
arcs, so rounded corners stay arcs.

## Checks

**Blockers** (exit status 2):

| Check | Catches |
|---|---|
| Unrouted connections | a board that is not finished (counts Altium's ratsnest) |
| Schematic vs PCB designators and footprints | an ECO that was not run |
| LCSC code on every placed part | a part JLC cannot buy |
| Expected Gerber layers and a drill file present | a layer missing from the OutJob or the board |
| Outline Gerber extent = board outline | a wrong or partial outline layer |
| Copper inside the outline | stray copper, or an outline on the wrong layer |
| Every plated hole and slot sits on a copper flash (≤ 0.01 mm) | Gerber and drill files with different units, format or origin |
| Drill hits inside the outline | the same, for unplated holes |

**Warnings:**
- parts within `edge_clearance_mm` of the edge
- unsaved documents (the outputs come from Altium's open documents)
- bottom-side parts
- one part number with two comments or footprints

**Notes:**
- outline size
- drill/copper alignment
- silkscreen that falls off the board (it is not printed)

## JLC's assembly requirements

These were read on jlcpcb.com in November 2026. Re-check them before relying
on them.

- **Component body to board edge.** "The distance between the body of the
  components and the edge of the board must be equal or greater than 2.5mm."
  ([assembly T&C](https://jlcpcb.com/help/article/terms-and-conditions-of-jlcpcb-assembly-service))
- **Edge rails and fiducials.** "Edge rails and fiducials are required for
  standard PCB assembly at JLCPCB."
  ([edge rails](https://jlcpcb.com/help/article/how-to-add-edge-rails-fiducials-for-pcb-assembly-order))
  - JLC recommends 5 mm process edges, 2 mm tooling holes, and 1 mm fiducials
    3.85 mm from the panel edge.
  - Parts close to the edge "might collide with the guide rails" when the
    process edge is too narrow.
  - Assembled boards ship with the rails attached by default.
  - ([process edges](https://jlcpcb.com/help/article/specifications-for-adding-process-edges-and-positioning-holes))
- **Economic vs Standard PCBA.** Economic PCBA takes single boards from
  10 × 10 mm and lists edge rails as "Not necessary". Standard PCBA starts at
  70 × 70 mm and lists them as "Necessary".
  ([capabilities](https://jlcpcb.com/capabilities/pcb-assembly-capabilities))
- **Small boards.** For boards smaller than 70 × 70 mm, or standard PCBA
  without an edge rail, JLC "will select a suitable production method for edge
  rail, for instance, mouse bite method." ([T&C](https://jlcpcb.com/help/article/terms-and-conditions-of-jlcpcb-assembly-service))
- **BOM and CPL formats.** JLC's own help pages describe both:
  [BOM](https://jlcpcb.com/help/article/bill-of-materials-for-pcb-assembly),
  [CPL](https://jlcpcb.com/help/article/pick-place-file-for-pcb-assembly).

The edge-clearance and rail questions are the designer's to answer. The
README lists the parts concerned; it does not move them or add rails.

## Limitations

- **Edge distance** is measured to the outline's bounding box, which is exact
  for rectangles and approximate near rounded corners and for other shapes.
- **No panelisation.** Rails are added by JLC on the order form, or by you.
- **Altium variants are ignored.** Fitting is decided by Component Kind,
  `dnp_words` and `exclude_designators`.
- **One PCB per project.** `--init` picks the first `.PcbDoc`; set `pcb` if
  there are several.
- **Gerber extents count coordinates and arcs** (multi-quadrant, as Altium
  writes them), not aperture sizes. The drill/copper check needs at least one
  plated hole.
- **`run_altium_script` closes open Altium dialogs** while it waits. Close
  yours first.

## Troubleshooting

| Message | Do this |
|---|---|
| `Mechanical 1 has no board outline yet` | Run once with `--make-outline`. Alternatively, in Altium use Design > Board Shape > Create Primitives From Board Shape on that layer, then save. |
| `--make-outline ... EXISTS` | The layer already holds tracks or arcs. If they are not the outline, set `outline_layer` to a free mechanical layer. |
| `the PCB is not open in Altium` / `the project is not open` | Open the project in Altium and retry. |
| `Altium wrote nothing into ...gerber` | Open `JLC Fabrication.OutJob` in Altium and run the "JLC Gerber" container by hand to see Altium's message. |
| `drill/copper misaligned` | The Gerber and drill settings were changed: delete the OutJob and re-run. |
| `openpyxl is needed` | `pip install openpyxl` in the Python that runs the script (`install.ps1` adds it to the tools environment). |

The offline tests are in `fab/tests/test_jlc_fab_export.py`:

```powershell
python fab\tests\test_jlc_fab_export.py
```

They cover the board dump, the BOM and CPL rules, the rotation log, Gerber
and Excellon parsing, and the checks, using output copied from Altium 26.11.
