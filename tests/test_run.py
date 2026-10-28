from __future__ import annotations

import pytest

import run
from app import config, job
from app.exceptions import ConfigurationError


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("SALES_MARKETS", "US_MARKETPLACE", "US_POWERBI_TABLE", "CA_MARKETPLACE", "CA_POWERBI_TABLE",
                "MX_MARKETPLACE", "MX_POWERBI_TABLE"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_help_lists_the_modes(capsys):
    with pytest.raises(SystemExit) as exit_info:
        run.build_parser().parse_args(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--login", "--fetch-only", "--no-fetch", "--dry-run", "--market"):
        assert flag in out


def test_default_markets(clean_env):
    assert config.markets() == (
        config.Market("US", "Amazon.com", "combined_transposed_file_US"),
        config.Market("CA", "Amazon.ca", "combined_transposed_file_CA"),
    )


def test_markets_from_env(clean_env):
    clean_env.setenv("SALES_MARKETS", "ca, mx")
    clean_env.setenv("MX_MARKETPLACE", "Amazon.com.mx")
    clean_env.setenv("MX_POWERBI_TABLE", "combined_transposed_file_MX")
    assert [m.code for m in config.markets()] == ["CA", "MX"]


@pytest.mark.parametrize("env, message", [
    ({"SALES_MARKETS": "MX"}, "set MX_MARKETPLACE"),
    ({"SALES_MARKETS": "US", "US_POWERBI_TABLE": "x; DROP TABLE y"}, "plain table name"),
    ({"SALES_MARKETS": "USA"}, "two-letter"),
])
def test_bad_market_settings(clean_env, env, message):
    for key, value in env.items():
        clean_env.setenv(key, value)
    with pytest.raises(ConfigurationError, match=message):
        config.markets()


def test_waiting_files_belong_to_their_market(tmp_path, clean_env):
    settings = config.Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", log_level="INFO",
                               markets=config.markets())
    settings.ensure_directories()
    for name in ("US_ProfitAndLoss-27_09_2026-27_10_2026_sellerboard.com.csv",
                 "CA_ProfitAndLoss-27_09_2026-27_10_2026_sellerboard.com.csv",
                 "ProfitAndLoss-27_09_2026-27_10_2026_sellerboard.com (1).csv"):
        (settings.incoming_dir / name).write_text("x", encoding="utf-8")
    us, ca = settings.markets
    assert [f.name[:3] for f in job.waiting_files(settings, us)] == ["US_"]
    assert [f.name[:3] for f in job.waiting_files(settings, ca)] == ["CA_"]
