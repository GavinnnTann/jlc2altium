"""Check a converted footprint against the datasheet's land pattern.

The split matters. Reading a recommended-land-pattern table out of a PDF is
a language task and belongs to the model. Measuring pad geometry and diffing
numbers is a deterministic task and belongs here. Mixing them gives you a
confident sentence instead of a number.

Workflow:
    jlcpick template > C1234.datasheet.json   # model fills this from the PDF
    jlcpick verify C1234 --datasheet C1234.datasheet.json

`selfcheck` runs the subset that needs no datasheet at all, and catches the
errors that actually kill boards: flipped pads, duplicate numbers, silk over
copper, zero-size pads.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from .models import Finding, Footprint, Pad

DEFAULT_TOLERANCES = {
    "pad_width": 0.05,
    "pad_height": 0.05,
    "pitch": 0.02,
    "span": 0.15,
    "thermal": 0.10,
    "hole_diameter": 0.05,
}

TEMPLATE = {
    "_comment": [
        "Fill from the RECOMMENDED LAND PATTERN table in the datasheet, not from",
        "the package-outline (body) drawing - they are different tables and the",
        "body dimensions will not match the pads.",
        "All values in millimetres. Omit any field the datasheet does not give;",
        "omitted fields are skipped, not failed.",
    ],
    "mpn": "",
    "package": "",
    "datasheet_url": "",
    "datasheet_page": None,
    "pad_count": None,
    "pitch": None,
    "pad_width": None,
    "pad_height": None,
    "span_outer": None,
    "span_inner": None,
    "thermal_pad_width": None,
    "thermal_pad_height": None,
    "hole_diameter": None,
}


def _cluster(values: list[float], tol: float = 0.1) -> list[list[float]]:
    if not values:
        return []
    groups: list[list[float]] = [[sorted(values)[0]]]
    for v in sorted(values)[1:]:
        if abs(v - groups[-1][-1]) <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def _segment_hits_pad(seg: tuple[float, float, float, float], pad: Pad) -> bool:
    """Liang-Barsky: does the line segment cross the pad's bounding rectangle?

    Pad rotation is ignored, so this is slightly conservative for rotated
    pads - it can flag a near-miss, never miss a real overlap.
    """
    x1, y1, x2, y2 = seg
    hw, hh = pad.width / 2, pad.height / 2
    rx1, ry1 = pad.x - hw, pad.y - hh
    rx2, ry2 = pad.x + hw, pad.y + hh

    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - rx1), (dx, rx2 - x1), (-dy, y1 - ry1), (dy, ry2 - y1)):
        if abs(p) < 1e-12:
            if q < 0:
                return False          # parallel and outside this slab
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def _signal_pads(pads: list[Pad]) -> tuple[list[Pad], list[Pad]]:
    """Split perimeter/signal pads from oversized thermal pads.

    A pad whose area is more than 4x the median is treated as thermal.
    Crude, but it holds for every QFN/DFN/SOIC-EP in practice and it is
    reported separately so you can see the call it made.
    """
    if len(pads) < 3:
        return pads, []
    areas = [max(p.width * p.height, 1e-9) for p in pads]
    median = statistics.median(areas)
    signal, thermal = [], []
    for pad, area in zip(pads, areas):
        (thermal if area > 4 * median else signal).append(pad)
    return (signal, thermal) if signal else (pads, [])


def summarize(fp: Footprint) -> dict[str, Any]:
    """Derive the dimensions a datasheet land-pattern table actually quotes."""
    pads = fp.pads
    if not pads:
        return {"lcsc": fp.lcsc, "pad_count": 0, "error": "no pads"}

    signal, thermal = _signal_pads(pads)

    sizes: dict[tuple[float, float], int] = {}
    for p in signal:
        key = (round(p.width, 3), round(p.height, 3))
        sizes[key] = sizes.get(key, 0) + 1
    dominant = max(sizes.items(), key=lambda kv: kv[1])[0] if sizes else (0.0, 0.0)

    row_groups = _cluster([p.y for p in signal])
    col_groups = _cluster([p.x for p in signal])

    # Pitch is measured along whichever axis the pads are actually spread out on.
    def pitch_along(coords: list[float]) -> float | None:
        uniq = sorted({round(c, 3) for c in coords})
        if len(uniq) < 2:
            return None
        deltas = [b - a for a, b in zip(uniq, uniq[1:]) if b - a > 1e-6]
        return round(min(deltas), 4) if deltas else None

    pitch_x = pitch_along([p.x for p in signal])
    pitch_y = pitch_along([p.y for p in signal])
    if len(row_groups) <= len(col_groups):
        pitch = pitch_x if pitch_x is not None else pitch_y
    else:
        pitch = pitch_y if pitch_y is not None else pitch_x

    def spans(axis: str) -> tuple[float | None, float | None]:
        if axis == "y":
            lo = [p.y - p.height / 2 for p in signal]
            hi = [p.y + p.height / 2 for p in signal]
            groups = row_groups
            half = {id(p): p.height / 2 for p in signal}
            centers = {id(p): p.y for p in signal}
        else:
            lo = [p.x - p.width / 2 for p in signal]
            hi = [p.x + p.width / 2 for p in signal]
            groups = col_groups
            half = {id(p): p.width / 2 for p in signal}
            centers = {id(p): p.x for p in signal}
        if not lo:
            return None, None
        outer = round(max(hi) - min(lo), 4)
        inner = None
        if len(groups) >= 2:
            low_c, high_c = groups[0][-1], groups[-1][0]
            low_pads = [p for p in signal if abs(centers[id(p)] - low_c) <= 0.1]
            high_pads = [p for p in signal if abs(centers[id(p)] - high_c) <= 0.1]
            if low_pads and high_pads:
                low_edge = max(centers[id(p)] + half[id(p)] for p in low_pads)
                high_edge = min(centers[id(p)] - half[id(p)] for p in high_pads)
                if high_edge > low_edge:
                    inner = round(high_edge - low_edge, 4)
        return outer, inner

    span_y_outer, span_y_inner = spans("y")
    span_x_outer, span_x_inner = spans("x")

    # The span a datasheet quotes (the "Z" dimension) is measured ACROSS the
    # pad rows, i.e. along whichever axis has FEWER clusters. A SOT-23-6 has
    # two y-clusters and three x-clusters, so its span is the y one.
    if len(row_groups) < len(col_groups):
        span_outer, span_inner = span_y_outer, span_y_inner
    elif len(col_groups) < len(row_groups):
        span_outer, span_inner = span_x_outer, span_x_inner
    else:
        # Square-ish arrangement (QFN): no single answer, quote the larger.
        if (span_y_outer or 0) >= (span_x_outer or 0):
            span_outer, span_inner = span_y_outer, span_y_inner
        else:
            span_outer, span_inner = span_x_outer, span_x_inner

    th_w = round(max((p.width for p in thermal), default=0.0), 4) or None
    th_h = round(max((p.height for p in thermal), default=0.0), 4) or None
    holes = [p.hole_diameter for p in pads if p.is_through_hole]

    return {
        "lcsc": fp.lcsc,
        "name": fp.name,
        "pad_count": len(pads),
        "signal_pad_count": len(signal),
        "thermal_pad_count": len(thermal),
        "pitch": pitch,
        "pitch_x": pitch_x,
        "pitch_y": pitch_y,
        "pad_width": dominant[0],
        "pad_height": dominant[1],
        "pad_size_variants": len(sizes),
        "span_outer": span_outer,
        "span_inner": span_inner,
        "span_x_outer": span_x_outer,
        "span_y_outer": span_y_outer,
        "thermal_pad_width": th_w,
        "thermal_pad_height": th_h,
        "hole_diameter": round(min(holes), 4) if holes else None,
        "rows": len(row_groups),
        "columns": len(col_groups),
        "silk_bbox": list(fp.silk_bbox) if fp.silk_bbox else None,
    }


def selfcheck(fp: Footprint) -> list[Finding]:
    """Checks that need no datasheet. Run these on everything you import."""
    out: list[Finding] = []
    pads = fp.pads

    if not pads:
        return [Finding("pads", None, 0, status="fail", note="footprint has no pads")]

    numbers = [p.number for p in pads]
    dupes = {n for n in numbers if numbers.count(n) > 1 and n not in ("", "?")}
    if dupes:
        out.append(
            Finding(
                "pad_numbers",
                "unique",
                f"duplicates: {', '.join(sorted(dupes))}",
                status="fail",
                note="duplicate pad numbers short nets together silently",
            )
        )

    layers = {p.layer for p in pads}
    if "top" in layers and "bottom" in layers:
        out.append(
            Finding(
                "pad_layers",
                "one side",
                ", ".join(sorted(layers)),
                status="fail",
                note="pads split across both copper layers - footprint was probably "
                "flipped and left part of its copper behind",
            )
        )

    zero = [p.number for p in pads if p.width <= 0 or p.height <= 0]
    if zero:
        out.append(
            Finding(
                "pad_size",
                "> 0",
                f"zero-size pads: {', '.join(zero)}",
                status="fail",
            )
        )

    numeric = sorted(int(n) for n in numbers if n.isdigit())
    if numeric and numeric != list(range(1, len(numeric) + 1)):
        missing = sorted(set(range(1, max(numeric) + 1)) - set(numeric))
        if missing:
            out.append(
                Finding(
                    "pad_numbering",
                    f"1..{max(numeric)}",
                    f"missing: {missing}",
                    status="warn",
                    note="gaps are legitimate for some packages (NC pins), "
                    "but check against the datasheet pin table",
                )
            )

    if fp.silk_segments:
        hit = sorted({
            p.number
            for seg in fp.silk_segments
            for p in pads
            if _segment_hits_pad(seg, p)
        })
        if hit:
            out.append(
                Finding(
                    "silkscreen",
                    "clear of pads",
                    f"crosses pad(s): {', '.join(hit)}",
                    status="warn",
                    note="silk printed over solderable area; JLC clips it, so your "
                    "pin 1 marker may vanish on the real board",
                )
            )
    elif not fp.silk_bbox:
        out.append(
            Finding(
                "silkscreen",
                "present",
                "none",
                status="warn",
                note="no silk outline - check there is a pin 1 marker before assembly",
            )
        )

    if not out:
        out.append(Finding("selfcheck", "clean", "clean", status="ok"))
    return out


def compare(
    summary: dict[str, Any],
    datasheet: dict[str, Any],
    tolerances: dict[str, float] | None = None,
) -> list[Finding]:
    tol = {**DEFAULT_TOLERANCES, **(tolerances or {})}
    out: list[Finding] = []

    for field in (
        "pad_count",
        "pitch",
        "pad_width",
        "pad_height",
        "span_outer",
        "span_inner",
        "thermal_pad_width",
        "thermal_pad_height",
        "hole_diameter",
    ):
        expected = datasheet.get(field)
        if expected in (None, "", []):
            continue
        actual = summary.get(field)
        if actual is None:
            out.append(
                Finding(field, expected, None, status="warn",
                        note="not measurable from the converted footprint")
            )
            continue
        try:
            expected_f, actual_f = float(expected), float(actual)
        except (TypeError, ValueError):
            out.append(Finding(field, expected, actual, status="info"))
            continue

        if field == "pad_count":
            status = "ok" if expected_f == actual_f else "fail"
            out.append(
                Finding(field, expected_f, actual_f, delta=actual_f - expected_f,
                        status=status)
            )
            continue

        limit = tol.get(field) or tol.get(field.split("_")[0]) or 0.05
        delta = round(actual_f - expected_f, 4)
        if abs(delta) <= limit:
            status = "ok"
        elif abs(delta) <= limit * 2:
            status = "warn"
        else:
            status = "fail"
        out.append(
            Finding(field, expected_f, actual_f, delta=delta,
                    tolerance=limit, status=status)
        )

    if not out:
        out.append(
            Finding("datasheet", None, None, status="info",
                    note="datasheet file had no comparable numeric fields")
        )
    return out


def load_datasheet(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return {k: v for k, v in data.items() if not k.startswith("_")}


def template_json() -> str:
    return json.dumps(TEMPLATE, indent=2)


def verdict(findings: list[Finding]) -> str:
    statuses = {f.status for f in findings}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"
