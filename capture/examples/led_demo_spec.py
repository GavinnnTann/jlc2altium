"""Example capture spec: a header-powered LED with decoupling and a test pad.

Try it:
  1. In Altium: File > New > Project, save it as  <this folder>/LedDemo/LedDemo.PrjPcb
  2. Build the parts library (JLC parts, symbols + footprints + 3D):
       lcsc2altium-cli --3d --lcsc C131337,C2286,C11702,C307331 \
           --schlib LedDemo/Libraries/LedDemo.SchLib --pcblib LedDemo/Libraries/LedDemo.PcbLib
       make-misc-lib LedDemo/Libraries Misc
  3. capture capture/examples/led_demo_spec.py setup
     capture capture/examples/led_demo_spec.py build all
     capture capture/examples/led_demo_spec.py audit
     capture capture/examples/led_demo_spec.py erc
     capture capture/examples/led_demo_spec.py render out/
  (run from the repo folder; the commands are the shims install.ps1 puts on PATH)

Coordinates are mils on the sheet (origin bottom left). Rotation is CCW: a
horizontal 2-pin part at 90 has pin 2 on top. Symbol names are what
lcsc2altium-cli printed (the MPN, made file-name safe).
"""
from pathlib import Path

PROJECT = Path(__file__).resolve().parent / "LedDemo" / "LedDemo.PrjPcb"
LIB = str(PROJECT.parent / "Libraries" / "LedDemo.SchLib")
MISC = str(PROJECT.parent / "Libraries" / "Misc.SchLib")

LIBRARIES = [r"Libraries\LedDemo.SchLib", r"Libraries\LedDemo.PcbLib",
             r"Libraries\Misc.SchLib", r"Libraries\Misc.PcbLib"]
RAILS = {"+5V"}            # drawn as bar ports; GND/AGND are ground ports
NO_BOM = ["TP"]            # test pads: bare copper, not in the BOM
NET_TIES = []

SHEETS = {
    1: {
        "file": "LED.SchDoc",
        "parts": [
            # (designator, schlib, symbol, x, y, rotation, mirror, comment)
            ("J1", LIB, "B2B-PH-K-S_LF__SN", 1000, 3000, 0, 0, "5V IN"),
            ("R1", LIB, "0402WGF1001TCE", 2600, 3100, 0, 0, "1k"),
            ("D1", LIB, "KT-0603R", 3200, 3050, 180, 0, "RED"),   # 180: anode left, cathode right
            ("C1", LIB, "CL05B104KB54PNC", 4200, 3000, 90, 0, "100nF"),
            ("TP1", MISC, "TestPoint", 4800, 3300, 0, 0, "+5V"),
        ],
        "conns": [
            # ("REF.PIN", "NET") draws a stub + label, or a port for rails/grounds
            ("J1.1", "+5V"), ("J1.2", "GND"),
            ("R1.1", "+5V"),
            ("wire", "R1.2", "D1.1"),          # direct wire: R1 into the LED anode
            ("D1.2", "GND"),
            ("C1.2", "+5V"), ("C1.1", "GND"),
            ("TP1.1", "+5V"),
            # ("U1.4", None)                   would mark a pin no-connect
            # ("tee", "A.1", "B.2", "C.2")     wire A-B tapped down to C, with a junction
            # ("U1.3", "GND", {"style": "label"})   label instead of a port in a dense pin row
        ],
    },
}
