"""Parse a synthetic EasyEDA payload, so record handling is tested offline.

Field orders below follow easyeda2kicad. If EasyEDA ever changes them this
is the test that should fail first.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jlcpick import easyeda
from jlcpick.verify import summarize

U = 1 / (10 * 0.0254)          # mm -> EasyEDA units (1 unit = 0.254 mm)
OX, OY = 4000.0, 3000.0        # package origin in EasyEDA canvas coords


def pad(num, x_mm, y_mm, w_mm, h_mm, layer=1, hole=0.0):
    """PAD~shape~x~y~w~h~layer~net~number~hole_r~points~rot~id~hlen~hpt~plated~locked"""
    return (
        f"PAD~RECT~{OX + x_mm * U}~{OY + y_mm * U}~{w_mm * U}~{h_mm * U}"
        f"~{layer}~~{num}~{hole * U / 2}~~0~gge{num}~0~~Y~0"
    )


def track(x1, y1, x2, y2, layer=3):
    """TRACK~stroke~layer~net~x1 y1 x2 y2~id~locked"""
    pts = f"{OX + x1 * U} {OY + y1 * U} {OX + x2 * U} {OY + y2 * U}"
    return f"TRACK~1~{layer}~~{pts}~gget~0"


SVGNODE = "SVGNODE~" + json.dumps({
    "gId": "g1", "nodeName": "svg", "nodeType": 1,
    "attrs": {"c_etype": "outline3D", "c_origin": f"{OX},{OY}",
              "c_rotation": "0,0,0", "uuid": "deadbeefcafe", "title": "SOT-23-6",
              "c_width": "2.9", "c_height": "1.1", "z": "0"},
})


def payload():
    """A SOT-23-6: pitch 0.95, pads 0.6 x 1.1, rows at y = +/-1.1."""
    shapes = []
    for i, x in enumerate((-0.95, 0.0, 0.95), start=1):
        shapes.append(pad(i, x, 1.1, 0.6, 1.1))      # EasyEDA is Y-down
    for i, x in enumerate((0.95, 0.0, -0.95), start=4):
        shapes.append(pad(i, x, -1.1, 0.6, 1.1))
    shapes += [track(-1.6, -1.75, 1.6, -1.75), track(-1.6, 1.75, 1.6, 1.75), SVGNODE]
    return {
        "uuid": "u1", "title": "TEST-PART",
        "dataStr": {"head": {"c_para": {"name": "U1", "package": "SOT-23-6"}},
                    "shape": ["P~show~0~1~~~~0~gge1"]},
        "packageDetail": {
            "title": "SOT-23-6_L2.9-W1.6-P0.95",
            "dataStr": {"head": {"x": OX, "y": OY}, "shape": shapes},
        },
    }


def approx(a, b, tol=2e-3):
    return a is not None and abs(a - b) <= tol


def _patch(monkey_result):
    easyeda.fetch_component = lambda lcsc, timeout=20.0: monkey_result


def test_pads_parse_and_recentre():
    _patch(payload())
    fp = easyeda.footprint("C9999")
    assert len(fp.pads) == 6, len(fp.pads)
    by_num = {p.number: p for p in fp.pads}
    # Pin 1 was at EasyEDA y = +1.1 (Y-down), so it must come out at -1.1.
    assert approx(by_num["1"].x, -0.95), by_num["1"].x
    assert approx(by_num["1"].y, -1.1), by_num["1"].y
    assert approx(by_num["1"].width, 0.6), by_num["1"].width
    assert approx(by_num["1"].height, 1.1), by_num["1"].height
    assert by_num["1"].layer == "top"
    assert not by_num["1"].is_through_hole


def test_silk_segments_parse():
    _patch(payload())
    fp = easyeda.footprint("C9999")
    assert len(fp.silk_segments) == 2, fp.silk_segments
    assert fp.silk_bbox is not None
    assert approx(fp.silk_bbox[0], -1.6, 5e-3), fp.silk_bbox


def test_summary_matches_known_land_pattern():
    _patch(payload())
    s = summarize(easyeda.footprint("C9999"))
    assert s["pad_count"] == 6
    assert approx(s["pitch"], 0.95), s["pitch"]
    assert approx(s["span_outer"], 3.3), s["span_outer"]
    assert approx(s["span_inner"], 1.1), s["span_inner"]


def test_3d_model_uuid_extracted():
    detail = payload()["packageDetail"]
    uuid, title = easyeda._find_model(detail)
    assert uuid == "deadbeefcafe", uuid
    assert title == "SOT-23-6", title
    urls = easyeda.model_urls(uuid)
    assert urls["obj"].endswith("deadbeefcafe") and "3dmodel" in urls["obj"]


def test_through_hole_pad_detected():
    _patch(payload())
    data = payload()
    data["packageDetail"]["dataStr"]["shape"][0] = pad(1, -0.95, 1.1, 1.6, 1.6, layer=11, hole=0.8)
    _patch(data)
    fp = easyeda.footprint("C9999")
    p1 = next(p for p in fp.pads if p.number == "1")
    assert p1.is_through_hole, p1.to_dict()
    assert approx(p1.hole_diameter, 0.8), p1.hole_diameter
    assert p1.layer == "multi", p1.layer


def test_missing_footprint_raises():
    _patch({"uuid": "u", "dataStr": {"shape": []}, "packageDetail": {}})
    try:
        easyeda.footprint("C9999")
    except easyeda.EasyEDAError:
        return
    raise AssertionError("expected EasyEDAError for a record with no footprint")


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
