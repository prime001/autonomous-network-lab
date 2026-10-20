"""cli_executor.py
Network Automation Command Executor

This script provides a lightweight command‑execution engine for network devices.
It connects to a device over SSH using **Netmiko**, runs one or more CLI
commands, and returns the combined output.  The tool is intended for
interactive use, CI pipelines, or as a building block for higher‑level
automation (e.g., intent engines, chat‑ops bots).

Usage
-----
    python cli_executor.py -t cisco_ios -H 10.0.0.1 -u admin -p secret \
        -c "show version" "show ip interface brief"

    # Using a command file
    python cli_executor.py -t juniper_junos -H 10.0.0.2 -u admin -p secret \
        -f commands.txt

Arguments
---------
* -t / --device-type   Netmiko device type (cisco_ios, juniper_junos, etc.)
* -H / --host           Target device IP or hostname
* -u / --username       SSH username
* -p / --password       SSH password
* --enable              Enable (privileged) password (optional)
* -c / --command        Command(s) to execute (can be repeated)
* -f / --file           Path to a file containing one command per line
* -o / --output         Write output to file instead of stdout
* -v / --verbose        Enable DEBUG level logging

Prerequisites
-------------
* Python 3.8+
* Netmiko (pip install netmiko)
* Access to the target device via SSH
* Appropriate credentials with sufficient privilege for the requested
  commands.

The script uses only the Python standard library plus Netmiko for device
interaction, follows PEP 8 conventions, and includes robust error handling
and structured logging.
"""

import argparse
import logging
import os
import sys
from typing import List, Dict

from netmiko import ConnectHandler
from netmiko.ssh_exception import (
    NetMikoTimeoutException,
    NetMikoAuthenticationException,
)

# ----------------------------------------------------------------------
# Logging configuration
# ----------------------------------------------------------------------


def _configure_logger(verbose: bool) -> logging.Logger:
    """Create a logger with console output and optional DEBUG level."""
    logger = logging.getLogger("cli_executor")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    console.setFormatter(logging.Formatter(fmt))

    if not logger.handlers:
        logger.addHandler(console)

    return logger


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------


def _load_commands_from_file(path: str) -> List[str]:
    """Read commands line‑by‑line from a file, ignoring comments/blank lines."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Command file not found: {path}")

    commands = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                commands.append(stripped)
    return commands


def _connect_device(
    device_type: str,
    host: str,
    username: str,
    password: str,
    secret: str = None,
    logger: logging.Logger = None,
) -> ConnectHandler:
    """Establish an SSH connection using Netmiko."""
    device_params = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "fast_cli": False,
    }
    if secret:
        device_params["secret"] = secret

    try:
        conn = ConnectHandler(**device_params)
        logger.debug("SSH connection established to %s (%s)", host, device_type)
        if secret:
            conn.enable()
            logger.debug("Entered enable mode")
        return conn
    except NetMikoTimeoutException as exc:
        logger.error("Connection timeout to %s: %s", host, exc)
        raise
    except NetMikoAuthenticationException as exc:
        logger.error("Authentication failed for %s: %s", host, exc)
        raise
    except Exception as exc:
        logger.error("Unexpected error while connecting to %s: %s", host, exc)
        raise


def _run_commands(conn: ConnectHandler, commands: List[str], logger: logging.Logger) -> Dict[str, str]:
    """Execute a list of commands and return a mapping of command → output."""
    results = {}
    for cmd in commands:
        logger.info("Running command: %s", cmd)
        try:
            output = conn.send_command(cmd, expect_string=r"#|>", strip_prompt=False, strip_command=False)
            results[cmd] = output
            logger.debug("Command output length: %d", len(output))
        except Exception as exc:
            logger.error("Failed to run command \"%s\": %s", cmd, exc)
            results[cmd] = f"ERROR: {exc}"
    return results


def _format_results(results: Dict[str, str]) -> str:
    """Create a human‑readable string from command results."""
    lines = []
    for cmd, out in results.items():
        lines.append(f"\n=== {cmd} ===\n{out.rstrip()}\n")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------


def parse_arguments() -> argparse.Namespace:
    """Define and parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Execute arbitrary CLI commands on a network device via SSH."
    )
    parser.add_argument("-t", "--device-type", required=True, help="Netmiko device type (e.g., cisco_ios).")
    parser.add_argument("-H", "--host", required=True, help="Target device IP or hostname.")
    parser.add_argument("-u", "--username", required=True, help="SSH username.")
    parser.add_argument("-p", "--password", required=True, help="SSH password.")
    parser.add_argument("--enable", dest="enable_password", help="Enable password (if required).")
    parser.add_argument("-c", "--command", action="append", help="Command to run (can be used multiple times).")
    parser.add_argument("-f", "--file", type=str, help="File containing commands (one per line).")
    parser.add_argument("-o", "--output", type=str, help="Write combined output to file.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging.")
    return parser.parse_args()


def main() -> int:
    """Coordinate argument parsing, device connection, command execution, and output."""
    args = parse_arguments()
    logger = _configure_logger(args.verbose)

    # Assemble command list
    commands: List[str] = []
    if args.command:
        commands.extend(args.command)
    if args.file:
        try:
            file_cmds = _load_commands_from_file(args.file)
            commands.extend(file_cmds)
            logger.debug("Loaded %d commands from %s", len(file_cmds), args.file)
        except Exception as exc:
            logger.error("Unable to read command file: %s", exc)
            return 1

    if not commands:
        logger.error("No commands supplied. Use -c or -f to specify commands.")
        return 1

    # Connect to device and run commands
    try:
        conn = _connect_device(
            device_type=args.device_type,
            host=args.host,
            username=args.username,
            password=args.password,
            secret=args.enable_password,
            logger=logger,
        )
        results = _run_commands(conn, commands, logger)
    except Exception:
        logger.exception("Aborting due to connection failure.")
        return 1
    finally:
        if "conn" in locals() and conn:
            conn.disconnect()
            logger.debug("SSH session closed")

    output_text = _format_results(results)

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as out_file:
                out_file.write(output_text)
            logger.info("Command output written to %s", args.output)
        except Exception as exc:
            logger.error("Failed to write output file: %s", exc)
            return 1
    else:
        print(output_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())