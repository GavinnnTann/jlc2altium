"""CLI wrapper around Gort-Power/LCSC2Altium for jlcpick's Altium backend.

jlcpick (jlcpick/convert.py: import_altium) shells out to the executable named
by JLCPICK_LCSC2ALTIUM with:

    --lcsc <C-code> --schlib <path> --pcblib <path> [--3d]

This wrapper fetches the part from EasyEDA with LCSC2Altium's own client and
parser, authors a one-off library with LCSC2Altium's builders (altium-monkey
underneath, so no hand-written OLE writer), and MERGES it into the target
libraries, so symbols or footprints already there - including ones edited by
hand in Altium - are kept. A part whose symbol is already in the target is
skipped unless --replace is given.

Extras beyond the jlcpick contract:
    --lcsc may repeat, or hold a comma/space separated list (bulk import)
    --replace   re-import parts already in the library
    --json      machine-readable summary on stdout

EasyEDA responses (and STEP models) are cached in ~/.cache/jlcpick/easyeda so a
re-run does not hit the API again.

Needs Python >= 3.12 with altium-monkey (install.ps1 sets this up in
~/.venvs/jlc2altium and puts an lcsc2altium-cli shim on PATH).

Environment:
    LCSC2ALTIUM_SRC       Gort-Power/LCSC2Altium checkout (default ~/src/LCSC2Altium)
    JLCPICK_CACHE         cache root (default ~/.cache/jlcpick)
    JLCPICK_EASYEDA_HOST  set to lceda.cn to use EasyEDA's China host from the start
    JLCPICK_EASYEDA_GAP   seconds between uncached EasyEDA calls (default 1.5)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

LCSC2ALTIUM_SRC = Path(os.environ.get("LCSC2ALTIUM_SRC", Path.home() / "src" / "LCSC2Altium"))
sys.path.insert(0, str(LCSC2ALTIUM_SRC))

# The checkout's package folder is "LCSC2Altium"; its own main.py imports
# "lcsc2altium", which only resolves on a case-insensitive import. Import the
# on-disk name; the package itself uses relative imports throughout.
from LCSC2Altium.builders.library import build_library, load_component  # noqa: E402
from LCSC2Altium.easyeda_client import EasyedaClient, EasyedaError  # noqa: E402
from LCSC2Altium.models import _sanitize  # noqa: E402

import altium_monkey as am  # noqa: E402

CACHE = Path(os.environ.get("JLCPICK_CACHE", Path.home() / ".cache" / "jlcpick")) / "easyeda"
log = logging.getLogger("lcsc2altium-cli")


class CachingClient(EasyedaClient):
    """EasyedaClient with an on-disk cache for components, search hits and STEP files.

    EasyEDA answers HTTP 403 after a burst of ~90 quick requests, so uncached
    calls are spaced out and a 403 backs off and retries instead of failing.
    """

    MIN_GAP_S = float(os.environ.get("JLCPICK_EASYEDA_GAP", "1.5"))
    BACKOFF_S = (30, 60, 120, 240)
    _last = 0.0

    @staticmethod
    def _use_mirror() -> bool:
        """Point component/search lookups at lceda.cn, EasyEDA's China host.

        It serves the same official library through the same API. Only the
        easyeda.com API host rate-limits with 403; STEP files come from
        modules.easyeda.com either way. Returns False if already switched.
        """
        import LCSC2Altium.easyeda_client as ec
        if "lceda.cn" in ec.COMPONENT_URL:
            return False
        ec.COMPONENT_URL = ec.COMPONENT_URL.replace("https://easyeda.com", "https://lceda.cn")
        ec.SEARCH_URL = ec.SEARCH_URL.replace("https://easyeda.com", "https://lceda.cn")
        log.warning("easyeda.com answered 403 - using the lceda.cn mirror for component data")
        return True

    def _paced(self, fn, *args):
        import time
        from LCSC2Altium.easyeda_client import EasyedaAccessDeniedError
        if os.environ.get("JLCPICK_EASYEDA_HOST", "").lower() == "lceda.cn":
            self._use_mirror()
        attempt = 0
        while True:
            wait = self.MIN_GAP_S - (time.monotonic() - CachingClient._last)
            if wait > 0:
                time.sleep(wait)
            try:
                return fn(*args)
            except EasyedaAccessDeniedError:
                if self._use_mirror():
                    continue  # retry at once on the mirror
                if attempt == len(self.BACKOFF_S):
                    raise
                log.warning("EasyEDA 403 - backing off %ss", self.BACKOFF_S[attempt])
                time.sleep(self.BACKOFF_S[attempt])
                attempt += 1
            finally:
                CachingClient._last = time.monotonic()

    def fetch_component(self, lcsc: str):
        path = CACHE / f"{lcsc.strip().upper()}.component.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        result = self._paced(super().fetch_component, lcsc)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
        return result

    def find_exact(self, keyword: str, lcsc: str):
        path = CACHE / f"{lcsc.strip().upper()}.search.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")) or None
        hit = self._paced(super().find_exact, keyword, lcsc)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hit or {}), encoding="utf-8")
        return hit

    def download_step(self, model_uuid: str) -> bytes:
        path = CACHE / f"{model_uuid}.step"
        if path.is_file():
            return path.read_bytes()
        data = self._paced(super().download_step, model_uuid)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data


def _codes(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        for tok in v.replace(",", " ").replace(";", " ").split():
            tok = tok.strip().upper()
            if tok and not tok.startswith("C"):
                tok = "C" + tok
            if tok and tok not in out:
                out.append(tok)
    return out


# Names come from the parsed libraries, not from the static get_*_names()
# helpers: those return OLE storage names, which Altium truncates to 31
# characters ("SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BR" -> "...LS2.8-"), so any
# name comparison against them fails. LCSC2Altium's verify_library has the
# same problem, hence _verify below.
def _existing_symbols(schlib: Path) -> list[str]:
    return [s.name for s in am.AltiumSchLib(schlib).symbols] if schlib.is_file() else []


def _existing_footprints(pcblib: Path) -> list[str]:
    return list(am.AltiumPcbLib(pcblib).footprint_names()) if pcblib.is_file() else []


def _verify(built) -> list[str]:
    problems = []
    missing = set(built.symbols) - set(_existing_symbols(built.schlib))
    if missing:
        problems.append(f"SchLib missing symbols: {sorted(missing)}")
    missing = set(built.footprints) - set(_existing_footprints(built.pcblib))
    if missing:
        problems.append(f"PcbLib missing footprints: {sorted(missing)}")
    return problems


def _replace_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def merge_into(new_sch: Path, new_pcb: Path, schlib: Path, pcblib: Path,
               replace_symbols: set[str], replace_footprints: set[str]) -> None:
    """Merge the freshly built one-off libraries into the targets."""
    work = Path(tempfile.mkdtemp(prefix="l2a-merge-"))
    try:
        # ---- SchLib
        if schlib.is_file():
            base = schlib
            if replace_symbols:
                lib = am.AltiumSchLib(schlib)
                for name in replace_symbols:
                    lib.remove_symbol(name)
                base = work / "base.SchLib"
                lib.save(base)
            merged = work / "merged.SchLib"
            am.AltiumSchLib.merge([base, new_sch], merged, handle_conflicts="skip")
            _replace_file(merged, schlib)
        else:
            _replace_file(new_sch, schlib)

        # ---- PcbLib: keep the target's footprints, add only new names
        have = set(_existing_footprints(pcblib)) - replace_footprints
        fresh = [n for n in _existing_footprints(new_pcb) if n not in have]
        if pcblib.is_file():
            parts = []
            if have:
                keep = work / "keep.PcbLib"
                am.AltiumPcbLib(pcblib).save_subset(keep, sorted(have))
                parts.append(keep)
            if fresh:
                add = work / "add.PcbLib"
                am.AltiumPcbLib(new_pcb).save_subset(add, fresh)
                parts.append(add)
            if parts and (fresh or replace_footprints):
                combined = am.AltiumPcbLib.combine(parts)
                out = work / "merged.PcbLib"
                combined.save(out)
                _replace_file(out, pcblib)
        else:
            _replace_file(new_pcb, pcblib)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lcsc2altium-cli", description=__doc__.split("\n\n")[0])
    ap.add_argument("--lcsc", action="append", required=True, help="LCSC code(s); repeatable")
    ap.add_argument("--schlib", required=True)
    ap.add_argument("--pcblib", required=True)
    ap.add_argument("--3d", dest="model_3d", action="store_true", help="embed STEP bodies")
    ap.add_argument("--replace", action="store_true", help="re-import parts already present")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("altium_monkey").setLevel(logging.WARNING)

    schlib, pcblib = Path(a.schlib).expanduser().resolve(), Path(a.pcblib).expanduser().resolve()
    codes = _codes(a.lcsc)
    have_symbols = set(_existing_symbols(schlib))

    report, entries = [], []
    with CachingClient() as client:
        for code in codes:
            try:
                entry = load_component(client, code, want_footprint=True, want_step=a.model_3d)
            except (EasyedaError, Exception) as exc:  # noqa: BLE001
                report.append({"lcsc": code, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                continue
            # Naming convention: footprints take EasyEDA's footprint TITLE
            # (e.g. SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BL), not the LCSC package
            # string. Two parts with the same package string can use different
            # EasyEDA footprints (DW01A "-BL" vs FS8205A "-BR"); since the merge
            # de-duplicates footprints by name, a generic name would silently
            # link one part's symbol to the other's pads. The title is unique
            # per EasyEDA footprint, so same name means same geometry. The 3D
            # body takes this name too.
            title = (client.fetch_component(code).get("packageDetail") or {}).get("title")
            if entry.footprint is not None and title:
                entry.footprint_name = _sanitize(title)
            row = {"lcsc": code, "mpn": entry.info.mpn, "package": entry.info.package,
                   "symbol": entry.symbol_name, "footprint": entry.footprint_name or None,
                   "model_3d": bool(entry.step_data), "ok": True}
            if entry.model_error:
                row["model_error"] = entry.model_error
            if entry.symbol_name in have_symbols and not a.replace:
                row["skipped"] = "symbol already in library (use --replace to re-import)"
            else:
                entries.append(entry)
            report.append(row)

    if entries:
        work = Path(tempfile.mkdtemp(prefix="l2a-build-"))
        try:
            built = build_library(entries, work, "new", include_symbols=True,
                                  include_footprints=True, include_models=a.model_3d)
            problems = _verify(built)
            if problems:
                raise RuntimeError("; ".join(problems))
            replace_sym = {e.symbol_name for e in entries} if a.replace else set()
            replace_fp = {e.footprint_name for e in entries if e.footprint_name} if a.replace else set()
            merge_into(built.schlib, built.pcblib, schlib, pcblib, replace_sym, replace_fp)
        except Exception as exc:  # noqa: BLE001
            for row in report:
                if row.get("ok") and not row.get("skipped"):
                    row.update(ok=False, error=f"build/merge failed: {exc}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # Final round-trip check on the target files
    final_syms = set(_existing_symbols(schlib))
    final_fps = set(_existing_footprints(pcblib))
    for row in report:
        if row.get("ok"):
            row["in_schlib"] = row["symbol"] in final_syms
            row["in_pcblib"] = (row["footprint"] in final_fps) if row.get("footprint") else None
            if not row["in_schlib"]:
                row["ok"] = False

    ok = all(r.get("ok") for r in report)
    if a.json:
        print(json.dumps({"schlib": str(schlib), "pcblib": str(pcblib), "ok": ok, "parts": report}, indent=1))
    else:
        for r in report:
            status = "ok" if r.get("ok") else "FAIL"
            extra = r.get("skipped") or r.get("error") or ""
            print(f"{status:4} {r['lcsc']:<10} {str(r.get('mpn')):<24} sym={r.get('symbol')} "
                  f"fp={r.get('footprint')} 3d={r.get('model_3d')} {extra}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
