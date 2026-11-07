"""Parametric search over the JLCPCB assembly catalogue.

Two backends:

  LocalCatalog  - the jlcparts SQLite dump (yaqwsx/jlcparts). Fast, offline,
                  good for sweeping a whole category. Stock figures are as
                  old as your snapshot.
  LiveCatalog   - jlcsearch.tscircuit.com, a free public mirror with no key.
                  Slower, fewer filters, but current.

Rule of thumb: sweep locally, then re-check the shortlist live before you
commit, because stock moves.

The local schema is INTROSPECTED rather than hardcoded. jlcparts has changed
its column names more than once and this tool should not break when it does.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import Part
from .util import http_get_json, log

DEFAULT_DB_ENV = "JLCPICK_DB"
JLCPARTS_DOWNLOAD = "https://yaqwsx.github.io/jlcparts/data/cache.zip"
LIVE_BASE = "https://jlcsearch.tscircuit.com"

# Candidate column names, most-preferred first. Introspection picks the first
# one that actually exists in the table.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "lcsc": ("lcsc", "lcsc_id", "id"),
    "mpn": ("mfr", "mpn", "manufacturer_part", "part_number"),
    "manufacturer": ("manufacturer", "manufacturer_name", "mfr_name"),
    "package": ("package", "footprint"),
    "description": ("description", "desc"),
    "basic": ("basic", "is_basic", "library_type"),
    "preferred": ("preferred", "is_preferred"),
    "stock": ("stock", "quantity", "qty"),
    "price": ("price", "prices", "price_json"),
    "datasheet": ("datasheet", "datasheet_url"),
    "joints": ("joints", "pin_count"),
    "extra": ("extra", "attributes", "extra_json"),
    "category_id": ("category_id", "category"),
    "subcategory": ("subcategory",),
    "manufacturer_id": ("manufacturer_id",),
    # source-db-v2: 1 while the part is still listed by JLC
    "present": ("present",),
}


class CatalogError(RuntimeError):
    pass


def resolve_db_path(explicit: str | None = None) -> Path | None:
    for candidate in (
        explicit,
        os.environ.get(DEFAULT_DB_ENV),
        "./cache.sqlite3",
        str(Path.home() / ".cache" / "jlcpick" / "cache.sqlite3"),
    ):
        if candidate and Path(candidate).expanduser().is_file():
            return Path(candidate).expanduser()
    return None


def _parse_price(raw: Any, qty: int) -> float | None:
    """jlcparts stores price as a JSON array of {qFrom, qTo, price} tiers."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and ":" in raw and not raw.lstrip().startswith(("[", "{")):
        # jlcsearch tier string: "1-49:0.2029,50-149:0.1635,4000-:0.0981"
        tiers = []
        for chunk in raw.split(","):
            rng, _, price = chunk.partition(":")
            lo, _, hi = rng.partition("-")
            try:
                tiers.append({"qFrom": int(lo), "qTo": int(hi) if hi.strip() else None,
                              "price": float(price)})
            except ValueError:
                continue
        raw = tiers
    try:
        tiers = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(tiers, list):
        return None
    best: float | None = None
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        q_from = tier.get("qFrom") or tier.get("qty") or 1
        q_to = tier.get("qTo")
        price = tier.get("price")
        if price is None:
            continue
        try:
            q_from = int(q_from)
            price = float(price)
        except (TypeError, ValueError):
            continue
        if qty >= q_from and (q_to in (None, "", 0) or qty <= int(q_to)):
            return price
        if qty >= q_from:
            best = price
    return best


def _truthy_basic(raw: Any) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    # source-db-v2 stores library_type as "base" / "expand"
    if s in {"1", "true", "basic", "base", "yes"}:
        return True
    if s in {"0", "false", "extended", "expand", "expanded", "no"}:
        return False
    return None


class LocalCatalog:
    """Query a jlcparts SQLite snapshot."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise CatalogError(
                f"No catalogue at {self.db_path}.\n"
                f"Download the jlcparts dump:\n"
                f"  mkdir -p ~/.cache/jlcpick && cd ~/.cache/jlcpick\n"
                f"  curl -L -o cache.zip {JLCPARTS_DOWNLOAD} && unzip cache.zip\n"
                f"Then export {DEFAULT_DB_ENV}=~/.cache/jlcpick/cache.sqlite3"
            )
        self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.table = self._find_components_table()
        self.cols = self._map_columns()

    def _tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
        return [r["name"] for r in rows]

    def _find_components_table(self) -> str:
        tables = self._tables()
        # jlc_components: jlcparts "source-db-v2" format (2026)
        for name in ("components", "jlc_components", "component", "parts", "part"):
            if name in tables:
                return name
        raise CatalogError(
            f"Could not find a components table in {self.db_path}. "
            f"Tables present: {', '.join(tables) or '(none)'}"
        )

    def _map_columns(self) -> dict[str, str]:
        present = {
            r["name"] for r in self.conn.execute(f"PRAGMA table_info({self.table})")
        }
        mapping: dict[str, str] = {}
        for logical, candidates in COLUMN_ALIASES.items():
            for c in candidates:
                if c in present:
                    mapping[logical] = c
                    break
        if "lcsc" not in mapping:
            raise CatalogError(
                f"Table {self.table} has no recognisable LCSC column. "
                f"Columns: {', '.join(sorted(present))}"
            )
        log(f"catalogue schema: table={self.table} mapped={sorted(mapping)}")
        return mapping

    def _category_lookup(self) -> dict[int, tuple[str, str]]:
        if "categories" not in self._tables():
            return {}
        out: dict[int, tuple[str, str]] = {}
        try:
            for r in self.conn.execute("SELECT * FROM categories"):
                keys = r.keys()
                cid = r["id"] if "id" in keys else None
                cat = r["category"] if "category" in keys else ""
                sub = r["subcategory"] if "subcategory" in keys else ""
                if cid is not None:
                    out[int(cid)] = (cat or "", sub or "")
        except sqlite3.Error:
            return {}
        return out

    def _manufacturer_lookup(self) -> dict[int, str]:
        if "manufacturers" not in self._tables():
            return {}
        out: dict[int, str] = {}
        try:
            for r in self.conn.execute("SELECT * FROM manufacturers"):
                keys = r.keys()
                mid = r["id"] if "id" in keys else None
                name = r["name"] if "name" in keys else ""
                if mid is not None:
                    out[int(mid)] = name or ""
        except sqlite3.Error:
            return {}
        return out

    def _category_clause(self, needle: str) -> tuple[str, list[Any]] | None:
        """SQL matching parts whose category or subcategory contains needle.

        The substring is resolved to exact names first, so the query is an
        IN (...) that an index can serve (fetch_jlcparts builds them),
        instead of a LIKE over every row. None when nothing matches.
        """
        c = self.cols
        low = needle.lower()
        cats = self._category_lookup()
        if cats and "category_id" in c and c["category_id"] == "category_id":
            ids = [cid for cid, (cat, sub) in cats.items()
                   if low in (cat or "").lower() or low in (sub or "").lower()]
            if not ids:
                return None
            return f"{c['category_id']} IN ({','.join('?' * len(ids))})", ids
        parts: list[str] = []
        params: list[Any] = []
        for logical in ("category_id", "subcategory"):
            col = c.get(logical)
            if not col:
                continue
            names = [r[0] for r in self.conn.execute(f"SELECT DISTINCT {col} FROM {self.table}")
                     if isinstance(r[0], str) and low in r[0].lower()]
            if names:
                parts.append(f"{col} IN ({','.join('?' * len(names))})")
                params.extend(names)
        if not parts:
            return None
        return "(" + " OR ".join(parts) + ")", params

    def search(
        self,
        terms: Iterable[str] = (),
        package: str | None = None,
        category: str | None = None,
        basic_only: bool = False,
        min_stock: int = 1,
        max_price: float | None = None,
        qty: int = 100,
        limit: int = 40,
    ) -> list[Part]:
        c = self.cols
        where: list[str] = []
        params: list[Any] = []

        searchable = [c[k] for k in ("description", "mpn", "package") if k in c]
        # "3.3V LDO 300mA" as one shell argument means three terms, not one
        # exact substring: split on whitespace, every word must match.
        words = [w for term in terms for w in str(term).split()]
        for word in words:
            if not searchable:
                break
            clause = " OR ".join(f"{col} LIKE ? COLLATE NOCASE" for col in searchable)
            where.append(f"({clause})")
            params.extend([f"%{word}%"] * len(searchable))

        if category:
            clause = self._category_clause(category)
            if clause is None:
                log(f"no category or subcategory contains {category!r}")
                return []
            where.append(clause[0])
            params.extend(clause[1])

        if package and "package" in c:
            where.append(f"{c['package']} LIKE ? COLLATE NOCASE")
            params.append(f"%{package}%")

        if basic_only and "basic" in c:
            where.append(f"{c['basic']} IN (1, 'true', 'Basic', 'basic', 'base')")

        if "present" in c:
            where.append(f"COALESCE({c['present']}, 1) = 1")

        if min_stock and "stock" in c:
            # A plain range (not COALESCE) so the stock index can skip the
            # ~85% of the catalogue that is out of stock. NULL never passes.
            where.append(f"{c['stock']} >= ?")
            params.append(int(min_stock))

        order = f"{c['stock']} DESC" if "stock" in c else f"{c['lcsc']} ASC"
        sql = f"SELECT * FROM {self.table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Over-fetch so price capping (tiers are JSON, filtered in Python)
        # still fills the page.
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(max(limit * 8, 200) if max_price is not None else limit)

        cats = self._category_lookup()
        mfrs = self._manufacturer_lookup()

        out: list[Part] = []
        for row in self.conn.execute(sql, params):
            part = self._row_to_part(row, qty, cats, mfrs)
            if max_price is not None and part.price is not None and part.price > max_price:
                continue
            out.append(part)
            if len(out) >= limit:
                break
        return out

    def get(self, lcsc: str, qty: int = 100) -> Part | None:
        key = str(lcsc).upper().lstrip("C")
        col = self.cols["lcsc"]
        row = self.conn.execute(
            f"SELECT * FROM {self.table} WHERE CAST({col} AS TEXT) IN (?, ?) LIMIT 1",
            (key, f"C{key}"),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_part(
            row, qty, self._category_lookup(), self._manufacturer_lookup()
        )

    def _row_to_part(
        self,
        row: sqlite3.Row,
        qty: int,
        cats: dict[int, tuple[str, str]],
        mfrs: dict[int, str],
    ) -> Part:
        c = self.cols

        def val(logical: str):
            col = c.get(logical)
            if not col:
                return None
            try:
                return row[col]
            except (IndexError, KeyError):
                return None

        raw_lcsc = str(val("lcsc") or "").strip()
        lcsc = raw_lcsc if raw_lcsc.upper().startswith("C") else f"C{raw_lcsc}"

        manufacturer = val("manufacturer")
        if manufacturer is None:
            mid = val("manufacturer_id")
            if mid is not None:
                try:
                    manufacturer = mfrs.get(int(mid))
                except (TypeError, ValueError):
                    manufacturer = None

        category = subcategory = None
        cid = val("category_id")
        if cid is not None:
            try:
                category, subcategory = cats.get(int(cid), (None, None))
            except (TypeError, ValueError):
                # source-db-v2 stores the category name itself
                category, subcategory = str(cid), val("subcategory")

        attrs: dict[str, Any] = {}
        extra = val("extra")
        if extra:
            try:
                parsed = json.loads(extra) if isinstance(extra, str) else extra
                if isinstance(parsed, dict):
                    attrs = parsed.get("attributes") or parsed
            except (ValueError, TypeError):
                pass

        return Part(
            lcsc=lcsc,
            mpn=val("mpn"),
            manufacturer=manufacturer,
            package=val("package"),
            description=val("description"),
            basic=_truthy_basic(val("basic")),
            preferred=_truthy_basic(val("preferred")),
            stock=int(val("stock")) if val("stock") not in (None, "") else None,
            price=_parse_price(val("price"), qty),
            price_qty=qty,
            datasheet=val("datasheet"),
            category=category or None,
            subcategory=subcategory or None,
            attrs=attrs if isinstance(attrs, dict) else {},
        )


class LiveCatalog:
    """Thin client for jlcsearch.tscircuit.com (free, no API key).

    Endpoint shapes have changed before, so every response is parsed
    defensively and a miss returns an empty list rather than raising.
    """

    def __init__(self, base: str = LIVE_BASE, timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def search(self, terms: Iterable[str] = (), limit: int = 40, qty: int = 100, **_) -> list[Part]:
        q = " ".join(t for t in terms if t.strip())
        data = None
        for path in ("/api/search", "/search.json", "/components/list.json"):
            try:
                data = http_get_json(
                    f"{self.base}{path}",
                    # /api/search takes q; /components/list.json takes search
                    params={"q": q, "search": q, "limit": limit},
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - endpoint drift is expected
                log(f"live catalogue {path} failed: {exc}")
                continue
            if data:
                break
        if not data:
            return []

        rows = data if isinstance(data, list) else (
            data.get("components") or data.get("results") or data.get("data") or []
        )
        out: list[Part] = []
        for r in rows[:limit]:
            if not isinstance(r, dict):
                continue
            raw = str(r.get("lcsc") or r.get("lcsc_id") or r.get("id") or "").strip()
            if not raw:
                continue
            out.append(
                Part(
                    lcsc=raw if raw.upper().startswith("C") else f"C{raw}",
                    mpn=r.get("mfr") or r.get("mpn"),
                    manufacturer=r.get("manufacturer"),
                    package=r.get("package"),
                    description=r.get("description"),
                    # jlcsearch now reports is_basic / is_preferred
                    basic=_truthy_basic(next((r[k] for k in ("is_basic", "basic", "library_type")
                                              if k in r), None)),
                    preferred=_truthy_basic(next((r[k] for k in ("is_preferred", "preferred")
                                                  if k in r), None)),
                    stock=r.get("stock"),
                    price=_parse_price(r.get("price") or r.get("prices"), qty),
                    price_qty=qty,
                    datasheet=r.get("datasheet"),
                )
            )
        return out


def open_catalog(db: str | None = None, live: bool = False) -> LocalCatalog | LiveCatalog:
    if live:
        return LiveCatalog()
    path = resolve_db_path(db)
    if path is None:
        raise CatalogError(
            "No local catalogue found. Either pass --db, set "
            f"{DEFAULT_DB_ENV}, or use --live.\n"
            f"To fetch the jlcparts dump:\n"
            f"  mkdir -p ~/.cache/jlcpick && cd ~/.cache/jlcpick\n"
            f"  curl -L -o cache.zip {JLCPARTS_DOWNLOAD} && unzip cache.zip"
        )
    return LocalCatalog(path)
