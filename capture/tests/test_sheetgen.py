"""Wiring and netlist logic of sheetgen, on synthetic pin positions.

No Altium and no altium-mcp needed: read_pins() is the only part of
sheetgen that reads a .SchDoc, and these tests build its output by hand.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sheetgen  # noqa: E402
from sheetgen import netlist_from_conns, pin_of, wiring_ops  # noqa: E402


def comp(*pins):
    """pins: (number, name, (x, y), dir) -> the dict read_pins() returns per part."""
    out = {"pins": {}, "by_name": {}}
    for number, name, hot, d in pins:
        p = {"number": number, "name": name, "hot": hot, "dir": d}
        out["pins"][number] = p
        out["by_name"].setdefault(name, []).append(p)
    return out


def board():
    # R1 horizontal at y=0 (pin 1 left, pin 2 right), D1 to its right on the
    # same line, C1 below the R1-D1 run, U1 with two pins both named "GND".
    return {
        "R1": comp(("1", "A", (0, 0), 2), ("2", "B", (300, 0), 0)),
        "D1": comp(("1", "A", (600, 0), 2), ("2", "K", (900, 0), 0)),
        "C1": comp(("1", "1", (450, -300), 3), ("2", "2", (450, -200), 1)),
        "U1": comp(("1", "VIN", (0, 1000), 2), ("2", "GND", (0, 900), 2),
                   ("3", "GND", (0, 800), 2), ("4", "EN", (300, 1000), 0)),
    }


def test_stub_and_label_points_away_from_the_pin():
    ops = wiring_ops(board(), [("R1.1", "SIG")])
    assert ops[0] == {"op": "add_wire", "points": [[0, 0], [-200.0, 0.0]]}
    assert ops[1]["op"] == "add_net_label" and ops[1]["text"] == "SIG"
    assert (ops[1]["x"], ops[1]["y"]) == (-200.0, 0.0)


def test_grounds_and_rails_become_power_ports():
    ops = wiring_ops(board(), [("D1.2", "GND"), ("R1.1", "+5V"), ("U1.4", "AGND")])
    ports = [o for o in ops if o["op"] == "add_power_port"]
    assert [(p["text"], p["style"]) for p in ports] == [
        ("GND", "power_ground"), ("+5V", "bar"), ("AGND", "signal_ground")]


def test_spec_rails_replace_the_name_pattern():
    ops = wiring_ops(board(), [("R1.1", "+5V"), ("R1.2", "VMAIN")], rails={"VMAIN"})
    kinds = [o["op"] for o in ops if o["op"] != "add_wire"]
    assert kinds == ["add_net_label", "add_power_port"]


def test_forced_label_style_and_stub_length():
    ops = wiring_ops(board(), [("U1.2", "GND", {"style": "label", "stub": 100})])
    assert ops[0]["points"][1] == [-100.0, 900.0]
    assert ops[1]["op"] == "add_net_label"


def test_no_connect():
    assert wiring_ops(board(), [("U1.4", None)]) == [{"op": "add_no_erc", "x": 300, "y": 1000}]


def test_direct_wire_between_aligned_pins():
    ops = wiring_ops(board(), [("wire", "R1.2", "D1.1"), ("wire", "R1.2", "U1.4")])
    assert ops[0]["points"] == [[300, 0], [600, 0]]       # same y
    assert ops[1]["points"] == [[300, 0], [300, 1000]]    # same x


def test_l_route_corner():
    ops = wiring_ops(board(), [("wire", "R1.1", "C1.2")])          # default "hv"
    assert ops[0]["points"] == [[0, 0], [450, 0], [450, -200]]
    ops = wiring_ops(board(), [("wire", "R1.1", "C1.2", "vh")])
    assert ops[0]["points"] == [[0, 0], [0, -200], [450, -200]]


def test_tee_taps_the_run_with_a_junction():
    ops = wiring_ops(board(), [("tee", "R1.2", "D1.1", "C1.2")])
    assert [o["op"] for o in ops] == ["add_wire", "add_wire", "add_wire", "add_junction"]
    assert (ops[3]["x"], ops[3]["y"]) == (450, 0)


def test_tee_needs_aligned_ends():
    try:
        wiring_ops(board(), [("tee", "R1.1", "C1.1", "D1.1")])
    except ValueError:
        return
    raise AssertionError("misaligned tee was accepted")


def test_pins_by_name_and_ambiguous_names():
    assert pin_of(board(), "U1.VIN")["number"] == "1"
    try:
        pin_of(board(), "U1.GND")
    except KeyError as exc:
        assert "not unique" in str(exc)
    else:
        raise AssertionError("ambiguous pin name was accepted")


def test_netlist_joins_wires_into_named_nets():
    nets = netlist_from_conns(board(), [
        ("R1.1", "+5V"), ("wire", "R1.2", "D1.1"), ("D1.2", "GND"),
        ("C1.1", "GND"), ("tee", "R1.2", "D1.1", "C1.2"), ("U1.4", None),
    ])
    assert nets["+5V"] == {"R1.1"}
    assert nets["GND"] == {"D1.2", "C1.1"}
    joined = [s for n, s in nets.items() if n.startswith("(wire")]
    assert joined == [{"R1.2", "D1.1", "C1.2"}]


def test_wire_merges_two_named_nets():
    nets = netlist_from_conns(board(), [("R1.2", "A"), ("D1.1", "B"), ("wire", "R1.2", "D1.1")])
    assert len(nets) == 1 and next(iter(nets.values())) == {"R1.2", "D1.1"}


def test_net_labels_use_pin_numbers():
    nets = netlist_from_conns(board(), [("U1.VIN", "VBAT")])
    assert nets == {"VBAT": {"U1.1"}}


def test_import_needs_no_altium_mcp():
    # Importing sheetgen and wiring must not pull in altium-mcp's reader.
    assert "schdoc_reader" not in sys.modules
    assert callable(sheetgen.read_pins)


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {name}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
