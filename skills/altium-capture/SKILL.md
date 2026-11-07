---
name: altium-capture
description: Capture a schematic into Altium Designer from a design description (netlist, HTML/PDF schematic, notes) using JLCPCB/LCSC parts - build the part library from LCSC codes with 3D models, place and wire sheets through altium-mcp, then prove the result with ERC and a pin-by-pin netlist audit. Use when asked to draw, capture, recreate or port a schematic into Altium, to build an Altium library from LCSC/JLC part numbers, or to verify a captured Altium netlist against a source design.
---

# Altium schematic capture with JLC parts

Toolkit: the jlc2altium repo (`$JLC2ALTIUM_HOME`, set by `install.ps1`).
Altium must be running. The altium-mcp server (`altium`) gives the
interactive tools; batch work goes through `capture/capture.py`, which calls
the same bridge.

## Rules that come first

- If the project has a hardware `CLAUDE.md`, follow it. jlc2altium ships one
  in `templates/CLAUDE.md`; offer to copy it into a new hardware project.
- **The designer picks the parts.** Present 3-6 candidates with trade-offs
  (`jlcpick search`, `jlc-query`); never silently substitute. Power path,
  RF and thermal choices are theirs.
- **Never state an electrical spec you have not read in a datasheet.**
  Catalogue data (stock, price, package, LCSC code) is fact; LCSC listing
  values are labelled as such.
- **Write the netlist down before drawing anything.** For each net, the
  pins on it. When the source is ambiguous (a resistor "near" a pin), read the
  source's geometry or ask; do not infer from what looks typical.

## Workflow

1. **Netlist.** Extract it from the source design. For an SVG/HTML schematic,
   read wire endpoints, junction dots and stroke colours (net colours mark
   rails and AGND vs GND).
2. **Parts.** Shortlist with `jlcpick` / `jlc-query`, get the designer's
   picks, then import every LCSC code with 3D models into the project:
   ```
   lcsc2altium-cli --3d --lcsc C1,C2,... --schlib <proj>/Libraries/Board.SchLib --pcblib <proj>/Libraries/Board.PcbLib
   make-misc-lib <proj>/Libraries Misc   # test pads, net tie
   ```
   Then `jlcpick verify <lcsc>` on each footprint, and a datasheet
   land-pattern comparison for fine-pitch and thermal-pad parts.
3. **Pin geometry.** Print each symbol's pins before planning the layout:
   ```python
   import altium_monkey as am
   for s in am.AltiumSchLib("Board.SchLib").symbols:
       print(s.name, [(p.designator, p.name, p.orientation_name, p.get_hot_spot()) for p in s.pins])
   ```
   Pin names from EasyEDA can differ from the datasheet (DW01A pin 2 "VM" vs
   "CS"); match by number. Diodes may have unnamed pins: find the cathode
   from the symbol's bar in the cached EasyEDA record.
4. **Spec.** Copy `capture/examples/led_demo_spec.py`. One dict per sheet:
   parts `(ref, schlib, symbol, x, y, rotation, mirror, comment)` in mils and
   connections (`("U1.4", "VIN")`, `("wire", "R5.2", "D2.1")`,
   `("tee", "U2.5", "R7.1", "C3.2")`, `("U2.4", None)` for no-connect).
5. **Build and prove:**
   ```
   capture.py spec.py setup        # sheets (A3), libraries in project, global net scope
   capture.py spec.py build all    # clear, place, wire from MEASURED pins, fix text, check
   capture.py spec.py audit        # must print MATCH, then review every net by hand
   capture.py spec.py erc          # expect 0 errors, 0 warnings
   capture.py spec.py render out/  # PNGs drawn by Altium; show them to the user
   ```
   Review the audit's netlist pin by pin against the source design and the
   datasheet pin tables (straps, decoupling, feedback, no-connects).
6. **Changes after a PCB exists:** edit the spec, then touch only what
   changed so every other part keeps its unique ID and its PCB placement:
   ```
   capture.py spec.py update <sheet> J2 J3 U5.16   # parts to re-place, REF.PIN to re-wire
   ```
   Then audit and ERC again, and tell the user to run Design > Update PCB
   Document. Never `build` (clear + re-place) a sheet once layout has begun.
   When a connector changes package, remember its mounting-tab pins: mark
   them no-connect in the spec.

## Layout conventions that keep check_schematic clean

- A3 sheet is 16535 x 11693 mil; keep parts off the title block (bottom right).
- Pins on a 100 mil grid. Two-pin passives vertical: rotation 90 puts pin 2
  on top, 270 puts pin 1 on top.
- Every connection is a stub from the pin's electrical end with a label or
  port at the stub end. Use direct wires only between pins that face each
  other on one line, and tees (with a junction) for a shunt part on a wire.
- In dense pin rows (100 mil pitch), force labels instead of ground ports:
  `("U5.13", "GND", {"style": "label"})`. A label named GND joins the GND
  port net on the same sheet; keep at least one real GND port per sheet.
- Rails (spec `RAILS`) become bar ports, GND a power-ground port, AGND a
  signal-ground port; join AGND to GND through a net-tie part (`NET_TIES`).

## Things that went wrong, so they do not again

- **Window screenshots are stale.** Altium only paints views it has displayed,
  so `get_screenshot` can return an old frame. `capture.py render` asks Altium
  to copy the sheet as a metafile and rasterises it (overwrites the clipboard).
- **A sheet resized by script keeps its old size in memory** until it is
  reopened; ERC then reports objects off the sheet. `capture.py erc` reopens
  the sheets first.
- **Placing parts opens the library and focuses it.** Focus a sheet before
  tools that act on "the current view".
- **Removing objects:** re-run the iterator after each removal; storing
  interfaces in `TStringList.Objects` fails in DelphiScript.
- **Content Vault parts** (Manufacturer Part Search) have no LCSC number and
  their 3D models cannot be checked without a PCB; replace them with LCSC
  imports when the BOM must be JLC-assemblable.
- **Altium footprint names over 31 characters** are stored truncated in the
  file's storage table but intact in the record; read names through
  `AltiumPcbLib(path).footprint_names()`, not the static helper.
- **EasyEDA 403s** after ~90 quick requests: the import wrapper caches and
  falls back to `lceda.cn`.
