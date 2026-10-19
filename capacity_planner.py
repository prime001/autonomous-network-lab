"""capacity_planner.py

A lightweight capacity‑planning utility for network devices.

The script connects to a network device over SSH, runs a user‑provided
command (default ``show interfaces``), parses the output for interface
statistics and calculates aggregate bandwidth usage.  The results are
displayed as a human readable summary and can be exported as JSON.

Usage
-----
    python capacity_planner.py \\
        --host 10.0.0.1 \\
        --username admin \\
        --password secret \\
        [--command "show interfaces"] \\
        [--output json|text] \\
        [--log-level INFO]

Prerequisites
-------------
* Python 3.8+
* ``paramiko`` library (install with ``pip install paramiko``)

The script relies only on the standard library and ``paramiko`` for SSH
communication.  It is intended for demonstration and small‑scale planning;
for large environments consider a full‑featured automation framework.
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from typing import Dict, List, Tuple

import paramiko

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Configure root logger."""
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --------------------------------------------------------------------------- #
# SSH interaction
# --------------------------------------------------------------------------- #
def get_ssh_output(
    host: str,
    username: str,
    password: str,
    command: str,
    timeout: int = 30,
) -> str:
    """
    Execute a command on a remote device via SSH and return the raw output.

    Args:
        host: IP or hostname of the device.
        username: SSH username.
        password: SSH password.
        command: Command string to execute.
        timeout: Connection timeout in seconds.

    Raises:
        paramiko.SSHException: For SSH‑related errors.
        TimeoutError: If the command execution exceeds the timeout.
    """
    LOGGER.debug("Establishing SSH connection to %s", host)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
        )
        LOGGER.info("SSH connection established to %s", host)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            err = stderr.read().decode()
            raise RuntimeError(f"Command failed with exit status {exit_status}: {err}")

        output = stdout.read().decode()
        LOGGER.debug("Command output received (%d bytes)", len(output))
        return output
    finally:
        client.close()
        LOGGER.info("SSH connection closed")


# --------------------------------------------------------------------------- #
# Parsing utilities
# --------------------------------------------------------------------------- #
INTERFACE_REGEX = re.compile(
    r"^(?P<name>\S+)\s+is\s+(?P<status>up|down),.*"
    r"line protocol is\s+(?P<protocol>up|down)", re.MULTILINE
)

BANDWIDTH_REGEX = re.compile(
    r"BW\s+(?P<bandwidth>\d+)\s+Kbit", re.IGNORECASE
)

UTIL_REGEX = re.compile(
    r"(\d+)%\s+util", re.IGNORECASE
)


def parse_interface_output(output: str) -> List[Dict[str, str]]:
    """
    Extract interface details from raw command output.

    The parser is intentionally simple and works with common IOS‑like
    output.  It extracts the interface name, operational status, line
    protocol status and configured bandwidth.

    Returns:
        List of dictionaries with keys: name, status, protocol, bandwidth_kbps.
    """
    interfaces = []
    blocks = output.split("\n\n")
    for block in blocks:
        header_match = INTERFACE_REGEX.search(block)
        if not header_match:
            continue
        name = header_match.group("name")
        status = header_match.group("status")
        protocol = header_match.group("protocol")

        bw_match = BANDWIDTH_REGEX.search(block)
        bandwidth = int(bw_match.group("bandwidth")) if bw_match else 0

        interfaces.append(
            {
                "name": name,
                "status": status,
                "protocol": protocol,
                "bandwidth_kbps": bandwidth,
            }
        )
    LOGGER.info("Parsed %d interfaces", len(interfaces))
    return interfaces


def extract_utilization(output: str) -> Dict[str, int]:
    """
    Find utilization percentages for each interface.

    Returns:
        Mapping of interface name to utilization percentage.
    """
    util = {}
    for line in output.splitlines():
        match = INTERFACE_REGEX.search(line)
        if not match:
            continue
        name = match.group("name")
        util_match = UTIL_REGEX.search(line)
        if util_match:
            util[name] = int(util_match.group(1))
    LOGGER.debug("Utilization extracted for %d interfaces", len(util))
    return util


# --------------------------------------------------------------------------- #
# Capacity calculation
# --------------------------------------------------------------------------- #
def calculate_capacity(
    interfaces: List[Dict[str, str]],
    utilization: Dict[str, int],
) -> Dict[str, int]:
    """
    Compute aggregate bandwidth metrics.

    Returns a dictionary containing:
        total_interfaces, up_interfaces, total_bandwidth_kbps,
        used_bandwidth_kbps, utilization_percent.
    """
    total_bw = 0
    used_bw = 0
    up_count = 0

    for iface in interfaces:
        bw = iface["bandwidth_kbps"]
        total_bw += bw
        if iface["status"] == "up" and iface["protocol"] == "up":
            up_count += 1
            util = utilization.get(iface["name"], 0)
            used_bw += bw * util // 100

    utilization_percent = (
        round((used_bw / total_bw) * 100, 2) if total_bw else 0
    )

    result = {
        "total_interfaces": len(interfaces),
        "up_interfaces": up_count,
        "total_bandwidth_kbps": total_bw,
        "used_bandwidth_kbps": used_bw,
        "overall_utilization_percent": utilization_percent,
    }
    LOGGER.info("Capacity calculation completed")
    return result


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def print_text_summary(summary: Dict[str, int]) -> None:
    """Print a human‑readable summary."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"Capacity Planning Summary – {now}")
    print("-" * 50)
    for key, value in summary.items():
        print(f"{key.replace('_', ' ').title():30}: {value}")


def print_json(summary: Dict[int, int]) -> None:
    """Print the summary as JSON."""
    print(json.dumps(summary, indent=4))


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Network device capacity planner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="Device IP or hostname")
    parser.add_argument("--username", required=True, help="SSH username")
    parser.add_argument(
        "--password", required=True, help="SSH password (use keyring for safety)"
    )
    parser.add_argument(
        "--command",
        default="show interfaces",
        help="Command to retrieve interface statistics",
    )
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Result formatting",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity",
    )

    args = parser.parse_args()
    configure_logging(args.log_level)

    try:
        raw_output = get_ssh_output(
            host=args.host,
            username=args.username,
            password=args.password,
            command=args.command,
        )
    except Exception as exc:  # pragma: no cover
        LOGGER.error("Failed to retrieve data: %s", exc)
        sys.exit(1)

    interfaces = parse_interface_output(raw_output)
    utilization = extract_utilization(raw_output)
    summary = calculate_capacity(interfaces, utilization)

    if args.output == "json":
        print_json(summary)
    else:
        print_text_summary(summary)


if __name__ == "__main__":
    main()