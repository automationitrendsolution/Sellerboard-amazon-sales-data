from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app import pnl
from app.exceptions import DatabaseError, MalformedReport
from app.upload import build_rows, replace_days, to_cell

# Column types as in powerbidb (a few of them; the US and CA tables differ).
TYPES = {"index": "datetime", "id": "int", "Sales": "float", "Units sold": "int", "Organic_Sales": "float",
         "Refund сost": "float", "Expenses": "float", "FBA storage fee": "text", "Net profit": "float",
         "Real ACOS": "text", "Margin": "text", "Gift wrap": "int", "ROI": "text"}


@pytest.mark.parametrize("raw, data_type, expected", [
    ("1,015.68", "float", 1015.68),
    ("", "float", None),
    ("12.5", "int", 13),
    ("-12.5", "int", -13),
    ("", "int", 0),
    ("16.96%", "text", "16.96%"),
    ("", "text", None),
    ("3.14", "decimal", Decimal("3.14")),
])
def test_to_cell(raw, data_type, expected):
    assert to_cell(raw, data_type) == expected


def test_text_in_a_number_column_is_refused():
    with pytest.raises(ValueError):
        to_cell("n/a", "float")


def test_build_rows(sample_csv):
    columns, rows = build_rows(pnl.read(sample_csv), TYPES)
    assert columns[0] == "index" and "id" not in columns
    assert "Sponsored Television" not in columns  # no column in the table: not stored
    assert columns[-1] == "Gift wrap"             # an int column the export lacks: 0
    first = dict(zip(columns, rows[0]))
    assert first["index"] == dt.datetime(2026, 10, 27)
    assert first["Sales"] == 2211.52 and first["Units sold"] == 130
    assert first["Refund сost"] == -144.51 and first["Expenses"] == -10.0
    assert first["FBA storage fee"] == "-12.50" and first["Real ACOS"] == "16.96%"
    assert first["Gift wrap"] == 0
    assert dict(zip(columns, rows[2]))["Sales"] == 1015.68


def test_bad_number_names_the_cell(tmp_path, sample_csv):
    bad = tmp_path / sample_csv.name
    bad.write_text(sample_csv.read_text(encoding="utf-8").replace("107.50", "n/a"), encoding="utf-8")
    with pytest.raises(MalformedReport, match="Net profit on 2026-10-27"):
        build_rows(pnl.read(bad), TYPES)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append(sql.split()[0])
        if "information_schema" in sql:
            self._result = list(TYPES.items())
        elif sql.startswith("SELECT COUNT"):
            self._result = [(self.conn.count_after if "INSERT" in self.conn.log else self.conn.existing,)]
        elif sql.startswith("DELETE"):
            return self.conn.existing
        return None

    def executemany(self, sql, rows):
        self.conn.log.append("INSERT")
        self.conn.inserted.extend(rows)

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0]


class FakeConn:
    def __init__(self, existing=3, count_after=3):
        self.existing, self.count_after = existing, count_after
        self.log, self.inserted = [], []

    def cursor(self):
        return FakeCursor(self)

    def begin(self):
        self.log.append("BEGIN")

    def commit(self):
        self.log.append("COMMIT")

    def rollback(self):
        self.log.append("ROLLBACK")


def test_replace_days_in_one_transaction(sample_csv):
    conn = FakeConn()
    assert replace_days(conn, "t", pnl.read(sample_csv), dry_run=False) == (3, 3)
    assert conn.log[-5:] == ["BEGIN", "DELETE", "INSERT", "SELECT", "COMMIT"]
    assert len(conn.inserted) == 3


def test_dry_run_writes_nothing(sample_csv):
    conn = FakeConn(existing=2)
    assert replace_days(conn, "t", pnl.read(sample_csv), dry_run=True) == (2, 3)
    assert "DELETE" not in conn.log and "INSERT" not in conn.log and "COMMIT" not in conn.log


def test_wrong_count_rolls_back(sample_csv):
    conn = FakeConn(count_after=4)  # e.g. a day had a row the delete did not match
    with pytest.raises(DatabaseError, match="expected 3; rolled back"):
        replace_days(conn, "t", pnl.read(sample_csv), dry_run=False)
    assert conn.log[-1] == "ROLLBACK" and "COMMIT" not in conn.log
