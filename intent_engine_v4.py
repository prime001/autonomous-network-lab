"""
network_cli_tool.py

A lightweight CLI utility for interacting with network devices via SSH.
Supported operations:
* Execute a single show/diagnostic command.
* Push a configuration file in “config mode” (line‑by‑line).

Usage:
    python network_cli_tool.py -H <host> -u <user> -p <pass> -t <device_type>
                                    (--command "show version"
                                    | --config-file path/to/file)
Options:
    -H, --host            Target device IP or hostname (required)
    -u, --username        SSH username (required)
    -p, --password        SSH password (if omitted, will be prompted)
    -t, --device-type     Netmiko device_type (default: cisco_ios)
    -c, --command         Command to execute on the device
    -f, --config-file     Path to a configuration file to push
    -v, --verbose         Enable verbose logging

Prerequisites:
    * Python 3.8+
    * netmiko (pip install netmiko)
    * Optional: paramiko (installed as a netmiko dependency)

The script deliberately avoids external configuration management frameworks
to stay self‑contained and easy to understand for portfolio reviewers.
"""

import argparse
import getpass
import logging
import sys
from pathlib import Path
from typing import List, Optional

from netmiko import ConnectHandler, NetMikoAuthenticationException, NetMikoTimeoutException

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)


def configure_logging(verbose: bool) -> None:
    """Configure root logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --------------------------------------------------------------------------- #
# Core functionality
# --------------------------------------------------------------------------- #
def build_connection_params(
    host: str,
    username: str,
    password: str,
    device_type: str,
) -> dict:
    """Return a dictionary suitable for Netmiko ConnectHandler."""
    return {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "fast_cli": False,
        # Most devices benefit from a small delay when sending config lines.
        "global_delay_factor": 2,
    }


def run_command(connection: ConnectHandler, command: str) -> str:
    """Execute a command on the device and return its output."""
    logger.debug("Running command: %s", command)
    try:
        output = connection.send_command(command, strip_prompt=True, strip_command=True)
        logger.debug("Command output received (%d bytes)", len(output))
        return output
    except Exception as exc:
        logger.error("Failed to run command '%s': %s", command, exc)
        raise


def push_config(connection: ConnectHandler, config_lines: List[str]) -> str:
    """Push configuration lines to the device in config mode."""
    logger.debug("Entering configuration mode")
    try:
        output = connection.send_config_set(config_lines)
        logger.debug("Configuration push completed")
        return output
    except Exception as exc:
        logger.error("Failed to push configuration: %s", exc)
        raise


def load_config_file(file_path: Path) -> List[str]:
    """Read a configuration file and return a list of non‑empty lines."""
    if not file_path.is_file():
        raise FileNotFoundError(f"Config file not found: {file_path}")

    lines = [line.rstrip() for line in file_path.read_text().splitlines() if line.strip()]
    logger.debug("Loaded %d configuration lines from %s", len(lines), file_path)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute commands or push configs to a network device via SSH."
    )
    parser.add_argument("-H", "--host", required=True, help="Device IP or hostname")
    parser.add_argument("-u", "--username", required=True, help="SSH username")
    parser.add_argument("-p", "--password", help="SSH password (will prompt if omitted)")
    parser.add_argument("-t", "--device-type", default="cisco_ios", help="Netmiko device type")
    parser.add_argument("-c", "--command", help="Show/diagnostic command to run")
    parser.add_argument("-f", "--config-file", type=Path, help="Path to configuration file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    configure_logging(args.verbose)

    if not args.command and not args.config_file:
        parser.error("At least one of --command or --config-file must be provided")

    password = args.password or getpass.getpass(prompt="SSH Password: ")

    conn_params = build_connection_params(
        host=args.host,
        username=args.username,
        password=password,
        device_type=args.device_type,
    )

    try:
        logger.info("Establishing SSH connection to %s (%s)", args.host, args.device_type)
        with ConnectHandler(**conn_params) as net_connect:
            net_connect.enable()  # Enter enable mode if applicable

            if args.command:
                logger.info("Executing command: %s", args.command)
                cmd_output = run_command(net_connect, args.command)
                print("\n--- Command Output ---\n")
                print(cmd_output)

            if args.config_file:
                config_lines = load_config_file(args.config_file)
                logger.info("Pushing configuration from %s", args.config_file)
                cfg_output = push_config(net_connect, config_lines)
                print("\n--- Config Push Output ---\n")
                print(cfg_output)

    except (NetMikoAuthenticationException, NetMikoTimeoutException) as exc:
        logger.error("Connection failure: %s", exc)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()