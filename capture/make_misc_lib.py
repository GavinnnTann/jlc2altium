"""Build <name>.SchLib/.PcbLib with a 1.5 mm test pad and a 2-pad net tie.

Usage: make_misc_lib.py <output folder> [name]   (default name: Misc)

These have no LCSC part (bare copper), so they are drawn here. Each footprint
gets an extruded 3D body so every footprint in the design has a 3D model.
Component kinds (Standard No BOM / Net Tie No BOM) are set on the placed
parts in Altium, since the library writer does not expose them.

Run with the jlc2altium venv (~/.venvs/jlc2altium, which has altium-monkey):
    ~/.venvs/jlc2altium/Scripts/python.exe capture/make_misc_lib.py <folder>
"""
import math
from pathlib import Path

import altium_monkey as am
from altium_monkey.altium_pcb_mask_expansion import PcbMaskExpansionMode

import sys

LIB = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
NAME = sys.argv[2] if len(sys.argv) > 2 else "Misc"
LIB.mkdir(parents=True, exist_ok=True)
MM = 1 / 0.0254  # mils per mm


def circle(r_mils: float, n: int = 16):
    return [(r_mils * math.cos(2 * math.pi * k / n), r_mils * math.sin(2 * math.pi * k / n)) for k in range(n)]


sch = am.AltiumSchLib()
pcb = am.AltiumPcbLib()

# ---------------------------------------------------------------- test point
sym = sch.add_symbol("TestPoint", description="Test pad, 1.5 mm, no component")
sym.add_ellipse(0, 50, 50, 50, color=0x000080, area_color=0xFFFFFF, is_solid=False, line_width=am.LineWidth.SMALL)
sym.add_pin(am.make_sch_pin(designator="1", location_mils=am.SchPointMils(0, 0), name="TP",
                            orientation=am.Rotation90.DEG_270, length_mils=100,
                            electrical_type=am.PinElectrical.PASSIVE,
                            name_visible=False, designator_visible=False))
sym.add_parameter("Description", "1.5 mm test pad (bare copper, no part)", is_hidden=True)
sym.set_part_count(1)

fp = pcb.add_footprint("TP_PAD_1.5MM", description="Test pad, 1.5 mm round, top, mask open, no paste")
fp.add_pad(designator="1", position_mils=(0, 0), width_mils=1.5 * MM, height_mils=1.5 * MM,
           layer=am.PcbLayer.TOP, shape=am.PadShape.CIRCLE,
           paste_mask_expansion_mode=PcbMaskExpansionMode.NONE,
           solder_mask_expansion_mode=PcbMaskExpansionMode.RULE)
fp.add_extruded_3d_body(outline_points_mils=circle(0.75 * MM), layer=am.PcbLayer.MECHANICAL_1,
                        overall_height_mils=0.05 * MM, name="TP_PAD_1.5MM", body_color_3d=0x3399CC)
sym.add_footprint("TP_PAD_1.5MM", description="1.5 mm test pad", is_current=True)

# ---------------------------------------------------------------- net tie
sym = sch.add_symbol("NetTie2", description="Net tie: joins two nets at one point (AGND to GND)")
sym.add_line(-50, 0, 50, 0, color=0x000080, line_width=am.LineWidth.MEDIUM)
for des, x, rot in (("1", -50, am.Rotation90.DEG_180), ("2", 50, am.Rotation90.DEG_0)):
    sym.add_pin(am.make_sch_pin(designator=des, location_mils=am.SchPointMils(x, 0), name=des,
                                orientation=rot, length_mils=100,
                                electrical_type=am.PinElectrical.PASSIVE,
                                name_visible=False, designator_visible=False))
sym.set_part_count(1)

fp = pcb.add_footprint("NETTIE_2PAD_0.5MM", description="Net tie: two 0.5 mm pads joined by 0.3 mm copper")
for des, x in (("1", -0.4 * MM), ("2", 0.4 * MM)):
    fp.add_pad(designator=des, position_mils=(x, 0), width_mils=0.5 * MM, height_mils=0.5 * MM,
               layer=am.PcbLayer.TOP, shape=am.PadShape.RECTANGLE,
               paste_mask_expansion_mode=PcbMaskExpansionMode.NONE)
fp.add_track((-0.4 * MM, 0), (0.4 * MM, 0), width_mils=0.3 * MM, layer=am.PcbLayer.TOP)
fp.add_extruded_3d_body(outline_points_mils=[(-0.65 * MM, -0.25 * MM), (0.65 * MM, -0.25 * MM),
                                             (0.65 * MM, 0.25 * MM), (-0.65 * MM, 0.25 * MM)],
                        layer=am.PcbLayer.MECHANICAL_1, overall_height_mils=0.05 * MM,
                        name="NETTIE_2PAD_0.5MM", body_color_3d=0x3399CC)
sym.add_footprint("NETTIE_2PAD_0.5MM", description="net tie", is_current=True)

sch.save(LIB / f"{NAME}.SchLib")
pcb.save(LIB / f"{NAME}.PcbLib")
chk = am.AltiumPcbLib(LIB / f"{NAME}.PcbLib")
print("symbols:", [s.name for s in am.AltiumSchLib(LIB / f"{NAME}.SchLib").symbols])
print("footprints:", chk.footprint_names(),
      "bodies:", [len(chk.find_footprint(n).component_bodies) for n in chk.footprint_names()])
