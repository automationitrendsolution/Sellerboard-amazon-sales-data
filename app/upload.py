"""Replace the export's days in a Power BI table: delete + insert in ONE transaction.

Each value is converted to the type of the column it goes into, read from the table
itself, because the US and CA tables were created with different types for the same
columns (e.g. "FBA storage fee" is int in one and text in the other).

* float / double / decimal: the number ("1,234.50" and "12%" are accepted)
* int: the rounded number; a blank cell or a row the export does not have is 0,
  as the earlier upload script stored it
* text: Sellerboard's text as shown ("32.01%")
* anything blank: NULL

The row count is checked before the commit; on any error the table stays exactly
as it was.
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .exceptions import ConfigurationError, DatabaseError, MalformedReport
from .logging_config import get_logger
from .pnl import Report

DATE_COLUMN = "index"
SKIP_COLUMNS = {"id", DATE_COLUMN}
INT_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}
FLOAT_TYPES = {"float", "double", "real"}
TEXT_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"}
INSERT_CHUNK = 500

log = get_logger("upload")


def column_types(cur, table: str) -> dict[str, str]:
    cur.execute("SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
    types = {name: data_type.lower() for name, data_type in cur.fetchall()}
    if not types:
        raise ConfigurationError(f"table {table} not found in the Power BI database")
    if DATE_COLUMN not in types:
        raise ConfigurationError(f"table {table} has no `{DATE_COLUMN}` date column")
    return types


def to_cell(raw: str, data_type: str):
    """One CSV cell as the column's type. Raises ValueError for text in a number column."""
    if data_type in TEXT_TYPES:
        return raw or None
    if raw == "":
        return 0 if data_type in INT_TYPES else None
    try:
        number = Decimal(raw.replace(",", "").replace("%", ""))
    except InvalidOperation:
        raise ValueError(f"{raw!r} is not a number") from None
    if data_type in INT_TYPES:
        return int(number.to_integral_value(ROUND_HALF_UP))
    if data_type in FLOAT_TYPES:
        return float(number)
    if data_type == "decimal":
        return number
    raise ConfigurationError(f"unexpected column type {data_type!r}")


def build_rows(report: Report, types: dict[str, str]) -> tuple[list[str], list[tuple]]:
    """The INSERT column list and one tuple per day."""
    filled = [c for c in report.values if c in types and c not in SKIP_COLUMNS]
    zeroed = [c for c in types if c not in report.values and c not in SKIP_COLUMNS and types[c] in INT_TYPES]
    columns = [DATE_COLUMN, *filled, *zeroed]
    rows = []
    for i, day in enumerate(report.days):
        row = [dt.datetime.combine(day, dt.time())]
        for column in filled:
            try:
                row.append(to_cell(report.values[column][i], types[column]))
            except ValueError as exc:
                raise MalformedReport(f"{report.path.name}: {column} on {day}: {exc}") from None
        row.extend(0 for _ in zeroed)
        rows.append(tuple(row))
    return columns, rows


def replace_days(conn, table: str, report: Report, *, dry_run: bool) -> tuple[int, int]:
    """Replace the report's days in ``table``. Returns (rows deleted, rows inserted);
    with ``dry_run`` the rows that would be deleted and inserted, and nothing is written."""
    with conn.cursor() as cur:
        types = column_types(cur, table)
    columns, rows = build_rows(report, types)
    unused = [c for c in report.values if c not in types]
    if unused:
        log.debug("%s: rows with no column in %s (not stored): %s", report.path.name, table, ", ".join(unused))
    empty = [c for c in types if c not in report.values and c not in SKIP_COLUMNS]
    if empty:
        log.debug("%s: columns of %s the export does not have: %s", report.path.name, table, ", ".join(empty))

    days = [dt.datetime.combine(d, dt.time()) for d in report.days]
    where = f"`{DATE_COLUMN}` IN ({', '.join(['%s'] * len(days))})"
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}` WHERE {where}", days)
        existing = cur.fetchone()[0]
    log.info("%s: %d rows now for %s..%s; the export has %d days",
             table, existing, min(report.days), max(report.days), len(rows))
    if dry_run:
        conn.rollback()  # end the read-only transaction
        return existing, len(rows)

    names = ", ".join(f"`{c}`".replace("%", "%%") for c in columns)
    insert = f"INSERT INTO `{table}` ({names}) VALUES ({', '.join(['%s'] * len(columns))})"
    conn.begin()
    try:
        with conn.cursor() as cur:
            deleted = cur.execute(f"DELETE FROM `{table}` WHERE {where}", days)
            for start in range(0, len(rows), INSERT_CHUNK):
                cur.executemany(insert, rows[start:start + INSERT_CHUNK])
            cur.execute(f"SELECT COUNT(*) FROM `{table}` WHERE {where}", days)
            now = cur.fetchone()[0]
        if now != len(rows):
            raise DatabaseError(f"{table}: after the insert {now} rows match, expected {len(rows)}; rolled back")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return deleted, len(rows)
