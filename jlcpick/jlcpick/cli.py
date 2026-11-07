"""jlcpick command line interface.

Every subcommand takes --json so an agent can parse the result instead of
scraping a table. Human output is the default because you will use this
yourself more often than an agent will.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
from pathlib import Path

from . import __version__, easyeda, verify as verify_mod
from .catalog import CatalogError, open_catalog
from .convert import import_altium, import_kicad
from .models import Part
from .util import dump_json, render_table, set_verbose

SEARCH_COLUMNS = [
    ("lcsc", "LCSC"),
    ("mpn", "MPN"),
    ("package", "PACKAGE"),
    ("lib", "LIB"),
    ("stock", "STOCK"),
    ("price", "PRICE"),
    ("assets", "ASSETS"),
    ("description", "DESCRIPTION"),
]


def _lib_label(part: Part) -> str:
    if part.basic is True:
        return "basic"
    if part.preferred is True:
        return "pref"
    if part.basic is False:
        return "ext"
    return "-"


def _row(part: Part) -> dict:
    return {
        "lcsc": part.lcsc,
        "mpn": part.mpn,
        "package": part.package,
        "lib": _lib_label(part),
        "stock": part.stock,
        "price": part.price,
        "assets": part.assets.score if part.assets else "?",
        "description": part.description,
    }


def _attach_assets(parts: list[Part], workers: int, check_model: bool) -> None:
    if not parts:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(easyeda.probe, p.lcsc, check_model): p for p in parts
        }
        for fut in concurrent.futures.as_completed(futures):
            futures[fut].assets = fut.result()


def cmd_search(args) -> int:
    try:
        cat = open_catalog(args.db, live=args.live)
    except CatalogError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    parts = cat.search(
        terms=args.terms,
        package=args.package,
        category=args.category,
        basic_only=args.basic_only,
        min_stock=args.min_stock,
        max_price=args.max_price,
        qty=args.qty,
        limit=args.limit,
    )

    if not args.no_assets:
        _attach_assets(parts, args.workers, not args.fast)
        if args.require_3d:
            parts = [p for p in parts if p.assets and p.assets.has_3d]
        elif args.require_assets:
            parts = [
                p for p in parts
                if p.assets and p.assets.has_symbol and p.assets.has_footprint
            ]

    if args.json:
        print(dump_json({
            "query": " ".join(args.terms),
            "count": len(parts),
            "qty": args.qty,
            "parts": [p.to_dict() for p in parts],
        }))
    else:
        print(render_table([_row(p) for p in parts], SEARCH_COLUMNS))
        print(f"\n{len(parts)} result(s) - price at qty {args.qty}")
        print("ASSETS: full = symbol+footprint+3D, no-3d, partial, none")
        print("Verify every spec against the datasheet before committing.")
    return 0


def cmd_probe(args) -> int:
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for assets in pool.map(
            lambda c: easyeda.probe(c, not args.fast), args.lcsc
        ):
            results.append(assets)

    if args.json:
        print(dump_json([a.to_dict() for a in results]))
    else:
        rows = [
            {
                "lcsc": a.lcsc,
                "score": a.score,
                "symbol": a.has_symbol,
                "footprint": a.has_footprint,
                "model_3d": a.has_3d,
                "pads": a.pad_count,
                "package": a.package_name or a.error,
            }
            for a in results
        ]
        print(render_table(rows, [
            ("lcsc", "LCSC"), ("score", "SCORE"), ("symbol", "SYM"),
            ("footprint", "FP"), ("model_3d", "3D"), ("pads", "PADS"),
            ("package", "PACKAGE"),
        ]))
    return 0


def cmd_geom(args) -> int:
    try:
        fp = easyeda.footprint(args.lcsc)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summary = verify_mod.summarize(fp)
    if args.json:
        print(dump_json({"summary": summary, "footprint": fp.to_dict()}))
        return 0

    print(f"{fp.lcsc}  {fp.name or '(unnamed)'}")
    print("-" * 60)
    for key in (
        "pad_count", "signal_pad_count", "thermal_pad_count", "rows", "columns",
        "pitch", "pad_width", "pad_height", "span_outer", "span_inner",
        "thermal_pad_width", "thermal_pad_height", "hole_diameter",
    ):
        if summary.get(key) is not None:
            print(f"  {key:22} {summary[key]}")
    print("\nAll dimensions in mm, origin at the footprint origin.")
    if args.pads:
        print()
        print(render_table([p.to_dict() for p in fp.pads], [
            ("number", "PIN"), ("shape", "SHAPE"), ("x", "X"), ("y", "Y"),
            ("width", "W"), ("height", "H"), ("rotation", "ROT"),
            ("layer", "LAYER"), ("hole_diameter", "HOLE"),
        ]))
    return 0


def cmd_verify(args) -> int:
    try:
        fp = easyeda.footprint(args.lcsc)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summary = verify_mod.summarize(fp)
    findings = verify_mod.selfcheck(fp)

    if args.datasheet:
        try:
            ds = verify_mod.load_datasheet(args.datasheet)
        except Exception as exc:  # noqa: BLE001
            print(f"error reading datasheet json: {exc}", file=sys.stderr)
            return 1
        findings += verify_mod.compare(summary, ds, {"pitch": args.tol_pitch,
                                                     "pad_width": args.tol_pad,
                                                     "pad_height": args.tol_pad,
                                                     "span": args.tol_span})

    status = verify_mod.verdict(findings)

    if args.json:
        print(dump_json({
            "lcsc": fp.lcsc,
            "verdict": status,
            "summary": summary,
            "findings": [f.to_dict() for f in findings],
        }))
    else:
        print(f"{fp.lcsc}  {fp.name or ''}   verdict: {status.upper()}")
        print("-" * 72)
        print(render_table([f.to_dict() for f in findings], [
            ("status", "STATUS"), ("field", "FIELD"), ("expected", "EXPECTED"),
            ("actual", "ACTUAL"), ("delta", "DELTA"), ("note", "NOTE"),
        ]))
        if not args.datasheet:
            print("\nSelf-checks only. For land-pattern comparison:")
            print("  jlcpick template > ds.json   # fill from the datasheet")
            print(f"  jlcpick verify {fp.lcsc} --datasheet ds.json")
    return 0 if status != "fail" else 1


def cmd_template(args) -> int:
    print(verify_mod.template_json())
    return 0


def cmd_import(args) -> int:
    if args.backend == "altium" and not (args.schlib and args.pcblib):
        print("error: the altium backend needs --schlib and --pcblib "
              "(or use --backend kicad)", file=sys.stderr)
        return 2
    results = []
    for lcsc in args.lcsc:
        if args.backend == "kicad":
            results.append(import_kicad(lcsc, args.output, project_relative=args.project_relative))
        else:
            results.append(import_altium(lcsc, args.schlib or "", args.pcblib or "",
                                         include_3d=not args.no_3d))

    if args.json:
        print(dump_json([r.to_dict() for r in results]))
    else:
        print(render_table([r.to_dict() for r in results], [
            ("lcsc", "LCSC"), ("backend", "BACKEND"), ("ok", "OK"),
            ("symbol", "SYM"), ("footprint", "FP"), ("model_3d", "3D"),
            ("message", "MESSAGE"),
        ]))
    return 0 if all(r.ok for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jlcpick",
        description="Shortlist JLCPCB parts by stock AND by whether a symbol, "
                    "footprint and 3D model already exist.",
    )
    p.add_argument("--version", action="version", version=f"jlcpick {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="parametric search with asset availability")
    s.add_argument("terms", nargs="*", help="free-text terms, ANDed together")
    s.add_argument("--package", help="package substring, e.g. SOT-23")
    s.add_argument("--category", help="category/subcategory substring")
    s.add_argument("--basic-only", action="store_true", help="JLC Basic parts only")
    s.add_argument("--min-stock", type=int, default=1)
    s.add_argument("--max-price", type=float, help="max unit price at --qty")
    s.add_argument("--qty", type=int, default=100, help="quantity for pricing")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--db", help="path to jlcparts cache.sqlite3")
    s.add_argument("--live", action="store_true", help="use jlcsearch instead of local db")
    s.add_argument("--no-assets", action="store_true", help="skip the EasyEDA probe")
    s.add_argument("--require-assets", action="store_true", help="drop parts without symbol+footprint")
    s.add_argument("--require-3d", action="store_true", help="drop parts without a 3D model")
    s.add_argument("--fast", action="store_true", help="trust the model reference, skip the URL check")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("probe", help="check EasyEDA assets for specific parts")
    s.add_argument("lcsc", nargs="+")
    s.add_argument("--fast", action="store_true")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("geom", help="dump normalised footprint geometry in mm")
    s.add_argument("lcsc")
    s.add_argument("--pads", action="store_true", help="list every pad")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_geom)

    s = sub.add_parser("verify", help="self-check, and diff against a datasheet land pattern")
    s.add_argument("lcsc")
    s.add_argument("--datasheet", help="JSON produced from the datasheet (see: jlcpick template)")
    s.add_argument("--tol-pitch", type=float, default=0.02)
    s.add_argument("--tol-pad", type=float, default=0.05)
    s.add_argument("--tol-span", type=float, default=0.15)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("template", help="emit the datasheet JSON template to fill in")
    s.set_defaults(func=cmd_template)

    s = sub.add_parser("import", help="pull parts into a library")
    s.add_argument("lcsc", nargs="+")
    s.add_argument("--backend", choices=["kicad", "altium"], default="altium")
    s.add_argument("--output", default="./libs/jlcpick", help="kicad: library path prefix")
    s.add_argument("--project-relative", action="store_true", help="kicad: ${KIPRJMOD} 3D paths")
    s.add_argument("--schlib", help="altium: target .SchLib")
    s.add_argument("--pcblib", help="altium: target .PcbLib")
    s.add_argument("--no-3d", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_import)

    return p


def main(argv: list[str] | None = None) -> int:
    # Part descriptions carry ℃, Ω, ±, µ. When stdout is a pipe (an agent,
    # | more) Windows defaults to the ANSI code page and print() would crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    set_verbose(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
