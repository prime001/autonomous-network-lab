#!/usr/bin/env python3
"""
autonomous_network_lab/health_monitor.py

Purpose
-------
A lightweight network‑device health monitor that can be used from the command
line or imported as a module.  It performs three basic checks using only the
Python standard library:

1. ICMP ping (via the system ``ping`` utility)
2. TCP connectivity test for one or more ports
3. Optional Telnet command execution (e.g., ``show version``)

The script is intended for demonstration and portfolio purposes; it does not
replace full‑featured NMS solutions.

Usage
-----
Run the script directly::

    python health_monitor.py 10.0.0.1 -u admin -p secret \\
        --ping --ports 22 443 --telnet-cmd "show version"

Arguments
---------
* ``device`` – IP address or hostname of the network device.
* ``-u/--username`` – Username for Telnet login (required if ``--telnet-cmd`` is used).
* ``-p/--password`` – Password for Telnet login (required if ``--telnet-cmd`` is used).
* ``--ping`` – Perform an ICMP ping test.
* ``--ports`` – Space‑separated list of TCP ports to test (default: 22).
* ``--telnet-cmd`` – Command to run over Telnet after successful login.
* ``--timeout`` – Socket timeout in seconds (default: 3).

Prerequisites
-------------
* The local system must have the ``ping`` executable available in ``PATH``.
* Telnet must be enabled on the target device if ``--telnet-cmd`` is used.
* The script requires Python 3.8+.

Logging
-------
Info and error messages are printed to ``stderr`` using the standard ``logging``
module.  Adjust the log level with the ``--log-level`` option if needed.
"""

import argparse
import logging
import socket
import subprocess
import sys
import time
import telnetlib
from typing import List, Optional

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Configure the root logger."""
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        numeric = logging.INFO
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #
def run_ping(host: str, count: int = 3, timeout: int = 2) -> bool:
    """
    Perform an ICMP ping using the system ``ping`` command.

    Returns ``True`` if at least one reply is received, ``False`` otherwise.
    """
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", str(count), "-w", str(timeout * 1000), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(timeout), host]

    LOGGER.debug("Executing ping command: %s", " ".join(cmd))
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        success = completed.returncode == 0
        LOGGER.info("Ping to %s %s", host, "succeeded" if success else "failed")
        return success
    except FileNotFoundError:
        LOGGER.error("Ping utility not found on this system.")
        return False
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Unexpected error during ping: %s", exc)
        return False


def check_tcp_port(host: str, port: int, timeout: int = 3) -> bool:
    """
    Attempt to open a TCP socket to ``host:port``.

    Returns ``True`` if the connection succeeds, ``False`` otherwise.
    """
    LOGGER.debug("Testing TCP connectivity to %s:%d (timeout=%ds)", host, port, timeout)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            LOGGER.info("TCP port %d on %s is open", port, host)
            return True
        except (socket.timeout, ConnectionRefusedError) as exc:
            LOGGER.warning("TCP port %d on %s is not reachable: %s", port, host, exc)
            return False
        except socket.gaierror as exc:
            LOGGER.error("Address resolution error for %s: %s", host, exc)
            return False
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("Unexpected error checking TCP port: %s", exc)
            return False


def telnet_command(
    host: str,
    username: str,
    password: str,
    command: str,
    timeout: int = 10,
    login_prompt: str = b"login: ",
    password_prompt: bytes = b"Password: ",
    shell_prompt: bytes = b">",
) -> Optional[str]:
    """
    Connect to a device via Telnet, authenticate, run a single command, and
    return the output (excluding the echoed command and prompt).  Returns ``None``
    on failure.
    """
    LOGGER.debug("Opening Telnet connection to %s", host)
    try:
        tn = telnetlib.Telnet(host, timeout=timeout)
        tn.read_until(login_prompt, timeout=timeout)
        tn.write(username.encode("utf-8") + b"\n")
        tn.read_until(password_prompt, timeout=timeout)
        tn.write(password.encode("utf-8") + b"\n")
        # Wait for the shell prompt indicating a successful login
        tn.read_until(shell_prompt, timeout=timeout)

        LOGGER.debug("Sending command: %s", command)
        tn.write(command.encode("utf-8") + b"\n")
        time.sleep(0.5)  # Give the device time to respond
        output = tn.read_until(shell_prompt, timeout=timeout).decode("utf-8", errors="ignore")

        tn.write(b"exit\n")
        tn.close()

        # Strip command echo and prompts
        lines = output.splitlines()
        cleaned = "\n".join(line for line in lines if line.strip() and command not in line)
        LOGGER.info("Telnet command output received (%d bytes)", len(cleaned))
        return cleaned
    except Exception as exc:  # pragma: no cover
        LOGGER.error("Telnet command failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Core class
# --------------------------------------------------------------------------- #
class HealthMonitor:
    """
    Encapsulates health‑check logic for a single device.
    """

    def __init__(
        self,
        host: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 3,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout

    def ping(self) -> bool:
        """Perform a ping test."""
        return run_ping(self.host, timeout=self.timeout)

    def tcp_ports(self, ports: List[int]) -> dict:
        """Check a list of TCP ports, returning a ``port -> bool`` mapping."""
        results = {}
        for port in ports:
            results[port] = check_tcp_port(self.host, port, timeout=self.timeout)
        return results

    def telnet(self, command: str) -> Optional[str]:
        """Execute a command via Telnet; requires credentials."""
        if not (self.username and self.password):
            LOGGER.error("Telnet requires both username and password.")
            return None
        return telnet_command(
            self.host,
            self.username,
            self.password,
            command,
            timeout=self.timeout,
        )


# --------------------------------------------------------------------------- #
# CLI handling
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Network device health monitor (ping, TCP, Telnet)."
    )
    parser.add_argument(
        "device",
        help="IP address or hostname of the target device.",
    )
    parser.add_argument(
        "-u",
        "--username",
        help="Username for Telnet login (required if --telnet-cmd is used).",
    )
    parser.add_argument(
        "-p",
        "--password",
        help="Password for Telnet login (required if --telnet-cmd is used).",
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="Execute an ICMP ping test.",
    )
    parser.add_argument(
        "--ports",
        nargs="+",
        type=int,
        default=[22],
        help="Space‑separated list of TCP ports to test (default: 22).",
    )
    parser.add_argument(
        "--telnet-cmd",
        help="Command to run over Telnet after login (e.g., \"show version\").",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3,
        help="Network timeout in seconds (default: 3).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)

    monitor = HealthMonitor(
        host=args.device,
        username=args.username,
        password=args.password,
        timeout=args.timeout,
    )

    overall_success = True

    if args.ping:
        if not monitor.ping():
            overall_success = False

    port_results = monitor.tcp_ports(args.ports)
    for port, ok in port_results.items():
        if not ok:
            overall_success = False

    if args.telnet_cmd:
        output = monitor.telnet(args.telnet_cmd)
        if output is None:
            overall_success = False
        else:
            print("\n--- Telnet command output ---")
            print(output.rstrip())
            print("--- End of output ---\n")

    if overall_success:
        LOGGER.info("All health checks passed for %s.", args.device)
    else:
        LOGGER.warning("One or more health checks failed for %s.", args.device)
        sys.exit(1)


if __name__ == "__main__":
    main()