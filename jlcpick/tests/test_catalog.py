"""Catalogue search on a tiny synthetic jlcparts "source-db-v2" database."""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jlcpick.catalog import LocalCatalog  # noqa: E402

ROWS = [
    # lcsc, category, subcategory, mfr, package, library_type, stock, description
    (6186, "Power Management", "Linear Voltage Regulators (LDO)", "AMS1117-3.3", "SOT-223", "base",
     182400, "1A 3.3V LDO Positive -40℃~+125℃"),
    (347376, "Power Management", "Linear Voltage Regulators (LDO)", "XC6206P332MR", "SOT-23", "expand",
     1774668, "250mA 3.3V LDO 240mV@(50mA)"),
    (347386, "Power Management", "Linear Voltage Regulators (LDO)", "XC6219B332MR", "SOT-23-5", "expand",
     721898, "300mA 3.3V LDO 280mV@(100mA)"),
    (167223, "Inductors/Coils/Transformers", "Power Inductors", "FXL0630-100-M", "SMD,7x6.6mm", "expand",
     176945, "10uH 4.5A 5.5A 68mΩ Molded inductor"),
] + [
    # many high-stock resistors, so a filter applied after LIMIT would miss the rest
    (100000 + i, "Resistors", "Chip Resistor - Surface Mount", f"R{i}", "0402", "base",
     10_000_000 - i, "62.5mW 1% resistor") for i in range(300)
] + [
    (999, "Power Management", "Linear Voltage Regulators (LDO)", "OLD-LDO", "SOT-23", "expand",
     0, "3.3V LDO out of stock"),
]


def make_db() -> Path:
    path = Path(tempfile.mkdtemp()) / "cache.sqlite3"
    con = sqlite3.connect(path)
    con.execute("create table jlc_components (lcsc integer, present integer, category text, "
                "subcategory text, mfr text, package text, library_type text, preferred integer, "
                "stock integer, price text, attributes text, description text)")
    con.executemany("insert into jlc_components values (?, 1, ?, ?, ?, ?, ?, 0, ?, '1-:0.01', '{}', ?)", ROWS)
    con.commit()
    con.close()
    return path


CAT = LocalCatalog(make_db())


def lcscs(parts):
    return [p.lcsc for p in parts]


def test_quoted_phrase_is_split_into_words():
    # One shell argument "3.3V LDO 300mA" used to be one exact substring.
    assert lcscs(CAT.search(["3.3V LDO 300mA"])) == ["C347386"]
    assert lcscs(CAT.search(["3.3V LDO 300mA"])) == lcscs(CAT.search(["3.3V", "LDO", "300mA"]))


def test_category_filter_is_applied_before_the_limit():
    # The top 200 parts by stock are all resistors; inductors must still show.
    assert lcscs(CAT.search(category="Power Inductors", limit=5)) == ["C167223"]


def test_category_matches_category_or_subcategory_case_insensitively():
    assert len(CAT.search(category="power management", limit=10)) == 3
    assert len(CAT.search(category="(LDO)", limit=10)) == 3


def test_unknown_category_returns_nothing():
    assert CAT.search(category="Flux capacitors") == []


def test_out_of_stock_parts_are_dropped_by_default():
    assert "C999" not in lcscs(CAT.search(["LDO"], limit=10))
    assert "C999" in lcscs(CAT.search(["LDO"], min_stock=0, limit=10))


def test_package_and_basic_filters():
    assert lcscs(CAT.search(["LDO"], package="SOT-23", limit=10)) == ["C347376", "C347386"]
    assert lcscs(CAT.search(["LDO"], basic_only=True, limit=10)) == ["C6186"]


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {name}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
