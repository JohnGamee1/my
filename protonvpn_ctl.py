#!/usr/bin/env python3
"""Proton VPN control script.

Wraps the Proton VPN command-line client (`protonvpn-cli` v3 or the newer
`protonvpn` CLI shipped with the official Linux app) to give you a small,
safe Python surface for:

    * turning the VPN on (connect to a country, specific server, fastest,
      random, secure-core, or peer-to-peer server)
    * turning the VPN off (disconnect)
    * changing location (same as connect, plus reconnect helper)
    * checking status

Usage as a CLI::

    python protonvpn_ctl.py on --country US
    python protonvpn_ctl.py on --server US-NY#1
    python protonvpn_ctl.py on --fastest
    python protonvpn_ctl.py on --random
    python protonvpn_ctl.py off
    python protonvpn_ctl.py status
    python protonvpn_ctl.py switch --country JP        # disconnect then reconnect
    python protonvpn_ctl.py list-countries

Usage as a library::

    from protonvpn_ctl import ProtonVPN
    vpn = ProtonVPN()
    vpn.connect(country="US")
    vpn.status()
    vpn.disconnect()

Requires the Proton VPN Linux client to be installed and logged in
(`protonvpn-cli login <username>` for v3, or the equivalent GUI login for
the newer client). See https://protonvpn.com/support/linux-vpn-tool/ for
install instructions.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Sequence


logger = logging.getLogger("protonvpn_ctl")


# The two supported CLIs, in preference order. `protonvpn-cli` is the
# widely-scripted v3 community CLI; `protonvpn` is the newer official one.
_CANDIDATE_CLIS = ("protonvpn-cli", "protonvpn")


class ProtonVPNError(RuntimeError):
    """Any failure returned by the Proton VPN CLI."""


class ProtonVPNNotInstalled(ProtonVPNError):
    """No Proton VPN CLI binary was found on PATH."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


class ProtonVPN:
    """Thin, safe wrapper around the Proton VPN CLI."""

    def __init__(
        self,
        binary: Optional[str] = None,
        timeout: float = 90.0,
        sudo: bool = False,
    ) -> None:
        self.binary = binary or self._detect_binary()
        self.timeout = timeout
        self.sudo = sudo

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    @staticmethod
    def _detect_binary() -> str:
        for name in _CANDIDATE_CLIS:
            path = shutil.which(name)
            if path:
                logger.debug("Using Proton VPN CLI at %s", path)
                return path
        raise ProtonVPNNotInstalled(
            "Could not find `protonvpn-cli` or `protonvpn` on PATH. "
            "Install the Proton VPN Linux client "
            "(https://protonvpn.com/support/linux-vpn-tool/) and log in first."
        )

    # ------------------------------------------------------------------ #
    # Public actions
    # ------------------------------------------------------------------ #

    def connect(
        self,
        *,
        country: Optional[str] = None,
        server: Optional[str] = None,
        fastest: bool = False,
        random: bool = False,
        secure_core: bool = False,
        p2p: bool = False,
        tor: bool = False,
        protocol: Optional[str] = None,
    ) -> CommandResult:
        """Turn Proton VPN on.

        Exactly one location selector must be given: ``country``, ``server``,
        ``fastest``, ``random``, ``secure_core``, ``p2p``, or ``tor``.

        ``protocol`` is optional and passed through as ``-p`` (e.g. ``udp``,
        ``tcp``). Only used by v3 CLI; newer CLIs may ignore it.
        """
        selectors = [
            ("country", country),
            ("server", server),
            ("fastest", fastest),
            ("random", random),
            ("secure_core", secure_core),
            ("p2p", p2p),
            ("tor", tor),
        ]
        chosen = [name for name, val in selectors if val]
        if len(chosen) != 1:
            raise ValueError(
                "connect() needs exactly one selector; got: "
                f"{chosen or 'none'}"
            )

        args: list[str] = ["connect" if self._is_v3() else "c"]

        if country:
            self._validate_country(country)
            args += ["--cc", country.upper()]
        elif server:
            self._validate_server(server)
            args.append(server)
        elif fastest:
            args.append("--fastest")
        elif random:
            args.append("--random")
        elif secure_core:
            args.append("--sc")
        elif p2p:
            args.append("--p2p")
        elif tor:
            args.append("--tor")

        if protocol:
            if protocol.lower() not in {"udp", "tcp"}:
                raise ValueError("protocol must be 'udp' or 'tcp'")
            args += ["-p", protocol.lower()]

        return self._run(args, check=True)

    def disconnect(self) -> CommandResult:
        """Turn Proton VPN off."""
        cmd = ["disconnect"] if self._is_v3() else ["d"]
        return self._run(cmd, check=True)

    def status(self) -> CommandResult:
        """Return current connection status."""
        return self._run(["status"], check=False)

    def reconnect(self) -> CommandResult:
        """Reconnect to the last-used server."""
        cmd = ["reconnect"] if self._is_v3() else ["r"]
        return self._run(cmd, check=True)

    def switch(self, **connect_kwargs) -> CommandResult:
        """Disconnect (if connected) and connect somewhere new."""
        try:
            self.disconnect()
        except ProtonVPNError as exc:
            # Not being connected is fine; anything else propagates.
            logger.debug("disconnect before switch failed: %s", exc)
        return self.connect(**connect_kwargs)

    def is_connected(self) -> bool:
        """True if the CLI reports an active tunnel."""
        result = self.status()
        text = result.output.lower()
        # Both CLIs print "Status:" line; v3 uses "Connected" / "Disconnected".
        if "no active" in text or "disconnected" in text or "not connected" in text:
            return False
        return "connected" in text or "ip:" in text

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    _COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
    # Server names look like "US-NY#1", "JP#12", "CH-US-1#3", etc.
    _SERVER_RE = re.compile(r"^[A-Za-z]{2}(?:-[A-Za-z0-9]+)*#\d+$")

    @classmethod
    def _validate_country(cls, code: str) -> None:
        if not cls._COUNTRY_RE.match(code):
            raise ValueError(
                f"country must be a 2-letter ISO code (got {code!r})"
            )

    @classmethod
    def _validate_server(cls, name: str) -> None:
        if not cls._SERVER_RE.match(name):
            raise ValueError(
                f"server name looks wrong (got {name!r}); "
                "expected e.g. 'US-NY#1' or 'JP#12'"
            )

    def _is_v3(self) -> bool:
        return os.path.basename(self.binary) == "protonvpn-cli"

    def _run(self, args: Sequence[str], *, check: bool) -> CommandResult:
        cmd = [self.binary, *args]
        if self.sudo:
            cmd = ["sudo", "-n", *cmd]
        logger.debug("running: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ProtonVPNNotInstalled(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProtonVPNError(
                f"`{' '.join(cmd)}` timed out after {self.timeout}s"
            ) from exc

        result = CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        if check and not result.ok:
            raise ProtonVPNError(
                f"`{' '.join(cmd)}` exited {result.returncode}: {result.output}"
            )
        return result


# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protonvpn_ctl",
        description="Turn Proton VPN on/off and change its location.",
    )
    p.add_argument("--binary", help="Path to protonvpn-cli / protonvpn binary.")
    p.add_argument(
        "--timeout", type=float, default=90.0, help="Command timeout in seconds."
    )
    p.add_argument("--sudo", action="store_true", help="Prefix commands with sudo -n.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")

    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_location_flags(sp: argparse.ArgumentParser) -> None:
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("--country", "-c", help="2-letter country code, e.g. US, JP.")
        g.add_argument("--server", "-s", help="Server name, e.g. US-NY#1.")
        g.add_argument("--fastest", action="store_true", help="Fastest server.")
        g.add_argument("--random", action="store_true", help="Random server.")
        g.add_argument("--secure-core", action="store_true", dest="secure_core")
        g.add_argument("--p2p", action="store_true", help="Peer-to-peer server.")
        g.add_argument("--tor", action="store_true", help="Tor-over-VPN server.")
        sp.add_argument("--protocol", "-p", choices=("udp", "tcp"))

    sp_on = sub.add_parser("on", help="Turn the VPN on.")
    _add_location_flags(sp_on)

    sub.add_parser("off", help="Turn the VPN off.")
    sub.add_parser("status", help="Show current connection status.")
    sub.add_parser("reconnect", help="Reconnect to the last server.")

    sp_switch = sub.add_parser(
        "switch", help="Disconnect (if needed) and connect somewhere new."
    )
    _add_location_flags(sp_switch)

    return p


def _connect_kwargs(ns: argparse.Namespace) -> dict:
    return {
        "country": ns.country,
        "server": ns.server,
        "fastest": ns.fastest,
        "random": ns.random,
        "secure_core": ns.secure_core,
        "p2p": ns.p2p,
        "tor": ns.tor,
        "protocol": ns.protocol,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        vpn = ProtonVPN(binary=args.binary, timeout=args.timeout, sudo=args.sudo)
    except ProtonVPNNotInstalled as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 127

    try:
        if args.cmd == "on":
            result = vpn.connect(**_connect_kwargs(args))
        elif args.cmd == "off":
            result = vpn.disconnect()
        elif args.cmd == "status":
            result = vpn.status()
        elif args.cmd == "reconnect":
            result = vpn.reconnect()
        elif args.cmd == "switch":
            result = vpn.switch(**_connect_kwargs(args))
        else:  # pragma: no cover - argparse guards this
            raise AssertionError(args.cmd)
    except (ProtonVPNError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result.output:
        print(result.output)
    return 0 if result.ok else result.returncode


if __name__ == "__main__":
    sys.exit(main())
