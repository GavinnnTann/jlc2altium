"""Call an altium-mcp tool function from a shell, without an MCP client.

altium-mcp (github.com/coffeenmusic/altium-mcp) exposes its tools as async
functions in server/main.py. Importing that module sets up the same bridge the
MCP server uses (request.json -> Altium DelphiScript -> response.json), so a
script or a shell gets every tool an MCP client would have. Useful for batch
work and for agents whose MCP session has not loaded the server yet.

Usage:
    altium_call.py --list                          list tools and signatures
    altium_call.py <tool>                          no arguments
    altium_call.py <tool> '{"key": "value"}'       JSON arguments
    altium_call.py <tool> @args.json               arguments from a file (best on Windows)

Environment:
    ALTIUM_MCP_SERVER   altium-mcp's server/ folder (default ~/src/altium-mcp/server)
    ALTIUM_MCP_HOME     altium-mcp data folder (default ~/.altium-mcp); its
                        venvs/ hold the interpreter this script re-runs under
                        when mcp is not importable here
"""
from __future__ import annotations

import asyncio
import glob
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

SERVER_DIR = Path(os.environ.get("ALTIUM_MCP_SERVER", Path.home() / "src" / "altium-mcp" / "server"))


def _relaunch_in_server_venv() -> None:
    """Re-run this script with the python of altium-mcp's own venv."""
    home = Path(os.environ.get("ALTIUM_MCP_HOME", Path.home() / ".altium-mcp"))
    pythons = sorted(glob.glob(str(home / "venvs" / "*" / "Scripts" / "python.exe")) +
                     glob.glob(str(home / "venvs" / "*" / "bin" / "python")),
                     key=os.path.getmtime, reverse=True)
    if not pythons or os.path.samefile(pythons[0], sys.executable):
        raise SystemExit("altium-mcp's venv was not found. Build it first: "
                         "python <altium-mcp>/start_server.py --build")
    sys.exit(subprocess.call([pythons[0], "-X", "utf8", __file__, *sys.argv[1:]]))


try:
    import mcp  # noqa: F401  (only to test the interpreter)
except ImportError:
    _relaunch_in_server_venv()

sys.path.insert(0, str(SERVER_DIR))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import main  # noqa: E402  (altium-mcp server module)


def _tools() -> dict:
    out = {}
    for name, fn in vars(main).items():
        if name.startswith("_") or not inspect.iscoroutinefunction(fn):
            continue
        params = list(inspect.signature(fn).parameters)
        if params and params[0] == "ctx":
            out[name] = fn
    return out


def cli(argv: list[str]) -> int:
    tools = _tools()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        for name in sorted(tools):
            sig = str(inspect.signature(tools[name]))
            print(f"{name}{sig.split(', ', 1)[-1] if ', ' in sig else '()'}")
        return 0
    name = argv[0]
    if name not in tools:
        print(f"unknown tool: {name} (try --list)", file=sys.stderr)
        return 2
    args: dict = {}
    if len(argv) > 1:
        raw = argv[1]
        args = json.loads(Path(raw[1:]).read_text(encoding="utf-8")) if raw.startswith("@") else json.loads(raw)

    result = asyncio.run(tools[name](None, **args))
    items = result if isinstance(result, (list, tuple)) else [result]
    for i, item in enumerate(items):
        data = getattr(item, "data", None)
        if isinstance(data, (bytes, bytearray)):          # an MCP image (get_screenshot)
            out = Path(os.environ.get("TEMP", ".")) / f"{name}_{i}.png"
            out.write_bytes(data)
            print(json.dumps({"image_saved": str(out)}))
        elif isinstance(item, str):
            print(item)
        else:
            print(json.dumps(item, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
