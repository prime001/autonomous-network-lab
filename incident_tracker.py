"""incident_tracker.py
====================

A lightweight incident tracking utility for network devices.

The script connects to a network device over SSH, executes one or more
diagnostic commands, and records the output as an incident entry in a
JSON log file.  Optionally, it can forward the incident to an external
ticketing system via a simple HTTP POST request.

Usage
-----

    python incident_tracker.py \\
        --host 10.0.0.1 \\
        --username admin \\
        --password secret \\
        --commands "show version,show interfaces status" \\
        --severity critical \\
        --description "Link flaps detected on Gi0/1" \\
        --log-file incidents.json \\
        [--ticket-url https://tracker.example.com/api/tickets]

Prerequisites
-------------

* Python 3.8+
* ``paramiko`` – SSH client library
* ``requests`` – HTTP client library (only required when ``--ticket-url`` is used)

Both libraries can be installed via ``pip install paramiko requests``.
"""

import argparse
import datetime
import json
import logging
import os
import sys
from typing import Dict, List, Optional

import paramiko
import requests


LOGGER = logging.getLogger(__name__)


def configure_logging(verbose: bool) -> None:
    """Configure the root logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress noisy libraries unless debug is requested
    if not verbose:
        logging.getLogger("paramiko").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def ssh_execute(
    host: str,
    username: str,
    password: str,
    command: str,
    timeout: int = 30,
) -> str:
    """
    Execute a single command on a network device via SSH.

    Args:
        host: Device IP or hostname.
        username: SSH username.
        password: SSH password.
        command: Command string to execute.
        timeout: Connection timeout in seconds.

    Returns:
        Command output as a string.

    Raises:
        paramiko.SSHException: If SSH negotiation fails.
        socket.timeout: If the connection times out.
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
        LOGGER.debug("Running command: %s", command)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode()
        error = stderr.read().decode()
        if error:
            LOGGER.warning("Command error output: %s", error.strip())
        return output.strip()
    finally:
        client.close()
        LOGGER.debug("SSH connection to %s closed", host)


def parse_commands(commands: str) -> List[str]:
    """Split a comma‑separated command list into a clean list."""
    return [cmd.strip() for cmd in commands.split(",") if cmd.strip()]


def build_incident(
    host: str,
    command_outputs: Dict[str, str],
    severity: str,
    description: str,
) -> Dict:
    """
    Create an incident dictionary ready for JSON serialization.

    Args:
        host: Device identifier.
        command_outputs: Mapping of command → output.
        severity: Incident severity level.
        description: Human‑readable description.

    Returns:
        Dictionary representing the incident.
    """
    incident = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "device": host,
        "severity": severity,
        "description": description,
        "commands": [
            {"command": cmd, "output": out} for cmd, out in command_outputs.items()
        ],
    }
    LOGGER.debug("Incident built: %s", incident)
    return incident


def append_to_log(incident: Dict, log_file: str) -> None:
    """
    Append an incident to a JSON log file.

    The log file contains a JSON array. If the file does not exist,
    it is created with an opening array.

    Args:
        incident: Incident dictionary.
        log_file: Path to the log file.
    """
    # Ensure the directory exists
    os.makedirs(os.path.dirname(os.path.abspath(log_file)) or ".", exist_ok=True)

    if not os.path.isfile(log_file):
        LOGGER.debug("Log file %s does not exist, creating new one", log_file)
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump([incident], f, indent=2)
        return

    # Append while preserving valid JSON
    LOGGER.debug("Appending incident to existing log file %s", log_file)
    try:
        with open(log_file, "r+", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Log file does not contain a JSON array")
            data.append(incident)
            f.seek(0)
            json.dump(data, f, indent=2)
            f.truncate()
    except json.JSONDecodeError as exc:
        LOGGER.error("Failed to parse existing log file %s: %s", log_file, exc)
        raise


def post_to_ticketing(
    incident: Dict,
    ticket_url: str,
    api_key: Optional[str] = None,
    timeout: int = 15,
) -> None:
    """
    POST the incident to an external ticketing system.

    Args:
        incident: Incident payload.
        ticket_url: Endpoint accepting JSON payload.
        api_key: Optional bearer token for authentication.
        timeout: HTTP request timeout in seconds.

    Raises:
        requests.RequestException: If the request fails.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    LOGGER.info("Posting incident to %s", ticket_url)
    response = requests.post(
        ticket_url, headers=headers, json=incident, timeout=timeout
    )
    response.raise_for_status()
    LOGGER.info("Ticket created successfully, response: %s", response.text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect device diagnostics and log them as incidents."
    )
    parser.add_argument("--host", required=True, help="Device IP or hostname")
    parser.add_argument("--username", required=True, help="SSH username")
    parser.add_argument(
        "--password", required=True, help="SSH password (will not be stored)"
    )
    parser.add_argument(
        "--commands",
        required=True,
        help="Comma‑separated list of commands to run, e.g. "
        '"show version,show interfaces status"',
    )
    parser.add_argument(
        "--severity",
        default="warning",
        choices=["info", "warning", "minor", "major", "critical"],
        help="Incident severity level",
    )
    parser.add_argument(
        "--description",
        default="Automated incident report",
        help="Short description of the incident",
    )
    parser.add_argument(
        "--log-file",
        default="incidents.json",
        help="Path to JSON incident log file",
    )
    parser.add_argument(
        "--ticket-url",
        help="Endpoint of external ticketing system (optional)",
    )
    parser.add_argument(
        "--api-key",
        help="API key or token for ticketing system (if required)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="SSH command timeout in seconds",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    args = parser.parse_args()
    configure_logging(args.verbose)

    try:
        command_list = parse_commands(args.commands)
        if not command_list:
            LOGGER.error("No valid commands supplied")
            return 1

        command_outputs = {}
        for cmd in command_list:
            output = ssh_execute(
                host=args.host,
                username=args.username,
                password=args.password,
                command=cmd,
                timeout=args.timeout,
            )
            command_outputs[cmd] = output

        incident = build_incident(
            host=args.host,
            command_outputs=command_outputs,
            severity=args.severity,
            description=args.description,
        )

        append_to_log(incident, args.log_file)

        if args.ticket_url:
            post_to_ticketing(
                incident,
                ticket_url=args.ticket_url,
                api_key=args.api_key,
            )

        LOGGER.info("Incident processing completed successfully")
        return 0

    except paramiko.SSHException as exc:
        LOGGER.error("SSH error: %s", exc)
        return 1
    except requests.RequestException as exc:
        LOGGER.error("Ticketing request failed: %s", exc)
        return 1
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())