#!/usr/bin/env python3
"""Package an Altium PCB project for a JLCPCB board + assembly (PCBA) order.

Reads the project open in Altium through altium-mcp (tools/altium_call.py) and
writes everything JLC's order page asks for into one folder:

    <name>_Gerber.zip          Gerber RS-274X + Excellon NC drill: the PCB order upload
    <name>_BOM_JLC.xlsx        Comment | Designator | Footprint | LCSC, one row per part number
    <name>_CPL_JLC.xlsx        Designator | Mid X | Mid Y | Layer | Rotation
    <name>_BOM_JLC.csv, <name>_CPL_JLC.csv   the same tables as CSV, for diffs
    JLC_ORDER_README.md        blockers, checks, warnings, excluded parts, order-form decisions
    gerber/                    Altium's raw output, reports included (not zipped)

Usage:
    jlc_fab_export.py --init <project>.PrjPcb           write a starter jlc-fab.json
    jlc_fab_export.py --config <folder>/jlc-fab.json    build the package
        --make-outline   first run: copy the board shape onto the outline layer (saves the PCB)
        --skip-gerbers   BOM, CPL and checks only
        --drc            also run Altium's DRC and summarise it in the README
        --qty N          number of boards to price the BOM at (catalogue snapshot)

Exit status: 0 = ready to order, 2 = blockers (the files are still written and
the README says why), 1 = error. Needs Altium running with the project open,
and openpyxl for the .xlsx files. Documentation: docs/jlc-fab-export.md.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import functools
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- constants

MM_PER_COORD = 0.0254 / 10000            # Altium internal unit: 1/10000 mil

# Altium V7 layer ids, as an OutJob's Gerber Plot.Set lists them. Keep-Out
# (16973837) is deliberately absent: hole keep-out rings would read as cuts.
V7_TOP, V7_BOTTOM = 16777217, 16842751
V7_MID_BASE, V7_PLANE_BASE, V7_MECH_BASE = 16777216, 16842752, 16908288
NON_COPPER = {                           # Gerber extension -> (layer, V7 id)
    "GTO": ("Top silkscreen", 16973830), "GBO": ("Bottom silkscreen", 16973831),
    "GTP": ("Top paste", 16973832), "GBP": ("Bottom paste", 16973833),
    "GTS": ("Top solder mask", 16973834), "GBS": ("Bottom solder mask", 16973835),
}
REQUIRED_NON_COPPER = ("GTO", "GTS", "GBS")   # bottom silk and paste may legitimately be empty

OBJ_ARC, OBJ_TRACK = 1, 4                # Altium object ids
COMPONENT_KINDS = {0: "Standard", 1: "Mechanical", 2: "Graphical", 3: "Net Tie", 4: "Net Tie (No BOM)",
                   5: "Standard (No BOM)", 6: "Jumper"}

JLC = {
    "rails": "https://jlcpcb.com/help/article/how-to-add-edge-rails-fiducials-for-pcb-assembly-order",
    "process_edges": "https://jlcpcb.com/help/article/specifications-for-adding-process-edges-and-positioning-holes",
    "terms": "https://jlcpcb.com/help/article/terms-and-conditions-of-jlcpcb-assembly-service",
    "capabilities": "https://jlcpcb.com/capabilities/pcb-assembly-capabilities",
    "bom": "https://jlcpcb.com/help/article/bill-of-materials-for-pcb-assembly",
    "cpl": "https://jlcpcb.com/help/article/pick-place-file-for-pcb-assembly",
}

DEFAULTS = {
    "name": None,                         # file prefix; default: the PCB file name
    "out_dir": None,                      # default: "<project folder>/Project Outputs for <project>/JLC"
    "outjob": None,                       # default: "<project folder>/JLC Fabrication.OutJob"
    "outline_layer": 1,                   # mechanical layer carrying the board outline (.GM<n>)
    "outline_width_mm": 0.1,
    "lcsc_parameter": "LCSC Part Number",
    "bom_part_header": "LCSC",            # BOM column for the JLC/LCSC part number
    "exclude_designators": [],            # never placed by JLC (hand-fitted, ...)
    "dnp_words": ["DNP"],                 # a Comment containing one of these words = do not populate
    "rotation_log": None,                 # docs/cpl-rotations.md of the hardware project
    "edge_clearance_mm": 2.5,             # JLC assembly T&C: component body >= 2.5 mm from the edge
    "qty": 5,
}


# --------------------------------------------------------------------------- Altium bridge

@functools.lru_cache(maxsize=1)
def find_altium_call() -> Path:
    """tools/altium_call.py of this repo, $JLC2ALTIUM_HOME, or the one the altium-call shim runs."""
    cands = [Path(__file__).resolve().parents[1] / "tools" / "altium_call.py"]
    if os.environ.get("JLC2ALTIUM_HOME"):
        cands.append(Path(os.environ["JLC2ALTIUM_HOME"]) / "tools" / "altium_call.py")
    for name in ("altium-call.cmd", "altium-call"):
        shim = shutil.which(name)
        if shim:
            m = re.search(r'"?([^"\r\n]*altium_call\.py)"?', Path(shim).read_text(errors="replace"))
            if m:
                cands.append(Path(m.group(1)))
    for c in cands:
        if c.is_file():
            return c
    raise SystemExit("tools/altium_call.py not found: run from the jlc2altium repo, set JLC2ALTIUM_HOME, "
                     "or put altium-call on PATH (install.ps1 does both)")


def altium(tool: str, args: dict | None = None, timeout: int = 900):
    """Run one altium-mcp tool. Returns its JSON reply, or the raw text when it is not JSON."""
    with tempfile.TemporaryDirectory() as td:
        argfile = Path(td) / "args.json"
        argfile.write_text(json.dumps(args or {}), encoding="utf-8")
        p = subprocess.run([sys.executable, str(find_altium_call()), tool, "@" + str(argfile)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    out = p.stdout.strip()
    if p.returncode != 0:
        raise RuntimeError(f"altium-call {tool} failed ({p.returncode}): {(p.stderr or out)[-2000:]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        start = out.find("{")
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(out[start:])[0]
            except json.JSONDecodeError:
                pass
        return out


def run_script(script: str, timeout: int = 120) -> str:
    """Run DelphiScript in altium-mcp's sandbox; returns its ResultText."""
    r = altium("run_altium_script", {"script": script, "timeout_seconds": timeout}, timeout=timeout + 180)
    if not isinstance(r, dict) or not r.get("success"):
        raise RuntimeError("Altium script failed: " + json.dumps(r, indent=1)[:4000])
    return r.get("result") or ""


def pas(s) -> str:
    """Body of a Pascal string literal."""
    return str(s).replace("'", "''")


# One line per fact, tab-separated, coordinates in internal units from the
# board origin: OUTLINE, LAYER, COMP, HOLE, FREEPAD, FREE (off-copper free
# primitives) and CONN (one per unrouted connection). parse_dump() reads it.
DUMP_SCRIPT = r"""var
  Board : IPCB_Board;
  BO : IPCB_BoardOutline;
  Iter : IPCB_BoardIterator;
  GIter : IPCB_GroupIterator;
  Comp : IPCB_Component;
  Prim : IPCB_Primitive;
  Pad : IPCB_Pad;
  Conn : IPCB_Connection;
  R : TCoordRect;
  LS : IPCB_LayerStack_V7;
  LO : IPCB_LayerObject;
  X0, Y0, NP, NB, PL, PB, PR, PT, BL, BB, BR, BT : Integer;
  Plated, NetName : String;
begin
  SandboxLog('board');
  Board := PCBServer.GetPCBBoardByPath('%(pcb)s');
  if Board = nil then
  begin
    ResultText := 'ERROR' + #9 + 'the PCB is not open in Altium';
    exit;
  end;
  X0 := Board.XOrigin;
  Y0 := Board.YOrigin;
  List1.Clear;
  BO := Board.BoardOutline;
  R := BO.BoundingRectangle;
  List1.Add('OUTLINE' + #9 + IntToStr(R.Left - X0) + #9 + IntToStr(R.Bottom - Y0) + #9 + IntToStr(R.Right - X0) + #9 + IntToStr(R.Top - Y0) + #9 + IntToStr(BO.PointCount));

  SandboxLog('layer stack');
  LS := Board.LayerStack_V7;
  LO := LS.FirstLayer;
  while LO <> nil do
  begin
    List1.Add('LAYER' + #9 + LO.Name + #9 + IntToStr(LO.V6_LayerID));
    LO := LS.NextLayer(LO);
  end;

  SandboxLog('components');
  Iter := Board.BoardIterator_Create;
  Iter.AddFilter_ObjectSet(MkSet(eComponentObject));
  Iter.AddFilter_LayerSet(AllLayers);
  Iter.AddFilter_Method(eProcessAll);
  Comp := Iter.FirstPCBObject;
  while Comp <> nil do
  begin
    NP := 0;
    NB := 0;
    PL := 0; PB := 0; PR := 0; PT := 0;
    BL := 0; BB := 0; BR := 0; BT := 0;
    GIter := Comp.GroupIterator_Create;
    GIter.AddFilter_ObjectSet(MkSet(ePadObject, eComponentBodyObject));
    Prim := GIter.FirstPCBObject;
    while Prim <> nil do
    begin
      R := Prim.BoundingRectangle;
      if Prim.ObjectId = ePadObject then
      begin
        if (NP = 0) or (R.Left < PL) then PL := R.Left;
        if (NP = 0) or (R.Bottom < PB) then PB := R.Bottom;
        if (NP = 0) or (R.Right > PR) then PR := R.Right;
        if (NP = 0) or (R.Top > PT) then PT := R.Top;
        NP := NP + 1;
        Pad := Prim;
        if Pad.Layer = eMultiLayer then
        begin
          if Pad.Plated then Plated := '1' else Plated := '0';
          List1.Add('HOLE' + #9 + Comp.Name.Text + #9 + Pad.Name + #9 + IntToStr(Pad.x - X0) + #9 + IntToStr(Pad.y - Y0) + #9 + IntToStr(Pad.HoleSize) + #9 + IntToStr(Pad.HoleType) + #9 + IntToStr(Pad.HoleWidth) + #9 + Plated);
        end;
      end
      else
      begin
        if (NB = 0) or (R.Left < BL) then BL := R.Left;
        if (NB = 0) or (R.Bottom < BB) then BB := R.Bottom;
        if (NB = 0) or (R.Right > BR) then BR := R.Right;
        if (NB = 0) or (R.Top > BT) then BT := R.Top;
        NB := NB + 1;
      end;
      Prim := GIter.NextPCBObject;
    end;
    Comp.GroupIterator_Destroy(GIter);
    List1.Add('COMP' + #9 + Comp.Name.Text + #9 + Comp.Pattern + #9 + Comp.Comment.Text + #9 + IntToStr(Comp.x - X0) + #9 + IntToStr(Comp.y - Y0) + #9 + IntToStr(Round(Comp.Rotation * 1000)) + #9 + Layer2String(Comp.Layer) + #9 + IntToStr(Comp.ComponentKind) + #9 + IntToStr(NP) + #9 + IntToStr(PL - X0) + #9 + IntToStr(PB - Y0) + #9 + IntToStr(PR - X0) + #9 + IntToStr(PT - Y0) + #9 + IntToStr(NB) + #9 + IntToStr(BL - X0) + #9 + IntToStr(BB - Y0) + #9 + IntToStr(BR - X0) + #9 + IntToStr(BT - Y0));
    Comp := Iter.NextPCBObject;
  end;
  Board.BoardIterator_Destroy(Iter);

  SandboxLog('free pads');
  Iter := Board.BoardIterator_Create;
  Iter.AddFilter_ObjectSet(MkSet(ePadObject));
  Iter.AddFilter_LayerSet(AllLayers);
  Iter.AddFilter_Method(eProcessFree);
  Pad := Iter.FirstPCBObject;
  while Pad <> nil do
  begin
    if Pad.Plated then Plated := '1' else Plated := '0';
    List1.Add('FREEPAD' + #9 + Layer2String(Pad.Layer) + #9 + IntToStr(Pad.x - X0) + #9 + IntToStr(Pad.y - Y0) + #9 + IntToStr(Pad.HoleSize) + #9 + Plated);
    Pad := Iter.NextPCBObject;
  end;
  Board.BoardIterator_Destroy(Iter);

  SandboxLog('free primitives off copper');
  Iter := Board.BoardIterator_Create;
  Iter.AddFilter_ObjectSet(MkSet(eTrackObject, eArcObject, eTextObject, eFillObject, eRegionObject));
  Iter.AddFilter_LayerSet(AllLayers);
  Iter.AddFilter_Method(eProcessFree);
  Prim := Iter.FirstPCBObject;
  while Prim <> nil do
  begin
    if (Prim.Layer < eTopLayer) or (Prim.Layer > eBottomLayer) then
    begin
      R := Prim.BoundingRectangle;
      List1.Add('FREE' + #9 + IntToStr(Prim.ObjectId) + #9 + Layer2String(Prim.Layer) + #9 + IntToStr(R.Left - X0) + #9 + IntToStr(R.Bottom - Y0) + #9 + IntToStr(R.Right - X0) + #9 + IntToStr(R.Top - Y0));
    end;
    Prim := Iter.NextPCBObject;
  end;
  Board.BoardIterator_Destroy(Iter);

  SandboxLog('connections');
  Iter := Board.BoardIterator_Create;
  Iter.AddFilter_ObjectSet(MkSet(eConnectionObject));
  Iter.AddFilter_LayerSet(AllLayers);
  Iter.AddFilter_Method(eProcessAll);
  Conn := Iter.FirstPCBObject;
  while Conn <> nil do
  begin
    NetName := '';
    if Conn.Net <> nil then NetName := Conn.Net.Name;
    List1.Add('CONN' + #9 + NetName);
    Conn := Iter.NextPCBObject;
  end;
  Board.BoardIterator_Destroy(Iter);
  ResultText := List1.Text;
end;
"""

# Board shape -> tracks and arcs on one mechanical layer, then save. Refuses
# (EXISTS) when that layer already carries free tracks or arcs.
OUTLINE_SCRIPT = r"""var
  Board : IPCB_Board;
  BO : IPCB_BoardOutline;
  Iter : IPCB_BoardIterator;
  Prim : IPCB_Primitive;
  Track : IPCB_Track;
  Arc : IPCB_Arc;
  I, N, Added, Existing : Integer;
begin
  Board := PCBServer.GetPCBBoardByPath('%(pcb)s');
  if Board = nil then
  begin
    ResultText := 'ERROR' + #9 + 'the PCB is not open in Altium';
    exit;
  end;
  Existing := 0;
  Iter := Board.BoardIterator_Create;
  Iter.AddFilter_ObjectSet(MkSet(eTrackObject, eArcObject));
  Iter.AddFilter_LayerSet(MkSet(eMechanical%(mech)d));
  Iter.AddFilter_Method(eProcessFree);
  Prim := Iter.FirstPCBObject;
  while Prim <> nil do
  begin
    Existing := Existing + 1;
    Prim := Iter.NextPCBObject;
  end;
  Board.BoardIterator_Destroy(Iter);
  if Existing > 0 then
  begin
    ResultText := 'EXISTS' + #9 + IntToStr(Existing);
    exit;
  end;
  BO := Board.BoardOutline;
  N := BO.PointCount;
  Added := 0;
  PCBServer.PreProcess;
  for I := 0 to N - 1 do
  begin
    if BO.Segments[I].Kind = ePolySegmentLine then
    begin
      Track := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);
      Track.X1 := BO.Segments[I].vx;
      Track.Y1 := BO.Segments[I].vy;
      Track.X2 := BO.Segments[(I + 1) mod N].vx;
      Track.Y2 := BO.Segments[(I + 1) mod N].vy;
      Track.Width := MM(%(w)s);
      Track.Layer := eMechanical%(mech)d;
      Board.AddPCBObject(Track);
      PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast, PCBM_BoardRegisteration, Track.I_ObjectAddress);
    end
    else
    begin
      Arc := PCBServer.PCBObjectFactory(eArcObject, eNoDimension, eCreate_Default);
      Arc.XCenter := BO.Segments[I].cx;
      Arc.YCenter := BO.Segments[I].cy;
      Arc.Radius := BO.Segments[I].Radius;
      Arc.StartAngle := BO.Segments[I].Angle1;
      Arc.EndAngle := BO.Segments[I].Angle2;
      Arc.LineWidth := MM(%(w)s);
      Arc.Layer := eMechanical%(mech)d;
      Board.AddPCBObject(Arc);
      PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast, PCBM_BoardRegisteration, Arc.I_ObjectAddress);
    end;
    Added := Added + 1;
  end;
  PCBServer.PostProcess;
  Board.ViewManager_FullUpdate;
  SaveDoc('%(pcb)s');
  ResultText := 'ADDED' + #9 + IntToStr(Added);
end;
"""

# Adds a document to an open project and saves the project (PRESENT if it is already there).
ADD_DOCUMENT_SCRIPT = r"""var
  WS : IWorkspace;
  Prj, P : IProject;
  I : Integer;
  Doc : IDocument;
begin
  WS := GetWorkspace;
  Prj := nil;
  for I := 0 to WS.DM_ProjectCount - 1 do
  begin
    P := WS.DM_Projects(I);
    if SameText(P.DM_ProjectFullPath, '%(prj)s') then Prj := P;
  end;
  if Prj = nil then
  begin
    ResultText := 'ERROR' + #9 + 'the project is not open in Altium';
    exit;
  end;
  Doc := Prj.DM_GetDocumentFromPath('%(doc)s');
  if Doc <> nil then
    ResultText := 'PRESENT'
  else
  begin
    Prj.DM_AddSourceDocument('%(doc)s');
    SaveDoc('%(prj)s');
    ResultText := 'ADDED';
  end;
end;
"""


# --------------------------------------------------------------------------- board model

@dataclass
class Part:
    ref: str
    footprint: str
    comment: str
    x: float                     # footprint origin, mm from the board origin
    y: float
    rot: float                   # degrees, counter-clockwise
    side: str                    # "Top" or "Bottom"
    kind: str                    # Component Kind (schematic text when known)
    lcsc: str = ""
    pads: tuple | None = None    # pad bounding box (l, b, r, t), mm
    body: tuple | None = None    # 3D body bounding box, mm
    npads: int = 0
    excluded: str = ""           # why JLC does not place it; empty = placed

    @property
    def centre(self) -> tuple[float, float]:
        """Middle of the pads: the CPL position. Falls back to the origin for padless parts."""
        if self.pads:
            l, b, r, t = self.pads
            return (l + r) / 2, (b + t) / 2
        return self.x, self.y


@dataclass
class Board:
    outline: tuple               # board outline bounding box (l, b, r, t), mm
    outline_points: int
    layers: list                 # copper stack top to bottom: (name, Altium V6 layer id)
    parts: dict                  # ref -> Part
    holes: list = field(default_factory=list)       # multi-layer pads of components
    free_pads: list = field(default_factory=list)   # free pads (mounting holes, ...)
    free: list = field(default_factory=list)        # free primitives off copper: (object id, layer, bbox)
    unrouted: dict = field(default_factory=dict)    # net -> unrouted connection count

    def copper(self) -> list[tuple[str, int, int, str]]:
        """Copper layers top to bottom: (name, V6 id, V7 id for the OutJob, Gerber extension)."""
        out = []
        for name, v6 in self.layers:
            if v6 == 1:
                out.append((name, v6, V7_TOP, "GTL"))
            elif v6 == 32:
                out.append((name, v6, V7_BOTTOM, "GBL"))
            elif 2 <= v6 <= 31:                     # MidLayer n = V6 n + 1
                out.append((name, v6, V7_MID_BASE + v6, f"G{v6 - 1}"))
            elif 39 <= v6 <= 54:                    # InternalPlane n = V6 n + 38
                out.append((name, v6, V7_PLANE_BASE + v6 - 38, f"GP{v6 - 38}"))
        return out

    def outline_primitives(self, mech: int) -> list:
        names = {f"mechanicallayer{mech}", f"mechanical{mech}"}
        return [f for f in self.free if f[0] in (OBJ_ARC, OBJ_TRACK) and f[1].lower().replace(" ", "") in names]


def natural(ref: str) -> list:
    """Sort key: C2 before C10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


def _mm(v: str) -> float:
    return int(v) * MM_PER_COORD


def parse_dump(text: str) -> Board:
    """DUMP_SCRIPT's output -> Board."""
    if text.startswith("ERROR"):
        raise SystemExit(text.split("\t", 1)[-1])
    outline, npts, layers, parts = None, 0, [], {}
    holes, free_pads, free, unrouted = [], [], [], {}
    for line in text.splitlines():
        f = line.split("\t")
        tag = f[0]
        if tag == "OUTLINE":
            outline, npts = tuple(_mm(v) for v in f[1:5]), int(f[5])
        elif tag == "LAYER":
            layers.append((f[1], int(f[2])))
        elif tag == "COMP":
            n_pads, n_body = int(f[9]), int(f[14])
            parts[f[1]] = Part(
                ref=f[1], footprint=f[2], comment=f[3], x=_mm(f[4]), y=_mm(f[5]),
                rot=(int(f[6]) / 1000.0) % 360, side="Bottom" if "bottom" in f[7].lower() else "Top",
                kind=COMPONENT_KINDS.get(int(f[8]), f[8]), npads=n_pads,
                pads=tuple(_mm(v) for v in f[10:14]) if n_pads else None,
                body=tuple(_mm(v) for v in f[15:19]) if n_body else None)
        elif tag == "HOLE":
            holes.append({"ref": f[1], "pad": f[2], "x": _mm(f[3]), "y": _mm(f[4]), "size": _mm(f[5]),
                          "type": int(f[6]), "length": _mm(f[7]), "plated": f[8] == "1"})
        elif tag == "FREEPAD":
            free_pads.append({"layer": f[1], "x": _mm(f[2]), "y": _mm(f[3]), "size": _mm(f[4]), "plated": f[5] == "1"})
        elif tag == "FREE":
            free.append((int(f[1]), f[2], tuple(_mm(v) for v in f[3:7])))
        elif tag == "CONN":
            unrouted[f[1]] = unrouted.get(f[1], 0) + 1
    if outline is None:
        raise SystemExit("could not read the board outline: " + text[:500])
    return Board(outline, npts, layers, parts, holes, free_pads, free, unrouted)


def read_board(pcb: Path) -> Board:
    return parse_dump(run_script(DUMP_SCRIPT % {"pcb": pas(pcb)}, timeout=180))


def project_components(project: Path, lcsc_param: str) -> dict:
    """Schematic side, paged: ref -> {"comment", "footprint", "params": {name: value}}."""
    out, offset = {}, 0
    while True:
        r = altium("get_project_components", {"include_parameters": True, "offset": offset,
                                              "parameter_names": [lcsc_param, "Component Kind"],
                                              "project_path": str(project)})
        if not isinstance(r, dict) or "components" not in r:
            raise RuntimeError(f"get_project_components: {str(r)[:1000]}")
        for c in r["components"]:
            out[c["designator"]] = {"comment": c.get("comment", ""), "footprint": c.get("footprint", ""),
                                    "params": {p["name"]: p.get("value", "") for p in c.get("parameters", [])}}
        if not r.get("next_offset"):
            return out
        offset = int(r["next_offset"])


# --------------------------------------------------------------------------- BOM / CPL rules

def classify(board: Board, sch: dict, cfg: dict) -> list[str]:
    """Merge the schematic's data into the PCB parts and decide what JLC places.

    Sets part.kind, part.lcsc, part.comment and part.excluded; returns blockers.
    """
    blockers, fp_mismatch = [], []
    pcb_refs, sch_refs = set(board.parts), set(sch)
    if pcb_refs - sch_refs:
        blockers.append("on the PCB but not in the schematic (run the ECO): "
                        + ", ".join(sorted(pcb_refs - sch_refs, key=natural)))
    if sch_refs - pcb_refs:
        blockers.append("in the schematic but not on the PCB (run the ECO): "
                        + ", ".join(sorted(sch_refs - pcb_refs, key=natural)))
    words = cfg.get("dnp_words") or []
    dnp = re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.I) if words else None
    for ref, part in sorted(board.parts.items(), key=lambda kv: natural(kv[0])):
        s = sch.get(ref, {})
        params = s.get("params", {})
        part.kind = params.get("Component Kind") or part.kind
        part.lcsc = (params.get(cfg["lcsc_parameter"]) or "").strip().upper()
        part.comment = s.get("comment") or part.comment
        if s.get("footprint") and s["footprint"] != part.footprint:
            fp_mismatch.append(f"{ref} ({s['footprint']} -> {part.footprint})")
        kind = part.kind.lower()
        if "no bom" in kind:
            part.excluded = f"Component Kind '{part.kind}'"
        elif "net tie" in kind or "graphical" in kind or "jumper" in kind:
            part.excluded = f"Component Kind '{part.kind}' (copper only, nothing to place)"
        elif ref in cfg.get("exclude_designators", []):
            part.excluded = "listed in exclude_designators"
        elif dnp and dnp.search(part.comment or ""):
            part.excluded = f"DNP (Comment '{part.comment}')"
        elif not part.lcsc:
            part.excluded = "no LCSC part number"
            blockers.append(f"{ref} ({part.comment}) has no {cfg['lcsc_parameter']}: add one, or mark it DNP / No BOM")
        elif not re.fullmatch(r"C\d+", part.lcsc):
            blockers.append(f"{ref}: '{part.lcsc}' is not an LCSC code (C12345)")
    if fp_mismatch:
        blockers.append("footprint differs between schematic and PCB (run the ECO): " + ", ".join(fp_mismatch))
    return blockers


def bom_rows(placed: list[Part]) -> list[list[str]]:
    """[Comment, Designators, Footprint, LCSC], one row per (part number, comment, footprint)."""
    groups: dict = {}
    for p in placed:
        groups.setdefault((p.lcsc, p.comment, p.footprint), []).append(p.ref)
    rows = [[c, ",".join(sorted(refs, key=natural)), fp, lcsc] for (lcsc, c, fp), refs in groups.items()]
    return sorted(rows, key=lambda r: natural(r[1].split(",")[0]))


def bom_conflicts(rows: list[list[str]]) -> list[str]:
    """Part numbers that appear with more than one Comment or Footprint."""
    seen: dict = {}
    for c, _, fp, lcsc in rows:
        seen.setdefault(lcsc, set()).add((c, fp))
    return [f"{lcsc} appears as {sorted(v)}: same part, check the schematic"
            for lcsc, v in sorted(seen.items()) if len(v) > 1]


def read_rotation_log(text: str) -> list[dict]:
    """Table rows of a cpl-rotations.md: key (footprint or LCSC code), rotation, dx, dy.

    Header, separator and rows without a number in the rotation column are skipped.
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        key = cells[0].strip("`* ")
        if not key or set(key) <= set("-: "):
            continue
        try:
            rot = float(cells[1])
            dx = float(cells[2] or 0)
            dy = float(cells[3] or 0)
        except ValueError:
            continue
        rows.append({"key": key, "rot": rot, "dx": dx, "dy": dy})
    return rows


def correction_for(part: Part, log: list[dict]) -> dict | None:
    """The logged correction for a part: by LCSC code first, then by footprint; the last row wins."""
    for match in (lambda r: part.lcsc and r["key"].upper() == part.lcsc, lambda r: r["key"] == part.footprint):
        rows = [r for r in log if match(r)]
        if rows:
            return rows[-1]
    return None


def cpl_rows(placed: list[Part], log: list[dict]) -> tuple[list, list, dict, list]:
    """CPL rows plus what the README reports: applied corrections, unconfirmed footprints, off-centre origins."""
    rows, applied, unconfirmed, off_centre = [], [], {}, []
    for p in placed:
        cx, cy = p.centre
        rot = p.rot
        corr = correction_for(p, log)
        if corr is None:
            unconfirmed.setdefault(p.footprint, []).append(p.ref)
        elif corr["rot"] or corr["dx"] or corr["dy"]:
            dx = -corr["dx"] if p.side == "Bottom" else corr["dx"]
            a = math.radians(p.rot)
            cx += dx * math.cos(a) - corr["dy"] * math.sin(a)
            cy += dx * math.sin(a) + corr["dy"] * math.cos(a)
            rot += corr["rot"]
            applied.append(f"{p.ref} ({corr['key']}: {corr['rot']:+g} deg, dX {corr['dx']:g}, dY {corr['dy']:g} mm)")
        offset = math.hypot(p.centre[0] - p.x, p.centre[1] - p.y)
        if p.pads and offset > 0.05:
            off_centre.append((p.ref, p.footprint, offset))
        r = round(rot % 360, 2)
        rows.append([p.ref, f"{cx:.3f}mm", f"{cy:.3f}mm", p.side, int(r) if float(r).is_integer() else r])
    return rows, applied, unconfirmed, off_centre


def edge_clearance(placed: list[Part], outline: tuple, limit: float) -> list[tuple]:
    """(distance, edge, ref, comment) for parts whose pads or 3D body come closer than limit to the outline's box."""
    l, b, r, t = outline
    out = []
    for p in placed:
        boxes = [x for x in (p.pads, p.body) if x]
        if not boxes:
            continue
        d = {"left": min(x[0] for x in boxes) - l, "right": r - max(x[2] for x in boxes),
             "bottom": min(x[1] for x in boxes) - b, "top": t - max(x[3] for x in boxes)}
        edge = min(d, key=d.get)
        if d[edge] < limit:
            out.append((d[edge], edge, p.ref, p.comment))
    return sorted(out)


# --------------------------------------------------------------------------- catalogue (optional)

def catalogue(lcscs: list[str]) -> tuple[dict, str]:
    """Basic/Extended, process, snapshot stock and price tiers from the local jlcparts catalogue, if present."""
    db = os.environ.get("JLCPICK_DB") or str(Path.home() / ".cache" / "jlcpick" / "cache.sqlite3")
    nums = [int(c[1:]) for c in lcscs if re.fullmatch(r"C\d+", c)]
    if not Path(db).is_file() or not nums:
        return {}, ""
    out, newest = {}, 0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.text_factory = lambda b: b.decode("utf-8", errors="replace")
        q = ("select lcsc, library_type, preferred, stock, price, assembly_process, fetched_at "
             f"from jlc_components where lcsc in ({','.join('?' * len(nums))})")
        for lcsc, lib, pref, stock, price, process, fetched in con.execute(q, nums):
            tiers = [(int(m.group(1)), int(m.group(2)) if m.group(2) else None, float(m.group(3)))
                     for m in re.finditer(r"(\d+)-(\d*):([\d.]+)", price or "")]
            label = "Basic" if lib == "base" else ("Extended (Preferred)" if pref else "Extended")
            out[f"C{lcsc}"] = {"lib": label, "stock": stock, "tiers": tiers, "process": process or ""}
            newest = max(newest, fetched or 0)
        con.close()
    except sqlite3.Error:
        return {}, ""
    return out, dt.datetime.fromtimestamp(newest).strftime("%Y-%m-%d") if newest else "?"


def unit_price(tiers: list, n: int) -> float | None:
    for lo, hi, price in tiers:
        if lo <= n and (hi is None or n <= hi):
            return price
    return tiers[0][2] if tiers and n < tiers[0][0] else None


# --------------------------------------------------------------------------- Gerber + NC drill

def outjob_text(pcb_name: str, rel_out: str, copper_v7: list[int], mech: int) -> str:
    """An OutJob with one GeneratedFiles container, "JLC Gerber": Gerber RS-274X + NC drill.

    Inch 2:5 for both, relative origin (the board origin, which the CPL uses too), embedded
    apertures, plated and non-plated holes in separate drill files, outline on Mechanical <mech>.
    """
    plot = [v7 for _, v7 in NON_COPPER.values()] + [V7_MECH_BASE + mech] + list(copper_v7)
    plot_set = "SerializeLayerHash.Version~2,ClassName~TLayerToBoolean," + ",".join(f"{i}~1" for i in plot)
    gerber = ("AddToAllLayerClasses.Set= |AddToAllPlots.Set=SerializeLayerHash.Version~2,ClassName~TLayerToBoolean|"
              "CentrePlots=False|DrillDrawingSymbol=GraphicsSymbol|DrillDrawingSymbolSize=500000|EmbeddedApertures=True|"
              "FilmBorderSize=10000000|FilmXSize=200000000|FilmYSize=160000000|FlashAllFills=False|FlashPadShapes=True|"
              "G54OnApertureChange=False|GenerateDRCRulesFile=False|GenerateReliefShapes=True|GerberUnit=Imperial|"
              "IncludeUnconnectedMidLayerPads=False|LayerClassesMirror.Set= |LayerClassesPlot.Set= |"
              "LeadingAndTrailingZeroesMode=SuppressLeadingZeroes|MaxApertureSize=2500000|MinusApertureTolerance=50|"
              "Mirror.Set=SerializeLayerHash.Version~2,ClassName~TLayerToBoolean|MirrorDrillDrawingPlots=False|"
              "MirrorDrillGuidePlots=False|NumberOfDecimals=5|OptimizeChangeLocationCommands=True|OriginPosition=Relative|"
              f"Panelize=False|Plot.Set={plot_set}|PlotPositivePlaneLayers=False|PlotUsedDrillDrawingLayerPairs=False|"
              "PlotUsedDrillGuideLayerPairs=False|PlusApertureTolerance=50|Record=GerberView|SoftwareArcs=False|Sorted=False")
    drill = ("BoardEdgeRoutToolDia=2000000|GenerateBoardEdgeRout=False|GenerateDrilledSlotsG85=False|"
             "GenerateEIADrillFile=False|GenerateSeparatePlatedNonPlatedFiles=True|NumberOfDecimals=5|NumberOfUnits=2|"
             "OptimizeChangeLocationCommands=True|OriginPosition=Relative|Record=DrillView|Units=Imperial|"
             "ZeroesMode=SuppressTrailingZeroes")
    rel = rel_out.rstrip("\\") + "\\"
    lines = [
        "[OutputJobFile]", "Version=1.0", "Caption=",
        "Description=JLCPCB fabrication: Gerber RS-274X (inch 2:5) + Excellon NC drill. Written by jlc_fab_export.py",
        "VaultGUID=", "ItemGUID=", "ItemHRID=", "RevisionGUID=", "RevisionId=", "VaultHRID=", "AutoItemHRID=",
        "NextRevId=", "FolderGUID=", "LifeCycleDefinitionGUID=", "RevisionNamingSchemeGUID=", "",
        "[OutputGroup1]", "Name=JLC", "Description=", "TargetOutputMedium=JLC Gerber",
        "VariantName=[No Variations]", "VariantScope=1", "CurrentConfigurationName=", "TargetPrinter=",
        "PrinterOptions=Record=PrinterOptions|Copies=1|Duplex=1|TrueTypeOptions=3|Collate=1|PrintJobKind=1|PrintWhat=1",
        "OutputMedium1=JLC Gerber", "OutputMedium1_Type=GeneratedFiles",
        "OutputType1=Gerber", "OutputName1=Gerber Files", "OutputCategory1=Fabrication",
        f"OutputDocumentPath1={pcb_name}", "OutputVariantName1=", "OutputEnabled1=1",
        "OutputEnabled1_OutputMedium1=1", "OutputDefault1=0",
        "Configuration1_Name1=OutputConfigurationParameter1", f"Configuration1_Item1={gerber}",
        "OutputType2=NC Drill", "OutputName2=NC Drill Files", "OutputCategory2=Fabrication",
        f"OutputDocumentPath2={pcb_name}", "OutputVariantName2=", "OutputEnabled2=1",
        "OutputEnabled2_OutputMedium1=2", "OutputDefault2=0",
        "Configuration2_Name1=OutputConfigurationParameter1", f"Configuration2_Item1={drill}",
        "", "[PublishSettings]",
        f"OutputFilePath1={rel}", "ReleaseManaged1=0", f"OutputBasePath1={rel_out}",
        "OutputPathMedia1=", "OutputPathMediaValue1=", "OutputPathOutputer1=", "OutputPathOutputerPrefix1=",
        "OutputPathOutputerValue1=", "OutputFileName1=", "OutputFileNameMulti1=", "UseOutputNameForMulti1=1",
        "OutputFileNameSpecial1=", "OpenOutput1=0",
        "", "[GeneratedFilesSettings]",
        f"RelativeOutputPath1={rel}", "OpenOutputs1=0", "AddToProject1=0", "TimestampFolder1=0",
        "UseOutputName1=0", "OpenODBOutput1=0", "OpenGerberOutput1=0", "OpenNCDrillOutput1=0",
        "OpenIPCOutput1=0", "EnableReload1=0", ""]
    return "\r\n".join(lines)


def generate_fab_files(cfg: dict, board: Board, log: list) -> list[Path]:
    """Write the OutJob, make sure it is in the project, run it into <out_dir>/gerber; returns the files."""
    project, outjob, gdir = cfg["project"], cfg["outjob"], cfg["out_dir"] / "gerber"
    try:
        rel = os.path.relpath(gdir, project.parent)
    except ValueError:                       # another drive: Altium takes the absolute path
        rel = str(gdir)
    outjob.write_text(outjob_text(cfg["pcb"].name, rel, [c[2] for c in board.copper()], int(cfg["outline_layer"])),
                      encoding="utf-8")
    r = run_script(ADD_DOCUMENT_SCRIPT % {"prj": pas(project), "doc": pas(outjob)}, timeout=60).strip()
    if r.startswith("ERROR"):
        raise SystemExit(r.split("\t", 1)[-1])
    log.append(f"OutJob `{outjob.name}` written; " + ("added to the project (project saved)" if r == "ADDED"
                                                      else "already in the project"))
    if gdir.exists():                        # this folder is ours: no stale files in the zip
        for f in gdir.iterdir():
            if f.is_file():
                f.unlink()
    gdir.mkdir(parents=True, exist_ok=True)
    res = altium("run_output_jobs", {"container_names": ["JLC Gerber"], "outjob_path": str(outjob)}, timeout=900)
    files = sorted(f for f in gdir.iterdir() if f.is_file())
    if not files:
        raise SystemExit(f"Altium wrote nothing into {gdir}: {json.dumps(res)[:800]}")
    return files


def _gerber_number(s: str, ints: int, decs: int, leading_omitted: bool) -> float:
    neg = s.startswith("-")
    s = s.lstrip("+-")
    v = int(s) / 10 ** decs if leading_omitted else int(s.ljust(ints + decs, "0")) / 10 ** decs
    return -v if neg else v


def _arc_extremes(start: tuple, end: tuple, centre: tuple, ccw: bool) -> list[tuple[float, float]]:
    """Points where a multi-quadrant (G75) arc crosses 0/90/180/270 degrees; start == end is a full circle."""
    (sx, sy), (ex, ey), (cx, cy) = start, end, centre
    r = math.hypot(sx - cx, sy - cy)
    a0, a1 = math.atan2(sy - cy, sx - cx), math.atan2(ey - cy, ex - cx)
    if not ccw:                                          # clockwise s -> e covers counter-clockwise e -> s
        a0, a1 = a1, a0
    sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
    return [(cx + r * math.cos(q), cy + r * math.sin(q))
            for q in (k * math.pi / 2 for k in range(4)) if (q - a0) % (2 * math.pi) <= sweep + 1e-9]


def parse_gerber(text: str, name: str = "") -> dict:
    """Flashes, draw count and coordinate extent (mm) of an RS-274X file.

    The extent counts coordinates and the bulges of multi-quadrant (G75) arcs, not aperture sizes.
    """
    fs = re.search(r"%FS([LT])([AI])X(\d)(\d)Y(\d)(\d)\*%", text)
    leading_omitted = fs.group(1) == "L" if fs else True
    xi, xd, yi, yd = (int(fs.group(i)) for i in (3, 4, 5, 6)) if fs else (2, 5, 2, 5)
    unit = 25.4 if "%MOIN*%" in text else 1.0
    x = y = 0.0
    mode, multi_quadrant = 1, False                      # G01 linear / G02 clockwise / G03 counter-clockwise
    xs, ys, flashes, draws = [], [], [], 0
    for block in re.sub(r"%[^%]*%", "", text).split("*"):
        block = block.strip()
        if not block or block.startswith("G04"):        # comments carry GUIDs such as 5DD3: not D-codes
            continue
        if block.startswith("G75"):
            multi_quadrant = True
        elif block.startswith("G74"):
            multi_quadrant = False
        mg = re.match(r"G0*([123])(?!\d)", block)
        if mg:
            mode = int(mg.group(1))
        mx = re.search(r"X([+-]?\d+)", block)
        my = re.search(r"Y([+-]?\d+)", block)
        mi = re.search(r"I([+-]?\d+)", block)
        mj = re.search(r"J([+-]?\d+)", block)
        md = re.search(r"D0*([123])(?!\d)", block)
        px, py = x, y
        if mx:
            x = _gerber_number(mx.group(1), xi, xd, leading_omitted) * unit
        if my:
            y = _gerber_number(my.group(1), yi, yd, leading_omitted) * unit
        if md and (mx or my or mi or mj or md.group(1) == "3"):
            xs.append(x)
            ys.append(y)
            if md.group(1) == "3":
                flashes.append((x, y))
            elif md.group(1) == "1":
                draws += 1
                if mode in (2, 3) and multi_quadrant and (mi or mj):
                    ci = _gerber_number(mi.group(1), xi, xd, leading_omitted) * unit if mi else 0.0
                    cj = _gerber_number(mj.group(1), yi, yd, leading_omitted) * unit if mj else 0.0
                    for ax, ay in _arc_extremes((px, py), (x, y), (px + ci, py + cj), ccw=mode == 3):
                        xs.append(ax)
                        ys.append(ay)
    info = {"file": name, "format": f"{xi}:{xd}", "unit": "inch" if unit == 25.4 else "mm",
            "flashes": flashes, "draws": draws, "objects": len(flashes) + draws}
    if xs:
        info["extent"] = (min(xs), min(ys), max(xs), max(ys))
    return info


def parse_excellon(text: str, name: str = "") -> dict:
    """Tools (mm), hits and slots (routed G00/M15/G01/M16 or G85) of an Excellon file."""
    unit = 1.0 if re.search(r"^METRIC", text, re.M) else 25.4
    lz = re.search(r"^(?:INCH|METRIC)\s*,\s*LZ", text, re.M)        # leading zeros kept, trailing omitted
    fm = re.search(r"FILE_FORMAT\s*=\s*(\d):(\d)", text)
    ints, decs = (int(fm.group(1)), int(fm.group(2))) if fm else (2, 5)
    typ = re.search(r";\s*TYPE\s*=\s*(NON_PLATED|PLATED)", text, re.I)
    plated = typ.group(1).upper() == "PLATED" if typ else "nonplated" not in re.sub(r"[-_ ]", "", name.lower())
    tools = {int(m.group(1)): float(m.group(2)) * unit for m in re.finditer(r"^T(\d+)[^C\r\n]*C([\d.]+)", text, re.M)}

    def num(s: str) -> float:
        if "." in s:
            return float(s) * unit
        neg = s.startswith("-")
        s = s.lstrip("+-")
        v = int(s.ljust(ints + decs, "0")) / 10 ** decs if lz else int(s) / 10 ** decs
        return (-v if neg else v) * unit

    hits, slots = [], []
    tool, x, y, routing, start, in_header = None, 0.0, 0.0, False, None, True
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if in_header:
            in_header = line not in ("%", "M95")
            continue
        if re.fullmatch(r"T(\d+)", line):
            tool = int(line[1:])
            continue
        if line == "M15":
            routing, start = True, (x, y)
            continue
        if line in ("M16", "M17"):
            routing = False
            continue
        if "G85" in line:                                # drilled slot: X..Y..G85X..Y..
            a, b = line.split("G85", 1)
            p1 = [num(v) for v in re.findall(r"[XY]([+-]?[\d.]+)", a)]
            p2 = [num(v) for v in re.findall(r"[XY]([+-]?[\d.]+)", b)]
            if len(p1) == 2 and len(p2) == 2:
                slots.append((tool, tuple(p1), tuple(p2)))
            continue
        mx = re.search(r"X([+-]?[\d.]+)", line)
        my = re.search(r"Y([+-]?[\d.]+)", line)
        if mx:
            x = num(mx.group(1))
        if my:
            y = num(my.group(1))
        if not (mx or my):
            continue
        if line.startswith("G00"):
            start = (x, y)
        elif line.startswith("G01") or routing:
            if start is not None:
                slots.append((tool, start, (x, y)))
            start = (x, y)
        else:
            hits.append((tool, x, y))
    return {"file": name, "unit": "inch" if unit == 25.4 else "mm", "format": f"{ints}:{decs}",
            "plated": plated, "tools": tools, "hits": hits, "slots": slots}


def read_fab_files(files: list[Path]) -> tuple[dict, list]:
    """Gerbers by extension and the Excellon files among Altium's output."""
    gerbers, drills = {}, []
    for f in files:
        ext = f.suffix.upper().lstrip(".")
        if re.fullmatch(r"GTL|GBL|G\d+|GP\d+|GM\d+", ext) or ext in NON_COPPER:
            gerbers[ext] = parse_gerber(f.read_text(errors="replace"), f.name)
        elif ext in ("TXT", "DRL", "XLN"):
            text = f.read_text(errors="replace")
            if "M48" in text[:2000]:
                drills.append(parse_excellon(text, f.name))
    return gerbers, drills


def check_fab_files(gerbers: dict, drills: list, outline: tuple, copper_exts: list[str], outline_ext: str) -> dict:
    """Blockers and notes for a set of parsed Gerber / drill files against the board."""
    blockers, notes = [], []
    missing = [e for e in [*copper_exts, *REQUIRED_NON_COPPER, outline_ext] if e not in gerbers]
    if missing:
        blockers.append("Gerber layers missing: " + ", ".join(missing))
    if not drills:
        blockers.append("no NC drill file was written")
    l, b, r, t = outline
    ex = gerbers.get(outline_ext, {}).get("extent")
    if ex:
        err = max(abs(ex[0] - l), abs(ex[1] - b), abs(ex[2] - r), abs(ex[3] - t))
        if err > 0.01:
            blockers.append(f"outline {outline_ext} spans {ex[2] - ex[0]:.2f} x {ex[3] - ex[1]:.2f} mm, "
                            f"the board is {r - l:.2f} x {t - b:.2f} mm")
        else:
            notes.append(f"outline {outline_ext}: {ex[2] - ex[0]:.2f} x {ex[3] - ex[1]:.2f} mm, matches the board shape")
    elif outline_ext in gerbers:
        blockers.append(f"outline {outline_ext} is empty")
    for ext in copper_exts:
        ex = gerbers.get(ext, {}).get("extent")
        if ex and (ex[0] < l - 0.01 or ex[1] < b - 0.01 or ex[2] > r + 0.01 or ex[3] > t + 0.01):
            blockers.append(f"{ext} has copper outside the board outline ({ex[0]:.2f}, {ex[1]:.2f}) - ({ex[2]:.2f}, {ex[3]:.2f})")
    for ext in ("GTO", "GBO"):
        ex = gerbers.get(ext, {}).get("extent")
        if ex:
            over = max(l - ex[0], b - ex[1], ex[2] - r, ex[3] - t)
            if over > 0.05:
                notes.append(f"{NON_COPPER[ext][0]} runs up to {over:.2f} mm outside the board outline "
                             f"(({ex[0]:.2f}, {ex[1]:.2f}) - ({ex[2]:.2f}, {ex[3]:.2f})): that part is not printed")
    # every plated hole and slot must sit on a copper flash: catches unit, format and origin mismatches
    flashes = [pt for ext in copper_exts for pt in gerbers.get(ext, {}).get("flashes", [])]

    def nearest(px: float, py: float) -> float:
        return min((math.hypot(px - fx, py - fy) for fx, fy in flashes), default=float("inf"))

    worst, n_pth, n_slot, n_npth, sizes, outside = 0.0, 0, 0, 0, {}, []
    for d in drills:
        kind = "PTH" if d["plated"] else "NPTH"
        for tool, x, y in d["hits"]:
            key = f"{d['tools'].get(tool, 0.0):.2f} mm {kind}"
            sizes[key] = sizes.get(key, 0) + 1
            if not (l - 0.01 <= x <= r + 0.01 and b - 0.01 <= y <= t + 0.01):
                outside.append((round(x, 3), round(y, 3)))
            if d["plated"]:
                n_pth += 1
                worst = max(worst, nearest(x, y))
            else:
                n_npth += 1
        for tool, p1, p2 in d["slots"]:
            dia = d["tools"].get(tool, 0.0)
            key = f"{dia:.2f} x {math.hypot(p2[0] - p1[0], p2[1] - p1[1]) + dia:.2f} mm slot {kind}"
            sizes[key] = sizes.get(key, 0) + 1
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            if not (l - 0.01 <= mx <= r + 0.01 and b - 0.01 <= my <= t + 0.01):
                outside.append((round(mx, 3), round(my, 3)))
            if d["plated"]:
                n_slot += 1
                worst = max(worst, nearest(mx, my))
            else:
                n_npth += 1
    if outside:
        blockers.append(f"{len(outside)} drill hits outside the board outline, e.g. {outside[0]}")
    if n_pth + n_slot and worst > 0.01:
        blockers.append(f"drill/copper misaligned: a plated hole is {worst:.3f} mm from the nearest copper pad "
                        "(units, format or origin differ between Gerber and drill)")
    elif n_pth + n_slot:
        notes.append(f"drill/copper alignment: all {n_pth + n_slot} plated holes and slots sit on copper pads "
                     f"(max offset {worst:.3f} mm)")
    return {"blockers": blockers, "notes": notes, "missing": missing, "hole_sizes": sizes,
            "n_pth": n_pth, "n_slot": n_slot, "n_npth": n_npth}


def zip_files(files: list[Path], keep: set[str], zpath: Path) -> list[str]:
    names = []
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if f.name in keep:
                z.write(f, arcname=f.name)
                names.append(f.name)
    return names


# --------------------------------------------------------------------------- writers

def write_table(rows: list[list], header: list[str], xlsx: Path, csv_path: Path, sheet: str, widths: list[int]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        raise SystemExit("openpyxl is needed for the .xlsx files: pip install openpyxl")
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append(r)
    for i, width in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = width
    ws.freeze_panes = "A2"
    wb.save(xlsx)


@dataclass
class Package:
    """Everything the README reports."""
    cfg: dict
    board: Board
    placed: list
    excluded: list
    bom: list
    cpl: list
    applied: list
    unconfirmed: dict
    off_centre: list
    edge: list
    log_rows: int
    cat: dict
    cat_date: str
    fab: dict | None
    gerbers: dict
    drills: list
    zipped: list
    zip_path: Path
    blockers: list
    warnings: list
    notes: list
    log: list
    drc: object
    unsaved: int


def readme_text(pk: Package) -> str:
    cfg, board = pk.cfg, pk.board
    name, qty, mech = cfg["name"], int(cfg["qty"]), int(cfg["outline_layer"])
    l, b, r, t = board.outline
    copper = board.copper()
    L = [f"# JLC order package - {name}", "",
         f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} by `jlc_fab_export.py` from `{cfg['pcb'].name}` "
         f"({'all documents saved' if not pk.unsaved else f'{pk.unsaved} UNSAVED document(s) in Altium'}).", ""]
    if pk.blockers:
        L += ["## NOT READY TO ORDER", ""] + [f"- {x}" for x in pk.blockers] + [""]
    else:
        L += ["## Ready to upload - no blockers found", ""]
    L += ["## Upload", "", "| JLC order step | File |", "|---|---|",
          f"| PCB: Add Gerber file | `{pk.zip_path.name}`" + ("" if pk.zipped else " (not generated: --skip-gerbers)") + " |",
          f"| PCB Assembly: BOM | `{name}_BOM_JLC.xlsx` |",
          f"| PCB Assembly: CPL (pick and place) | `{name}_CPL_JLC.xlsx` |", ""]
    sides = {s: sum(1 for p in pk.placed if p.side == s) for s in ("Top", "Bottom")}
    L += ["## Board facts for the order form", "",
          f"- Size: {r - l:.2f} x {t - b:.2f} mm (board outline, {board.outline_points} segments)",
          f"- Copper layers: {len(copper)} - " + ", ".join(f"L{i + 1} {n} (.{e})" for i, (n, _, _, e) in enumerate(copper)),
          f"- Placed parts: {len(pk.placed)} ({sides['Top']} top, {sides['Bottom']} bottom) in {len(pk.bom)} BOM lines"]
    if pk.fab:
        L.append(f"- Holes: {pk.fab['n_pth']} plated round, {pk.fab['n_slot']} plated slots, {pk.fab['n_npth']} non-plated - "
                 + ", ".join(f"{k} x{v}" for k, v in sorted(pk.fab["hole_sizes"].items())))
    L += [f"- Unrouted connections: {sum(board.unrouted.values())}", ""]
    L += ["## Decisions for the order form (not made by this tool)", "",
          f"- **Edge rails.** JLC: \"Edge rails and fiducials are required for standard PCB assembly\"; recommended "
          f"5 mm process edges, 2 mm tooling holes, 1 mm fiducials ([rails]({JLC['rails']}), [spec]({JLC['process_edges']})). "
          f"Economic PCBA lists edge rails as \"Not necessary\", Standard PCBA as \"Necessary\" "
          f"([capabilities]({JLC['capabilities']}))."]
    if (r - l) < 70 or (t - b) < 70:
        L.append(f"  The board is under 70 x 70 mm: JLC's T&C say that for such boards, or a missing edge rail on "
                 f"standard PCBA, JLC selects an edge-rail method itself, for instance mouse bites ([T&C]({JLC['terms']})).")
    L += ["- **PCBA type, assembly side, board thickness, surface finish, order-number placement.** Choose on the form.", ""]
    L += ["## Checks", ""]
    if not pk.fab:
        L.append("- Gerber and drill files were not generated (--skip-gerbers): no file checks.")
    L += [f"- {x}" for x in pk.notes]
    if pk.zipped:
        L.append(f"- In `{pk.zip_path.name}`: " + ", ".join(f"`{n}`" for n in pk.zipped))
    if pk.fab:
        L += ["", "| File | Layer | Format | Objects | Extent (mm) |", "|---|---|---|---|---|"]
        layer_names = {e: n for n, _, _, e in copper}
        layer_names.update({e: v[0] for e, v in NON_COPPER.items()})
        layer_names[f"GM{mech}"] = "Board outline"
        for ext, g in sorted(pk.gerbers.items()):
            ex = g.get("extent")
            span = f"({ex[0]:.2f}, {ex[1]:.2f}) - ({ex[2]:.2f}, {ex[3]:.2f})" if ex else "empty"
            L.append(f"| `{g['file']}` | {layer_names.get(ext, ext)} | {g['unit']} {g['format']} | {g['objects']} | {span} |")
        for d in pk.drills:
            L.append(f"| `{d['file']}` | NC drill, {'plated' if d['plated'] else 'non-plated'} | {d['unit']} {d['format']} | "
                     f"{len(d['hits'])} hits, {len(d['slots'])} slots | |")
    L.append("")
    if pk.warnings:
        L += ["## Warnings", ""] + [f"- {x}" for x in pk.warnings] + [""]
    if pk.edge:
        L += [f"## Edge clearance (closer than {cfg['edge_clearance_mm']} mm)", "",
              "JLC assembly T&C, DFM: \"The distance between the body of the components and the edge of the board must be "
              f"equal or greater than 2.5mm.\" ([T&C]({JLC['terms']})). Measured from the pads and the 3D body to the "
              "board outline's bounding box; negative = overhangs the edge.", "",
              "| Part | Comment | Edge | Distance (mm) |", "|---|---|---|---|"]
        for d, edge, ref, comment in pk.edge:
            L.append(f"| {ref} | {comment} | {edge} | {'0.00 (at the edge)' if abs(d) < 0.005 else f'{d:.2f}'} |")
        L.append("")
    L += [f"## BOM ({len(pk.bom)} lines, {len(pk.placed)} placements)", "",
          f"Columns as uploaded: Comment, Designator, Footprint, {cfg['bom_part_header']} ([JLC BOM help]({JLC['bom']})). "
          + (f"Library, process, stock and price come from the local jlcparts snapshot ({pk.cat_date}): stock moves, "
             "so confirm it on JLC's BOM-matching page before paying." if pk.cat else
             "No local jlcparts catalogue was found, so there are no catalogue columns."), "",
          f"| LCSC | Comment | Qty/board | Designators | Library | Process | Stock (snapshot) | Unit price @ {qty} boards |",
          "|---|---|---|---|---|---|---|---|"]
    for c, refs, _, lcsc in pk.bom:
        k = pk.cat.get(lcsc, {})
        n = len(refs.split(","))
        price = unit_price(k.get("tiers", []), n * qty) if k else None
        L.append(f"| {lcsc} | {c} | {n} | {refs} | {k.get('lib', '')} | {k.get('process', '')} | {k.get('stock', '')} | "
                 + (f"${price:.4f}" if price is not None else "") + " |")
    if pk.cat:
        missing = [lc for *_, lc in pk.bom if lc not in pk.cat]
        if missing:
            L += ["", "Not in the catalogue snapshot (check the number on jlcpcb.com): " + ", ".join(missing)]
        n_ext = sum(1 for *_, lc in pk.bom if pk.cat.get(lc, {}).get("lib", "").startswith("Extended"))
        L += ["", f"{n_ext} of {len(pk.bom)} lines are Extended parts, which carry JLC's extended-part setup fee "
                  "(Basic parts do not)."]
    L += ["", f"## Not placed by JLC ({len(pk.excluded)})", "", "| Part | Comment | Footprint | Reason |", "|---|---|---|---|"]
    L += [f"| {p.ref} | {p.comment} | {p.footprint} | {p.excluded} |" for p in pk.excluded]
    log_name = cfg["rotation_log"].name if cfg.get("rotation_log") else "cpl-rotations.md"
    L += ["", f"## CPL ({len(pk.cpl)} placements)", "",
          f"Columns: Designator, Mid X, Mid Y, Layer, Rotation ([JLC CPL help]({JLC['cpl']})). Mid X/Y = middle of the "
          "part's pads, mm from the board origin (the Gerber and drill origin too). Rotation = Altium rotation "
          f"(degrees, counter-clockwise) plus the corrections in `{log_name}`.", "",
          (f"Corrections applied ({len(pk.applied)}): " + "; ".join(pk.applied)) if pk.applied
          else f"No corrections applied ({pk.log_rows} rows in the rotation log)."]
    if pk.unconfirmed:
        L += ["", f"**{len(pk.unconfirmed)} footprints are not in the rotation log yet**: their rotation is Altium's and is "
                  "unconfirmed until JLC's placement preview shows it. Check each polarised part (diodes, LEDs, ICs, "
                  "connectors) there, then log every correction JLC makes, and every footprint that came out right with 0.", ""]
        L += [f"- `{fp}`: {', '.join(sorted(refs, key=natural))}" for fp, refs in sorted(pk.unconfirmed.items())]
    if pk.off_centre:
        L += ["", "Footprints whose origin is not at the middle of their pads (the CPL uses the pads; check these in the preview):", ""]
        L += [f"- {ref} `{fp}`: origin {d:.2f} mm from the pad centre" for ref, fp, d in sorted(pk.off_centre, key=lambda x: natural(x[0]))]
    L.append("")
    if pk.drc is not None:
        L += ["## DRC", ""]
        if isinstance(pk.drc, dict):
            L.append(f"- ran: {pk.drc.get('ran')}, violations: {pk.drc.get('violation_count')}, complete: {pk.drc.get('complete')}")
            by_rule = pk.drc.get("listed_by_rule")
            if isinstance(by_rule, dict):
                L += [f"  - {k}: {v}" for k, v in by_rule.items()]
        else:
            L.append(f"- {str(pk.drc)[:500]}")
        L.append("")
    L += ["## Export settings", "",
          f"- OutJob `{cfg['outjob'].name}` (in the project, rewritten on every run): Gerber RS-274X, inch 2:5, leading "
          "zeros suppressed, relative origin, embedded apertures; Excellon NC drill inch 2:5, trailing zeros suppressed, "
          "relative origin, plated and non-plated holes in separate files. Board outline on Mechanical "
          f"{mech} (.GM{mech}); the Keep-Out layer is not plotted."]
    L += [f"- {x}" for x in pk.log] + [""]
    return "\n".join(L)


# --------------------------------------------------------------------------- config

def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    base = path.resolve().parent

    def resolve(v):
        return None if v in (None, "") else (Path(v) if Path(v).is_absolute() else (base / v).resolve())

    for key in ("project", "pcb"):
        cfg[key] = resolve(cfg.get(key))
        if not cfg[key] or not cfg[key].is_file():
            raise SystemExit(f"{key} not found: {cfg[key]}")
    prj = cfg["project"]
    cfg["name"] = cfg["name"] or cfg["pcb"].stem
    cfg["out_dir"] = resolve(cfg["out_dir"]) or prj.parent / f"Project Outputs for {prj.stem}" / "JLC"
    cfg["outjob"] = resolve(cfg["outjob"]) or prj.parent / "JLC Fabrication.OutJob"
    cfg["rotation_log"] = resolve(cfg["rotation_log"])
    return cfg


def init_config(prjpcb: Path) -> int:
    """Write jlc-fab.json next to a project; point it at (and seed) docs/cpl-rotations.md of the hardware project."""
    prjpcb = prjpcb.resolve()
    pcbs = sorted(prjpcb.parent.glob("*.PcbDoc"))
    if not prjpcb.is_file() or not pcbs:
        raise SystemExit(f"need an existing .PrjPcb with a .PcbDoc next to it: {prjpcb}")
    cfg_path = prjpcb.parent / "jlc-fab.json"
    if cfg_path.exists():
        raise SystemExit(f"{cfg_path} already exists")
    # the hardware project's root is the nearest folder up with a CLAUDE.md, else the project folder
    root = next((d for d in [prjpcb.parent, *prjpcb.parents] if (d / "CLAUDE.md").is_file()), prjpcb.parent)
    log = root / "docs" / "cpl-rotations.md"
    template = Path(__file__).resolve().parents[1] / "templates" / "cpl-rotations.md"
    if not log.exists() and template.is_file():
        log.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(template, log)
        print(f"wrote {log}")
    cfg = {"project": prjpcb.name, "pcb": pcbs[0].name, "name": prjpcb.stem,
           "out_dir": f"Project Outputs for {prjpcb.stem}/JLC", "outjob": "JLC Fabrication.OutJob",
           "outline_layer": DEFAULTS["outline_layer"], "outline_width_mm": DEFAULTS["outline_width_mm"],
           "lcsc_parameter": DEFAULTS["lcsc_parameter"], "bom_part_header": DEFAULTS["bom_part_header"],
           "exclude_designators": [], "dnp_words": DEFAULTS["dnp_words"],
           "rotation_log": os.path.relpath(log, prjpcb.parent).replace("\\", "/"),
           "edge_clearance_mm": DEFAULTS["edge_clearance_mm"], "qty": DEFAULTS["qty"]}
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {cfg_path}" + (f" ({len(pcbs)} PcbDocs: using {pcbs[0].name})" if len(pcbs) > 1 else ""))
    return 0


# --------------------------------------------------------------------------- main

def build(cfg: dict, make_outline: bool = False, skip_gerbers: bool = False, drc: bool = False) -> int:
    out_dir: Path = cfg["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    name, mech = cfg["name"], int(cfg["outline_layer"])
    blockers, warnings, notes, log = [], [], [], []

    info = altium("get_project_info", {})
    unsaved = info.get("modified_document_count", 0) if isinstance(info, dict) else 0
    if unsaved:
        warnings.append(f"{unsaved} document(s) have unsaved changes in Altium: the outputs come from the open "
                        "documents, the files on disk differ. Save before ordering.")

    board = read_board(cfg["pcb"])
    if not skip_gerbers and not board.outline_primitives(mech):
        if not make_outline:
            raise SystemExit(f"Mechanical {mech} has no board outline yet. Run once with --make-outline "
                             "(copies the board shape onto that layer and saves the PCB).")
        if unsaved:
            raise SystemExit("save the open documents in Altium first: --make-outline saves the PCB")
        r = run_script(OUTLINE_SCRIPT % {"pcb": pas(cfg["pcb"]), "mech": mech, "w": repr(float(cfg["outline_width_mm"]))},
                       timeout=60).strip()
        log.append(f"--make-outline: {r.replace(chr(9), ' ')} primitives on Mechanical {mech}, PCB saved")
        board = read_board(cfg["pcb"])
        if not board.outline_primitives(mech):
            raise SystemExit(f"--make-outline left no tracks or arcs on Mechanical {mech}: {r}")

    blockers += classify(board, project_components(cfg["project"], cfg["lcsc_parameter"]), cfg)
    placed = sorted((p for p in board.parts.values() if not p.excluded), key=lambda p: natural(p.ref))
    excluded = sorted((p for p in board.parts.values() if p.excluded), key=lambda p: natural(p.ref))
    if board.unrouted:
        top = sorted(board.unrouted.items(), key=lambda kv: -kv[1])[:8]
        blockers.append(f"{sum(board.unrouted.values())} unrouted connections on {len(board.unrouted)} nets (e.g. "
                        + ", ".join(f"{k or '?'} x{v}" for k, v in top) + "): the board is not finished")

    bom = bom_rows(placed)
    warnings += bom_conflicts(bom)
    write_table(bom, ["Comment", "Designator", "Footprint", cfg["bom_part_header"]],
                out_dir / f"{name}_BOM_JLC.xlsx", out_dir / f"{name}_BOM_JLC.csv", "BOM", [26, 40, 34, 14])

    log_path = cfg.get("rotation_log")
    rot_log = read_rotation_log(log_path.read_text(encoding="utf-8")) if log_path and log_path.is_file() else []
    if log_path and not log_path.is_file():
        warnings.append(f"rotation log {log_path} not found: no corrections applied")
    cpl, applied, unconfirmed, off_centre = cpl_rows(placed, rot_log)
    write_table(cpl, ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"],
                out_dir / f"{name}_CPL_JLC.xlsx", out_dir / f"{name}_CPL_JLC.csv", "CPL", [12, 14, 14, 9, 10])
    if any(p.side == "Bottom" for p in placed):
        warnings.append("bottom-side parts: tools disagree on bottom rotations, so check every one in JLC's placement preview")

    edge = edge_clearance(placed, board.outline, float(cfg["edge_clearance_mm"]))
    if edge:
        warnings.append(f"{len(edge)} placed parts are closer than {cfg['edge_clearance_mm']} mm to the board edge "
                        "(JLC assembly DFM note): see 'Edge clearance'")

    cat, cat_date = catalogue(sorted({row[3] for row in bom}))

    fab, gerbers, drills, zipped, zpath = None, {}, [], [], out_dir / f"{name}_Gerber.zip"
    if not skip_gerbers:
        files = generate_fab_files(cfg, board, log)
        gerbers, drills = read_fab_files(files)
        fab = check_fab_files(gerbers, drills, board.outline, [c[3] for c in board.copper()], f"GM{mech}")
        blockers += fab["blockers"]
        notes += fab["notes"]
        zipped = zip_files(files, {g["file"] for g in gerbers.values()} | {d["file"] for d in drills}, zpath)

    drc_result = altium("run_drc", {"board_path": str(cfg["pcb"]), "max_violations": 20}, timeout=900) if drc else None

    readme = out_dir / "JLC_ORDER_README.md"
    readme.write_text(readme_text(Package(
        cfg=cfg, board=board, placed=placed, excluded=excluded, bom=bom, cpl=cpl, applied=applied,
        unconfirmed=unconfirmed, off_centre=off_centre, edge=edge, log_rows=len(rot_log), cat=cat, cat_date=cat_date,
        fab=fab, gerbers=gerbers, drills=drills, zipped=zipped, zip_path=zpath, blockers=blockers,
        warnings=warnings, notes=notes, log=log, drc=drc_result, unsaved=unsaved)), encoding="utf-8")

    print(json.dumps({"ready": not blockers, "out_dir": str(out_dir), "readme": str(readme),
                      "gerber_zip": str(zpath) if zipped else None,
                      "bom": str(out_dir / f"{name}_BOM_JLC.xlsx"), "cpl": str(out_dir / f"{name}_CPL_JLC.xlsx"),
                      "bom_lines": len(bom), "placements": len(cpl), "excluded": len(excluded),
                      "blockers": blockers, "warnings": warnings}, indent=1))
    return 0 if not blockers else 2


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="jlc-fab-export", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="the project's jlc-fab.json")
    ap.add_argument("--init", metavar="PRJPCB", help="write a starter jlc-fab.json next to this project")
    ap.add_argument("--make-outline", action="store_true", help="first run: copy the board shape onto the outline layer")
    ap.add_argument("--skip-gerbers", action="store_true", help="BOM, CPL and checks only")
    ap.add_argument("--drc", action="store_true", help="also run Altium's DRC")
    ap.add_argument("--qty", type=int, help="boards to price the BOM at")
    args = ap.parse_args(argv)
    if args.init:
        return init_config(Path(args.init))
    if not args.config:
        ap.error("--config is required (or --init)")
    cfg = load_config(Path(args.config))
    if args.qty:
        cfg["qty"] = args.qty
    return build(cfg, make_outline=args.make_outline, skip_gerbers=args.skip_gerbers, drc=args.drc)


if __name__ == "__main__":
    sys.exit(main())
