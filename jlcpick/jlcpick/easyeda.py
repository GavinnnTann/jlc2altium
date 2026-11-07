"""Probe EasyEDA/LCSC for existing symbol, footprint and 3D model.

This is the module that answers the question that actually costs you time:
"if I pick this part, do I have to draw it?"

Endpoint and record formats follow easyeda2kicad (uPesy/easyeda2kicad.py),
which is the reference implementation and worth re-reading if EasyEDA
changes anything. The two model URLs below in particular are the most
likely thing to drift - verify against easyeda2kicad before trusting a
`has_3d: false` on a part you expected to have a model.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import requests

from .models import Assets, Footprint, Pad
from .util import http_head_ok, log, to_mm

# easyeda.com answers HTTP 403 after a burst of ~90 quick requests. lceda.cn,
# EasyEDA's China host, serves the same library through the same API, so a
# 403 switches to it (as tools/lcsc2altium_cli.py does), then backs off.
HOSTS = ("https://easyeda.com", "https://lceda.cn")
API = "{host}/api/products/{lcsc}/components"
API_VERSION = "6.4.19.5"
MIN_GAP_S = float(os.environ.get("JLCPICK_EASYEDA_GAP", "0.5"))
BACKOFF_S = (10, 30, 60)
_pace = threading.Lock()
_state = {"last": 0.0, "mirror": os.environ.get("JLCPICK_EASYEDA_HOST", "").lower() == "lceda.cn"}

MODEL_OBJ = "https://modules.easyeda.com/3dmodel/{uuid}"
MODEL_STEP = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"

# EasyEDA layer ids we care about.
LAYER_TOP_COPPER = 1
LAYER_BOTTOM_COPPER = 2
LAYER_TOP_SILK = 3
LAYER_BOTTOM_SILK = 4
LAYER_MULTI = 11
LAYER_TOP_ASSEMBLY = 12

_LAYER_NAME = {
    LAYER_TOP_COPPER: "top",
    LAYER_BOTTOM_COPPER: "bottom",
    LAYER_MULTI: "multi",
}


class EasyEDAError(RuntimeError):
    pass


def normalise_lcsc(lcsc: str) -> str:
    s = str(lcsc).strip().upper()
    return s if s.startswith("C") else f"C{s}"


def _cache_path(lcsc: str):
    """Shared on-disk cache, the same files the LCSC2Altium wrapper writes."""
    import os
    from pathlib import Path
    root = Path(os.environ.get("JLCPICK_CACHE", Path.home() / ".cache" / "jlcpick")) / "easyeda"
    return root / f"{lcsc}.component.json"


def _get_component(lcsc: str, timeout: float) -> Any:
    """One paced component request, shared by every probe thread."""
    attempt = errors = 0
    while True:
        with _pace:  # space uncached requests out across all threads
            wait = MIN_GAP_S - (time.monotonic() - _state["last"])
            if wait > 0:
                time.sleep(wait)
            _state["last"] = time.monotonic()
        url = API.format(host=HOSTS[1] if _state["mirror"] else HOSTS[0], lcsc=lcsc)
        log(f"GET {url}")
        try:
            resp = requests.get(url, params={"version": API_VERSION}, timeout=timeout,
                                headers={"Accept": "application/json", "User-Agent": "jlcpick/0.1"})
        except requests.RequestException as exc:
            errors += 1
            if errors > 2:
                raise EasyEDAError(f"{lcsc}: {exc}") from exc
            time.sleep(0.6 * errors)
            continue
        if resp.status_code == 403:
            if not _state["mirror"]:
                _state["mirror"] = True
                log("easyeda.com answered 403 - using lceda.cn for component data")
                continue
            if attempt == len(BACKOFF_S):
                raise EasyEDAError(f"{lcsc}: EasyEDA rate limit (HTTP 403); try again in a few minutes")
            log(f"EasyEDA 403 - backing off {BACKOFF_S[attempt]}s")
            time.sleep(BACKOFF_S[attempt])
            attempt += 1
            continue
        if resp.status_code >= 400:
            raise EasyEDAError(f"{lcsc}: HTTP {resp.status_code} from {url}")
        try:
            return resp.json()
        except ValueError as exc:
            raise EasyEDAError(f"{lcsc}: malformed JSON from {url}") from exc


def fetch_component(lcsc: str, timeout: float = 20.0) -> dict:
    """Raw EasyEDA component record. Raises EasyEDAError when absent.

    Cached on disk: EasyEDA answers HTTP 403 after a burst of quick requests,
    and footprints do not change between a probe, an import and a verify.
    """
    lcsc = normalise_lcsc(lcsc)
    cache = _cache_path(lcsc)
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    data = _get_component(lcsc, timeout)
    if not isinstance(data, dict) or not data.get("success") or not data.get("result"):
        raise EasyEDAError(f"{lcsc}: no EasyEDA component record")
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data["result"]), encoding="utf-8")
    except OSError:
        pass
    return data["result"]


def _shapes(container: Any) -> list[str]:
    """dataStr.shape, tolerating both the dict and already-parsed forms."""
    if not isinstance(container, dict):
        return []
    data_str = container.get("dataStr")
    if isinstance(data_str, str):
        try:
            data_str = json.loads(data_str)
        except ValueError:
            return []
    if not isinstance(data_str, dict):
        return []
    shapes = data_str.get("shape")
    return [s for s in shapes if isinstance(s, str)] if isinstance(shapes, list) else []


def _find_model(package_detail: dict) -> tuple[str | None, str | None]:
    """Pull the 3D model uuid out of the footprint's SVGNODE record."""
    for shape in _shapes(package_detail):
        if not shape.startswith("SVGNODE"):
            continue
        payload = shape.split("~", 1)[1] if "~" in shape else ""
        try:
            node = json.loads(payload)
        except ValueError:
            continue
        attrs = node.get("attrs") if isinstance(node, dict) else None
        if isinstance(attrs, dict) and attrs.get("uuid"):
            return str(attrs["uuid"]), attrs.get("title") or attrs.get("c_etype")
    return None, None


def probe(lcsc: str, check_model_url: bool = True, timeout: float = 20.0) -> Assets:
    """What does EasyEDA already have for this part?

    check_model_url costs one extra request per part but distinguishes
    "a model is referenced" from "a model is actually downloadable",
    which are not the same thing often enough to matter.
    """
    lcsc = normalise_lcsc(lcsc)
    try:
        result = fetch_component(lcsc, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return Assets(lcsc=lcsc, error=str(exc))

    symbol_shapes = _shapes(result)
    package_detail = result.get("packageDetail") or {}
    fp_shapes = _shapes(package_detail)
    pad_count = sum(1 for s in fp_shapes if s.startswith("PAD"))

    model_uuid, model_title = _find_model(package_detail)
    has_3d = bool(model_uuid)
    if has_3d and check_model_url:
        has_3d = http_head_ok(MODEL_OBJ.format(uuid=model_uuid), timeout=timeout)
        if not has_3d:
            log(f"{lcsc}: model {model_uuid} referenced but not downloadable")

    package_name = None
    if isinstance(package_detail, dict):
        package_name = package_detail.get("title")
    if not package_name:
        head = (result.get("dataStr") or {}).get("head") if isinstance(result.get("dataStr"), dict) else None
        if isinstance(head, dict):
            package_name = (head.get("c_para") or {}).get("package")

    return Assets(
        lcsc=lcsc,
        has_symbol=bool(symbol_shapes),
        has_footprint=pad_count > 0,
        has_3d=has_3d,
        package_name=package_name,
        model_uuid=model_uuid,
        model_title=model_title,
        pad_count=pad_count or None,
    )


def _parse_pad(shape: str) -> Pad | None:
    """PAD~shape~x~y~w~h~layer~net~number~hole_r~points~rot~id~hole_len~hole_pt~plated~locked"""
    f = shape.split("~")
    if len(f) < 10:
        return None

    def num(i: int, default: float = 0.0) -> float:
        try:
            return float(f[i])
        except (IndexError, ValueError):
            return default

    try:
        layer_id = int(float(f[6]))
    except (IndexError, ValueError):
        layer_id = LAYER_TOP_COPPER

    hole_radius = num(9)
    plated = True
    if len(f) > 15:
        plated = str(f[15]).strip().lower() not in {"no", "false", "0"}

    return Pad(
        number=str(f[8]).strip() if len(f) > 8 else "?",
        shape=str(f[1]).strip().upper() or "RECT",
        x=to_mm(num(2)),
        y=to_mm(num(3)),
        width=to_mm(num(4)),
        height=to_mm(num(5)),
        rotation=num(11),
        layer=_LAYER_NAME.get(layer_id, "multi" if hole_radius else "top"),
        hole_diameter=to_mm(hole_radius * 2),
        hole_length=to_mm(num(13)),
        plated=plated,
    )


def _track_segments(shapes: list[str], layer_id: int) -> list[tuple[float, float, float, float]]:
    """TRACK records on one layer as (x1, y1, x2, y2) segments, in mm.

    TRACK~stroke_width~layer_id~net~x1 y1 x2 y2 ...~id~locked
    """
    segments: list[tuple[float, float, float, float]] = []
    for shape in shapes:
        if not shape.startswith("TRACK"):
            continue
        f = shape.split("~")
        if len(f) < 5:
            continue
        try:
            if int(float(f[2])) != layer_id:
                continue
        except ValueError:
            continue
        pts: list[tuple[float, float]] = []
        coords = f[4].split()
        for i in range(0, len(coords) - 1, 2):
            try:
                pts.append((to_mm(float(coords[i])), to_mm(float(coords[i + 1]))))
            except ValueError:
                continue
        segments.extend(
            (a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])
        )
    return segments


def _bbox_of(segments: list[tuple[float, float, float, float]]):
    if not segments:
        return None
    xs = [c for s in segments for c in (s[0], s[2])]
    ys = [c for s in segments for c in (s[1], s[3])]
    return (min(xs), min(ys), max(xs), max(ys))


def footprint(lcsc: str, timeout: float = 20.0) -> Footprint:
    """Normalised footprint geometry in mm, origin at the package origin."""
    lcsc = normalise_lcsc(lcsc)
    result = fetch_component(lcsc, timeout=timeout)
    package_detail = result.get("packageDetail") or {}
    shapes = _shapes(package_detail)
    if not shapes:
        raise EasyEDAError(f"{lcsc}: no footprint geometry in EasyEDA record")

    # EasyEDA stores absolute canvas coordinates; head.x/head.y is the origin.
    data_str = package_detail.get("dataStr")
    if isinstance(data_str, str):
        try:
            data_str = json.loads(data_str)
        except ValueError:
            data_str = {}
    head = (data_str or {}).get("head") or {}
    try:
        ox, oy = to_mm(head.get("x", 0)), to_mm(head.get("y", 0))
    except (TypeError, ValueError):
        ox = oy = 0.0

    pads: list[Pad] = []
    for shape in shapes:
        if not shape.startswith("PAD"):
            continue
        pad = _parse_pad(shape)
        if pad is None:
            continue
        # Recentre on the origin, and flip Y (EasyEDA is Y-down).
        pad.x = round(pad.x - ox, 4)
        pad.y = round(-(pad.y - oy), 4)
        pads.append(pad)

    def shift_seg(seg):
        x1, y1, x2, y2 = seg
        return (
            round(x1 - ox, 4), round(-(y1 - oy), 4),
            round(x2 - ox, 4), round(-(y2 - oy), 4),
        )

    silk = [shift_seg(s) for s in _track_segments(shapes, LAYER_TOP_SILK)]
    assembly = [shift_seg(s) for s in _track_segments(shapes, LAYER_TOP_ASSEMBLY)]

    return Footprint(
        lcsc=lcsc,
        name=package_detail.get("title"),
        pads=pads,
        silk_segments=silk,
        silk_bbox=_bbox_of(silk),
        assembly_bbox=_bbox_of(assembly),
    )


def model_urls(uuid: str) -> dict[str, str]:
    return {"obj": MODEL_OBJ.format(uuid=uuid), "step": MODEL_STEP.format(uuid=uuid)}
