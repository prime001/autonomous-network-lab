#!/usr/bin/env python3
"""
device_audit.py - Simple network device health and config audit tool

Purpose
-------
The script connects to a network device over SSH (using Netmiko) and executes a
set of diagnostic commands. The results are collected, optionally parsed, and
output in either human‑readable text or JSON format. It is intended as a
light‑weight health‑monitoring utility that can be integrated into larger
automation pipelines or run manually from the command line.

Usage
-----
    python device_audit.py \\
        --device 10.0.0.1 \\
        --username admin \\
        --password secret123 \\
        [--port 22] \\
        [--command "show version" --command "show ip interface brief"] \\
        [--output json] \\
        [--timeout 30]

If ``--command`` is omitted a default list of common health commands is used.

Prerequisites
-------------
* Python 3.8+
* Netmiko (`pip install netmiko`) – provides the SSH abstraction.
* Optional: ``pyyaml`` if you want to extend the script to read device inventories.

The script relies only on the Python standard library and Netmiko for device
communication, keeping the dependency footprint small for a portfolio project.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from typing import List, Dict, Any

from netmiko import ConnectHandler, NetMikoAuthenticationException, NetMikoTimeoutException

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_COMMANDS = [
    "show version",
    "show system resources",
    "show ip interface brief",
    "show logging",
]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOGGER = logging.getLogger("device_audit")


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #

def configure_logging(verbose: bool) -> None:
    """Configure root logger based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)


def build_connection_params(
    host: str,
    username: str,
    password: str,
    port: int,
    device_type: str = "cisco_ios",
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Return a dictionary suitable for Netmiko's ConnectHandler.

    The function isolates Netmiko‑specific parameters, making it easy to adapt
    the script for other platforms (e.g., juniper, arista) by changing the
    ``device_type`` argument.
    """
    return {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "port": port,
        "secret": password,  # many IOS devices use enable secret same as password
        "global_delay_factor": 2,
        "timeout": timeout,
    }


def run_commands(conn: ConnectHandler, commands: List[str]) -> Dict[str, str]:
    """
    Execute a list of commands on the device and return a dict of raw outputs.

    Parameters
    ----------
    conn : ConnectHandler
        An established Netmiko connection.
    commands : list[str]
        Commands to run in order.

    Returns
    -------
    dict
        Mapping of command → raw output string.
    """
    results = {}
    for cmd in commands:
        LOGGER.debug("Running command: %s", cmd)
        try:
            output = conn.send_command(cmd, expect_string=r"#|>", strip_prompt=True, strip_command=True)
            results[cmd] = output
        except Exception as exc:  # Broad catch to keep script robust
            LOGGER.error("Failed to run command '%s': %s", cmd, exc)
            results[cmd] = f"ERROR: {exc}"
    return results


def parse_version(output: str) -> Dict[str, str]:
    """
    Very light parsing of ``show version`` output to extract version and uptime.

    This parser is deliberately simple and works with typical Cisco IOS
    outputs. It can be extended for other vendors or more detailed parsing.
    """
    parsed = {}
    for line in output.splitlines():
        if "Version" in line and "Cisco IOS Software" in line:
            parsed["software_version"] = line.split("Version")[1].split(",")[0].strip()
        if "uptime is" in line.lower():
            parsed["uptime"] = line.split("is")[1].strip()
    return parsed


def aggregate_results(raw: Dict[str, str]) -> Dict[str, Any]:
    """
    Build a high‑level dictionary that includes timestamps and any parsed data.

    Currently only ``show version`` is parsed; other commands are retained as
    raw strings.
    """
    aggregated = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "commands": {},
        "parsed": {},
    }
    for cmd, out in raw.items():
        aggregated["commands"][cmd] = out
        if cmd.lower().startswith("show version"):
            aggregated["parsed"]["version"] = parse_version(out)
    return aggregated


def output_results(data: Dict[str, Any], fmt: str) -> None:
    """Print results to stdout in the requested format."""
    if fmt == "json":
        print(json.dumps(data, indent=2))
    else:
        # Human readable text
        print(f"--- Audit run @ {data['timestamp']} ---")
        for cmd, out in data["commands"].items():
            print(f"\n>>> {cmd}\n{out}\n{'-'*40}")
        if data["parsed"]:
            print("\nParsed summary:")
            for key, val in data["parsed"].items():
                print(f"{key}: {json.dumps(val, indent=2)}")


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def parse_cli() -> argparse.Namespace:
    """Define and parse command‑line arguments."""
    parser = argparse.ArgumentParser(
        description="Network device health audit via SSH (Netmiko)."
    )
    parser.add_argument(
        "--device",
        required=True,
        help="IP address or hostname of the target device.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Login username.",
    )
    parser.add_argument(
        "--password",
        required=True,
        help="Login password (will be echoed in process list).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port (default: 22).",
    )
    parser.add_argument(
        "--command",
        action="append",
        help=(
            "Command to run. Can be specified multiple times. "
            "If omitted, a default command list is used."
        ),
    )
    parser.add_argument(
        "--output",
        choices=["json", "text"],
        default="text",
        help="Result format (default: text).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Connection timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--device-type",
        default="cisco_ios",
        help="Netmiko device_type (default: cisco_ios).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def main() -> int:
    """Execute the audit workflow."""
    args = parse_cli()
    configure_logging(args.verbose)

    commands = args.command if args.command else DEFAULT_COMMANDS
    LOGGER.info("Target device: %s", args.device)
    LOGGER.debug("Commands to run: %s", commands)

    conn_params = build_connection_params(
        host=args.device,
        username=args.username,
        password=args.password,
        port=args.port,
        device_type=args.device_type,
        timeout=args.timeout,
    )

    try:
        LOGGER.debug("Establishing SSH connection with %s", conn_params)
        with ConnectHandler(**conn_params) as conn:
            conn.enable()  # Enter enable mode if required
            raw_output = run_commands(conn, commands)
    except NetMikoAuthenticationException:
        LOGGER.error("Authentication failed for %s", args.device)
        return 1
    except NetMikoTimeoutException:
        LOGGER.error("Connection timed out to %s", args.device)
        return 1
    except Exception as exc:  # Catch unexpected errors
        LOGGER.exception("Unexpected error: %s", exc)
        return 1

    audit_data = aggregate_results(raw_output)
    output_results(audit_data, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())