"""Offline tests of jlc_fab_export: the board dump, BOM / CPL rules, the
rotation log, Gerber and Excellon parsing, and the fabrication checks.

No Altium, altium-mcp or network. Inputs are synthetic or copied from Altium
26.11 output (a 27 x 58 mm board with R2 corners, four M2 NPTH holes, two
plated wire pads and a USB-C connector's four plated slots).
"""

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jlc_fab_export as J  # noqa: E402

OPENPYXL_AT_IMPORT = "openpyxl" in sys.modules
IU_PER_MM = 10000 / 0.0254

# --------------------------------------------------------------- Altium 26.11 output

OUTLINE_GM1 = """G04*
G04 #@! TF.GenerationSoftware,Altium Limited,Altium Designer,26.11.1 (5)*
G04 Board_Origin=10000000|10000000*
%FSLAX25Y25*%
%MOIN*%
G70*
G04 #@! TF.SameCoordinates,B27B3879-5DD3-4544-91D9-99AE239BB9F5*
G04 #@! TF.FilePolarity,Positive*
G01*
G75*
%ADD62C,0.00394*%
D62*
X0Y7874D02*
G03*
X7874Y0I7874J0D01*
G01*
Y228346D02*
G03*
X0Y220472I0J-7874D01*
G01*
X106299D02*
G03*
X98425Y228346I-7874J0D01*
G01*
Y0D02*
G03*
X106299Y7874I0J7874D01*
G01*
X0D02*
Y220472D01*
X7874Y228346D02*
X98425D01*
X106299Y7874D02*
Y220472D01*
X7874Y0D02*
X98425D01*
M02*
"""

NPTH_TXT = """M48
;Layer_Color=9474304
;FILE_FORMAT=2:5
INCH,LZ
;TYPE=NON_PLATED
T03F00S00C0.08661
%
T03
X0009843Y0009843
X0096457
X0009843Y0218504
X0096457
M30
"""

PTH_TXT = """M48
;FILE_FORMAT=2:5
INCH,LZ
;TYPE=PLATED
T02F00S00C0.04331
%
T02
X0099213Y007756
Y0089371
M30
"""

SLOTS_TXT = """M48
;FILE_FORMAT=2:5
INCH,LZ
;TYPE=PLATED
T01F00S00C0.02362
%
G90
G05
T01
G00X0032206Y0007637
M15
G01Y0004488
M16
G00X0032206Y0022598
M15
G01Y0019449
M16
G00X006622Y0022598
M15
G01Y0019449
M16
G00X006622Y0007637
M15
G01Y0004488
M16
M17
M30
"""

PTH_PADS = [(25.2, 19.7), (25.2, 22.7)]
SLOT_PADS = [(8.18, 1.54), (16.82, 1.54), (8.18, 5.34), (16.82, 5.34)]
BOARD = (0.0, 0.0, 27.0, 58.0)


# --------------------------------------------------------------- helpers

def iu(mm: float) -> str:
    return str(round(mm * IU_PER_MM))


def comp_line(ref, fp, comment, x, y, rot=0, pads=None, body=None, kind=0, layer="Top Layer"):
    pads = pads or (x - 0.5, y - 0.25, x + 0.5, y + 0.25)
    return "\t".join(["COMP", ref, fp, comment, iu(x), iu(y), str(int(rot * 1000)), layer, str(kind),
                      "2", *map(iu, pads), "1" if body else "0", *map(iu, body or (0, 0, 0, 0))])


def dump(*lines) -> str:
    head = ["OUTLINE\t0\t0\t" + iu(27) + "\t" + iu(58) + "\t8",
            "LAYER\tTop Layer\t1", "LAYER\tL2 GND\t2", "LAYER\tL3 PWR\t3", "LAYER\tBottom Layer\t32"]
    return "\n".join(head + list(lines))


def part(ref, fp="R0402", comment="10k", x=10.0, y=10.0, rot=0.0, lcsc="C25744", pads=None, body=None, side="Top"):
    p = J.Part(ref=ref, footprint=fp, comment=comment, x=x, y=y, rot=rot, side=side, kind="Standard", lcsc=lcsc,
               pads=pads or (x - 0.5, y - 0.25, x + 0.5, y + 0.25), body=body, npads=2)
    return p


def gerber(extent, flashes=()):
    return {"file": "x", "extent": extent, "flashes": list(flashes), "draws": 0, "objects": len(flashes),
            "unit": "inch", "format": "2:5"}


def good_gerbers():
    pads = PTH_PADS + SLOT_PADS
    return {"GTL": gerber((1.0, 1.0, 26.0, 57.0), pads), "G1": gerber((0.4, 0.4, 26.6, 57.6)),
            "G2": gerber((0.4, 0.4, 26.6, 57.6)), "GBL": gerber((2.5, 1.5, 25.2, 55.5), pads),
            "GTO": gerber((0.5, 0.5, 26.5, 57.5)), "GTS": gerber((1.0, 1.0, 26.0, 57.0)),
            "GBS": gerber((2.5, 1.5, 25.2, 55.5)), "GM1": J.parse_gerber(OUTLINE_GM1, "b.GM1")}


def close(a, b, tol=1e-3):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# --------------------------------------------------------------- board dump

def test_dump_reads_parts_in_mm_with_the_pad_centre():
    b = J.parse_dump(dump(
        comp_line("U5", "VFQFN-16", "DRV8876", 9.165, 49.0, rot=90,
                  pads=(7.15, 47.1, 10.95, 50.9), body=(7.655, 47.49, 10.675, 50.51)),
        "HOLE\tJ3\t1\t" + iu(25.2) + "\t" + iu(19.7) + "\t" + iu(1.1) + "\t0\t0\t1",
        "CONN\tGND", "CONN\tGND", "CONN\tVSYS"))
    u = b.parts["U5"]
    assert close((u.x, u.y), (9.165, 49.0)) and u.rot == 90 and u.side == "Top" and u.kind == "Standard"
    assert close(u.centre, (9.05, 49.0)), u.centre
    assert close(b.outline, BOARD) and b.outline_points == 8
    assert b.unrouted == {"GND": 2, "VSYS": 1}
    assert b.holes[0]["plated"] and abs(b.holes[0]["size"] - 1.1) < 1e-3


def test_dump_error_stops_the_run():
    try:
        J.parse_dump("ERROR\tthe PCB is not open in Altium")
    except SystemExit as exc:
        assert "not open" in str(exc)
    else:
        raise AssertionError("no SystemExit")


def test_copper_layers_get_v7_ids_and_gerber_extensions():
    b = J.Board(BOARD, 8, [("Top", 1), ("In1", 2), ("In2", 3), ("Plane", 39), ("Bottom", 32)], {})
    assert [(e, v7) for _, _, v7, e in b.copper()] == [
        ("GTL", 16777217), ("G1", 16777218), ("G2", 16777219), ("GP1", 16842753), ("GBL", 16842751)]


def test_outline_primitives_are_tracks_and_arcs_on_that_layer_only():
    b = J.Board(BOARD, 8, [], {}, free=[(1, "Keep Out Layer", (0, 0, 1, 1)), (4, "Mechanical 1", (0, 0, 27, 0)),
                                       (1, "Mechanical Layer 1", (0, 0, 2, 2)), (11, "Multi Layer", BOARD)])
    assert len(b.outline_primitives(1)) == 2
    assert b.outline_primitives(2) == []


# --------------------------------------------------------------- BOM / CPL rules

def test_classify_excludes_no_bom_net_ties_and_dnp():
    b = J.Board(BOARD, 8, [], {r: part(r) for r in ("R1", "TP1", "NT1", "C14", "R2", "R3")})
    sch = {
        "R1": {"comment": "10k", "footprint": "R0402", "params": {"Component Kind": "Standard", "LCSC Part Number": "c25744"}},
        "TP1": {"comment": "BAT+", "footprint": "R0402", "params": {"Component Kind": "Standard (No BOM)"}},
        "NT1": {"comment": "NetTie", "footprint": "R0402", "params": {"Component Kind": "Net Tie"}},
        "C14": {"comment": "22pF DNP", "footprint": "R0402", "params": {"Component Kind": "Standard", "LCSC Part Number": "C1555"}},
        "R2": {"comment": "1k", "footprint": "R0402", "params": {"Component Kind": "Standard"}},
        "R3": {"comment": "DNPX 1k", "footprint": "R0402", "params": {"Component Kind": "Standard", "LCSC Part Number": "C11702"}},
        "R99": {"comment": "1k", "footprint": "R0402", "params": {}},
    }
    blockers = J.classify(b, sch, dict(J.DEFAULTS))
    p = b.parts
    assert p["R1"].excluded == "" and p["R1"].lcsc == "C25744"
    assert "No BOM" in p["TP1"].excluded and "Net Tie" in p["NT1"].excluded
    assert p["C14"].excluded.startswith("DNP")
    assert p["R3"].excluded == "", "DNP must match a whole word"
    assert any(x.startswith("R2 (1k) has no LCSC Part Number") for x in blockers), blockers
    assert any("R99" in x and "not on the PCB" in x for x in blockers), blockers


def test_classify_flags_footprint_changed_since_the_eco():
    b = J.Board(BOARD, 8, [], {"R1": part("R1", fp="R0402")})
    sch = {"R1": {"comment": "10k", "footprint": "R0603", "params": {"LCSC Part Number": "C25744"}}}
    assert any("footprint differs" in x for x in J.classify(b, sch, dict(J.DEFAULTS)))


def test_bom_groups_by_part_number_in_natural_order():
    placed = [part("C10", "C0402", "100nF", lcsc="C1525"), part("R1", lcsc="C25744"),
              part("C2", "C0402", "100nF", lcsc="C1525"), part("C1", "C0402", "100nF", lcsc="C1525")]
    assert J.bom_rows(placed) == [["100nF", "C1,C2,C10", "C0402", "C1525"], ["10k", "R1", "R0402", "C25744"]]


def test_bom_conflict_when_one_part_number_has_two_comments():
    rows = J.bom_rows([part("R1", comment="10k"), part("R2", comment="10K 1%")])
    assert len(rows) == 2 and len(J.bom_conflicts(rows)) == 1


def test_rotation_log_reads_rows_and_skips_header_and_prose():
    text = "\n".join([
        "# JLC CPL rotation corrections", "",
        "- **Rotation (deg)**: added to the Altium rotation",
        "| Footprint or LCSC | Rotation (deg) | dX (mm) | dY (mm) | Board / order | Date | Notes |",
        "|---|---|---|---|---|---|---|",
        "| `SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR` | 180 | | | RevB / Y123 | 2026-11-20 | JLC turned it |",
        "| C456012 | 0 | 0.1 | -0.2 | RevB | 2026-11-20 | centre shift |",
        "| LED0603-RD | 0 | | | RevB | 2026-11-20 | came out right |",
        "| R0402 | ? | | | | | not checked yet |"])
    rows = J.read_rotation_log(text)
    assert [(r["key"], r["rot"], r["dx"], r["dy"]) for r in rows] == [
        ("SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR", 180.0, 0.0, 0.0), ("C456012", 0.0, 0.1, -0.2), ("LED0603-RD", 0.0, 0.0, 0.0)]


def test_cpl_uses_the_pad_centre_and_logged_corrections():
    u5 = part("U5", "QFN", "DRV8876", 9.165, 49.0, rot=90, lcsc="C1852100", pads=(7.15, 47.1, 10.95, 50.9))
    r1 = part("R1", "R0402", x=10.0, y=10.0, rot=90)
    d3 = part("D3", "LED0603-RD", "GREEN", x=5.0, y=5.0, lcsc="C12624")
    c1 = part("C1", "C0603", "10uF", x=2.0, y=2.0, rot=270, lcsc="C19702")
    log = [{"key": "QFN", "rot": 90.0, "dx": 0.0, "dy": 0.0},
           {"key": "C1852100", "rot": 180.0, "dx": 0.0, "dy": 0.0},      # LCSC row wins over the footprint row
           {"key": "R0402", "rot": 0.0, "dx": 0.1, "dy": 0.0},          # dX in the part's own frame
           {"key": "LED0603-RD", "rot": 0.0, "dx": 0.0, "dy": 0.0}]     # confirmed as exported
    rows, applied, unconfirmed, off_centre = J.cpl_rows([u5, r1, d3, c1], log)
    assert rows[0] == ["U5", "9.050mm", "49.000mm", "Top", 270], rows[0]
    assert rows[1] == ["R1", "10.000mm", "10.100mm", "Top", 90], rows[1]
    assert rows[2] == ["D3", "5.000mm", "5.000mm", "Top", 0]
    assert rows[3][4] == 270 and unconfirmed == {"C0603": ["C1"]}
    assert len(applied) == 2 and [o[0] for o in off_centre] == ["U5"]


def test_edge_clearance_lists_parts_inside_the_band():
    j1 = part("J1", "USB-C", "USB-C", 12.5, 2.6, pads=(7.63, 0.59, 17.37, 6.29), body=(8.03, 0.0, 16.97, 6.8))
    l1 = part("L1", "IND", "2.2uH", 24.5, 41.25, pads=(22.3, 39.05, 26.7, 43.45))
    r1 = part("R1", x=13.5, y=29.0)
    edge = J.edge_clearance([r1, l1, j1], BOARD, 2.5)
    assert [(round(d, 2), side, ref) for d, side, ref, _ in edge] == [(0.0, "bottom", "J1"), (0.3, "right", "L1")]


def test_unit_price_picks_the_tier():
    tiers = [(1, 199, 0.01), (200, None, 0.005)]
    assert J.unit_price(tiers, 10) == 0.01 and J.unit_price(tiers, 500) == 0.005 and J.unit_price([], 5) is None


# --------------------------------------------------------------- Gerber / Excellon

def test_gerber_comment_guid_is_not_a_d_code():
    # "5DD3" inside a G04 GUID once read as a D03 flash at (0, 0)
    info = J.parse_gerber("G04 #@! TF.SameCoordinates,B27B3879-5DD3-4544*\n%FSLAX25Y25*%\n%MOIN*%\nG01*\nM02*\n")
    assert info["objects"] == 0 and "extent" not in info


def test_gerber_outline_extent_matches_the_board():
    info = J.parse_gerber(OUTLINE_GM1, "b.GM1")
    assert close(info["extent"], BOARD) and info["draws"] == 8 and info["unit"] == "inch" and info["format"] == "2:5"


ROUND_GM1 = """%FSLAX25Y25*%
%MOIN*%
G75*
%ADD10C,0.00394*%
D10*
X118110Y59055D02*
G03*
X0Y59055I-59055J0D01*
G03*
X118110Y59055I59055J0D01*
G01*
M02*
"""


def test_gerber_arc_bulges_count_so_a_round_board_passes():
    # a 30 mm round board drawn as two half circles: the endpoints alone span 30 x 0 mm
    info = J.parse_gerber(ROUND_GM1, "r.GM1")
    assert close(info["extent"], (0.0, 0.0, 30.0, 30.0))
    r = J.check_fab_files({"GM1": info}, [], (0.0, 0.0, 30.0, 30.0), [], "GM1")
    assert not [b for b in r["blockers"] if "outline" in b]
    # clockwise, and a full circle given by start == end
    cw = J.parse_gerber(ROUND_GM1.replace("G03", "G02"), "r.GM1")
    assert close(cw["extent"], (0.0, 0.0, 30.0, 30.0))
    full = J.parse_gerber("%FSLAX25Y25*%\n%MOIN*%\nG75*\nX118110Y59055D02*\nG03X118110Y59055I-59055J0D01*\nM02*\n")
    assert close(full["extent"], (0.0, 0.0, 30.0, 30.0))


def test_excellon_round_holes_in_mm():
    npth = J.parse_excellon(NPTH_TXT, "b-RoundHoles-NonPlated.TXT")
    assert not npth["plated"] and abs(npth["tools"][3] - 2.2) < 1e-3
    assert [tuple(round(v, 3) for v in h[1:]) for h in npth["hits"]] == [(2.5, 2.5), (24.5, 2.5), (2.5, 55.5), (24.5, 55.5)]
    pth = J.parse_excellon(PTH_TXT, "b-RoundHoles-Plated.TXT")
    assert pth["plated"] and [tuple(round(v, 3) for v in h[1:]) for h in pth["hits"]] == PTH_PADS


def test_excellon_routed_slots_have_ends_and_width():
    d = J.parse_excellon(SLOTS_TXT, "b-SlotHoles-Plated.TXT")
    assert d["plated"] and d["hits"] == [] and len(d["slots"]) == 4
    tool, p1, p2 = d["slots"][0]
    assert abs(d["tools"][tool] - 0.6) < 1e-3
    assert close(p1, (8.18, 1.94), 0.01) and close(p2, (8.18, 1.14), 0.01)


def test_excellon_g85_slot_and_trailing_zero_format():
    text = "M48\nMETRIC,TZ\n;FILE_FORMAT=3:3\nT1C1.000\n%\nT1\nX010000Y020000\nX001000Y002000G85X003000Y002000\nM30\n"
    d = J.parse_excellon(text, "x.TXT")
    assert d["unit"] == "mm" and d["hits"] == [(1, 10.0, 20.0)]
    assert d["slots"] == [(1, (1.0, 2.0), (3.0, 2.0))]


def test_fab_check_passes_aligned_files():
    drills = [J.parse_excellon(t, n) for t, n in ((NPTH_TXT, "a"), (PTH_TXT, "b"), (SLOTS_TXT, "c"))]
    res = J.check_fab_files(good_gerbers(), drills, BOARD, ["GTL", "G1", "G2", "GBL"], "GM1")
    assert res["blockers"] == [], res["blockers"]
    assert (res["n_pth"], res["n_slot"], res["n_npth"]) == (2, 4, 4)
    assert res["hole_sizes"] == {"2.20 mm NPTH": 4, "1.10 mm PTH": 2, "0.60 x 1.40 mm slot PTH": 4}


def test_fab_check_flags_a_drill_file_shifted_from_the_copper():
    shifted = PTH_TXT.replace("X0099213", "X0100000")             # +0.2 mm in X
    drills = [J.parse_excellon(NPTH_TXT, "a"), J.parse_excellon(shifted, "b"), J.parse_excellon(SLOTS_TXT, "c")]
    res = J.check_fab_files(good_gerbers(), drills, BOARD, ["GTL", "G1", "G2", "GBL"], "GM1")
    assert any("misaligned" in x and "0.200 mm" in x for x in res["blockers"]), res["blockers"]


def test_fab_check_flags_missing_layers_wrong_outline_and_copper_off_board():
    g = good_gerbers()
    del g["GTS"]
    g["GM1"] = gerber((0.0, 0.0, 27.0, 57.0))
    g["GTL"] = gerber((-0.5, 1.0, 26.0, 57.0), PTH_PADS + SLOT_PADS)
    res = J.check_fab_files(g, [J.parse_excellon(PTH_TXT, "b")], BOARD, ["GTL", "G1", "G2", "GBL"], "GM1")
    text = " | ".join(res["blockers"])
    assert "missing: GTS" in text and "outline GM1 spans" in text and "GTL has copper outside" in text, text


def test_silkscreen_off_the_board_is_a_note_not_a_blocker():
    g = good_gerbers()
    g["GTO"] = gerber((-0.2, -1.06, 26.71, 57.51))
    res = J.check_fab_files(g, [J.parse_excellon(PTH_TXT, "b")], BOARD, ["GTL", "G1", "G2", "GBL"], "GM1")
    assert res["blockers"] == [] and any("1.06 mm outside" in n for n in res["notes"])


def test_outjob_plots_outline_and_copper_but_not_keepout():
    text = J.outjob_text("Board.PcbDoc", r"Project Outputs for Board\JLC\gerber", [16777217, 16777218, 16842751], 1)
    assert "16908289~1" in text and "16777218~1" in text and "16973837" not in text
    assert "GenerateSeparatePlatedNonPlatedFiles=True" in text and text.count("OriginPosition=Relative") == 2
    assert "OutputMedium1=JLC Gerber" in text and "OpenGerberOutput1=0" in text
    assert "RelativeOutputPath1=Project Outputs for Board\\JLC\\gerber\\" in text


# --------------------------------------------------------------- files and report

def test_bom_and_cpl_tables_round_trip():
    rows = [["100nF", "C1,C2", "C0402", "C1525"]]
    with tempfile.TemporaryDirectory() as td:
        x, c = Path(td) / "b.xlsx", Path(td) / "b.csv"
        try:
            J.write_table(rows, ["Comment", "Designator", "Footprint", "LCSC"], x, c, "BOM", [10, 10, 10, 10])
        except SystemExit as exc:                               # no openpyxl: the CSV is still written
            assert "openpyxl" in str(exc)
        with c.open(encoding="utf-8-sig", newline="") as f:
            assert list(csv.reader(f)) == [["Comment", "Designator", "Footprint", "LCSC"]] + rows
        if x.exists():
            from openpyxl import load_workbook
            ws = load_workbook(x).active
            assert ws.title == "BOM" and [list(r) for r in ws.iter_rows(values_only=True)][1] == rows[0]


def test_readme_puts_blockers_first():
    cfg = dict(J.DEFAULTS, name="Demo", pcb=Path("Demo.PcbDoc"), outjob=Path("JLC Fabrication.OutJob"))
    board = J.Board(BOARD, 8, [("Top Layer", 1), ("Bottom Layer", 32)], {})
    text = J.readme_text(J.Package(
        cfg=cfg, board=board, placed=[], excluded=[], bom=[], cpl=[], applied=[], unconfirmed={}, off_centre=[],
        edge=[], log_rows=0, cat={}, cat_date="", fab=None, gerbers={}, drills=[], zipped=[],
        zip_path=Path("Demo_Gerber.zip"), blockers=["3 unrouted connections"], warnings=[], notes=[], log=[],
        drc=None, unsaved=0))
    assert text.index("NOT READY TO ORDER") < text.index("## Upload")
    assert "not generated: --skip-gerbers" in text and "under 70 x 70 mm" in text


def test_import_needs_no_altium_or_openpyxl():
    assert "altium_call" not in sys.modules and "main" not in sys.modules
    assert not OPENPYXL_AT_IMPORT


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
