"""Dump the records of an Altium .SchDoc (OLE compound file) as text.

Usage: python dump_schdoc.py <file.SchDoc> [--all]
"""
import struct
import sys
from collections import Counter

import olefile

RECORD_NAMES = {
    "1": "Component", "2": "Pin", "3": "IEEESymbol", "4": "Label", "5": "Bezier",
    "6": "Polyline", "7": "Polygon", "8": "Ellipse", "9": "Piechart",
    "10": "RoundRect", "11": "EllipticalArc", "12": "Arc", "13": "Line",
    "14": "Rectangle", "15": "SheetSymbol", "16": "SheetEntry", "17": "PowerPort",
    "18": "Port", "22": "NoERC", "25": "NetLabel", "26": "Bus", "27": "Wire",
    "28": "TextFrame", "29": "Junction", "30": "Image", "31": "Sheet",
    "32": "SheetName", "33": "SheetFileName", "34": "Designator", "37": "BusEntry",
    "39": "Template", "41": "Parameter", "43": "WarningSign", "44": "ImplList",
    "45": "Implementation", "46": "MapDefinerList", "47": "MapDefiner",
    "48": "ImplParams", "215": "HarnessConnector", "216": "HarnessEntry",
    "217": "HarnessType", "218": "SignalHarness", "226": "Blanket", "228": "?",
}


def records(blob: bytes):
    i = 0
    while i + 4 <= len(blob):
        (n,) = struct.unpack_from("<I", blob, i)
        flag = n >> 24
        size = n & 0xFFFFFF
        i += 4
        payload = blob[i:i + size]
        i += size
        if flag == 0:  # text record
            txt = payload.rstrip(b"\x00").decode("latin-1", "replace")
            d = {}
            for part in txt.split("|"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    d[k.upper()] = v
            yield d
        else:
            yield {"RECORD": f"binary(flag={flag},len={size})"}


def main():
    path = sys.argv[1]
    show_all = "--all" in sys.argv
    ole = olefile.OleFileIO(path)
    print("streams:", ["/".join(s) for s in ole.listdir()])
    blob = ole.openstream("FileHeader").read()
    recs = list(records(blob))
    print("records:", len(recs))
    print("header:", {k: v for k, v in recs[0].items()})
    counts = Counter(RECORD_NAMES.get(r.get("RECORD", ""), r.get("RECORD", "?")) for r in recs[1:])
    print("counts:", dict(counts))

    # index = position in the file after the header record; OWNERINDEX refers to it
    body = recs[1:]
    comps = {idx: r for idx, r in enumerate(body) if r.get("RECORD") == "1"}
    children = {}
    for idx, r in enumerate(body):
        if "OWNERINDEX" in r:
            children.setdefault(int(r["OWNERINDEX"]), []).append((idx, r))

    for idx, c in comps.items():
        kids = children.get(idx, [])
        des = next((k.get("TEXT") for _, k in kids if k.get("RECORD") == "34"), "?")
        params = {k.get("NAME"): k.get("TEXT") for _, k in kids if k.get("RECORD") == "41"}
        pins = [(k.get("DESIGNATOR"), k.get("NAME")) for _, k in kids if k.get("RECORD") == "2"]
        impls = [k for _, k in kids if k.get("RECORD") == "44"]
        print(f"\n[{idx}] {des}  LIBREF={c.get('LIBREFERENCE')}  LIB={c.get('SOURCELIBRARYNAME')}  "
              f"LOC=({c.get('LOCATION.X')},{c.get('LOCATION.Y')})  PARTCOUNT={c.get('PARTCOUNT')}  "
              f"DESC={c.get('COMPONENTDESCRIPTION')}")
        print("   params:", params)
        print("   pins:", pins)
        # footprint implementations live under the ImplList child
        for il_idx, _ in [(i, k) for i, k in kids if k.get("RECORD") == "44"]:
            for _, impl in children.get(il_idx, []):
                if impl.get("RECORD") == "45":
                    print("   impl:", impl.get("MODELNAME"), impl.get("MODELTYPE"),
                          "current" if impl.get("ISCURRENT") == "T" else "")

    if show_all:
        for idx, r in enumerate(body):
            name = RECORD_NAMES.get(r.get("RECORD", ""), r.get("RECORD"))
            if name in ("Wire", "NetLabel", "PowerPort", "Junction", "Port", "Label", "NoERC"):
                print(idx, name, {k: v for k, v in r.items() if k not in ("RECORD",)})


if __name__ == "__main__":
    main()
