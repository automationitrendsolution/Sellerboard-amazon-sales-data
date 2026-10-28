#!/usr/bin/env python3
"""Sellerboard Amazon sales data -- command-line entry point.

    python run.py      download Sellerboard's P&L (last 30 days, by day) for Amazon.com
                       and Amazon.ca, and replace those days in Power BI:
                         Amazon.com -> powerbidb.combined_transposed_file_US
                         Amazon.ca  -> powerbidb.combined_transposed_file_CA

The days in the export replace the same days in the table (delete + insert in one
transaction, checked before the commit), so Sellerboard's later corrections to a
day reach Power BI on the next run. Nothing outside those days is touched.

Running under cron
------------------
Cron calls ``uv run --no-sync python run.py --quiet`` from the project directory
(see ``deploy/crontab.example``). Overlapping runs are refused by a lock (exit 11).

Exit codes
----------
    0   success, including "no files waiting"
    1   unexpected error (see logs/importer.log)
    2   configuration error
    3   Sellerboard sign-in needed -- run --login
    4   Sellerboard page not as expected (see data/screenshots/)
    6   report download failed or timed out
    7   an export was malformed (moved to data/failed/)
    9   database error (rolled back; the file is uploaded by the next run)
    11  another run holds the lock
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ folders in the project; set before app/ is imported

from app import __version__, job, sellerboard
from app.config import load_settings
from app.exceptions import ConfigurationError, PipelineError
from app.logging_config import append_run, configure_logging, get_logger

_HELP_EPILOG = """
examples:
  python run.py                     US and CA: download, replace the last 30 days in Power BI (cron)
  python run.py --market CA         Amazon.ca only
  python run.py --fetch-only        download into data/incoming/ only
  python run.py --no-fetch          upload what is already in data/incoming/
  python run.py --dry-run           read data/incoming/ and show what would be replaced; writes nothing
  python run.py --login             one-time Sellerboard sign-in (Chrome window, or the terminal on a server)
  python run.py --login --terminal  sign in in the terminal with SELLERBOARD_LOGIN_EMAIL/PASSWORD from .env
  python run.py --inspect-page      screenshot + control list of the P&L page (to fix config/selectors.json)

exit codes:
  0 success · 1 unexpected · 2 config · 3 sign-in needed (run --login) · 4 page changed
  6 download failed · 7 malformed export · 9 database · 11 locked
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sellerboard-amazon-sales-data",
        description="Download Sellerboard's P&L (last 30 days, by day) per marketplace and replace those days in "
                    "powerbidb.combined_transposed_file_US / _CA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"sellerboard-amazon-sales-data {__version__}")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--login", action="store_true", help="One-time Sellerboard sign-in by a person.")
    modes.add_argument("--inspect-page", action="store_true",
                       help="Save a screenshot and control list of the P&L page to data/screenshots/.")
    modes.add_argument("--fetch-only", action="store_true", help="Download into data/incoming/ and stop.")
    modes.add_argument("--no-fetch", action="store_true", help="Do not download; upload data/incoming/ only.")
    modes.add_argument("--dry-run", action="store_true",
                       help="Read data/incoming/ and show what would be replaced. No download, no writes, no moves.")
    parser.add_argument("--market", action="append", metavar="CODE", type=str.upper,
                        help="Only this market (US, CA; repeatable). Default: SALES_MARKETS from .env (US,CA).")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (debugging).")
    parser.add_argument("--terminal", action="store_true",
                        help="With --login: sign in in this terminal with the .env credentials (no window).")
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override LOG_LEVEL.")
    parser.add_argument("--quiet", action="store_true", help="Console shows warnings and errors only (cron).")
    parser.add_argument("--no-lock", action="store_true", help="Skip the single-run lock (supervised runs only).")
    return parser


def _has_display() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # the browser session and the exports stay private to this user
    args = build_parser().parse_args(argv)
    started = dt.datetime.now()
    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return exc.exit_code
    settings.ensure_directories()
    level = args.log_level or settings.log_level
    configure_logging(settings.log_dir, level=level, console_level="WARNING" if args.quiet else level)
    logger = get_logger("cli")

    code, summary = 1, ""
    try:
        code, summary = _run(args, settings, logger)
    except PipelineError as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        code, summary = exc.exit_code, f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        code = 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected failure")
        code, summary = 1, f"{type(exc).__name__}: {exc}"
    append_run(settings.log_dir, f"{started:%Y-%m-%d %H:%M:%S} exit={code} {' '.join(argv or sys.argv[1:])} | {summary}")
    return code


def _run(args: argparse.Namespace, settings, logger) -> tuple[int, str]:
    if args.login:
        sellerboard.login(settings, headed=(args.headed or _has_display()) and not args.terminal)
        return 0, "signed in"
    if args.inspect_page:
        shot, controls = sellerboard.inspect_page(settings, headless=not args.headed)
        print(f"Screenshot: {shot}\nControls:   {controls}")
        return 0, "inspected"

    markets = settings.markets
    if args.market:
        known = {m.code: m for m in markets}
        unknown = [c for c in args.market if c not in known]
        if unknown:
            raise ConfigurationError(f"--market {', '.join(unknown)}: not in SALES_MARKETS ({', '.join(known)})")
        markets = tuple(known[c] for c in dict.fromkeys(args.market))
    code, summary = job.run(args, settings, markets)
    logger.info("%s", summary)
    return code, summary


if __name__ == "__main__":
    sys.exit(main())
