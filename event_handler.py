#!/usr/bin/env python3
"""network_event_handler.py

A lightweight event‑handler script for network devices that executes a
single command via SSH or Telnet and returns the parsed result.  The
script is intended for inclusion in a public GitHub portfolio to
demonstrate real‑world network automation using only the Python
standard library.

Features
--------
* Supports SSH (key‑based authentication) and Telnet (username/password).
* Uses ``subprocess`` for SSH and ``telnetlib`` for Telnet.
* Detailed, timestamped logging to both console and a rotating log file.
* Robust error handling – network timeouts, authentication failures,
  command errors are captured and reported.
* CLI built with ``argparse`` for easy integration into automation
  pipelines or monitoring solutions.

Prerequisites
-------------
* Python 3.8+
* For SSH: an SSH client (``ssh`` command) must be available in the PATH.
* For Telnet: no external dependencies – uses the standard library
  ``telnetlib``.
* If password authentication is required for SSH, the user must have
  ``sshpass`` installed and accessible, although key‑based authentication
  is recommended for security.

Example
-------
>>> python3 network_event_handler.py \\
...     --device 10.1.1.10 \\
...     --username admin \\
...     --password secret \\
...     --protocol telnet \\
...     --command "show version"
"""

import argparse
import logging
import logging.handlers
import subprocess
import sys
import telnetlib
import threading
import time
from datetime import datetime
from typing import Optional, Tuple

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOGGER = logging.getLogger("network_event_handler")
LOGGER.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_console_handler.setFormatter(_console_formatter)
LOGGER.addHandler(_console_handler)

_file_handler = logging.handlers.RotatingFileHandler(
    "network_event_handler.log", maxBytes=1_048_576, backupCount=3, encoding="utf-8"
)
_file_handler.setLevel(logging.DEBUG)
_file_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
_file_handler.setFormatter(_file_formatter)
LOGGER.addHandler(_file_handler)

# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #
def _run_ssh(
    host: str,
    username: str,
    command: str,
    port: int = 22,
    password: Optional[str] = None,
    timeout: int = 30,
) -> Tuple[int, str, str]:
    """Execute a command over SSH using the local ``ssh`` client.

    Args:
        host: IP address or hostname of the device.
        username: SSH user name.
        command: Command string to execute on the remote device.
        port: TCP port for SSH (default 22).
        password: If supplied, ``sshpass`` is used to provide the password.
        timeout: Seconds to wait for command completion.

    Returns:
        A tuple of (return_code, stdout, stderr).
    """
    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-p",
        str(port),
        f"{username}@{host}",
        command,
    ]

    if password:
        # Use sshpass if password is provided; ensure it is installed.
        ssh_cmd = ["sshpass", "-p", password] + ssh_cmd

    LOGGER.debug("Running SSH command: %s", " ".join(ssh_cmd))
    try:
        proc = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        LOGGER.error("Required binary not found: %s", exc)
        raise
    except subprocess.TimeoutExpired as exc:
        LOGGER.error("SSH command timed out after %s seconds", timeout)
        raise


def _run_telnet(
    host: str,
    username: str,
    password: str,
    command: str,
    port: int = 23,
    timeout: int = 30,
) -> Tuple[int, str, str]:
    """Execute a command via Telnet.

    Args:
        host: IP address or hostname of the device.
        username: Telnet user name.
        password: Telnet password.
        command: Command to send after login.
        port: Telnet TCP port (default 23).
        timeout: Seconds to wait for the command to finish.

    Returns:
        A tuple of (return_code, stdout, stderr). ``return_code`` is 0 on
        success, 1 on failure.
    """
    tn = telnetlib.Telnet()
    try:
        LOGGER.debug("Connecting to %s:%s via Telnet", host, port)
        tn.open(host, port, timeout=timeout)
        tn.read_until(b"login:", timeout=timeout)
        tn.write(username.encode("ascii") + b"\n")
        tn.read_until(b"Password:", timeout=timeout)
        tn.write(password.encode("ascii") + b"\n")
        # Wait for the prompt (simple heuristic: a > or # character)
        idx, _, _ = tn.expect([b">", b"#"], timeout=timeout)
        if idx == -1:
            raise ConnectionError("Unable to detect device prompt after login")

        LOGGER.debug("Sending command: %s", command)
        tn.write(command.encode("ascii") + b"\n")
        # Read until next prompt or timeout.
        output = tn.read_until(b">", timeout=timeout).decode("utf-8", errors="ignore")
        tn.write(b"exit\n")
        tn.close()
        return 0, output, ""
    except Exception as exc:
        LOGGER.error("Telnet session failed: %s", exc)
        return 1, "", str(exc)


def _parse_output(command: str, output: str) -> str:
    """Very small parser that extracts useful information from common commands.

    Currently supports:
    * ``show version`` – returns the OS version line.
    * ``show interfaces status`` – lists interfaces that are down.

    Args:
        command: The command that generated the output.
        output: Raw command output.

    Returns:
        A human‑readable summary string.
    """
    cmd = command.strip().lower()
    lines = output.splitlines()
    if "show version" in cmd:
        for line in lines:
            if "version" in line.lower():
                return line.strip()
        return "Version information not found."
    if "show interfaces status" in cmd:
        down_intfs = [ln.split()[0] for ln in lines if "disabled" in ln.lower() or "down" in ln.lower()]
        if down_intfs:
            return f"Interfaces down: {', '.join(down_intfs)}"
        return "All interfaces are up."
    return "No parser available for the given command."


def execute_device_command(
    host: str,
    username: str,
    password: Optional[str],
    command: str,
    protocol: str = "ssh",
    port: Optional[int] = None,
    timeout: int = 30,
) -> None:
    """High‑level wrapper that selects the proper transport, runs the command,
    parses the output, and logs the result.

    Args:
        host: Device IP or hostname.
        username: Login name.
        password: Password (required for Telnet, optional for SSH).
        command: Command to execute.
        protocol: ``ssh`` or ``telnet``.
        port: Explicit port number; defaults to 22 for SSH, 23 for Telnet.
        timeout: Command timeout in seconds.
    """
    LOGGER.info("Connecting to %s via %s", host, protocol.upper())
    if protocol == "ssh":
        ssh_port = port if port else 22
        try:
            rc, out, err = _run_ssh(host, username, command, ssh_port, password, timeout)
        except Exception as exc:
            LOGGER.exception("SSH execution failed")
            sys.exit(1)
    elif protocol == "telnet":
        if password is None:
            LOGGER.error("Password is required for Telnet authentication")
            sys.exit(1)
        telnet_port = port if port else 23
        rc, out, err = _run_telnet(host, username, password, command, telnet_port, timeout)
    else:
        LOGGER.error("Unsupported protocol: %s", protocol)
        sys.exit(1)

    if rc != 0:
        LOGGER.error("Command failed with return code %s", rc)
        if err:
            LOGGER.error("Error output: %s", err.strip())
        sys.exit(rc)

    LOGGER.debug("Raw command output:\n%s", out)
    summary = _parse_output(command, out)
    LOGGER.info("Command summary: %s", summary)


def _positive_int(value: str) -> int:
    """Argparse helper to ensure a positive integer."""
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"Invalid positive int value: '{value}'")
    return ivalue


def build_arg_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Execute a single command on a network device via SSH or Telnet."
    )
    parser.add_argument(
        "--device",
        required=True,
        help="IP address or hostname of the target device.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Login username for the device.",
    )
    parser.add_argument(
        "--password",
        help="Login password (required for Telnet, optional for SSH).",
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Command string to execute on the remote device.",
    )
    parser.add_argument(
        "--protocol",
        choices=["ssh", "telnet"],
        default="ssh",
        help="Transport protocol (default: ssh).",
    )
    parser.add_argument(
        "--port",
        type=_positive_int,
        help="Custom port number for the chosen protocol.",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_int,
        default=30,
        help="Command timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="info",
        help="Logging verbosity (default: info).",
    )
    return parser


def configure_logging(level_name: str) -> None:
    """Adjust logger levels based on CLI argument."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    LOGGER.setLevel(level)
    _console_handler.setLevel(level if level > logging.DEBUG else logging.DEBUG)


def main() -> None:
    """Entry point for the CLI."""
    parser = build_arg_parser()
    args = parser.parse_args()

    configure_logging(args.log_level)

    try:
        execute_device_command(
            host=args.device,
            username=args.username,
            password=args.password,
            command=args.command,
            protocol=args.protocol,
            port=args.port,
            timeout=args.timeout,
        )
    except KeyboardInterrupt:
        LOGGER.warning("Execution interrupted by user")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()