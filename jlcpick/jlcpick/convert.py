"""Pull chosen parts into an EDA library.

Two backends:

  altium  - Gort-Power/LCSC2Altium, through the CLI wrapper in
            tools/lcsc2altium_cli.py. install.ps1 puts it on PATH as
            lcsc2altium-cli and points JLCPICK_LCSC2ALTIUM at it.
  kicad   - easyeda2kicad (pip install easyeda2kicad), if you use KiCad.

The Altium side is a subprocess call so that any wrapper taking
--lcsc --schlib --pcblib [--3d] can stand in for ours. Writing .SchLib and
.PcbLib means OLE compound documents with binary records inside; LCSC2Altium
and altium-monkey already do that, including the EasyEDA Y-down to Altium
Y-up flip and the origin computed from the converted geometry.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .easyeda import normalise_lcsc
from .util import log

LCSC2ALTIUM_ENV = "JLCPICK_LCSC2ALTIUM"


@dataclass
class ImportResult:
    lcsc: str
    backend: str
    ok: bool
    symbol: bool = False
    footprint: bool = False
    model_3d: bool = False
    output: str | None = None
    message: str | None = None

    def to_dict(self) -> dict:
        return {
            "lcsc": self.lcsc,
            "backend": self.backend,
            "ok": self.ok,
            "symbol": self.symbol,
            "footprint": self.footprint,
            "model_3d": self.model_3d,
            "output": self.output,
            "message": self.message,
        }


# --------------------------------------------------------------------------
# KiCad backend
# --------------------------------------------------------------------------

def import_kicad(
    lcsc: str,
    output: str | Path,
    overwrite: bool = True,
    project_relative: bool = False,
) -> ImportResult:
    lcsc = normalise_lcsc(lcsc)
    if shutil.which("easyeda2kicad") is None:
        return ImportResult(
            lcsc, "kicad", False,
            message="easyeda2kicad not on PATH - pip install easyeda2kicad",
        )

    out = Path(output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["easyeda2kicad", "--full", f"--lcsc_id={lcsc}", "--output", str(out)]
    if overwrite:
        cmd.append("--overwrite")
    if project_relative:
        cmd.append("--project-relative")

    log(f"running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    ok = proc.returncode == 0
    blob = (proc.stdout + proc.stderr).lower()
    return ImportResult(
        lcsc,
        "kicad",
        ok,
        symbol=ok and "symbol" in blob,
        footprint=ok and "footprint" in blob,
        model_3d=ok and "3d" in blob,
        output=str(out),
        message=(proc.stdout or proc.stderr or "").strip()[:800] or None,
    )


# --------------------------------------------------------------------------
# Altium backend
# --------------------------------------------------------------------------

def import_altium(
    lcsc: str,
    schlib: str | Path,
    pcblib: str | Path,
    include_3d: bool = True,
    tool_path: str | None = None,
) -> ImportResult:
    """Convert one LCSC part into your Altium libraries.

    Runs the wrapper named by JLCPICK_LCSC2ALTIUM (install.ps1 sets it to
    the lcsc2altium-cli shim) with --lcsc --schlib --pcblib [--3d]. The
    wrapper merges into existing libraries and skips parts already there.

    NOTE on the 3D body: LCSC2Altium places the model at the footprint
    origin with no offset, because Altium centres an embedded model there
    while EasyEDA's c_origin is an offset inside the package. If a part
    comes in with its model floating off the pads, that is the line to look
    at, not your own code.

    NOTE on naming: the model and body take the footprint's name, so renaming
    a footprint renames its 3D body too. Decide your naming convention before
    you batch-import a hundred parts.
    """
    lcsc = normalise_lcsc(lcsc)
    wrapper = tool_path or os.environ.get(LCSC2ALTIUM_ENV)

    if not wrapper:
        return ImportResult(
            lcsc, "altium", False,
            message=(
                f"Altium backend not configured: {LCSC2ALTIUM_ENV} is not set. "
                "Run jlc2altium's install.ps1, or point it at tools/lcsc2altium_cli.py "
                "(any command taking --lcsc --schlib --pcblib [--3d] works)."
            ),
        )

    cmd = [
        wrapper,
        "--lcsc", lcsc,
        "--schlib", str(Path(schlib).expanduser()),
        "--pcblib", str(Path(pcblib).expanduser()),
    ]
    if include_3d:
        cmd.append("--3d")

    log(f"running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return ImportResult(lcsc, "altium", False, message=f"wrapper not found: {wrapper}")
    except subprocess.TimeoutExpired:
        return ImportResult(lcsc, "altium", False, message="wrapper timed out after 300s")

    ok = proc.returncode == 0
    return ImportResult(
        lcsc,
        "altium",
        ok,
        symbol=ok,
        footprint=ok,
        model_3d=ok and include_3d,
        output=f"{schlib} / {pcblib}",
        message=(proc.stdout or proc.stderr or "").strip()[:800] or None,
    )
