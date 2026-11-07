"""Parametric search of the local JLC catalogue (jlcparts "source-db-v2").

jlcpick search matches free text. This matches the catalogue's own attribute
table, with units, which is what part selection actually needs:

    # 2.2 uH inductors that saturate at >= 6 A with <= 25 mOhm, smallest first
    jlc_query.py --subcat "Power Inductors" --attr "Inductance=2.2uH" \
        --where "Current - Saturation(Isat)>=6A" --where "DC Resistance(DCR)<=25mOhm" \
        --show "Current - Saturation(Isat)" --show "DC Resistance(DCR)" --sort area

    # the JLC parts for one MPN family
    jlc_query.py --mpn "TP4056%" --pkg ESOP

    # 1% 0402 resistor of a given value, JLC Basic first
    jlc_query.py --subcat "Chip Resistor - Surface Mount" --pkg 0402 \
        --attr "Resistance=49.9kOhm" --attr "Tolerance=±1%"

Attribute names are the catalogue's (see --keys). Values in --attr are
compared as text after normalising "Ohm" to the ohm sign; --where compares
numbers with SI prefixes (p n u m k M) and units (A V Ohm F H W Hz).
Rows are ranked JLC Basic, then Preferred, then stock, unless --sort says otherwise.

Catalogue data (stock, price, package, LCSC code) is fact. Electrical values
come from LCSC listings: confirm them in the datasheet before relying on them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6}
UNIT = r"(A|V|Ω|F|H|W|Hz|ohm|Ohm)"


def _db() -> Path:
    p = os.environ.get("JLCPICK_DB") or str(Path.home() / ".cache" / "jlcpick" / "cache.sqlite3")
    if not Path(p).is_file():
        raise SystemExit(f"no catalogue at {p} - run tools/fetch_jlcparts.py first")
    return Path(p)


def number(text: str) -> float | None:
    """First number in a catalogue value, in base units: '5.6A' -> 5.6, '25mΩ' -> 0.025."""
    m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([pnuµmkKM]?)\s*" + UNIT, str(text or ""))
    if m:
        return float(m.group(1)) * PREFIX[m.group(2)]
    m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(text or ""))
    return float(m.group(1)) if m else None


def parse_where(expr: str):
    m = re.match(r"^(.+?)\s*(<=|>=|<|>|==)\s*(.+)$", expr)
    if not m:
        raise SystemExit(f"bad --where {expr!r}: use 'Attribute Name>=6A'")
    key, op, val = m.group(1).strip(), m.group(2), number(m.group(3).replace("Ohm", "Ω"))
    ops = {"<=": lambda a: a <= val, ">=": lambda a: a >= val, "<": lambda a: a < val,
           ">": lambda a: a > val, "==": lambda a: abs(a - val) <= abs(val) * 1e-9}
    return key, ops[op]


def price_at(raw, qty):
    best = None
    for chunk in str(raw or "").split(","):
        rng, _, p = chunk.partition(":")
        try:
            if qty >= int(rng.partition("-")[0]):
                best = float(p)
        except ValueError:
            pass
    return best


def area(pkg: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*[xX*]\s*(\d+(?:\.\d+)?)", pkg)
    if m:
        return float(m.group(1)) * float(m.group(2))
    sizes = {"0201": 0.6 * 0.3, "0402": 1.0 * 0.5, "0603": 1.6 * 0.8, "0805": 2.0 * 1.25, "1206": 3.2 * 1.6,
             "1210": 3.2 * 2.5, "1812": 4.5 * 3.2, "2010": 5.0 * 2.5, "2512": 6.4 * 3.2}
    for code, a in sizes.items():
        if code in pkg:
            return a
    return 9999.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mpn", action="append", default=[], help="MPN LIKE pattern (%% wildcard); repeatable (OR)")
    ap.add_argument("--subcat", help="exact catalogue subcategory")
    ap.add_argument("--pkg", action="append", default=[], help="package substring; repeatable (OR)")
    ap.add_argument("--attr", action="append", default=[], help="'Key=value' exact attribute text")
    ap.add_argument("--where", action="append", default=[], help="'Key>=value' numeric attribute filter")
    ap.add_argument("--show", action="append", default=[], help="attribute to print; repeatable")
    ap.add_argument("--min-stock", type=int, default=100)
    ap.add_argument("--basic", action="store_true", help="JLC Basic (and Preferred) only")
    ap.add_argument("--qty", type=int, default=50, help="price tier")
    ap.add_argument("--sort", choices=["lib", "stock", "price", "area"], default="lib")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--keys", action="store_true", help="list attribute names in --subcat and exit")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    con = sqlite3.connect(f"file:{_db().as_posix()}?mode=ro", uri=True)
    if a.keys:
        if not a.subcat:
            raise SystemExit("--keys needs --subcat")
        keys = set()
        for (attrs,) in con.execute("select attributes from jlc_components where present=1 and subcategory=? limit 2000",
                                    (a.subcat,)):
            keys.update(json.loads(attrs or "{}"))
        print("\n".join(sorted(keys)))
        return 0

    where, params = ["present=1", "stock>=?"], [a.min_stock]
    if a.subcat:
        where.append("subcategory=?")
        params.append(a.subcat)
    if a.mpn:
        where.append("(" + " or ".join("mfr like ?" for _ in a.mpn) + ")")
        params += a.mpn
    if a.pkg:
        where.append("(" + " or ".join("package like ?" for _ in a.pkg) + ")")
        params += [f"%{p}%" for p in a.pkg]
    if a.basic:
        where.append("(library_type='base' or preferred=1)")
    attr_eq = []
    for kv in a.attr:
        k, _, v = kv.partition("=")
        v = v.replace("Ohm", "Ω")
        attr_eq.append((k.strip(), v.strip()))
        where.append("attributes like ?")
        params.append(f'%"{k.strip()}":"{v.strip()}"%')
    numeric = [parse_where(w) for w in a.where]

    sql = ("select lcsc, mfr, manufacturer, package, library_type, preferred, stock, price, attributes, description "
           "from jlc_components where " + " and ".join(where))
    rows = []
    for r in con.execute(sql, params):
        attrs = json.loads(r[8] or "{}")
        if any(attrs.get(k) != v for k, v in attr_eq):
            continue
        ok = True
        for k, test in numeric:
            n = number(attrs.get(k))
            if n is None or not test(n):
                ok = False
                break
        if not ok:
            continue
        rows.append({"lcsc": f"C{r[0]}", "mpn": r[1], "manufacturer": r[2], "package": r[3],
                     "lib": "basic" if r[4] == "base" else ("pref" if r[5] else "ext"),
                     "stock": r[6], "price": price_at(r[7], a.qty), "attrs": attrs, "description": r[9]})
    rank = {"basic": 0, "pref": 1, "ext": 2}
    keyf = {"lib": lambda d: (rank[d["lib"]], -d["stock"]), "stock": lambda d: -d["stock"],
            "price": lambda d: (d["price"] is None, d["price"] or 0), "area": lambda d: (area(d["package"]), -d["stock"])}
    rows.sort(key=keyf[a.sort])
    rows = rows[:a.limit]

    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    for d in rows:
        extra = "  ".join(f"{k}={d['attrs'].get(k)}" for k in a.show)
        price = f"${d['price']:.4f}" if d["price"] is not None else "-"
        print(f"{d['lcsc']:<10} {d['mpn'][:26]:<26} {str(d['manufacturer'])[:14]:<14} {d['package'][:20]:<20} "
              f"{d['lib']:<5} {d['stock']:>9} {price:>8}  {extra}")
    print(f"\n{len(rows)} result(s). Stock is the snapshot's; re-check live before ordering.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
