"""Capture a schematic into Altium from a Python spec, through altium-mcp.

A spec file (see examples/led_demo_spec.py) defines:
    PROJECT    full path of the .PrjPcb
    LIBRARIES  library files to add to the project (paths relative to the project folder)
    SHEETS     {n: {"file": "Power.SchDoc", "parts": [...], "conns": [...]}}
               part: (designator, schlib_path, symbol, x_mil, y_mil, rotation, mirror, comment)
               conn: see sheetgen.wiring_ops
    RAILS      optional set of net names drawn as supply ports
    NO_BOM     optional designator prefixes set to Standard (No BOM), e.g. ["TP"]
    NET_TIES   optional designator prefixes set to Net Tie (No BOM), e.g. ["NT"]

Usage (Altium running; re-runs itself under altium-mcp's venv):
    capture.py <spec.py> setup              create missing sheets (A3), add libraries,
                                            global net scope, save the project
    capture.py <spec.py> build <n|all>      clear, place, wire, fix text, check
    capture.py <spec.py> update <n> J2 U5.16 ...
                                            change only these parts / pins: their wiring
                                            is removed, parts re-placed from the spec,
                                            their connections re-wired. Everything else
                                            keeps its unique ID (PCB placement survives)
    capture.py <spec.py> place|wire|fixtext|check|clear <n>
    capture.py <spec.py> audit              component kinds, compile, compare Altium's
                                            netlist with the spec, print every net.
                                            Not read-only: it sets NO_BOM / NET_TIES
                                            component kinds and saves the sheets
    capture.py <spec.py> erc                reload sheets, compile, list ERC violations
    capture.py <spec.py> render <out_dir>   PNG of each sheet drawn by Altium itself
                                            (overwrites the clipboard)

Why measured pins: a pin's electrical end is Pin.Location + PinLength along the
pin; predicted coordinates after rotation/mirroring go wrong. Parts are placed
first, the saved sheet is read back, and wiring is drawn from measured points.
"""
from __future__ import annotations

import asyncio
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_DIR = Path(os.environ.get("ALTIUM_MCP_SERVER", Path.home() / "src" / "altium-mcp" / "server"))


def _relaunch_in_server_venv() -> None:
    home = Path(os.environ.get("ALTIUM_MCP_HOME", Path.home() / ".altium-mcp"))
    pythons = sorted(glob.glob(str(home / "venvs" / "*" / "Scripts" / "python.exe")) +
                     glob.glob(str(home / "venvs" / "*" / "bin" / "python")),
                     key=os.path.getmtime, reverse=True)
    if not pythons or os.path.samefile(pythons[0], sys.executable):
        raise SystemExit("altium-mcp's venv was not found. Build it: python <altium-mcp>/start_server.py --build")
    sys.exit(subprocess.call([pythons[0], "-X", "utf8", __file__, *sys.argv[1:]]))


try:
    import mcp  # noqa: F401
except ImportError:
    if __name__ == "__main__":
        _relaunch_in_server_venv()

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SERVER_DIR))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import sheetgen  # noqa: E402


def _main():
    import main  # altium-mcp server module (needs its venv)
    return main


def pas(s: str) -> str:
    """Quote text for a DelphiScript string literal."""
    return str(s).replace("'", "''")


def ps_quote(s) -> str:
    """Quote text for a PowerShell single-quoted string ("Bob's Board" paths)."""
    return str(s).replace("'", "''")


def load_spec(path: str):
    spec = importlib.util.spec_from_file_location("capture_spec", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def project_dir(spec) -> Path:
    return Path(spec.PROJECT).resolve().parent


def sheet_path(spec, n: int) -> str:
    return str(project_dir(spec) / spec.SHEETS[n]["file"])


def run_script(script: str, timeout: int = 300) -> dict:
    out = json.loads(asyncio.run(_main().run_altium_script(None, script=script, timeout_seconds=timeout)))
    if not out.get("success"):
        raise SystemExit(f"Altium script failed: {json.dumps(out)[:800]}")
    return out


def run_edit(spec, n: int, ops: list) -> dict:
    return json.loads(asyncio.run(_main().edit_schematic(None, sheet_path=sheet_path(spec, n), operations=ops,
                                                         unit="mil", save=True)))


FIND_PROJECT = """
Proj := nil;
for i := 0 to GetWorkspace.DM_ProjectCount - 1 do
    if SameText(GetWorkspace.DM_Projects(i).DM_ProjectFullPath, '{project}') then
        Proj := GetWorkspace.DM_Projects(i);
if Proj = nil then
    Proj := GetWorkspace.DM_OpenProject('{project}', True);
"""

SETUP_PAS = """var
    Proj, SrvDoc, Sch, PrjSrv : IDispatch;
    Dir, Path, Log : String;
    Names : TStringList;
    i, k, j, Found : Integer;
""" + FIND_PROJECT + """
Dir := ExtractFilePath(Proj.DM_ProjectFullPath);
Names := TStringList.Create;
{add_names}
Log := '';
for k := 0 to Names.Count - 1 do
begin
    Path := Dir + Names[k];
    Found := 0;
    for j := 0 to Proj.DM_LogicalDocumentCount - 1 do
        if SameText(Proj.DM_LogicalDocuments(j).DM_FullPath, Path) then Found := 1;
    if Found = 0 then
    begin
        if (UpperCase(ExtractFileExt(Path)) = '.SCHDOC') and (not FileExists(Path)) then
        begin
            SrvDoc := Client.OpenNewDocument('SCH', Path, Names[k], False);
            Client.ShowDocument(SrvDoc);
            Sch := SchServer.GetCurrentSchDocument;
            Sch.SheetStyle := {sheet_style};
            Sch.UseCustomSheet := False;
            Sch.SetState_xSizeySize;
            SrvDoc.SetFileName(Path);
            SrvDoc.Modified := True;
            SrvDoc.DoFileSave('');
            Log := Log + 'created ' + Names[k] + '; ';
        end;
        Proj.DM_AddSourceDocument(Path);
        Log := Log + 'added ' + Names[k] + '; ';
    end;
end;
Names.Free;
Proj.DM_SetHierarchyMode(3);   // eFlatten_Global: net labels join across sheets
PrjSrv := Client.GetDocumentByPath(Proj.DM_ProjectFullPath);
PrjSrv.Modified := True;
PrjSrv.DoFileSave('PrjPcb');
ResultText := Log + 'net scope global; project saved';
"""

CLEAR_PAS = """var
    Sch, Iter, Obj : IDispatch;
    k, Removed : Integer;
Sch := OpenSchDoc('{sheet}');
Removed := 0;
SchServer.ProcessControl.PreProcess(Sch, '');
for k := 1 to 5000 do
begin
    Iter := Sch.SchIterator_Create;
    Iter.AddFilter_ObjectSet(MkSet(eSchComponent, eWire, eNetLabel, ePowerObject, eJunction, eNoERC));
    Obj := Iter.FirstSchObject;
    Sch.SchIterator_Destroy(Iter);
    if Obj <> nil then
    begin
        Sch.RemoveSchObject(Obj);
        SchServer.RobotManager.SendMessage(Sch.I_ObjectAddress, c_BroadCast,
            SCHM_PrimitiveRegistration, Obj.I_ObjectAddress);
        Removed := Removed + 1;
    end;
end;
SchServer.ProcessControl.PostProcess(Sch, '');
SaveDoc('{sheet}');
ResultText := 'removed ' + IntToStr(Removed);
"""

AUDIT_PAS = """var
    Proj, Sch, Iter, Comp, Flat, Net, Pin : IDispatch;
    Des, Line : String;
    Sheets, Lines, NoBom, Ties : TStringList;
    i, j, k, Kinds : Integer;
""" + FIND_PROJECT + """
Sheets := TStringList.Create;
{add_sheets}
NoBom := TStringList.Create;
{add_nobom}
Ties := TStringList.Create;
{add_ties}
Kinds := 0;
for i := 0 to Sheets.Count - 1 do
begin
    Sch := OpenSchDoc(Sheets[i]);
    SchServer.ProcessControl.PreProcess(Sch, '');
    Iter := Sch.SchIterator_Create;
    Iter.AddFilter_ObjectSet(MkSet(eSchComponent));
    Comp := Iter.FirstSchObject;
    while Comp <> nil do
    begin
        Des := Comp.Designator.Text;
        for k := 0 to NoBom.Count - 1 do
            if Pos(NoBom[k], Des) = 1 then
            begin
                SchServer.RobotManager.SendMessage(Comp.I_ObjectAddress, c_BroadCast, SCHM_BeginModify, c_NoEventData);
                Comp.ComponentKind := 5;   // Standard (No BOM)
                SchServer.RobotManager.SendMessage(Comp.I_ObjectAddress, c_BroadCast, SCHM_EndModify, c_NoEventData);
                Kinds := Kinds + 1;
            end;
        for k := 0 to Ties.Count - 1 do
            if Pos(Ties[k], Des) = 1 then
            begin
                SchServer.RobotManager.SendMessage(Comp.I_ObjectAddress, c_BroadCast, SCHM_BeginModify, c_NoEventData);
                Comp.ComponentKind := 4;   // Net Tie (No BOM)
                SchServer.RobotManager.SendMessage(Comp.I_ObjectAddress, c_BroadCast, SCHM_EndModify, c_NoEventData);
                Kinds := Kinds + 1;
            end;
        Comp := Iter.NextSchObject;
    end;
    Sch.SchIterator_Destroy(Iter);
    SchServer.ProcessControl.PostProcess(Sch, '');
    SaveDoc(Sheets[i]);
end;
Proj.DM_Compile;
Flat := Proj.DM_DocumentFlattened;
Lines := TStringList.Create;
for i := 0 to Flat.DM_NetCount - 1 do
begin
    Net := Flat.DM_Nets(i);
    Line := 'NET|' + Net.DM_NetName + '|';
    for j := 0 to Net.DM_PinCount - 1 do
    begin
        Pin := Net.DM_Pins(j);
        Line := Line + Pin.DM_LogicalPartDesignator + '.' + Pin.DM_PinNumber + ' ';
    end;
    Lines.Add(Line);
end;
Lines.SaveToFile('{out}');
ResultText := 'kinds set: ' + IntToStr(Kinds) + '; nets: ' + IntToStr(Flat.DM_NetCount);
"""

REOPEN_PAS = """var
    D : IDispatch;
{body}
ResultText := 'reloaded';
"""

COPY_PAS = """var
    D : IDispatch;
D := Client.OpenDocument('SCH', '{sheet}');
Client.ShowDocument(D);
Sleep(1000);
ResetParameters;
AddStringParameter('Action', 'All');
RunProcess('Sch:Select');
ResetParameters;
RunProcess('Sch:Copy');
ResetParameters;
RunProcess('Sch:DeSelect');
ResultText := 'copied';
"""

RASTERIZE_PS = r"""
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class Clip {
  [DllImport("user32.dll")] public static extern bool OpenClipboard(IntPtr h);
  [DllImport("user32.dll")] public static extern bool CloseClipboard();
  [DllImport("user32.dll")] public static extern IntPtr GetClipboardData(uint fmt);
  [DllImport("user32.dll")] public static extern bool IsClipboardFormatAvailable(uint fmt);
  [DllImport("gdi32.dll")] public static extern IntPtr CopyEnhMetaFile(IntPtr h, string f);
  [DllImport("gdi32.dll")] public static extern bool DeleteEnhMetaFile(IntPtr h);
}
'@
if (-not [Clip]::IsClipboardFormatAvailable(14)) { Write-Output 'NOEMF'; exit 1 }
[Clip]::OpenClipboard([IntPtr]::Zero) | Out-Null
$h = [Clip]::GetClipboardData(14)
$copy = [Clip]::CopyEnhMetaFile($h, '__EMF__')
[Clip]::DeleteEnhMetaFile($copy) | Out-Null
[Clip]::CloseClipboard() | Out-Null
$img = [System.Drawing.Image]::FromFile('__EMF__')
$w = __W__; $h2 = [int]($w * $img.Height / $img.Width)
$bmp = New-Object System.Drawing.Bitmap $w, $h2
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::White)
$g.SmoothingMode = 'AntiAlias'
$g.TextRenderingHint = 'AntiAliasGridFit'
$g.DrawImage($img, 0, 0, $w, $h2)
$g.Dispose()
$bmp.Save('__PNG__', [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose(); $img.Dispose()
Write-Output ('rendered ' + $w + 'x' + $h2)
"""


# ------------------------------------------------------------------ commands
def cmd_setup(spec):
    names = [s["file"] for s in spec.SHEETS.values()] + list(getattr(spec, "LIBRARIES", []))
    script = (SETUP_PAS.replace("{project}", pas(Path(spec.PROJECT).resolve()))
              .replace("{add_names}", "\n".join(f"Names.Add('{pas(n)}');" for n in names))
              .replace("{sheet_style}", str(getattr(spec, "SHEET_STYLE", 1))))
    print(run_script(script).get("result"))


def cmd_clear(spec, n):
    print(run_script(CLEAR_PAS.replace("{sheet}", pas(sheet_path(spec, n)))).get("result"))


def place_ops(spec, n) -> list:
    parts = spec.SHEETS[n]["parts"]
    ops = [{"op": "place_component", "library": lib, "symbol": sym, "designator": ref, "x": x, "y": y,
            "rotation": rot, "mirror": bool(mir), "comment": com}
           for ref, lib, sym, x, y, rot, mir, com in parts]
    hide = set(getattr(spec, "HIDE_COMMENT_SYMBOLS", {"TestPoint"}))
    ops += [{"op": "move_text", "designator": ref, "name": "Comment", "visible": False}
            for ref, lib, sym, *_ in parts if sym in hide]
    return ops


def cmd_place(spec, n):
    r = run_edit(spec, n, place_ops(spec, n))
    print(f"placed: applied {r.get('applied')}, errors {r.get('errors')}")


def cmd_wire(spec, n):
    parts = sheetgen.read_pins(sheet_path(spec, n))
    missing = [p[0] for p in spec.SHEETS[n]["parts"] if p[0] not in parts]
    if missing:
        raise SystemExit(f"not on the sheet yet: {missing}")
    ops = sheetgen.wiring_ops(parts, spec.SHEETS[n]["conns"], rails=getattr(spec, "RAILS", None))
    r = run_edit(spec, n, ops)
    print(f"wired: {len(ops)} operations, applied {r.get('applied')}, errors {r.get('errors')}")


def check_report(spec, n) -> dict:
    return json.loads(asyncio.run(_main().check_schematic(None, path=sheet_path(spec, n), max_issues_per_sheet=200)))


def cmd_check(spec, n):
    rep = check_report(spec, n)
    print(f"sheet {n} issues: {rep.get('issue_counts')}")
    for r in rep.get("results", []):
        for i in r.get("issues", []):
            print("  ", i.get("message", "")[:160])


def cmd_fixtext(spec, n):
    ops = []
    for r in check_report(spec, n).get("results", []):
        for i in r.get("issues", []):
            des = i.get("designator")
            for t in (i.get("suggest") or {}).get("texts", []) if des else []:
                op = {"op": "move_text", "designator": des, "name": t.get("parameter") or t.get("name"),
                      "x": t["x"], "y": t["y"]}
                op.update({k: t[k] for k in ("justification", "orientation") if k in t})
                ops.append(op)
    if ops:
        r = run_edit(spec, n, ops)
        print(f"text moves: {len(ops)} applied {r.get('applied')}")
    return len(ops)


def cmd_build(spec, which):
    for n in (sorted(spec.SHEETS) if which == "all" else [int(which)]):
        print(f"== sheet {n}: {spec.SHEETS[n]['file']}")
        cmd_clear(spec, n)
        cmd_place(spec, n)
        cmd_wire(spec, n)
        for _ in range(2):
            if not cmd_fixtext(spec, n):
                break
        cmd_check(spec, n)


def attached_delete_ops(path: str, points: list, tol: float = 0.6) -> list:
    """edit_schematic deletions for the wiring attached at the given pin ends.

    A wire with an end on one of the points is removed. Its other end is
    treated as a free stub end (and the label, power port there removed) only
    when no other wire ends at it, so shared nodes and tees are left alone.
    No-ERC markers on the points go too.
    """
    from schdoc_reader import coord, read_schdoc

    wires, labels, ports, noercs = [], [], [], []
    for r in read_schdoc(path):
        kind = r.get("RECORD") if r else None
        loc = (coord(r, "LOCATION.X"), coord(r, "LOCATION.Y")) if r else None
        if kind == "27":
            n = int(r.get("LOCATIONCOUNT", 0) or 0)
            wires.append([(coord(r, f"X{k}"), coord(r, f"Y{k}")) for k in range(1, n + 1)])
        elif kind == "25":
            labels.append((loc, r.get("TEXT", "")))
        elif kind == "17":
            ports.append((loc, r.get("TEXT", "")))
        elif kind == "22":
            noercs.append(loc)

    def near(a, b):
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    ops = []
    for p in points:
        for w in wires:
            if not w or not (near(w[0], p) or near(w[-1], p)):
                continue
            other = w[-1] if near(w[0], p) else w[0]
            ops.append({"op": "delete_at", "kind": "wire", "x": p[0], "y": p[1]})
            if any(w2 is not w and w2 and (near(w2[0], other) or near(w2[-1], other)) for w2 in wires):
                continue
            ops += [{"op": "delete_at", "kind": "net_label", "x": loc[0], "y": loc[1], "text": t}
                    for loc, t in labels if near(loc, other)]
            ops += [{"op": "delete_at", "kind": "power_port", "x": loc[0], "y": loc[1], "text": t}
                    for loc, t in ports if near(loc, other)]
        ops += [{"op": "delete_at", "kind": "no_erc", "x": p[0], "y": p[1]} for loc in noercs if near(loc, p)]
    return ops


def cmd_update(spec, n, targets):
    """Change only some parts or pins of a sheet, keeping everything else.

    targets: designators (replace or add the whole part as the spec now says)
    and REF.PIN (re-wire one pin of a part that stays). Untouched parts keep
    their unique IDs, so an existing PCB keeps their placement on the next
    Update PCB. A part that sits in a "tee" connection is not supported here:
    rebuild that sheet instead.
    """
    path = sheet_path(spec, n)
    now = sheetgen.read_pins(path)
    comps = [t for t in targets if "." not in t]
    pins = [t for t in targets if "." in t]
    points = [p["hot"] for t in comps if t in now for p in now[t]["pins"].values()]
    points += [sheetgen.pin_of(now, t)["hot"] for t in pins]
    ops = attached_delete_ops(path, points)
    ops += [{"op": "delete_component", "designator": t} for t in comps if t in now]
    if ops:
        r = run_edit(spec, n, ops)
        print(f"removed: {r.get('applied')} objects, errors {r.get('errors')}")
    placing = [op for op in place_ops(spec, n) if op.get("designator") in comps]
    if placing:
        r = run_edit(spec, n, placing)
        print(f"placed: applied {r.get('applied')}, errors {r.get('errors')}")

    def refs(c):
        if c[0] == "wire":
            return [c[1], c[2]]
        if c[0] == "tee":
            return [c[1], c[2], c[3]]
        return [c[0]]

    conns = [c for c in spec.SHEETS[n]["conns"]
             if any(r.split(".")[0] in comps or r in pins for r in refs(c))]
    parts = sheetgen.read_pins(path)
    wiring = sheetgen.wiring_ops(parts, conns, rails=getattr(spec, "RAILS", None))
    if wiring:
        r = run_edit(spec, n, wiring)
        print(f"wired: {len(wiring)} operations, applied {r.get('applied')}, errors {r.get('errors')}")
    for _ in range(2):
        if not cmd_fixtext(spec, n):
            break
    cmd_check(spec, n)


def spec_netlist(spec) -> dict:
    nets: dict[str, set] = {}
    for n, sheet in spec.SHEETS.items():
        parts = sheetgen.read_pins(sheet_path(spec, n))
        for name, pins in sheetgen.netlist_from_conns(parts, sheet["conns"]).items():
            nets.setdefault(name, set()).update(pins)
    return nets


def cmd_audit(spec):
    out = Path(tempfile.gettempdir()) / "capture_netlist.txt"
    sheets = [sheet_path(spec, n) for n in sorted(spec.SHEETS)]
    lst = lambda var, items: "\n".join(f"{var}.Add('{pas(x)}');" for x in items)  # noqa: E731
    script = (AUDIT_PAS.replace("{project}", pas(Path(spec.PROJECT).resolve()))
              .replace("{add_sheets}", lst("Sheets", sheets))
              .replace("{add_nobom}", lst("NoBom", getattr(spec, "NO_BOM", [])))
              .replace("{add_ties}", lst("Ties", getattr(spec, "NET_TIES", [])))
              .replace("{out}", pas(out)))
    print(run_script(script, timeout=600).get("result"))

    compiled = {}
    for line in out.read_text(encoding="latin-1").splitlines():
        _, name, pins = line.split("|", 2)
        compiled[name] = set(pins.split())
    singles = {n for n, v in compiled.items() if len(v) == 1}
    spec_nets = spec_netlist(spec)
    # Single-pin nets (a label used once, a lone test pad) are left out on
    # both sides and listed below, so they do not read as a mismatch.
    cp = {frozenset(v) for n, v in compiled.items() if n not in singles}
    sp = {frozenset(v) for v in spec_nets.values() if len(v) > 1}
    print("MATCH: Altium's compiled netlist equals the spec" if cp == sp else "MISMATCH")
    for s in cp - sp:
        print("  only in Altium:", sorted(s))
    for s in sp - cp:
        print("  only in spec:  ", sorted(s))
    if singles:
        print("  single-pin nets (should be intentional no-connects):",
              sorted(next(iter(compiled[n])) for n in singles))
    names = {}
    for n in spec.SHEETS:
        for des, c in sheetgen.read_pins(sheet_path(spec, n)).items():
            for num, p in c["pins"].items():
                names[f"{des}.{num}"] = p["name"]
    print("\nNetlist for review:")
    for name in sorted(compiled, key=lambda k: (-len(compiled[k]), k)):
        if name in singles:
            continue
        pins = sorted(compiled[name])
        shown = [p if names.get(p, "") in ("", p.split(".")[1]) else f"{p}({names[p]})" for p in pins]
        print(f"  {name:<12}({len(pins):>2}) {' '.join(shown)}")


def cmd_erc(spec):
    body = "\n".join(f"D := Client.GetDocumentByPath('{pas(sheet_path(spec, n))}');\n"
                     f"if D <> nil then Client.CloseDocument(D);\n"
                     f"D := Client.OpenDocument('SCH', '{pas(sheet_path(spec, n))}');"
                     for n in sorted(spec.SHEETS))
    run_script(REOPEN_PAS.replace("{body}", body))   # stale sheet sizes give false off-sheet warnings
    r = json.loads(asyncio.run(_main().get_erc_violations(None, compile=True, project_path=str(spec.PROJECT))))
    print({k: r.get(k) for k in ("total", "fatal", "errors", "warnings")})
    for v in (r.get("violations") or [])[:60]:
        print("  ", v.get("level"), "|", v.get("type"), "|", str(v.get("detail"))[:160])


def cmd_render(spec, out_dir, width=3000):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    emf = Path(tempfile.gettempdir()) / "capture_sheet.emf"
    for n in sorted(spec.SHEETS):
        run_script(COPY_PAS.replace("{sheet}", pas(sheet_path(spec, n))), timeout=90)
        png = out / (Path(spec.SHEETS[n]["file"]).stem + ".png")
        emf.unlink(missing_ok=True)
        ps = (RASTERIZE_PS.replace("__EMF__", ps_quote(emf)).replace("__W__", str(width))
              .replace("__PNG__", ps_quote(png.resolve())))
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=200)
        print(png.name, (r.stdout + r.stderr).strip()[-120:])


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        return 1
    spec, cmd, rest = load_spec(argv[0]), argv[1], argv[2:]
    if cmd == "setup":
        cmd_setup(spec)
    elif cmd == "build":
        cmd_build(spec, rest[0] if rest else "all")
    elif cmd == "update":
        cmd_update(spec, int(rest[0]), rest[1:])
    elif cmd in ("place", "wire", "fixtext", "check", "clear"):
        {"place": cmd_place, "wire": cmd_wire, "fixtext": cmd_fixtext, "check": cmd_check,
         "clear": cmd_clear}[cmd](spec, int(rest[0]))
    elif cmd == "audit":
        cmd_audit(spec)
    elif cmd == "erc":
        cmd_erc(spec)
    elif cmd == "render":
        cmd_render(spec, rest[0] if rest else str(project_dir(spec) / "screenshots"))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
