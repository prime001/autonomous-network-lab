#!/usr/bin/env python3
"""
config_validator_stdlib.py

A lightweight configuration validator that connects to network devices using
the standard library's :mod:`telnetlib` module. The script retrieves the
running configuration from a device, compares it with an expected baseline
configuration file and reports any differences.

Usage
-----

    python config_validator_stdlib.py --device 10.0.0.1 \\
        --username admin --password secret \\
        --config-file baseline.cfg [--port 23] [--timeout 10]

Prerequisites
-------------
* The target device must support Telnet and provide a standard login prompt.
* Python 3.6+ (for f-strings).
* No third‑party packages are required; the script relies solely on the
  standard library.

The script is intended for demonstration and educational purposes. For
production use consider using SSH with libraries like Paramiko or Netmiko.
"""

import argparse
import difflib
import getpass
import logging
import sys
import telnetlib
from typing import List, Tuple

DEFAULT_PORT = 23
DEFAULT_TIMEOUT = 10
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


def setup_logging(verbose: bool) -> None:
    """Configure the root logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)


def parse_args() -> argparse.Namespace:
    """Parse command‑line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate device configuration against a baseline file."
    )
    parser.add_argument(
        "--device",
        required=True,
        help="Hostname or IP address of the network device.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Login username for the device.",
    )
    parser.add_argument(
        "--password",
        help="Login password. If omitted, you will be prompted securely.",
    )
    parser.add_argument(
        "--config-file",
        required=True,
        help="Path to the baseline configuration file.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Telnet port (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Connection timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug output.",
    )
    return parser.parse_args()


def read_baseline(file_path: str) -> List[str]:
    """Read the baseline configuration file and return a list of stripped lines."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f]
        logging.debug("Baseline file '%s' loaded (%d lines).", file_path, len(lines))
        return lines
    except OSError as exc:
        logging.error("Failed to read baseline file '%s': %s", file_path, exc)
        sys.exit(1)


def telnet_login(
    tn: telnetlib.Telnet,
    username: str,
    password: str,
    timeout: int,
) -> None:
    """Perform a simple login sequence over Telnet."""
    try:
        tn.read_until(b"Username:", timeout)
        tn.write(username.encode("ascii") + b"\n")
        tn.read_until(b"Password:", timeout)
        tn.write(password.encode("ascii") + b"\n")
        # Some devices display a prompt after login; consume it.
        tn.read_until(b">", timeout)
        logging.debug("Login successful.")
    except EOFError as exc:
        logging.error("Unexpected EOF during login: %s", exc)
        raise
    except Exception as exc:
        logging.error("Login failed: %s", exc)
        raise


def fetch_running_config(
    host: str,
    username: str,
    password: str,
    port: int,
    timeout: int,
) -> List[str]:
    """
    Connect to the device via Telnet, issue the command to display the running
    configuration, and return the output as a list of lines.
    """
    logging.info("Connecting to %s:%d via Telnet.", host, port)
    try:
        tn = telnetlib.Telnet(host, port, timeout)
        telnet_login(tn, username, password, timeout)

        # Enter enable mode if needed (common on Cisco IOS)
        tn.write(b"enable\n")
        tn.read_until(b"Password:", timeout)
        tn.write(password.encode("ascii") + b"\n")
        tn.read_until(b"#", timeout)

        # Issue the show command
        tn.write(b"terminal length 0\n")  # Disable pagination
        tn.read_until(b"#", timeout)
        tn.write(b"show running-config\n")
        output = tn.read_until(b"#", timeout)

        tn.write(b"exit\n")
        tn.close()

        # Decode and split into lines, removing the last prompt line
        decoded = output.decode("utf-8", errors="ignore")
        lines = decoded.splitlines()
        # Remove possible command echo and trailing prompt
        config_lines = [
            line for line in lines if not line.strip().startswith("show running-config")
        ]
        # Trim the final prompt line if present
        if config_lines and config_lines[-1].strip().endswith("#"):
            config_lines = config_lines[:-1]

        logging.debug(
            "Retrieved running config (%d lines).", len(config_lines)
        )
        return config_lines
    except Exception as exc:
        logging.error("Failed to retrieve configuration from %s: %s", host, exc)
        sys.exit(1)


def compare_configs(
    running: List[str], baseline: List[str]
) -> Tuple[bool, str]:
    """
    Compare two configurations and return a tuple:
    (is_match, unified_diff_string)
    """
    diff = list(
        difflib.unified_diff(
            baseline,
            running,
            fromfile="baseline",
            tofile="running",
            lineterm="",
        )
    )
    match = len(diff) == 0
    diff_text = "\n".join(diff)
    return match, diff_text


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    password = args.password or getpass.getpass(prompt="Device password: ")

    baseline_cfg = read_baseline(args.config_file)
    running_cfg = fetch_running_config(
        host=args.device,
        username=args.username,
        password=password,
        port=args.port,
        timeout=args.timeout,
    )

    match, diff_text = compare_configs(running_cfg, baseline_cfg)

    if match:
        logging.info("Configuration matches the baseline.")
    else:
        logging.warning("Configuration drift detected!")
        print(diff_text)


if __name__ == "__main__":
    main()