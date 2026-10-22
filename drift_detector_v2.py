"""drift_detector.py
------------------

Detect configuration drift on network devices by retrieving the current
running configuration and comparing it against a known good baseline.

Usage
=====
    python drift_detector.py \\
        --host 10.0.0.1 \\
        --username admin \\
        --password secret \\
        --baseline baseline.cfg \\
        [--port 23] [--command "show running-config"] [--method telnet|ssh]

Prerequisites
-------------
* Python 3.8+
* Access to the target device via Telnet (default) or SSH.
* The device must present a clear ``Username:`` / ``Password:`` prompt for
  Telnet or accept password authentication for SSH.
* ``baseline.cfg`` should contain the expected configuration for the device.

The script logs detailed information to the console and exits with a non‑zero
status code when drift is detected.
"""

import argparse
import difflib
import getpass
import logging
import os
import re
import socket
import subprocess
import sys
import telnetlib
from typing import List, Tuple

__author__ = "Network Engineer"
__email__ = "engineer@example.com"
__version__ = "1.0.0"

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
logger = logging.getLogger("drift_detector")
handler = logging.StreamHandler()
formatter = logging.Formatter(
    fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #
def _telnet_login(
    tn: telnetlib.Telnet,
    username: str,
    password: str,
    login_regex: bytes = b"(Username|login):",
    password_regex: bytes = b"Password:",
) -> None:
    """Perform a simple Telnet login sequence."""
    logger.debug("Waiting for login prompt")
    tn.read_until(login_regex, timeout=10)
    tn.write(username.encode("ascii") + b"\n")
    logger.debug("Sent username")
    tn.read_until(password_regex, timeout=10)
    tn.write(password.encode("ascii") + b"\n")
    logger.debug("Sent password")
    # Some devices echo a banner before the command prompt; pause briefly
    tn.read_until(b">", timeout=5)


def _execute_telnet_command(
    host: str,
    port: int,
    username: str,
    password: str,
    command: str,
) -> str:
    """Connect via Telnet, execute a command and return the raw output."""
    logger.info("Connecting to %s:%s via Telnet", host, port)
    try:
        tn = telnetlib.Telnet(host, port, timeout=15)
    except socket.error as exc:
        logger.error("Telnet connection failed: %s", exc)
        raise ConnectionError(f"Unable to telnet to {host}:{port}") from exc

    try:
        _telnet_login(tn, username, password)
        logger.debug("Login successful")
        tn.write(command.encode("ascii") + b"\n")
        logger.debug("Command sent: %s", command)

        # Capture until we see the command prompt again (assume '>' or '#')
        output = tn.read_until(b">", timeout=10)
        # Drain any remaining data
        output += tn.read_very_eager()
        tn.write(b"exit\n")
        tn.close()
    except Exception as exc:
        logger.exception("Error during Telnet session")
        raise RuntimeError("Telnet command execution failed") from exc

    # Decode and clean the output
    decoded = output.decode("utf-8", errors="ignore")
    cleaned = _strip_cli_echo(decoded, command)
    logger.debug("Raw command output captured")
    return cleaned


def _execute_ssh_command(
    host: str,
    port: int,
    username: str,
    password: str,
    command: str,
) -> str:
    """Execute a command over SSH using the local ``ssh`` client."""
    logger.info("Connecting to %s:%s via SSH", host, port)
    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-p",
        str(port),
        f"{username}@{host}",
        command,
    ]

    try:
        result = subprocess.run(
            ssh_cmd,
            input=password.encode(),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("SSH command failed: %s", exc)
        raise ConnectionError(f"Unable to SSH to {host}:{port}") from exc

    if result.returncode != 0:
        logger.error(
            "SSH returned non‑zero exit status %s: %s",
            result.returncode,
            result.stderr.strip(),
        )
        raise RuntimeError(f"SSH command error: {result.stderr.strip()}")

    logger.debug("SSH command output captured")
    return result.stdout


def _strip_cli_echo(output: str, command: str) -> str:
    """
    Remove the echoed command line and device prompt from raw CLI output.
    This varies per vendor; the implementation covers common patterns.
    """
    # Remove anything before the first occurrence of the command itself
    pattern = re.escape(command)
    parts = re.split(pattern, output, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) > 1:
        output = parts[1]

    # Remove typical prompt characters at the end of lines
    cleaned = "\n".join(line.rstrip(">#") for line in output.splitlines())
    return cleaned.strip()


def fetch_device_config(
    host: str,
    username: str,
    password: str,
    command: str,
    method: str = "telnet",
    port: int = None,
) -> str:
    """Retrieve the device configuration using the selected transport."""
    if method == "telnet":
        port = port or 23
        return _execute_telnet_command(host, port, username, password, command)
    elif method == "ssh":
        port = port or 22
        return _execute_ssh_command(host, port, username, password, command)
    else:
        raise ValueError(f"Unsupported method '{method}'. Use 'telnet' or 'ssh'.")


def load_baseline(path: str) -> str:
    """Read the baseline configuration from a file."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Baseline file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = f.read()
    logger.info("Baseline configuration loaded (%d bytes)", len(data))
    return data.strip()


def compare_configurations(
    baseline: str, current: str
) -> Tuple[bool, List[str]]:
    """
    Compare two configuration strings.

    Returns
    -------
    (is_equal, diff_lines)
        is_equal : bool – True if configurations match exactly.
        diff_lines : list – Unified diff lines (empty when equal).
    """
    baseline_lines = baseline.splitlines(keepends=True)
    current_lines = current.splitlines(keepends=True)

    diff = list(
        difflib.unified_diff(
            baseline_lines,
            current_lines,
            fromfile="baseline",
            tofile="current",
            lineterm="",
        )
    )
    is_equal = len(diff) == 0
    return is_equal, diff


def print_diff(diff: List[str]) -> None:
    """Pretty‑print a unified diff to stdout."""
    for line in diff:
        sys.stdout.write(line + "\n")


def parse_arguments() -> argparse.Namespace:
    """Configure CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Network configuration drift detector",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="Target device IP or hostname")
    parser.add_argument(
        "--port",
        type=int,
        help="TCP port (defaults: 23 for telnet, 22 for ssh)",
    )
    parser.add_argument("-u", "--username", required=True, help="Login username")
    parser.add_argument(
        "-p",
        "--password",
        help="Login password (will prompt if omitted)",
    )
    parser.add_argument(
        "-c",
        "--command",
        default="show running-config",
        help="Command to retrieve the current configuration",
    )
    parser.add_argument(
        "-b",
        "--baseline",
        required=True,
        help="Path to baseline configuration file",
    )
    parser.add_argument(
        "-m",
        "--method",
        choices=["telnet", "ssh"],
        default="telnet",
        help="Transport method",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Main execution flow
# --------------------------------------------------------------------------- #
def main() -> int:
    args = parse_arguments()
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    password = args.password or getpass.getpass(prompt="Password: ")

    try:
        baseline_cfg = load_baseline(args.baseline)
        current_cfg = fetch_device_config(
            host=args.host,
            username=args.username,
            password=password,
            command=args.command,
            method=args.method,
            port=args.port,
        )
    except Exception as exc:
        logger.error("Failed to retrieve configurations: %s", exc)
        return 2

    equal, diff = compare_configurations(baseline_cfg, current_cfg)

    if equal:
        logger.info("No drift detected – configuration matches baseline.")
        return 0
    else:
        logger.warning("Configuration drift detected!")
        print_diff(diff)
        return 1


if __name__ == "__main__":
    sys.exit(main())