---
name: jlc-fab-export
description: Package an Altium PCB project for a JLCPCB board + assembly (PCBA) order in one step - Gerber RS-274X and NC drill zipped for upload, an Excel BOM with JLC/LCSC part numbers (Comment, Designator, Footprint, LCSC), an Excel CPL / pick-and-place file with rotations (Designator, Mid X, Mid Y, Layer, Rotation), and a README that lists blockers, edge-clearance and rotation warnings. Use when asked to export, package or prepare fabrication / PCBA files for JLC, to make a JLC BOM or CPL, or to check whether a board is ready to order.
---

# JLC fabrication + assembly package

Toolkit: the jlc2altium repo (`$JLC2ALTIUM_HOME`, set by `install.ps1`).
Script: `fab/jlc_fab_export.py`, on PATH as `jlc-fab-export`. Full reference:
`docs/jlc-fab-export.md`.

It reads the project open in Altium through the altium-mcp bridge and writes
everything JLC's order page asks for into one folder.

## Rules that come first

- If the project has a hardware `CLAUDE.md`, follow it.
- **Never upload or order.** Ordering is the designer's step on jlcpcb.com.
- **Report blockers as blockers.** Do not soften them. The script exits with
  status 2 and the README starts with NOT READY TO ORDER when there are any.
- **The designer decides the order-form questions:**
  - edge rails or a panel
  - PCBA type (Economic / Standard)
  - board thickness and finish
  - moving parts away from the board edge

  Present JLC's guidance (linked in the README) and the options.
- **Stock in the README is a dated snapshot.** Say so. Live stock is on
  JLC's BOM-matching page.

## Run it

Altium must be running with the project open. Check the user is not in an
Altium dialog first: `run_altium_script` closes them.

```powershell
jlc-fab-export --init MyBoard\MyBoard.PrjPcb                 # once: writes MyBoard\jlc-fab.json
jlc-fab-export --config MyBoard\jlc-fab.json --make-outline  # first export of a board
jlc-fab-export --config MyBoard\jlc-fab.json                 # every export after that
```

- **`--init`** writes `jlc-fab.json` next to the project. It also seeds
  `docs/cpl-rotations.md` in the hardware project's root (the nearest folder
  up with a `CLAUDE.md`).
- **`--make-outline`** copies the board shape onto Mechanical 1 as 0.1 mm
  tracks and arcs, which become the `.GM1` outline, then **saves the PCB**.
  Tell the user. It refuses to run if Mechanical 1 already has free tracks or
  arcs, or if any document is unsaved.
- **`--skip-gerbers`** writes the BOM, CPL and checks only.
- **`--drc`** also runs Altium's DRC. That writes a report and marks the
  violations on the board, so use it only when asked.
- **`--qty N`** sets the number of boards for the price column.

## What it writes (default `Project Outputs for <project>\JLC\`)

| File | Upload to |
|---|---|
| `<name>_Gerber.zip` | PCB order: copper, mask, paste, silk, `.GM1` outline, Excellon drill (plated, non-plated, slots) |
| `<name>_BOM_JLC.xlsx` | Assembly BOM: one row per LCSC part number, designators grouped |
| `<name>_CPL_JLC.xlsx` | Assembly CPL: pad-centre X/Y in mm from the board origin, Top/Bottom, rotation |
| `JLC_ORDER_README.md` | read first: blockers, checks, warnings, excluded parts, order-form decisions |

There are CSV copies of the BOM and CPL for diffs, and `gerber\` holds
Altium's raw output. The script adds `JLC Fabrication.OutJob` to the project
and rewrites it on every run, so change `fab/jlc_fab_export.py`, not the
OutJob.

## Reading the result

The script prints a JSON summary: `ready`, `blockers`, `warnings`, paths.
Relay it.

**Blockers:**
- unrouted connections
- schematic/PCB mismatch (ECO not run)
- a part to be placed with no LCSC number
- missing Gerber layers or drill file
- outline not matching the board
- copper outside the outline
- a plated hole or slot not on a copper pad (Gerber/drill units, format or
  origin disagree)

**Warnings:**
- parts closer than 2.5 mm to the edge. JLC's assembly T&C ask for 2.5 mm from
  component body to board edge; rails are the usual fix.
- unsaved documents
- bottom-side parts
- one part number with two comments
- silkscreen off the board

**Not placed by JLC.** These are listed with the reason:
- Component Kind "No BOM", net ties, graphical, jumpers
- a Comment containing `DNP`
- `exclude_designators`

**Rotations** are Altium's plus the corrections in `docs/cpl-rotations.md`.
Footprints not yet in that log are listed as unconfirmed. After an order, log
every correction JLC made in the placement preview, and every footprint that
came out right with `0`. The next export applies them.
