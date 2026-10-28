"""One run: for each market (US, then CA) download the P&L export and replace its days in Power BI.

Markets are independent: a failure in one is logged and the other still runs; the
run's exit code is the worst of them. A file stays in data/incoming/ when the
database could not be written (the next run uploads it), goes to data/failed/ when
it is unusable, and to data/processed/ once its days are committed.
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pymysql

from . import config, db, pnl, sellerboard
from .config import Market, Settings
from .exceptions import DatabaseError, MalformedReport, PipelineError, SignInNeeded
from .lock import RunLock
from .logging_config import get_logger
from .upload import replace_days

log = get_logger("job")


def download(settings: Settings, market: Market, *, headless: bool) -> Path:
    """The Sellerboard export; if the saved session has expired, sign in once with .env and retry."""
    try:
        return sellerboard.fetch_pl(settings, market, headless=headless)
    except SignInNeeded:
        if not (settings.login_email and settings.login_password):
            raise
        log.warning("Sellerboard session missing or expired: signing in with the .env credentials")
        sellerboard.credential_login(settings, automatic=True)
        return sellerboard.fetch_pl(settings, market, headless=headless)


def waiting_files(settings: Settings, market: Market) -> list[Path]:
    """This market's exports in incoming/, oldest first, so the newest download is applied last."""
    files = [f for f in settings.incoming_dir.glob(f"{market.code}_ProfitAndLoss-*.csv")
             if pnl.market_code(f) == market.code]
    return sorted(files, key=lambda f: f.stat().st_mtime)


def _move(path: Path, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    if target.exists():
        target = target.with_name(f"{target.stem}_{dt.datetime.now():%Y%m%d%H%M%S}{target.suffix}")
    shutil.move(str(path), str(target))


class _Connection:
    """Opened on first use, so a run with nothing to upload never touches the database."""

    def __init__(self):
        self.conn = None

    def get(self):
        if self.conn is None:
            self.conn = db.connect(config.powerbi_db_config())
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()


def run(args, settings: Settings, markets: tuple[Market, ...]) -> tuple[int, str]:
    """args: no_fetch, fetch_only, dry_run, headed, no_lock (see run.py)."""
    lock = None if args.no_lock else RunLock(settings.log_dir / ".run.lock")
    if lock:
        lock.__enter__()
    connection = _Connection()
    worst, parts = 0, []
    try:
        for market in markets:
            code, summary = _market(args, settings, market, connection)
            worst = max(worst, code)
            parts.append(f"{market.code}: {summary}")
    finally:
        connection.close()
        if lock:
            lock.__exit__(None, None, None)
    return worst, " | ".join(parts)


def _market(args, settings: Settings, market: Market, connection: _Connection) -> tuple[int, str]:
    worst, done = 0, []
    if not (args.no_fetch or args.dry_run):
        log.info("%s: downloading the P&L (last 30 days, by day) for %s", market.code, market.marketplace)
        try:
            download(settings, market, headless=not args.headed)
        except PipelineError as exc:
            log.error("%s", exc)
            worst = exc.exit_code
            done.append(f"download failed ({type(exc).__name__})")
    if args.fetch_only:
        return worst, "; ".join(done) or "downloaded"

    files = waiting_files(settings, market)
    if not files:
        log.info("%s: no files waiting in %s", market.code, settings.incoming_dir)
        return worst, "; ".join(done) or "no files"
    for path in files:
        try:
            report = pnl.read(path)
            deleted, inserted = replace_days(connection.get(), market.table, report, dry_run=args.dry_run)
        except MalformedReport as exc:
            log.error("%s", exc)
            worst = max(worst, exc.exit_code)
            if not args.dry_run:
                _move(path, settings.failed_dir)
            done.append(f"{path.name}: failed")
            continue
        except (DatabaseError, pymysql.err.MySQLError) as exc:
            log.error("%s -> %s: %s; nothing changed, the file stays for the next run", path.name, market.table, exc)
            worst = max(worst, DatabaseError.exit_code)
            done.append(f"{path.name}: database error")
            break
        if args.dry_run:
            log.info("DRY RUN %s -> %s: would replace %d rows with %d", path.name, market.table, deleted, inserted)
        else:
            log.info("%s -> %s: replaced %d rows with %d (committed)", path.name, market.table, deleted, inserted)
            _move(path, settings.processed_dir)
        done.append(f"{market.table} -{deleted}/+{inserted}")
    return worst, "; ".join(done)
