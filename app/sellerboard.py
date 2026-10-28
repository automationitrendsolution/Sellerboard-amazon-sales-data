"""Sellerboard in a browser (Playwright): sign-in, P&L export, page inspection.

Scheduled runs reuse a saved session (data/browser/session.json, or another
project's via SELLERBOARD_SESSION_FILE) headless. A person signs in once with
``run.py --login``: a Chrome window on a PC, or in the terminal on a server with
SELLERBOARD_LOGIN_EMAIL/PASSWORD. If the session has expired a run signs in once by
itself; after a refused sign-in it waits 24 hours, so the account is not locked.

The export follows the Scribe "How to Export Amazon Sales Data from Sellerboard":
Profit > P&L, "Last 30 days, by day", one marketplace, Filter, download > .CSV.
The CSV does not name its marketplace, so before downloading the run checks that
the page shows exactly the requested account, marketplace and period. The
selectors live in config/selectors.json; when Sellerboard changes its page the run
stops with exit 4 and a screenshot in data/screenshots/.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .config import Market, Settings
from .exceptions import ConfigurationError, DownloadFailed, PageChanged, SignInNeeded
from .logging_config import get_logger

APP_URL = "https://app.sellerboard.com"
LOGIN_URL = f"{APP_URL}/en/auth/login"
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)
AUTOMATIC_RETRY_AFTER = dt.timedelta(hours=24)

log = get_logger("sellerboard")


def is_login_page(url: str) -> bool:
    return "/auth/login" in url


def load_selectors(settings: Settings) -> dict:
    try:
        return json.loads(settings.selectors_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read {settings.selectors_file}: {exc}") from None


def _save_session(context, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    try:
        path.chmod(0o600)  # the cookies are as good as the password
    except OSError:
        pass


def _screenshot(page, settings: Settings, name: str) -> Path:
    path = settings.screenshot_dir / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass
    return path


# --------------------------------------------------------------------------- sign-in


def login(settings: Settings, *, headed: bool, timeout_minutes: int = 20) -> None:
    """Save a session for scheduled runs: a Chrome window when there is a screen, else the terminal."""
    if not headed:
        credential_login(settings, automatic=False)
        return
    chrome = next((c for c in CHROME_CANDIDATES if Path(c).is_file()), None)
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome = chrome or (str(local) if local.is_file() else None)
    if chrome is None:
        raise ConfigurationError("Google Chrome is not installed; use `run.py --login --terminal`")
    _chrome_login(settings, chrome, timeout_minutes=timeout_minutes)


def _attempts_file(settings: Settings) -> Path:
    return settings.data_dir / "browser" / "login-attempts.json"


def _last_failure(settings: Settings) -> dt.datetime | None:
    try:
        record = json.loads(_attempts_file(settings).read_text(encoding="utf-8"))
        return dt.datetime.fromisoformat(record["last_failure"]) if record.get("last_failure") else None
    except (OSError, ValueError, KeyError):
        return None


def _record_attempt(settings: Settings, ok: bool, detail: str) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    path = _attempts_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_attempt": now, "ok": ok, "detail": detail[:300],
                                "last_failure": None if ok else now}, indent=1), encoding="utf-8")


def credential_login(settings: Settings, *, automatic: bool) -> None:
    """One headless sign-in with SELLERBOARD_LOGIN_EMAIL/PASSWORD ("Keep me logged in"), then save the session."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    if not (settings.login_email and settings.login_password):
        raise SignInNeeded("set SELLERBOARD_LOGIN_EMAIL and SELLERBOARD_LOGIN_PASSWORD in .env, or run "
                           "`python run.py --login` on a PC and copy data/browser/session.json")
    last = _last_failure(settings)
    if automatic and last and dt.datetime.now() - last < AUTOMATIC_RETRY_AFTER:
        raise SignInNeeded(f"the last automatic sign-in was refused at {last:%Y-%m-%d %H:%M}; not trying again "
                           "for 24 hours so the account is not locked. Run `python run.py --login` by hand.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-GB")
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="networkidle", timeout=90_000)
            page.fill("#username", settings.login_email)
            page.fill("#password", settings.login_password)
            keep = page.locator("#keeploggedin")
            try:
                if not keep.is_checked():
                    keep.check(timeout=5_000)
            except PlaywrightTimeout:
                page.get_by_text("Keep me logged in").first.click()
            print(f"signing in as {settings.login_email} (one attempt) ...", flush=True)
            page.locator("button[type=submit]").first.click()
            try:
                page.wait_for_url(lambda url: not is_login_page(url), timeout=45_000)
            except PlaywrightTimeout:
                pass

            code_box = page.locator("input[autocomplete=one-time-code], input[name*=code i], input[name*=otp i]")
            if is_login_page(page.url) and code_box.count() and sys.stdin.isatty():
                code_box.first.fill(input("Sellerboard verification code: ").strip())
                code_box.first.press("Enter")
                try:
                    page.wait_for_url(lambda url: not is_login_page(url), timeout=45_000)
                except PlaywrightTimeout:
                    pass

            if is_login_page(page.url):
                messages = [t.strip() for t in page.locator(
                    ".alert, .error, .invalid-feedback, .help-block, [class*=error]").all_inner_texts() if t.strip()]
                detail = "; ".join(dict.fromkeys(messages))[:200] or "no message shown"
                shot = _screenshot(page, settings, "login-refused")
                _record_attempt(settings, False, detail)
                raise SignInNeeded(f"Sellerboard did not accept the sign-in ({detail}); see {shot}")
            _save_session(context, settings.session_file)
            _record_attempt(settings, True, page.url)
            print(f"signed in; session saved to {settings.session_file}", flush=True)
        finally:
            browser.close()


def _chrome_login(settings: Settings, chrome: str, *, port: int = 9223, timeout_minutes: int = 20) -> None:
    """An ordinary Chrome window with its own profile; Playwright attaches over the local
    debugging port only to save the session once the person is signed in."""
    from playwright.sync_api import sync_playwright

    settings.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        chrome, f"--user-data-dir={settings.chrome_profile_dir}", f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check", LOGIN_URL,
    ])
    saved = False
    try:
        with sync_playwright() as p:
            browser = None
            for _ in range(30):
                try:
                    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    if process.poll() is not None:
                        break
                    time.sleep(1)
            if browser is None:
                raise SignInNeeded(f"could not attach to Chrome (is another Chrome using port {port}?)")
            context = browser.contexts[0]
            print("Sign in in the Chrome window (tick 'Keep me logged in'), then close Chrome.", flush=True)
            deadline = time.monotonic() + timeout_minutes * 60
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    if any(APP_URL in pg.url and not is_login_page(pg.url) for pg in context.pages):
                        _save_session(context, settings.session_file)
                        if not saved:
                            saved = True
                            print(f"Signed in; session saved to {settings.session_file}", flush=True)
                    # A Playwright call, not time.sleep: the sync API only sees navigation inside its own calls.
                    context.pages[0].wait_for_timeout(3000)
                except Exception:
                    if process.poll() is not None:
                        break
                    time.sleep(1)
    finally:
        if process.poll() is None:
            process.terminate()
    if not saved:
        raise SignInNeeded("Chrome was closed before signing in; no session saved")


# --------------------------------------------------------------------------- headless use


def _open(p, settings: Settings, headless: bool):
    if not settings.session_file.is_file():
        raise SignInNeeded(f"no saved session at {settings.session_file}: run `python run.py --login`")
    browser = p.chromium.launch(headless=headless)
    context = browser.new_context(storage_state=str(settings.session_file), accept_downloads=True,
                                  viewport={"width": 1600, "height": 1000}, timezone_id=settings.timezone)
    return browser, context


def _goto(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=60_000)
    if is_login_page(page.url):
        raise SignInNeeded("Sellerboard ended the saved session: run `python run.py --login`")


def _ensure_account(page, sel: dict, settings: Settings, url: str) -> None:
    """Switch Sellerboard to the configured account if another one is selected."""
    wanted = settings.account or sel.get("account")
    if not wanted:
        return
    opener = page.locator(sel["account_opener"]).first
    if opener.inner_text().strip() == wanted:
        return
    opener.click(timeout=30_000)
    page.get_by_text(wanted, exact=True).first.click(timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=90_000)
    _goto(page, url)
    current = page.locator(sel["account_opener"]).first.inner_text().strip()
    if current != wanted:
        raise PageChanged(f"could not switch to Sellerboard account {wanted!r} (still {current!r})")
    log.info("switched Sellerboard account to %s", wanted)


def _selected_markets(page, sel: dict) -> list[str]:
    return page.locator(sel["market_select"]).first.evaluate("s => [...s.selectedOptions].map(o => o.value)")


def _choose_period(page, sel: dict, attempts: int = 3) -> None:
    """Open the period list and pick the period. While the page is still settling the list sometimes
    ignores the click, so close it and click again."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    item = page.locator(sel["period_item"], has_text=sel["period"]).first
    for attempt in range(1, attempts + 1):
        page.locator(sel["period_opener"]).first.click(timeout=30_000)
        try:
            item.click(timeout=10_000)
            return
        except PlaywrightTimeout:
            if attempt == attempts:
                raise
            log.info("the period list did not open; trying again (%d/%d)", attempt + 1, attempts)
            page.keyboard.press("Escape")
            page.wait_for_timeout(3_000)


def fetch_pl(settings: Settings, market: Market, *, headless: bool = True) -> Path:
    """Export the P&L, last 30 days by day, for one marketplace into data/incoming/<CODE>_<file>.csv."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    sel = load_selectors(settings)
    timeout_ms = int(sel.get("download_timeout_seconds", 180)) * 1000
    url = sel["pl_url"].format(market=quote(market.marketplace))
    with sync_playwright() as p:
        browser, context = _open(p, settings, headless)
        page = context.new_page()
        step = "open the P&L page"
        try:
            _goto(page, url)
            step = "select the account"
            _ensure_account(page, sel, settings, url)
            step = f"choose '{sel['period']}'"
            _choose_period(page, sel)
            step = "click Filter"
            page.locator(sel["filter_button"]).first.click(timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=90_000)

            step = "check the filters"
            shown = page.locator(sel["period_opener"]).first.inner_text().strip()
            markets = _selected_markets(page, sel)
            if markets != [market.marketplace] or shown != sel["period"] or sel["period_url_value"] not in page.url:
                raise PageChanged(f"the page shows {markets} / {shown!r}, not [{market.marketplace!r}] / "
                                  f"{sel['period']!r}; nothing downloaded")

            step = "download the .CSV"
            page.locator(sel["export_opener"]).first.click(timeout=30_000)
            with page.expect_download(timeout=timeout_ms) as pending:
                page.locator(sel["export_csv"]).first.click(timeout=30_000)
            download = pending.value
            target = settings.incoming_dir / f"{market.code}_{download.suggested_filename}"
            if target.exists():
                target = target.with_name(f"{target.stem}_{dt.datetime.now():%H%M%S}{target.suffix}")
            download.save_as(str(target))
            _save_session(context, settings.session_file)  # keep the refreshed cookies
            log.info("downloaded %s (%s)", target.name, market.marketplace)
            return target
        except PageChanged as exc:
            shot = _screenshot(page, settings, f"page-changed-{market.code}")
            raise PageChanged(f"{market.code}: {exc}; see {shot}") from None
        except PlaywrightTimeout:
            shot = _screenshot(page, settings, f"timeout-{market.code}")
            error = DownloadFailed if step == "download the .CSV" else PageChanged
            raise error(f"{market.code}: could not {step} (timed out); see {shot}") from None
        except PlaywrightError as exc:
            raise DownloadFailed(f"{market.code}: browser error while trying to {step}: {exc}") from None
        finally:
            browser.close()


def inspect_page(settings: Settings, *, headless: bool = True) -> tuple[Path, Path]:
    """Screenshot of the P&L page plus a JSON list of its controls, for fixing config/selectors.json."""
    from playwright.sync_api import sync_playwright

    sel = load_selectors(settings)
    url = sel["pl_url"].format(market=quote(settings.markets[0].marketplace))
    with sync_playwright() as p:
        browser, context = _open(p, settings, headless)
        page = context.new_page()
        try:
            _goto(page, url)
            shot = _screenshot(page, settings, "inspect")
            controls = page.evaluate("""() => [...document.querySelectorAll(
                'button, a, input, select, li, [role=button], [data-toggle], [ng-click]')]
                .filter(e => e.offsetParent !== null && e.getBoundingClientRect().top < 160)
                .map(e => ({tag: e.tagName.toLowerCase(), text: (e.innerText || e.value || '').trim().slice(0, 80),
                            id: e.id || null, cls: typeof e.className === 'string' ? e.className.slice(0, 120) : null,
                            attrs: Object.fromEntries([...e.attributes]
                                .filter(a => a.name.startsWith('data-') || a.name.startsWith('ng-') || a.name === 'href')
                                .map(a => [a.name, a.value.slice(0, 80)]))}))""")
            controls_file = shot.with_suffix(".json")
            controls_file.write_text(json.dumps({"url": page.url, "controls": controls}, indent=1,
                                                ensure_ascii=False), encoding="utf-8")
            _save_session(context, settings.session_file)
            return shot, controls_file
        finally:
            browser.close()
