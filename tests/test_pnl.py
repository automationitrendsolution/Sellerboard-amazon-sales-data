from __future__ import annotations

import datetime as dt

import pytest

from app import pnl
from app.exceptions import MalformedReport


def test_days_and_values(sample_csv):
    report = pnl.read(sample_csv)
    assert report.days == [dt.date(2026, 10, 27), dt.date(2026, 10, 26), dt.date(2026, 10, 25)]
    assert report.column("Sales") == ["2211.52", "2167.07", "1,015.68"]  # "Total" is left out


def test_sub_rows_are_named_after_their_parent(sample_csv):
    values = pnl.read(sample_csv).values
    assert values["Organic_Sales"][0] == "1500.00"
    assert values["Organic_Units"][0] == "90"
    assert values["Sponsored Products (same day)_Units"][1] == "43"
    assert values["FBA storage fee"] == ["-12.50", "0", ""]


def test_renamed_rows_fill_the_old_columns(sample_csv):
    values = pnl.read(sample_csv).values
    assert values["Units sold"][0] == "130"
    assert values["Refund сost"][0] == "-144.51"  # Cyrillic "с", as in the tables
    assert values["Expenses"][0] == "-10"
    assert values["Real ACOS"][0] == "16.96%"
    assert "Refund cost" not in values and "TACOS" not in values


def test_repeated_row_keeps_the_first(sample_csv):
    report = pnl.read(sample_csv)
    assert report.values["VAT"] == ["0", "0", "0"]  # the total, not the "    VAT" sub-row
    assert report.repeated == ["VAT"]


def test_market_code():
    assert pnl.market_code(pnl.Path("CA_ProfitAndLoss-27_09_2026-27_10_2026_sellerboard.com.csv")) == "CA"
    assert pnl.market_code(pnl.Path("ProfitAndLoss-27_09_2026-27_10_2026_sellerboard.com (1).csv")) is None


def test_period_in_name_must_match(tmp_path, sample_csv):
    wrong = tmp_path / "US_ProfitAndLoss-01_10_2026-27_10_2026_sellerboard.com.csv"
    wrong.write_text(sample_csv.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(MalformedReport, match="not the period in its name"):
        pnl.read(wrong)


@pytest.mark.parametrize("text, message", [
    ("Date,1\nSales,1\n", "not a Sellerboard P&L export"),
    ("Parameter/Date,October 2026,September 2026,Total\nSales,1,2,3\n", "not single days"),
    ("Parameter/Date,27 October 2026,Total\nSales,1,1\nUnits,1,1\n", "Net profit"),
    ("Parameter/Date,27 October 2026,26 October 2026,Total\nSales,1\n", "has 1 values for 2 days"),
])
def test_unusable_exports_are_refused(tmp_path, text, message):
    path = tmp_path / "US_export.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(MalformedReport, match=message):
        pnl.read(path)
