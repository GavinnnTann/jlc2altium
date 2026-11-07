"""Geometry tests that need no network: a synthetic SOT-23-6 land pattern."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jlcpick.models import Footprint, Pad
from jlcpick.verify import compare, selfcheck, summarize, verdict


def sot23_6() -> Footprint:
    """Pitch 0.95, pads 0.6 x 1.1, rows at y = +/-1.1 (centre to centre 2.2).

    => span_outer = 2.2 + 1.1 = 3.3,  span_inner = 2.2 - 1.1 = 1.1
    """
    pads = []
    for i, x in enumerate((-0.95, 0.0, 0.95), start=1):
        pads.append(Pad(str(i), "RECT", x, -1.1, 0.6, 1.1))
    for i, x in enumerate((0.95, 0.0, -0.95), start=4):
        pads.append(Pad(str(i), "RECT", x, 1.1, 0.6, 1.1))
    # Two silk lines running outboard of both pad rows - clear of copper.
    silk = [(-1.6, -1.75, 1.6, -1.75), (-1.6, 1.75, 1.6, 1.75)]
    return Footprint(lcsc="CTEST", name="SOT-23-6", pads=pads,
                     silk_segments=silk,
                     silk_bbox=(-1.6, -1.75, 1.6, 1.75))


def qfn_with_thermal() -> Footprint:
    pads = [Pad(str(i + 1), "RECT", -2.0 + 0.5 * i, -2.3, 0.3, 0.8) for i in range(8)]
    pads.append(Pad("9", "RECT", 0.0, 0.0, 3.0, 3.0))  # exposed pad
    return Footprint(lcsc="CTEST2", name="QFN-8-EP", pads=pads)


def approx(a, b, tol=1e-3):
    return a is not None and abs(a - b) <= tol


def test_summarize_sot23():
    s = summarize(sot23_6())
    assert s["pad_count"] == 6, s
    assert approx(s["pitch"], 0.95), s["pitch"]
    assert approx(s["pad_width"], 0.6), s["pad_width"]
    assert approx(s["pad_height"], 1.1), s["pad_height"]
    assert approx(s["span_outer"], 3.3), s["span_outer"]
    assert approx(s["span_inner"], 1.1), s["span_inner"]
    assert s["rows"] == 2 and s["columns"] == 3, s


def test_thermal_pad_split():
    s = summarize(qfn_with_thermal())
    assert s["signal_pad_count"] == 8, s
    assert s["thermal_pad_count"] == 1, s
    assert approx(s["thermal_pad_width"], 3.0), s


def test_selfcheck_clean():
    findings = selfcheck(sot23_6())
    assert verdict(findings) == "ok", [f.to_dict() for f in findings]


def test_selfcheck_catches_duplicates_and_split_layers():
    fp = sot23_6()
    fp.pads[1].number = "1"
    fp.pads[2].layer = "bottom"
    findings = selfcheck(fp)
    fields = {f.field: f.status for f in findings}
    assert fields.get("pad_numbers") == "fail", fields
    assert fields.get("pad_layers") == "fail", fields


def test_compare_pass_and_fail():
    s = summarize(sot23_6())
    ok = compare(s, {"pitch": 0.95, "pad_width": 0.6, "span_outer": 3.3, "pad_count": 6})
    assert verdict(ok) == "ok", [f.to_dict() for f in ok]

    bad = compare(s, {"pitch": 0.65, "pad_count": 6})   # wrong pitch: SOT-363 values
    assert verdict(bad) == "fail", [f.to_dict() for f in bad]
    pitch = next(f for f in bad if f.field == "pitch")
    assert approx(pitch.delta, 0.30), pitch.to_dict()


def test_silk_crossing_a_pad_is_flagged():
    fp = sot23_6()
    # Drag a silk line straight through pin 1.
    fp.silk_segments.append((-0.95, -2.0, -0.95, 0.0))
    findings = selfcheck(fp)
    silk = [f for f in findings if f.field == "silkscreen"]
    assert silk and silk[0].status == "warn", [f.to_dict() for f in findings]
    assert "1" in str(silk[0].actual), silk[0].to_dict()


def test_silk_clear_of_pads_is_not_flagged():
    findings = selfcheck(sot23_6())
    assert not [f for f in findings if f.field == "silkscreen"], \
        [f.to_dict() for f in findings]


def test_qfn_span_uses_larger_axis():
    s = summarize(qfn_with_thermal())
    assert s["span_outer"] is not None


def test_missing_datasheet_fields_are_skipped():
    s = summarize(sot23_6())
    out = compare(s, {"pitch": 0.95})
    assert len(out) == 1 and out[0].field == "pitch"


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
