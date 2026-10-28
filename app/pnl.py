"""Read a Sellerboard P&L export (CSV, "Last 30 days, by day") into one row per day.

The export is the P&L table as shown on the page: rows are parameters, columns are
days (newest first) and then "Total". Sub-rows are indented, and some names repeat
under two parents ("Organic" under Sales and under Units), so those are qualified
with the parent the way the Power BI tables name them: Organic_Sales, Organic_Units.

Rows are matched by name, never by position, so a row Sellerboard adds or moves
cannot shift values into the wrong column. RENAMED maps rows Sellerboard renamed
after the tables were made back to their table column.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from .exceptions import MalformedReport

# Sub-rows that appear under both Sales and Units: stored as "<name>_<parent>".
QUALIFIED = {"Organic", "Sponsored Products (same day)", "Sponsored Display (same day)"}

# Sellerboard's row name -> the table column. The tables spell "Refund сost" with a Cyrillic "с".
RENAMED = {
    "Units": "Units sold",
    "Refund cost": "Refund сost",  # Sellerboard switched to a Latin "c" (column empty since Jan 2025)
    "Indirect expenses": "Expenses",    # renamed by Sellerboard (column empty since May 2025)
    "TACOS": "Real ACOS",               # renamed by Sellerboard (column empty since Jul 2026)
}

REQUIRED = ("Sales", "Units sold", "Net profit")

# The name the job saves an export under: <market code>_<Sellerboard's file name>.
FILE_NAME = re.compile(r"^(?P<code>[A-Z]{2})_ProfitAndLoss-(?P<start>\d\d_\d\d_\d{4})-(?P<end>\d\d_\d\d_\d{4})")


@dataclass
class Report:
    path: Path
    days: list[dt.date]           # in the file's order (newest first)
    values: dict[str, list[str]]  # table column -> the raw cell for each day
    repeated: list[str]           # rows dropped because an earlier row has the same column

    def column(self, name: str) -> list[str]:
        return self.values[name]


def market_code(path: Path) -> str | None:
    match = FILE_NAME.match(path.name)
    return match["code"] if match else None


def _period(path: Path) -> tuple[dt.date, dt.date] | None:
    match = FILE_NAME.match(path.name)
    if not match:
        return None
    return tuple(dt.datetime.strptime(match[key], "%d_%m_%Y").date() for key in ("start", "end"))


def read(path: Path) -> Report:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            table = [row for row in csv.reader(handle) if row and row[0].strip()]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise MalformedReport(f"{path.name}: cannot read it ({exc})") from None
    if not table or table[0][0].strip() != "Parameter/Date":
        raise MalformedReport(f"{path.name}: not a Sellerboard P&L export (first cell is not 'Parameter/Date')")

    header = [h.strip() for h in table[0][1:]]
    if header and header[-1] == "Total":
        header.pop()
    try:
        days = [dt.datetime.strptime(h, "%d %B %Y").date() for h in header]
    except ValueError:
        raise MalformedReport(f"{path.name}: the columns are not single days ({header[:2]}...); "
                              "export the P&L with 'Last 30 days, by day'") from None
    if not days or len(set(days)) != len(days):
        raise MalformedReport(f"{path.name}: no days, or a day appears twice")
    period = _period(path)
    if period and period != (min(days), max(days)):
        raise MalformedReport(f"{path.name}: holds {min(days)}..{max(days)}, not the period in its name")

    values: dict[str, list[str]] = {}
    repeated: list[str] = []
    parent = None
    for row in table[1:]:
        name = row[0].strip()
        if row[0] == name:
            parent = name  # not indented: a top-level row
        elif name in QUALIFIED and parent:
            name = f"{name}_{parent}"
        column = RENAMED.get(name, name)
        cells = [cell.strip() for cell in row[1:1 + len(days)]]
        if len(cells) != len(days):
            raise MalformedReport(f"{path.name}: row {name!r} has {len(cells)} values for {len(days)} days")
        if column in values:
            repeated.append(name)  # e.g. the "VAT" sub-row repeats the "VAT" total above it; the total is kept
            continue
        values[column] = cells

    missing = [c for c in REQUIRED if c not in values]
    if missing:
        raise MalformedReport(f"{path.name}: missing the {', '.join(missing)} row(s)")
    return Report(path, days, values, repeated)
