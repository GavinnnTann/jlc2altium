"""Small shared helpers: HTTP with retry, logging, unit conversion, tables."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlencode

import requests

# EasyEDA's internal unit is 10 mil. 1 mil = 0.0254 mm, so one unit = 0.254 mm.
EASYEDA_UNIT_MM = 10 * 0.0254

_VERBOSE = bool(os.environ.get("JLCPICK_VERBOSE"))


def set_verbose(on: bool) -> None:
    global _VERBOSE
    _VERBOSE = on


def log(msg: str) -> None:
    if _VERBOSE:
        print(f"[jlcpick] {msg}", file=sys.stderr)


def to_mm(value: Any) -> float:
    """EasyEDA raw coordinate -> millimetres."""
    try:
        return round(float(value) * EASYEDA_UNIT_MM, 4)
    except (TypeError, ValueError):
        return 0.0


def http_get_json(
    url: str,
    params: dict | None = None,
    timeout: float = 20.0,
    retries: int = 2,
) -> Any:
    full = f"{url}?{urlencode(params)}" if params else url
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            log(f"GET {full}")
            resp = requests.get(
                full,
                timeout=timeout,
                headers={"Accept": "application/json", "User-Agent": "jlcpick/0.1"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"GET {full} failed: {last}")


def http_head_ok(url: str, timeout: float = 12.0) -> bool:
    try:
        log(f"HEAD {url}")
        resp = requests.head(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": "jlcpick/0.1"},
        )
        if resp.status_code == 405:  # some CDNs refuse HEAD
            resp = requests.get(
                url, timeout=timeout, stream=True,
                headers={"User-Agent": "jlcpick/0.1"},
            )
            resp.close()
        return resp.status_code < 400
    except Exception as exc:  # noqa: BLE001
        log(f"HEAD {url} failed: {exc}")
        return False


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=False, default=str)


def render_table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    """Fixed-width table. columns is [(key, header), ...]."""
    if not rows:
        return "(no results)"

    def cell(row: dict, key: str) -> str:
        v = row.get(key)
        if v is None:
            return "-"
        if isinstance(v, bool):
            return "yes" if v else "no"
        if isinstance(v, float):
            return f"{v:.4g}"
        s = str(v)
        return s if len(s) <= 44 else s[:41] + "..."

    widths = []
    for key, header in columns:
        w = max(len(header), *(len(cell(r, key)) for r in rows))
        widths.append(min(w, 44))

    out = [
        "  ".join(h.ljust(w) for (_, h), w in zip(columns, widths)),
        "  ".join("-" * w for w in widths),
    ]
    for r in rows:
        out.append("  ".join(cell(r, k).ljust(w) for (k, _), w in zip(columns, widths)))
    return "\n".join(out)
