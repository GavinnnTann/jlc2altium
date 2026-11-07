"""Typed records passed between modules and serialised to JSON by the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


@dataclass
class Part:
    """One candidate from the JLC/LCSC catalogue."""

    lcsc: str
    mpn: str | None = None
    manufacturer: str | None = None
    package: str | None = None
    description: str | None = None
    basic: bool | None = None          # JLC "Basic" => no extended-part setup fee
    preferred: bool | None = None      # JLC "Preferred extended"
    stock: int | None = None
    price: float | None = None         # unit price at the requested qty
    price_qty: int | None = None
    datasheet: str | None = None
    category: str | None = None
    subcategory: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    # Filled in by the EasyEDA probe (jlcpick.easyeda), not by the catalogue.
    assets: "Assets | None" = None

    @property
    def lcsc_url(self) -> str:
        return f"https://www.lcsc.com/product-detail/{self.lcsc}.html"

    @property
    def jlc_url(self) -> str:
        return f"https://jlcpcb.com/partdetail/{self.lcsc.lstrip('Cc')}"

    def to_dict(self) -> dict:
        d = _clean(asdict(self))
        d["lcsc_url"] = self.lcsc_url
        d["jlc_url"] = self.jlc_url
        if self.assets is not None:
            d["assets"] = self.assets.to_dict()
        return d


@dataclass
class Assets:
    """What EasyEDA/LCSC already has for a part.

    This is the column that decides whether a part costs you two minutes
    or two hours, so it is a first-class field rather than a footnote.
    """

    lcsc: str
    has_symbol: bool = False
    has_footprint: bool = False
    has_3d: bool = False
    package_name: str | None = None
    model_uuid: str | None = None
    model_title: str | None = None
    pad_count: int | None = None
    error: str | None = None

    @property
    def score(self) -> str:
        if self.error:
            return "error"
        if self.has_symbol and self.has_footprint and self.has_3d:
            return "full"
        if self.has_symbol and self.has_footprint:
            return "no-3d"
        if self.has_symbol or self.has_footprint:
            return "partial"
        return "none"

    def to_dict(self) -> dict:
        d = _clean(asdict(self))
        d["score"] = self.score
        return d


@dataclass
class Pad:
    """A single pad, normalised to millimetres, origin at the footprint origin."""

    number: str
    shape: str                 # RECT | ELLIPSE | OVAL | POLYGON
    x: float
    y: float
    width: float
    height: float
    rotation: float = 0.0
    layer: str = "top"         # top | bottom | multi (through-hole)
    hole_diameter: float = 0.0
    hole_length: float = 0.0   # non-zero => slot
    plated: bool = True

    @property
    def is_through_hole(self) -> bool:
        return self.hole_diameter > 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_through_hole"] = self.is_through_hole
        return d


@dataclass
class Footprint:
    """Normalised footprint geometry, in millimetres."""

    lcsc: str
    name: str | None = None
    pads: list[Pad] = field(default_factory=list)
    # (x1, y1, x2, y2) line segments on the top silk layer, in mm.
    silk_segments: list[tuple[float, float, float, float]] = field(default_factory=list)
    silk_bbox: tuple[float, float, float, float] | None = None
    assembly_bbox: tuple[float, float, float, float] | None = None
    source: str = "easyeda"

    def to_dict(self) -> dict:
        return _clean(
            {
                "lcsc": self.lcsc,
                "name": self.name,
                "source": self.source,
                "pads": [p.to_dict() for p in self.pads],
                "silk_segments": [list(seg) for seg in self.silk_segments] or None,
                "silk_bbox": list(self.silk_bbox) if self.silk_bbox else None,
                "assembly_bbox": list(self.assembly_bbox) if self.assembly_bbox else None,
            }
        )


@dataclass
class Finding:
    """One line of a verify report."""

    field: str
    expected: float | str | None
    actual: float | str | None
    delta: float | None = None
    tolerance: float | None = None
    status: str = "info"       # ok | warn | fail | info
    note: str | None = None

    def to_dict(self) -> dict:
        return _clean(asdict(self))
