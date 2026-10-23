"""incident_tracker_cli.py

A lightweight incident tracking utility for network devices using only the
Python standard library. The script connects to a device via Telnet,
executes a log‑retrieval command (default ``show logging``), parses the
output for common incident keywords and records any findings in a JSON
report.

Usage:
    python incident_tracker_cli.py --host 10.0.0.1 \\
        --username admin [--password secret] [--command "show log"] \\
        [--output incidents.json] [--dry-run]

Prerequisites:
    * Telnet access must be enabled on the target device.
    * The device must present a standard “login:” and “Password:” prompt.
    * The executing environment must have network connectivity to the
      device's Telnet port (23 by default).

The script is intentionally self‑contained and avoids third‑party
dependencies to showcase pure‑Python network automation skills.
"""

import argparse
import datetime
import getpass
import json
import logging
import re
import socket
import sys
import telnetlib
from pathlib import Path
from typing import List, Dict, Any

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)

CONSOLE_HANDLER = logging.StreamHandler()
CONSOLE_HANDLER.setLevel(logging.INFO)
CONSOLE_FORMAT = logging.Formatter("%(levelname)s: %(message)s")
CONSOLE_HANDLER.setFormatter(CONSOLE_FORMAT)
LOGGER.addHandler(CONSOLE_HANDLER)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
DEFAULT_PORT = 23
DEFAULT_COMMAND = "show logging"
DEFAULT_OUTPUT = "incidents.json"
INCIDENT_PATTERNS = [
    re.compile(r"\bERROR\b", re.IGNORECASE),
    re.compile(r"\bCRITICAL\b", re.IGNORECASE),
    re.compile(r"\bFAIL(?:ED)?\b", re.IGNORECASE),
    re.compile(r"\bDOWN\b", re.IGNORECASE),
]


# --------------------------------------------------------------------------- #
# Core functionality
# --------------------------------------------------------------------------- #
def telnet_connect(
    host: str,
    username: str,
    password: str,
    port: int = DEFAULT_PORT,
    timeout: int = 10,
) -> telnetlib.Telnet:
    """Establish a Telnet connection to a network device.

    Args:
        host: IP address or hostname of the device.
        username: Login name.
        password: Login password.
        port: Telnet port, defaults to 23.
        timeout: Connection timeout in seconds.

    Returns:
        An active :class:`telnetlib.Telnet` session.

    Raises:
        ConnectionError: If the TCP connection cannot be established.
        TimeoutError: If the login prompt is not received in time.
        ValueError: If authentication fails.
    """
    try:
        LOGGER.debug("Connecting to %s:%s", host, port)
        tn = telnetlib.Telnet(host, port, timeout)
    except socket.error as exc:
        raise ConnectionError(f"Unable to connect to {host}:{port}") from exc

    try:
        tn.read_until(b"login:", timeout)
        tn.write(username.encode("ascii") + b"\n")
        tn.read_until(b"Password:", timeout)
        tn.write(password.encode("ascii") + b"\n")
    except EOFError as exc:
        tn.close()
        raise ValueError("Unexpected EOF during login") from exc
    except socket.timeout as exc:
        tn.close()
        raise TimeoutError("Login prompt timed out") from exc

    # Some devices echo "Login incorrect" on failure – check for that.
    output = tn.read_very_eager().decode(errors="ignore")
    if "incorrect" in output.lower():
        tn.close()
        raise ValueError("Authentication failed")
    LOGGER.debug("Login successful")
    return tn


def execute_command(tn: telnetlib.Telnet, command: str, timeout: int = 5) -> str:
    """Run a command on the Telnet session and return its output.

    Args:
        tn: An active Telnet session.
        command: Command string to execute.
        timeout: Seconds to wait for command output.

    Returns:
        The command output as a string.

    Raises:
        RuntimeError: If the command does not produce output.
    """
    tn.write(command.encode("ascii") + b"\n")
    tn.write(b"exit\n")  # Ensure session terminates after command.
    raw = tn.read_all()
    output = raw.decode(errors="ignore")
    if not output.strip():
        raise RuntimeError(f"No output received for command: {command}")
    LOGGER.debug("Command output captured (%d bytes)", len(output))
    return output


def parse_incidents(log_output: str) -> List[Dict[str, Any]]:
    """Extract incident lines from device log output.

    Args:
        log_output: Multiline string containing device logs.

    Returns:
        A list of dictionaries, each with ``timestamp`` and ``message`` keys.
    """
    incidents = []
    for line in log_output.splitlines():
        if any(p.search(line) for p in INCIDENT_PATTERNS):
            # Attempt to extract a timestamp from the start of the line.
            timestamp_match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            timestamp = (
                timestamp_match.group(1)
                if timestamp_match
                else datetime.datetime.utcnow().isoformat()
            )
            incidents.append({"timestamp": timestamp, "message": line.strip()})
    LOGGER.info("Found %d incident(s) in log output", len(incidents))
    return incidents


def save_incidents(
    incidents: List[Dict[str, Any]], output_path: Path, dry_run: bool = False
) -> None:
    """Persist incidents to a JSON file.

    Args:
        incidents: List of incident dictionaries.
        output_path: Destination file path.
        dry_run: If True, only log what would be written.
    """
    if not incidents:
        LOGGER.info("No incidents to save.")
        return

    payload = {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "incident_count": len(incidents),
        "incidents": incidents,
    }

    if dry_run:
        LOGGER.info("[DRY RUN] Would write %d incidents to %s", len(incidents), output_path)
        LOGGER.debug(json.dumps(payload, indent=2))
        return

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        LOGGER.info("Saved %d incident(s) to %s", len(incidents), output_path)
    except OSError as exc:
        LOGGER.error("Failed to write incidents: %s", exc)
        raise


def run_tracker(
    host: str,
    username: str,
    password: str,
    command: str = DEFAULT_COMMAND,
    output: str = DEFAULT_OUTPUT,
    dry_run: bool = False,
) -> None:
    """Main orchestration function.

    Handles connection, command execution, incident parsing and persistence.
    """
    tn = None
    try:
        tn = telnet_connect(host, username, password)
        raw_output = execute_command(tn, command)
        incidents = parse_incidents(raw_output)
        save_incidents(incidents, Path(output), dry_run)
    except Exception as exc:
        LOGGER.error("Tracker failed: %s", exc)
        sys.exit(1)
    finally:
        if tn:
            tn.close()


# --------------------------------------------------------------------------- #
# CLI handling
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Network device incident tracker via Telnet."
    )
    parser.add_argument(
        "--host",
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
        help="Login password. If omitted, the script will prompt securely.",
    )
    parser.add_argument(
        "--command",
        default=DEFAULT_COMMAND,
        help=f"Command to retrieve logs (default: '{DEFAULT_COMMAND}').",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"JSON file to store incidents (default: '{DEFAULT_OUTPUT}').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without writing output files; useful for testing.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging verbosity.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Configure log level based on the argument.
    CONSOLE_HANDLER.setLevel(getattr(logging, args.log_level.upper()))
    LOGGER.debug("Parsed arguments: %s", args)

    password = args.password or getpass.getpass(prompt="Device password: ")

    run_tracker(
        host=args.host,
        username=args.username,
        password=password,
        command=args.command,
        output=args.output,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()