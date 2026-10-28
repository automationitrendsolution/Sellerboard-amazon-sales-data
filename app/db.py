"""MySQL connection (PyMySQL) to the Power BI database."""
from __future__ import annotations

import pymysql

from .config import DbConfig
from .exceptions import DatabaseError


def connect(config: DbConfig) -> pymysql.connections.Connection:
    """Open a connection with autocommit off; callers commit or roll back explicitly."""
    kwargs = dict(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=config.connect_timeout,
        read_timeout=config.read_timeout,
        write_timeout=config.read_timeout,
    )
    if config.ssl_ca:
        kwargs["ssl"] = {"ca": config.ssl_ca}
    try:
        return pymysql.connect(**kwargs)
    except pymysql.err.MySQLError as exc:
        detail = exc.args[-1] if exc.args else exc
        raise DatabaseError(f"cannot connect to the Power BI database ({config.describe()}): {detail}") from exc
