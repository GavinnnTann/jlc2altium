"""Download and extract the jlcparts catalogue (yaqwsx/jlcparts) for jlcpick.

The dump is a *split* zip: cache.z01 .. cache.zNN plus cache.zip (the last
volume, holding the central directory). Info-ZIP unzip and Python's zipfile
cannot read split archives, so this maps each entry's (disk, offset) onto the
concatenated volumes and inflates it directly.

The dump ships without indexes, so every query scans ~3.8M rows. After
extracting, this adds indexes on stock, category and subcategory (a few
hundred MB, about a minute) so searches return in about a second.

Usage: python fetch_jlcparts.py [dest_dir]          (default ~/.cache/jlcpick)
       python fetch_jlcparts.py --index-only [db]   index an existing catalogue
Result: <dest_dir>/cache.sqlite3   -> set JLCPICK_DB to it
"""
from __future__ import annotations

import os
import sqlite3
import struct
import sys
import time
import zlib
from pathlib import Path

import requests

BASE = "https://yaqwsx.github.io/jlcparts/data/"


def download(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    vols: list[Path] = []
    n = 1
    while True:
        name = f"cache.z{n:02d}"
        r = s.head(BASE + name, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            break
        vols.append(dest / name)
        n += 1
    vols.append(dest / "cache.zip")
    for v in vols:
        url = BASE + v.name
        size = int(s.head(url, timeout=30, allow_redirects=True).headers.get("Content-Length", 0))
        if v.exists() and v.stat().st_size == size:
            print(f"have {v.name} ({size/1e6:.0f} MB)")
            continue
        t0 = time.time()
        with s.get(url, stream=True, timeout=120) as r, open(v, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        print(f"got  {v.name} ({v.stat().st_size/1e6:.0f} MB, {time.time()-t0:.0f}s)", flush=True)
    return vols


def extract(vols: list[Path], dest: Path) -> list[Path]:
    starts, total = [], 0
    for v in vols:
        starts.append(total)
        total += v.stat().st_size
    last = vols[-1].read_bytes()
    eocd = last.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise RuntimeError("no end-of-central-directory record")
    (_sig, _disk, cd_disk, _n_here, n_total, cd_size, cd_off, _cl) = struct.unpack_from("<IHHHHIIH", last, eocd)
    zip64 = cd_off == 0xFFFFFFFF or n_total == 0xFFFF
    if zip64:
        loc = last.rfind(b"PK\x06\x07", 0, eocd)
        (_s, z64_disk, z64_off, _nd) = struct.unpack_from("<IIQI", last, loc)
        buf = read_span(vols, starts, z64_disk, z64_off, 56)
        (_s, _sz, _v, _vn, _d, cd_disk, _nh, n_total, cd_size, cd_off) = struct.unpack_from("<IQHHIIQQQQ", buf)
    cd = read_span(vols, starts, cd_disk, cd_off, cd_size)

    out_paths = []
    p = 0
    for _ in range(n_total):
        (sig, _vm, _vn, _fl, method, _t, _d, _crc, csize, usize, nlen, xlen, clen,
         disk, _ia, _ea, lho) = struct.unpack_from("<IHHHHHHIIIHHHHHII", cd, p)
        assert sig == 0x02014B50, "bad central directory"
        name = cd[p + 46:p + 46 + nlen].decode()
        extra = cd[p + 46 + nlen:p + 46 + nlen + xlen]
        p += 46 + nlen + xlen + clen
        # zip64 extra field overrides the 0xFFFFFFFF placeholders, in order
        q = 0
        while q + 4 <= len(extra):
            hid, hlen = struct.unpack_from("<HH", extra, q)
            if hid == 1:
                vals = list(struct.unpack_from("<" + "Q" * (hlen // 8), extra, q + 4))
                if usize == 0xFFFFFFFF: usize = vals.pop(0)
                if csize == 0xFFFFFFFF: csize = vals.pop(0)
                if lho == 0xFFFFFFFF: lho = vals.pop(0)
                if disk == 0xFFFF and len(extra) - q - 4 >= 4: disk = struct.unpack_from("<I", extra, q + 4 + hlen - 4)[0]
            q += 4 + hlen
        hdr = read_span(vols, starts, disk, lho, 30)
        (_sig, _v, _f, _m, _t, _d, _c, _cs, _us, n2, x2) = struct.unpack_from("<IHHHHHIIIHH", hdr)
        data_off = lho + 30 + n2 + x2
        target = dest / name
        print(f"extracting {name}: {csize/1e6:.0f} MB -> {usize/1e9:.2f} GB", flush=True)
        dec = zlib.decompressobj(-15) if method == 8 else None
        with open(target, "wb") as f:
            for chunk in iter_span(vols, starts, disk, data_off, csize):
                f.write(dec.decompress(chunk) if dec else chunk)
            if dec:
                f.write(dec.flush())
        if target.stat().st_size != usize:
            raise RuntimeError(f"{name}: size {target.stat().st_size} != {usize}")
        out_paths.append(target)
    return out_paths


def iter_span(vols, starts, disk, offset, length, step=1 << 22):
    pos = starts[disk] + offset
    end = pos + length
    while pos < end:
        i = max(k for k, s in enumerate(starts) if s <= pos)
        with open(vols[i], "rb") as f:
            f.seek(pos - starts[i])
            n = min(step, end - pos, vols[i].stat().st_size - (pos - starts[i]))
            chunk = f.read(n)
        if not chunk:
            raise RuntimeError("unexpected end of archive")
        pos += len(chunk)
        yield chunk


def read_span(vols, starts, disk, offset, length) -> bytes:
    return b"".join(iter_span(vols, starts, disk, offset, length))


INDEXES = {
    "jlcpick_stock": "stock",
    "jlcpick_category": "category",
    "jlcpick_subcategory": "subcategory",
}


def ensure_indexes(db: Path) -> None:
    """Index the columns jlcpick and jlc_query filter and sort on."""
    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        table = next((t for t in ("jlc_components", "components") if t in tables), None)
        if table is None:
            print("no components table; skipping indexes")
            return
        cols = {r[1] for r in con.execute(f"pragma table_info({table})")}
        for name, col in INDEXES.items():
            if col not in cols:
                continue
            t0 = time.time()
            con.execute(f"create index if not exists {name} on {table}({col})")
            print(f"index {name} ({time.time() - t0:.0f}s)", flush=True)
        con.execute("analyze")
        con.commit()
    finally:
        con.close()


def default_dir() -> Path:
    return Path.home() / ".cache" / "jlcpick"


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--index-only":
        db = Path(args[1]) if len(args) > 1 else Path(os.environ.get("JLCPICK_DB") or default_dir() / "cache.sqlite3")
        if not db.is_file():
            raise SystemExit(f"no catalogue at {db}")
        ensure_indexes(db)
        sys.exit(0)
    dest = Path(args[0]) if args else default_dir()
    vols = download(dest)
    for p in extract(vols, dest):
        print("wrote", p)
    for v in vols:
        os.remove(v)  # volumes are only needed for extraction
    ensure_indexes(dest / "cache.sqlite3")
    print("done; set JLCPICK_DB to", dest / "cache.sqlite3")
