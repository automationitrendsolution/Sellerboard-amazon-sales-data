"""Settings from environment variables (the project's .env is loaded first).

Error messages name variables, never their values.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Market code (file prefix, --market value) -> Sellerboard marketplace and the Power BI table it fills.
# <CODE>_MARKETPLACE / <CODE>_POWERBI_TABLE in .env override these; SALES_MARKETS picks which run.
DEFAULT_MARKETS: dict[str, tuple[str, str]] = {
    "US": ("Amazon.com", "combined_transposed_file_US"),
    "CA": ("Amazon.ca", "combined_transposed_file_CA"),
}


@dataclass(frozen=True)
class Market:
    code: str         # "US": prefix of the saved file name and value of --market
    marketplace: str  # Sellerboard's marketplace filter, e.g. "Amazon.com"
    table: str        # powerbidb table whose days the export replaces


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _project_path(value: str) -> Path:
    """A path setting; relative paths are taken from the project folder, not the current one."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _table_name(name: str, setting: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ConfigurationError(f"{setting} must be a plain table name")
    return name


def markets() -> tuple[Market, ...]:
    codes = [c.strip().upper() for c in (_env("SALES_MARKETS", ",".join(DEFAULT_MARKETS)) or "").split(",") if c.strip()]
    if not codes:
        raise ConfigurationError("SALES_MARKETS lists no market")
    result = []
    for code in dict.fromkeys(codes):
        if not re.fullmatch(r"[A-Z]{2}", code):
            raise ConfigurationError(f"SALES_MARKETS: {code!r} must be a two-letter code such as US")
        marketplace, table = DEFAULT_MARKETS.get(code, (None, None))
        marketplace = _env(f"{code}_MARKETPLACE", marketplace)
        table = _env(f"{code}_POWERBI_TABLE", table)
        if not (marketplace and table):
            raise ConfigurationError(f"market {code}: set {code}_MARKETPLACE and {code}_POWERBI_TABLE in .env")
        result.append(Market(code, marketplace, _table_name(table, f"{code}_POWERBI_TABLE")))
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_dir: Path
    log_level: str
    markets: tuple[Market, ...]
    account: str | None = None  # Sellerboard account; default: "account" in config/selectors.json
    login_email: str | None = field(default=None, repr=False)
    login_password: str | None = field(default=None, repr=False)
    shared_session_file: Path | None = None  # reuse another project's Sellerboard session

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.data_dir / "failed"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def session_file(self) -> Path:
        return self.shared_session_file or self.data_dir / "browser" / "session.json"

    @property
    def chrome_profile_dir(self) -> Path:
        return self.data_dir / "browser" / "chrome-profile"

    @property
    def selectors_file(self) -> Path:
        return PROJECT_ROOT / "config" / "selectors.json"

    def ensure_directories(self) -> None:
        for path in (self.incoming_dir, self.processed_dir, self.failed_dir, self.screenshot_dir,
                     self.session_file.parent, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Path | None = None) -> Settings:
    """Shell variables win over the --env-file, which wins over the project's .env."""
    if env_file is not None:
        load_dotenv(env_file, override=False)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    session = _env("SELLERBOARD_SESSION_FILE")
    return Settings(
        data_dir=_project_path(_env("SELLERBOARD_DATA_DIR", "data")),
        log_dir=_project_path(_env("SELLERBOARD_LOG_DIR", "logs")),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        markets=markets(),
        account=_env("SELLERBOARD_ACCOUNT"),
        login_email=_env("SELLERBOARD_LOGIN_EMAIL"),
        login_password=_env("SELLERBOARD_LOGIN_PASSWORD"),
        shared_session_file=_project_path(session) if session else None,
    )


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    ssl_ca: str | None = None
    connect_timeout: int = 10
    read_timeout: int = 120

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


def powerbi_db_config() -> DbConfig:
    values = {key: _env("POWERBI_DB_" + key) for key in ("HOST", "PORT", "NAME", "USER", "PASSWORD")}
    values["PORT"] = values["PORT"] or "3306"
    missing = ["POWERBI_DB_" + key for key, value in values.items() if value is None]
    if missing:
        raise ConfigurationError(f"set {', '.join(missing)} in {PROJECT_ROOT / '.env'} (see .env.example)")
    try:
        port = int(values["PORT"])
    except ValueError:
        raise ConfigurationError("POWERBI_DB_PORT must be a whole number") from None
    return DbConfig(host=values["HOST"], port=port, database=values["NAME"], user=values["USER"],
                    password=values["PASSWORD"], ssl_ca=_env("POWERBI_DB_SSL_CA"))
