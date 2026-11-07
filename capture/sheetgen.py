"""Generate Altium schematic wiring from measured pin positions.

Workflow per sheet (capture.py drives it):
  1. place parts with altium-mcp edit_schematic (place_component / move_component), save
  2. read_pins(): read the SAVED .SchDoc and get every pin's electrical end
     ("hot" point = Pin.Location + PinLength along the pin, as the altium-mcp
     SCHEMATIC_CONVENTIONS.md requires) and its outward direction
  3. wiring_ops(): turn a connection list into edit_schematic operations
       ("REF.PIN", "NET")            stub + net label (or power port for rails)
       ("REF.PIN", None)            no-ERC marker on the pin
       ("wire", "A.PIN", "B.PIN")   direct wire between two aligned pins
     PIN may be the pin number or the pin name (unique names only).

Pure Python, no Altium needed. read_pins() uses altium-mcp's SchDoc reader;
wiring_ops() and netlist_from_conns() need nothing beyond this file.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SERVER_DIR = os.environ.get("ALTIUM_MCP_SERVER", str(Path.home() / "src" / "altium-mcp" / "server"))

DIRS = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}   # pin orientation -> outward unit vector

# Nets drawn as power ports (global). Everything else gets net labels.
POWER_STYLE = {"GND": "power_ground", "AGND": "signal_ground", "PGND": "power_ground", "DGND": "power_ground"}
# Supply rails drawn as bar ports. A spec can replace this set (capture.py
# passes spec.RAILS); otherwise names that look like rails qualify.
RAILS: set[str] = set()
RAIL_PATTERN = re.compile(r"^(\+|V(CC|DD|BUS|SYS|BAT|IN|BOOST|MOT|M)\b|VBAT|VSYS|VBUS)")


def is_rail(net: str, rails: set | None = None) -> bool:
    rails = RAILS if rails is None else rails
    return net in rails if rails else bool(RAIL_PATTERN.match(net))


def read_pins(path: str | Path) -> dict:
    """{designator: {"pins": {number: pin}, "by_name": {name: [pin]}, "location": (x, y)}}

    pin = {"number", "name", "hot": (x, y), "dir": 0..3}
    """
    if SERVER_DIR not in sys.path:
        sys.path.insert(0, SERVER_DIR)
    from schdoc_reader import coord, read_schdoc  # altium-mcp, standard library only

    recs = read_schdoc(path)
    comps = {}
    for i, r in enumerate(recs):
        if r and r.get("RECORD") == "1":
            comps[i] = {"index": i, "designator": "?", "part": int(r.get("CURRENTPARTID", 1) or 1),
                        "mode": int(r.get("DISPLAYMODE", 0) or 0),
                        "location": (coord(r, "LOCATION.X"), coord(r, "LOCATION.Y")),
                        "pins": {}, "by_name": {}, "lib": r.get("LIBREFERENCE", "")}
    for i, r in enumerate(recs):
        if not r or "OWNERINDEX" not in r:
            continue
        owner = comps.get(int(r["OWNERINDEX"]) + 1)
        if owner is None:
            continue
        kind = r.get("RECORD")
        if kind == "34":
            owner["designator"] = r.get("TEXT", "?")
        elif kind == "2":
            part = int(r.get("OWNERPARTID", -1) or -1)
            mode = int(r.get("OWNERPARTDISPLAYMODE", 0) or 0)
            if part not in (-1, 0, owner["part"]) or mode != owner["mode"]:
                continue
            cong = int(r.get("PINCONGLOMERATE", 0) or 0)
            if cong & 0x04:          # hidden pin
                continue
            orient = cong & 3
            base = (coord(r, "LOCATION.X"), coord(r, "LOCATION.Y"))
            length = coord(r, "PINLENGTH")
            d = DIRS[orient]
            hot = (round(base[0] + d[0] * length, 4), round(base[1] + d[1] * length, 4))
            pin = {"number": r.get("DESIGNATOR", ""), "name": r.get("NAME", ""), "hot": hot, "dir": orient}
            owner["pins"][pin["number"]] = pin
            owner["by_name"].setdefault(pin["name"], []).append(pin)
    return {c["designator"]: c for c in comps.values()}


def pin_of(parts: dict, ref: str) -> dict:
    des, _, key = ref.partition(".")
    comp = parts.get(des)
    if comp is None:
        raise KeyError(f"{ref}: no component {des} on the sheet")
    if key in comp["pins"]:
        return comp["pins"][key]
    named = comp["by_name"].get(key, [])
    if len(named) == 1:
        return named[0]
    if not named:
        raise KeyError(f"{ref}: {des} has no pin {key!r} (pins: {sorted(comp['pins'])})")
    raise KeyError(f"{ref}: pin name {key!r} is not unique on {des}; use the number")


def _label_ops(end, d, net):
    # Middle-justified label anchored on the wire end, reading outward like a
    # port: accepted by altium-mcp check_schematic and keeps the stub short.
    if d == 0:
        return [{"op": "add_net_label", "x": end[0], "y": end[1], "text": net, "orientation": 0, "justification": 3}]
    if d == 2:
        return [{"op": "add_net_label", "x": end[0], "y": end[1], "text": net, "orientation": 0, "justification": 5}]
    if d == 1:
        return [{"op": "add_net_label", "x": end[0], "y": end[1], "text": net, "orientation": 1, "justification": 3}]
    return [{"op": "add_net_label", "x": end[0], "y": end[1], "text": net, "orientation": 1, "justification": 5}]


def _port_ops(end, d, net):
    style = POWER_STYLE.get(net, "bar")
    return [{"op": "add_power_port", "x": end[0], "y": end[1], "text": net, "style": style,
             "orientation": d, "show_net_name": True}]


def wiring_ops(parts: dict, conns: list, stub: float = 200.0, stub_overrides: dict | None = None,
               rails: set | None = None) -> list:
    """Connection forms:
         ("REF.PIN", "NET")                   stub (200 mil) + label, or power port for GND/AGND/rails
         ("REF.PIN", "NET", {"style": "label"|"port", "stub": mils})
         ("REF.PIN", None)                    no-ERC marker
         ("wire", "A.PIN", "B.PIN"[, "hv"|"vh"])   direct wire (L-route if not aligned)
         ("tee", "A.PIN", "B.PIN", "C.PIN")   wire A-B (same y or same x) tapped at C's
                                              x (or y) down/up to C, with a junction
    """
    ops = []
    stub_overrides = stub_overrides or {}
    for c in conns:
        if c[0] == "tee":
            a, b, t = (pin_of(parts, r)["hot"] for r in c[1:4])
            if a[1] == b[1]:          # horizontal run, vertical tap
                tap = (t[0], a[1])
            elif a[0] == b[0]:        # vertical run, horizontal tap
                tap = (a[0], t[1])
            else:
                raise ValueError(f"tee {c}: {c[1]} and {c[2]} are not aligned")
            ops.append({"op": "add_wire", "points": [list(a), list(tap)]})
            ops.append({"op": "add_wire", "points": [list(tap), list(b)]})
            ops.append({"op": "add_wire", "points": [list(tap), list(t)]})
            ops.append({"op": "add_junction", "x": tap[0], "y": tap[1]})
            continue
        if c[0] == "wire":
            a, b = pin_of(parts, c[1])["hot"], pin_of(parts, c[2])["hot"]
            if a[0] == b[0] or a[1] == b[1]:
                pts = [list(a), list(b)]
            else:
                via = c[3] if len(c) > 3 else "hv"
                mid = (b[0], a[1]) if via == "hv" else (a[0], b[1])
                pts = [list(a), list(mid), list(b)]
            ops.append({"op": "add_wire", "points": pts})
            continue
        ref, net = c[0], c[1]
        p = pin_of(parts, ref)
        if net is None:
            ops.append({"op": "add_no_erc", "x": p["hot"][0], "y": p["hot"][1]})
            continue
        opt = c[2] if len(c) > 2 and isinstance(c[2], dict) else {}
        L = stub_overrides.get(ref, opt.get("stub", c[2] if len(c) > 2 and not isinstance(c[2], dict) else stub))
        d = p["dir"]
        dx, dy = DIRS[d]
        end = (p["hot"][0] + dx * L, p["hot"][1] + dy * L)
        ops.append({"op": "add_wire", "points": [list(p["hot"]), list(end)]})
        style = opt.get("style") or ("port" if (net in POWER_STYLE or is_rail(net, rails)) else "label")
        if style == "port":
            ops += _port_ops(end, d, net)
        else:
            ops += _label_ops(end, d, net)
    return ops


def netlist_from_conns(parts: dict, conns: list) -> dict:
    """{net: set("REF.PINNUMBER")} implied by a connection list (for the audit)."""
    nets: dict[str, set] = {}
    wires = []
    for c in conns:
        if c[0] == "wire":
            wires.append((pin_of(parts, c[1]), c[1].split(".")[0], pin_of(parts, c[2]), c[2].split(".")[0]))
            continue
        if c[0] == "tee":
            pa, pb, pc = (pin_of(parts, r) for r in c[1:4])
            da, db, dc = (r.split(".")[0] for r in c[1:4])
            wires.append((pa, da, pb, db))
            wires.append((pa, da, pc, dc))
            continue
        if c[1] is None:
            continue
        p = pin_of(parts, c[0])
        nets.setdefault(c[1], set()).add(f"{c[0].split('.')[0]}.{p['number']}")
    # a direct wire joins its two pins into whichever net either belongs to
    for pa, da, pb, db in wires:
        a, b = f"{da}.{pa['number']}", f"{db}.{pb['number']}"
        na = next((n for n, s in nets.items() if a in s), None)
        nb = next((n for n, s in nets.items() if b in s), None)
        if na and nb and na != nb:
            nets[na] |= nets.pop(nb)
        elif na:
            nets[na].add(b)
        elif nb:
            nets[nb].add(a)
        else:
            nets.setdefault(f"(wire {a}-{b})", set()).update({a, b})
    return nets
