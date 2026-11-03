#!/usr/bin/env python3
"""
Network Device Health Monitor
==============================

A lightweight command‑line utility that connects to a network device over SSH,
executes a set of health‑check commands, parses the results and emits a JSON
summary.  The script uses only the Python Standard Library (subprocess, json,
argparse, logging, etc.) and therefore works on any modern Python 3
installation without additional dependencies.

Usage
-----
    python3 health_monitor_cli.py --host 10.0.0.1 --username admin \
        [--port 22] [--commands "show version,show uptime"] \
        [--output health.json] [--log monitor.log]

Prerequisites
-------------
* SSH access to the target device (key‑based authentication is recommended).
* The local machine must have the ``ssh`` client available in the PATH.
* The device must support the commands supplied (Cisco‑like CLI syntax is
  assumed for the built‑in parsers).

The script is intended for demonstration and educational purposes; adapt the
command set and parsers to match your environment.
"""

import argparse
import json
import logging
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

DEFAULT_COMMANDS = [
    "show version",
    "show uptime",
    "show ip interface brief",
]

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s - %(message)s"


def configure_logging(log_file: Path | None = None) -> None:
    """Configure root logger."""
    level = logging.DEBUG
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level, format=_LOG_FORMAT, handlers=handlers)


class SSHCommandError(RuntimeError):
    """Raised when an SSH command fails."""


def run_ssh_command(
    host: str,
    username: str,
    command: str,
    port: int = 22,
    timeout: int = 30,
) -> str:
    """
    Execute a single command on a remote host via the local ``ssh`` client.

    Parameters
    ----------
    host : str
        IP address or hostname of the target device.
    username : str
        SSH login name.
    command : str
        Command string to execute on the remote device.
    port : int, optional
        SSH port (default 22).
    timeout : int, optional
        Seconds to wait before aborting the command.

    Returns
    -------
    str
        The command's stdout output.

    Raises
    ------
    SSHCommandError
        If the ``ssh`` subprocess returns a non‑zero exit status.
    """
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-p", str(port),
        f"{username}@{host}",
        command,
    ]
    logging.debug("Running SSH command: %s", " ".join(shlex.quote(arg) for arg in ssh_cmd))

    try:
        completed = subprocess.run(
            ssh_cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SSHCommandError(f"Command timed out after {timeout}s: {command}") from exc
    except Exception as exc:
        raise SSHCommandError(f"Unexpected error executing SSH command: {exc}") from exc

    if completed.returncode != 0:
        msg = (
            f"SSH command failed (rc={completed.returncode}): {command}\n"
            f"stderr: {completed.stderr.strip()}"
        )
        raise SSHCommandError(msg)

    logging.debug("Command output (first 200 chars): %s", completed.stdout[:200])
    return completed.stdout


def parse_show_version(output: str) -> Dict[str, str]:
    """
    Very simple parser for Cisco ``show version`` output.

    Returns a dictionary with keys ``version`` and ``model`` if they can be
    identified; otherwise the values are empty strings.
    """
    version = ""
    model = ""
    for line in output.splitlines():
        line = line.strip()
        if "Cisco IOS Software" in line and "Version" in line:
            # Example: Cisco IOS Software, C880 Software (C880DATA-UNIVERSALK9-M), Version 15.4(3)M2, RELEASE SOFTWARE (fc1)
            parts = line.split("Version")
            if len(parts) > 1:
                version = parts[1].split(",")[0].strip()
        if "processor" in line.lower() and "bytes of memory" in line.lower():
            # Example: cisco 881 (MPC8300) processor (revision 1.0) with 236544K/12288K bytes of memory.
            model = line.split()[1]
    return {"version": version, "model": model}


def parse_show_uptime(output: str) -> Dict[str, str]:
    """
    Parses Cisco ``show uptime`` (actually part of ``show version``) to extract
    uptime string.
    """
    uptime = ""
    for line in output.splitlines():
        line = line.strip()
        if " uptime is " in line.lower():
            # Example: Router uptime is 2 weeks, 5 days, 3 hours, 12 minutes
            uptime = line.split(" uptime is ")[1].strip()
            break
    return {"uptime": uptime}


def parse_show_ip_int_brief(output: str) -> List[Dict[str, str]]:
    """
    Parses ``show ip interface brief`` output into a list of dictionaries,
    each representing an interface.
    """
    interfaces = []
    lines = output.splitlines()
    header_found = False
    for line in lines:
        if not header_found:
            if line.lower().startswith("interface"):
                header_found = True
            continue
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 6:
            iface = {
                "interface": parts[0],
                "ip_address": parts[1],
                "status": parts[4],
                "protocol": parts[5],
            }
            interfaces.append(iface)
    return interfaces


PARSERS = {
    "show version": parse_show_version,
    "show uptime": parse_show_uptime,
    "show ip interface brief": parse_show_ip_int_brief,
}


def collect_health(
    host: str,
    username: str,
    commands: List[str],
    port: int = 22,
) -> Dict[str, object]:
    """
    Execute the supplied commands on the device and collect parsed results.

    Returns a dictionary keyed by command string with either parsed data or raw
    command output if no parser is defined.
    """
    results: Dict[str, object] = {}
    for cmd in commands:
        try:
            raw_output = run_ssh_command(host, username, cmd, port=port)
        except SSHCommandError as exc:
            logging.error("Failed to run command '%s': %s", cmd, exc)
            results[cmd] = {"error": str(exc)}
            continue

        parser = PARSERS.get(cmd)
        if parser:
            try:
                parsed = parser(raw_output)
                results[cmd] = parsed
                logging.info("Parsed result for command '%s': %s", cmd, parsed)
            except Exception as exc:  # pragma: no cover
                logging.exception("Parsing failed for command '%s'", cmd)
                results[cmd] = {"raw": raw_output, "parse_error": str(exc)}
        else:
            results[cmd] = {"raw": raw_output}
            logging.info("No parser for command '%s'; raw output stored.", cmd)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect health information from a network device via SSH."
    )
    parser.add_argument(
        "--host", required=True, help="IP address or hostname of the device."
    )
    parser.add_argument(
        "--username", required=True, help="SSH username."
    )
    parser.add_argument(
        "--port", type=int, default=22, help="SSH port (default: 22)."
    )
    parser.add_argument(
        "--commands",
        help=(
            "Comma‑separated list of commands to run. "
            "If omitted, a default set of health checks is used."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Path to write JSON health report. Prints to STDOUT if omitted.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="File path for detailed log output (optional).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) console logging.",
    )

    args = parser.parse_args()

    # Logging configuration
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format=_LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(args.log, encoding="utf-8") if args.log else logging.NullHandler()
        ],
    )
    logging.debug("Parsed arguments: %s", args)

    command_list = (
        [c.strip() for c in args.commands.split(",")] if args.commands else DEFAULT_COMMANDS
    )
    logging.info("Executing commands: %s", command_list)

    health_data = collect_health(
        host=args.host,
        username=args.username,
        commands=command_list,
        port=args.port,
    )

    report = {
        "device": args.host,
        "username": args.username,
        "port": args.port,
        "health": health_data,
    }

    json_report = json.dumps(report, indent=2)
    if args.output:
        try:
            args.output.write_text(json_report, encoding="utf-8")
            logging.info("Health report written to %s", args.output)
        except Exception as exc:  # pragma: no cover
            logging.error("Failed to write report: %s", exc)
            return 1
    else:
        print(json_report)

    return 0


if __name__ == "__main__":
    sys.exit(main())