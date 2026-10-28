"""telnet_intent_engine.py

A lightweight intent engine that connects to network devices via Telnet, authenticates,
executes a list of operational commands, and returns their output.  The script is
intended for demonstration and portfolio purposes and showcases:

* Use of only the Python standard library for device interaction.
* Structured error handling and detailed logging.
* A clean CLI built with :pymod:`argparse`.
* Re‑usable functions that can be imported by other automation tools.

Prerequisites
-------------
* The target device must have Telnet enabled and allow the supplied credentials.
* For Cisco‑like devices, the optional ``--enable`` password can be used to enter
  privileged EXEC mode.

Example
-------
```
python telnet_intent_engine.py \\
    --host 10.0.0.1 \\
    --username admin \\
    --password secret \\
    --enable enablepwd \\
    --commands "show version,show ip interface brief"
```
"""

import argparse
import logging
import socket
import sys
import telnetlib
import time
from typing import List, Dict, Optional

# ----------------------------------------------------------------------
# Logging configuration
# ----------------------------------------------------------------------
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
HANDLER = logging.StreamHandler(sys.stdout)
FORMATTER = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s - %(message)s", "%Y-%m-%d %H:%M:%S"
)
HANDLER.setFormatter(FORMATTER)
LOGGER.addHandler(HANDLER)


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
class TelnetConnectionError(Exception):
    """Raised when a Telnet connection cannot be established."""


class TelnetAuthenticationError(Exception):
    """Raised when login or enable authentication fails."""


# ----------------------------------------------------------------------
# Core functionality
# ----------------------------------------------------------------------
def _read_until(tn: telnetlib.Telnet, expected: bytes, timeout: int = 10) -> bytes:
    """Wrapper around :pymeth:`Telnet.read_until` that logs timeout events."""
    try:
        data = tn.read_until(expected, timeout)
        if not data.endswith(expected):
            LOGGER.debug("Expected prompt %s not received within %s seconds.", expected, timeout)
        return data
    except EOFError as exc:
        raise TelnetConnectionError("Connection closed by remote host.") from exc


def _send_line(tn: telnetlib.Telnet, line: str) -> None:
    """Send a line followed by CRLF."""
    LOGGER.debug("Sending: %s", line)
    tn.write(line.encode("ascii") + b"\r\n")
    time.sleep(0.2)  # small pause to give the device time to process


def _enter_enable(tn: telnetlib.Telnet, enable_pwd: str, timeout: int = 10) -> None:
    """Enter privileged EXEC mode using the provided enable password."""
    _send_line(tn, "enable")
    resp = _read_until(tn, b"Password:", timeout)
    if b"Password:" not in resp:
        raise TelnetAuthenticationError("Enable prompt not received.")
    _send_line(tn, enable_pwd)
    # Verify we are now in privileged mode (prompt ends with '#')
    prompt = _read_until(tn, b"#", timeout)
    if not prompt.strip().endswith(b"#"):
        raise TelnetAuthenticationError("Failed to enter enable mode.")


def _execute_command(tn: telnetlib.Telnet, command: str, timeout: int = 10) -> str:
    """Send a single command and capture its output."""
    _send_line(tn, command)
    # Capture until we see a prompt again (either '>' or '#')
    output = tn.read_until(b"\n", timeout)  # consume the echo line
    output += tn.read_until(b"#", timeout) if b"#" in output else tn.read_until(b">", timeout)
    return output.decode("utf-8", errors="ignore")


def run_telnet_commands(
    host: str,
    username: str,
    password: str,
    commands: List[str],
    enable_password: Optional[str] = None,
    port: int = 23,
    timeout: int = 10,
) -> Dict[str, str]:
    """
    Connect to a network device via Telnet, optionally enter enable mode,
    run a list of commands and return a mapping of command -> output.

    Args:
        host: IP address or hostname of the device.
        username: Login username.
        password: Login password.
        commands: List of commands to execute.
        enable_password: Optional enable (privileged) password.
        port: Telnet TCP port (default 23).
        timeout: Socket timeout in seconds.

    Returns:
        A dictionary where keys are the commands and values are the raw CLI output.

    Raises:
        TelnetConnectionError: If the socket cannot be opened.
        TelnetAuthenticationError: If login or enable fails.
    """
    LOGGER.info("Connecting to %s:%s via Telnet", host, port)

    try:
        tn = telnetlib.Telnet(host, port, timeout)
    except (socket.timeout, ConnectionRefusedError) as exc:
        raise TelnetConnectionError(f"Unable to connect to {host}:{port}") from exc

    # Login sequence
    _read_until(tn, b"Username:", timeout)
    _send_line(tn, username)
    _read_until(tn, b"Password:", timeout)
    _send_line(tn, password)

    # Check for successful login prompt
    login_prompt = tn.read_until(b">", timeout)
    if b">" not in login_prompt and b"#" not in login_prompt:
        tn.close()
        raise TelnetAuthenticationError("Login failed – prompt not received.")

    LOGGER.info("Login successful on %s", host)

    # If enable mode requested
    if enable_password:
        LOGGER.info("Attempting to enter enable mode")
        _enter_enable(tn, enable_password, timeout)
        LOGGER.info("Enable mode entered")

    results: Dict[str, str] = {}
    for cmd in commands:
        cmd = cmd.strip()
        if not cmd:
            continue
        LOGGER.info("Executing command: %s", cmd)
        try:
            output = _execute_command(tn, cmd, timeout)
            results[cmd] = output
            LOGGER.debug("Output for %s: %s", cmd, output[:200])
        except Exception as exc:  # broad to capture read_until timeouts etc.
            LOGGER.error("Error executing command %s: %s", cmd, exc)
            results[cmd] = f"Error: {exc}"

    tn.write(b"exit\n")
    tn.close()
    LOGGER.info("Connection to %s closed", host)
    return results


# ----------------------------------------------------------------------
# CLI handling
# ----------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute a set of commands on a network device via Telnet."
    )
    parser.add_argument(
        "--host", required=True, help="Device IP address or hostname."
    )
    parser.add_argument(
        "--username", required=True, help="Login username."
    )
    parser.add_argument(
        "--password", required=True, help="Login password."
    )
    parser.add_argument(
        "--enable",
        dest="enable_password",
        help="Enable (privileged) password, if required.",
    )
    parser.add_argument(
        "--commands",
        required=True,
        help=(
            "Comma‑separated list of commands to run, e.g. "
            "\"show version,show ip interface brief\""
        ),
    )
    parser.add_argument(
        "--port", type=int, default=23, help="Telnet port (default: 23)."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Network timeout in seconds (default: 10).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging verbosity.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    LOGGER.setLevel(getattr(logging, args.log_level.upper(), logging.INFO))

    command_list = [c.strip() for c in args.commands.split(",") if c.strip()]

    try:
        results = run_telnet_commands(
            host=args.host,
            username=args.username,
            password=args.password,
            commands=command_list,
            enable_password=args.enable_password,
            port=args.port,
            timeout=args.timeout,
        )
    except (TelnetConnectionError, TelnetAuthenticationError) as exc:
        LOGGER.error("%s", exc)
        sys.exit(1)
    except Exception as exc:  # unexpected errors
        LOGGER.exception("Unexpected error: %s", exc)
        sys.exit(1)

    # Pretty‑print results
    for cmd, output in results.items():
        print(f"\n=== {cmd} ===")
        print(output.rstrip())


if __name__ == "__main__":
    main()