#!/usr/bin/env python3
"""Proton VPN Chrome-extension controller (Playwright).

Drives the Proton VPN browser extension by opening its popup page in a
Chrome instance that reuses your existing profile (so the extension is
already installed and logged in). Exposes turn-on, turn-off, switch
location, and status.

Prereqs
-------
    pip install playwright
    playwright install chromium        # only needed if you don't use
                                       # your system Chrome via `channel="chrome"`

Google Chrome must be installed. **Close every Chrome window that is
using the profile you point at** before running — Chrome will not let
two processes share the same profile lock.

CLI
---
    python protonvpn_ext.py on                       # quick connect
    python protonvpn_ext.py on --country "United States"
    python protonvpn_ext.py off
    python protonvpn_ext.py status
    python protonvpn_ext.py switch --country Japan
    python protonvpn_ext.py dump-dom                 # for tuning selectors

Library
-------
    from protonvpn_ext import ProtonVPNExtension
    with ProtonVPNExtension() as vpn:
        vpn.connect(country="Japan")
        print(vpn.status())
        vpn.disconnect()

Selectors
---------
The Proton VPN extension DOM changes with updates. If a click stops
finding its target, dump the popup HTML with ``dump-dom`` and override
the offending selector via ``--selectors-file selectors.json`` (or the
``selectors=`` kwarg). Each field of :class:`Selectors` is documented
below.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

try:  # Optional so `--help` works without Playwright installed.
    from playwright.sync_api import (
        BrowserContext,
        Page,
        TimeoutError as PWTimeout,
        sync_playwright,
    )
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]
    BrowserContext = Page = object  # type: ignore[assignment]

    class PWTimeout(Exception):  # type: ignore[no-redef]
        pass


logger = logging.getLogger("protonvpn_ext")


# Chrome Web Store id of "Proton VPN — Free VPN made by Proton".
PROTONVPN_EXTENSION_ID = "jplnlifepflhkbkgonidnobkakhmpnmh"


# --------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------- #

def default_user_data_dir() -> Path:
    """Best-guess path to the user's Chrome profile root."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "Google/Chrome/User Data"
    # Linux / *BSD
    return Path.home() / ".config/google-chrome"


@dataclass
class Selectors:
    """Playwright selectors for the extension popup.

    Defaults use role/text lookups so they survive minor markup changes.
    ``country_item`` is a format string; ``{country}`` is interpolated.
    """

    quick_connect: str = "button:has-text('Quick Connect'), button:has-text('Quick connect')"
    disconnect: str = "button:has-text('Disconnect'), [aria-label='Disconnect']"
    locations_tab: str = (
        "button:has-text('Locations'), button:has-text('Countries'),"
        " [role='tab']:has-text('Locations')"
    )
    country_search: str = "input[type='search'], input[placeholder*='Search' i]"
    country_item: str = "button:has-text('{country}'), li:has-text('{country}')"
    status_text: str = (
        "[data-testid='connection-status'], .connection-status,"
        " header:has-text('Connected'), header:has-text('Not connected')"
    )


# --------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------- #

class ProtonVPNExtensionError(RuntimeError):
    """Any failure driving the Proton VPN extension."""


# --------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------- #

class ProtonVPNExtension:
    """Playwright-driven controller for the Proton VPN Chrome extension."""

    def __init__(
        self,
        *,
        user_data_dir: Optional[os.PathLike] = None,
        profile: str = "Default",
        extension_id: str = PROTONVPN_EXTENSION_ID,
        chrome_channel: str = "chrome",
        chrome_executable: Optional[str] = None,
        headless: bool = False,
        selectors: Optional[Selectors] = None,
        slow_mo_ms: int = 0,
    ) -> None:
        if sync_playwright is None:
            raise ProtonVPNExtensionError(
                "Playwright is not installed. Run: "
                "`pip install playwright` (and `playwright install chromium` "
                "if you won't be using your system Chrome)."
            )
        self.user_data_dir = Path(user_data_dir) if user_data_dir else default_user_data_dir()
        self.profile = profile
        self.extension_id = extension_id
        self.chrome_channel = chrome_channel
        self.chrome_executable = chrome_executable
        self.headless = headless
        self.selectors = selectors or Selectors()
        self.slow_mo_ms = slow_mo_ms
        self._pw = None
        self._ctx: Optional[BrowserContext] = None

    # -- context management --------------------------------------------- #

    def __enter__(self) -> "ProtonVPNExtension":
        self._start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _start(self) -> None:
        self._pw = sync_playwright().start()
        launch_kwargs: dict = {
            "user_data_dir": str(self.user_data_dir),
            "headless": self.headless,
            "slow_mo": self.slow_mo_ms,
            # Extensions need these off to load reliably.
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.chrome_executable:
            launch_kwargs["executable_path"] = self.chrome_executable
        else:
            launch_kwargs["channel"] = self.chrome_channel

        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            msg = str(exc)
            if "SingletonLock" in msg or "ProcessSingleton" in msg or "profile" in msg.lower():
                raise ProtonVPNExtensionError(
                    f"Could not open Chrome profile at {self.user_data_dir!s}. "
                    "Close every Chrome window using this profile and try again."
                ) from exc
            raise ProtonVPNExtensionError(msg) from exc

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._ctx = None
            self._pw = None

    # -- extension popup ----------------------------------------------- #

    def _popup_url(self) -> str:
        """Locate the extension's popup HTML from its on-disk manifest."""
        ext_root = self.user_data_dir / self.profile / "Extensions" / self.extension_id
        if not ext_root.exists():
            raise ProtonVPNExtensionError(
                f"Proton VPN extension (id={self.extension_id}) is not installed "
                f"in profile {self.profile!r} at {self.user_data_dir}. "
                "Install it from the Chrome Web Store and sign in first."
            )
        version_dirs = sorted(
            (d for d in ext_root.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for v in version_dirs:
            manifest_path = v / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("skipping unreadable manifest %s: %s", manifest_path, exc)
                continue
            popup = (
                manifest.get("action", {}).get("default_popup")
                or manifest.get("browser_action", {}).get("default_popup")
            )
            if popup:
                return f"chrome-extension://{self.extension_id}/{popup.lstrip('/')}"
        return f"chrome-extension://{self.extension_id}/popup.html"

    def _open_popup(self) -> Page:
        if self._ctx is None:
            raise ProtonVPNExtensionError("Driver not started; use as a context manager.")
        page = self._ctx.new_page()
        url = self._popup_url()
        logger.debug("opening popup: %s", url)
        page.goto(url)
        page.wait_for_load_state("domcontentloaded")
        return page

    # -- public actions ------------------------------------------------ #

    def connect(
        self,
        *,
        country: Optional[str] = None,
        timeout: float = 25.0,
    ) -> str:
        """Turn the VPN on. If ``country`` is None, uses Quick Connect."""
        page = self._open_popup()
        try:
            if country:
                self._click(page, self.selectors.locations_tab, timeout=10.0)
                with contextlib.suppress(PWTimeout):
                    page.locator(self.selectors.country_search).first.fill(
                        country, timeout=3000
                    )
                sel = self.selectors.country_item.format(country=country)
                self._click(page, sel, timeout=15.0)
            else:
                self._click(page, self.selectors.quick_connect, timeout=timeout)
            self._wait_for_state(page, want_connected=True, timeout=timeout)
            return self._read_status(page)
        finally:
            page.close()

    def disconnect(self, *, timeout: float = 15.0) -> str:
        """Turn the VPN off."""
        page = self._open_popup()
        try:
            self._click(page, self.selectors.disconnect, timeout=timeout)
            self._wait_for_state(page, want_connected=False, timeout=timeout)
            return self._read_status(page)
        finally:
            page.close()

    def switch(self, *, country: str, timeout: float = 30.0) -> str:
        """Disconnect (if connected) then connect to ``country``."""
        with contextlib.suppress(ProtonVPNExtensionError):
            self.disconnect(timeout=timeout / 2)
        return self.connect(country=country, timeout=timeout)

    def status(self) -> str:
        """Return the popup's current status text."""
        page = self._open_popup()
        try:
            return self._read_status(page)
        finally:
            page.close()

    def is_connected(self) -> bool:
        text = self.status().lower()
        if "not connected" in text or "disconnected" in text:
            return False
        return "connected" in text

    def dump_dom(self) -> str:
        """Return the popup's rendered HTML — useful for tuning selectors."""
        page = self._open_popup()
        try:
            page.wait_for_timeout(500)
            return page.content()
        finally:
            page.close()

    # -- internals ----------------------------------------------------- #

    def _click(self, page: Page, selector: str, *, timeout: float) -> None:
        try:
            page.locator(selector).first.click(timeout=int(timeout * 1000))
        except PWTimeout as exc:
            raise ProtonVPNExtensionError(
                f"Could not find/click selector {selector!r} within {timeout}s. "
                "The extension DOM may have changed — run `dump-dom` and override "
                "the selector."
            ) from exc

    def _read_status(self, page: Page) -> str:
        with contextlib.suppress(PWTimeout):
            return page.locator(self.selectors.status_text).first.inner_text(
                timeout=5000
            ).strip()
        # Fallback: return a truncated body dump so the caller sees *something*.
        with contextlib.suppress(PWTimeout):
            return page.locator("body").inner_text(timeout=2000)[:400].strip()
        return ""

    def _wait_for_state(
        self, page: Page, *, want_connected: bool, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            text = self._read_status(page).lower()
            last = text
            connected_now = "connected" in text and "not connected" not in text
            if connected_now == want_connected:
                return
            time.sleep(0.5)
        state = "connect" if want_connected else "disconnect"
        raise ProtonVPNExtensionError(
            f"Timed out waiting for {state} after {timeout}s. Last status: {last!r}"
        )


# --------------------------------------------------------------------- #
# Selector loading
# --------------------------------------------------------------------- #

def load_selectors(path: Optional[str]) -> Selectors:
    if not path:
        return Selectors()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {f.name for f in fields(Selectors)}
    unknown = set(data) - known
    if unknown:
        raise ProtonVPNExtensionError(
            f"Unknown selector keys in {path}: {sorted(unknown)}. "
            f"Known: {sorted(known)}."
        )
    return Selectors(**{**asdict(Selectors()), **data})


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protonvpn_ext",
        description="Drive the Proton VPN Chrome extension from Python.",
    )
    p.add_argument("--user-data-dir", help="Chrome profile root (default: your OS default).")
    p.add_argument("--profile", default="Default", help="Chrome profile directory name.")
    p.add_argument("--extension-id", default=PROTONVPN_EXTENSION_ID)
    p.add_argument("--chrome-executable", help="Explicit Chrome binary path.")
    p.add_argument(
        "--chrome-channel", default="chrome",
        help="Playwright browser channel (chrome, chrome-beta, msedge, chromium).",
    )
    p.add_argument("--headless", action="store_true")
    p.add_argument("--slow-mo", type=int, default=0, help="Slow every action by N ms.")
    p.add_argument("--selectors-file", help="JSON file overriding one or more selectors.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp_on = sub.add_parser("on", help="Turn the VPN on (quick connect if no --country).")
    sp_on.add_argument("--country", "-c", help="Country name as shown in the extension.")
    sp_on.add_argument("--timeout", type=float, default=25.0)

    sp_off = sub.add_parser("off", help="Turn the VPN off.")
    sp_off.add_argument("--timeout", type=float, default=15.0)

    sub.add_parser("status", help="Show current status.")

    sp_switch = sub.add_parser("switch", help="Disconnect then reconnect somewhere new.")
    sp_switch.add_argument("--country", "-c", required=True)
    sp_switch.add_argument("--timeout", type=float, default=30.0)

    sub.add_parser(
        "dump-dom",
        help="Print the popup HTML (useful for tuning selectors).",
    )

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        selectors = load_selectors(args.selectors_file)
    except (ProtonVPNExtensionError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        with ProtonVPNExtension(
            user_data_dir=args.user_data_dir,
            profile=args.profile,
            extension_id=args.extension_id,
            chrome_channel=args.chrome_channel,
            chrome_executable=args.chrome_executable,
            headless=args.headless,
            selectors=selectors,
            slow_mo_ms=args.slow_mo,
        ) as vpn:
            if args.cmd == "on":
                print(vpn.connect(country=args.country, timeout=args.timeout))
            elif args.cmd == "off":
                print(vpn.disconnect(timeout=args.timeout))
            elif args.cmd == "status":
                print(vpn.status())
            elif args.cmd == "switch":
                print(vpn.switch(country=args.country, timeout=args.timeout))
            elif args.cmd == "dump-dom":
                sys.stdout.write(vpn.dump_dom())
            else:  # pragma: no cover - argparse guards this
                raise AssertionError(args.cmd)
    except ProtonVPNExtensionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
