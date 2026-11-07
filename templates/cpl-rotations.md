# JLC CPL rotation and centre corrections

JLC's expected rotations differ from Altium's for many footprints. This file
records two things, and it compounds from order to order:

- every correction JLC applied on an assembly order;
- every footprint that JLC's preview showed correct as exported.

`jlc-fab-export` (jlc2altium, `fab/jlc_fab_export.py`) reads the table below
on every export:

- **Footprint or LCSC.** The footprint name exactly as on the PCB (the
  export's README lists them), or an LCSC code such as `C456012`. A row keyed
  by LCSC code wins over one keyed by footprint, and a later row wins over an
  earlier one.
- **Rotation (deg).** Added to the Altium rotation, counter-clockwise
  positive, as JLC's preview showed it. Write `0` for a footprint that came
  out right: it then counts as confirmed.
- **dX / dY (mm).** Shift of the CPL centre in the footprint's own frame
  (unrotated, X right, Y up), so one row fixes every placement of that
  footprint. The export's centre is the middle of the part's pads.

**After each order:**
1. Open JLC's placement preview.
2. Compare each part with its pads: pin 1, diode bars, LED cathodes,
   connector openings.
3. Add one row per footprint.

| Footprint or LCSC | Rotation (deg) | dX (mm) | dY (mm) | Board / order | Date | Notes |
|---|---|---|---|---|---|---|
