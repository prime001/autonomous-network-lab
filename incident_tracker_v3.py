"""interface_error_reporter.py

Collect interface error statistics from a network device via SSH and generate a
JSON report.  The script is intended for inclusion in a public portfolio to
demonstrate real‑world network automation using Python.

Usage:
    python interface_error_reporter.py \\
        --host 10.0.0.1 \\
        --user admin \\
        --password secret \\
        [--command "show interfaces"] \\
        [--output errors_10.0.0.1.json]

Prerequisites:
    * Python 3.8+
    * paramiko library (install with ``pip install paramiko``)
    * Network device reachable over SSH and supporting the supplied command.
"""

import argparse
import datetime
import json
import logging
import re
import sys
from pathlib import Path

import paramiko


def configure_logging(verbose: bool = False) -> None:
    """Configure the root logger.

    Args:
        verbose: If True, set log level to DEBUG, otherwise INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_ssh_output(
    host: str,
    username: str,
    password: str,
    command: str,
    timeout: int = 30,
) -> str:
    """Execute a command on a remote device via SSH and return its output.

    Args:
        host: Device IP or hostname.
        username: SSH username.
        password: SSH password.
        command: Command to run (e.g., ``show interfaces``).
        timeout: Connection timeout in seconds.

    Returns:
        The raw command output as a string.

    Raises:
        paramiko.SSHException: For SSH protocol errors.
        socket.timeout: If the connection times out.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        logging.debug("Connecting to %s", host)
        client.connect(
            hostname=host,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
        )
        logging.debug("Connected. Executing command: %s", command)
        stdin, stdout, stderr = client.exec_command(command, get_pty=True)
        # Ensure the command has finished
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            err = stderr.read().decode()
            raise RuntimeError(f"Command failed with status {exit_status}: {err}")
        output = stdout.read().decode()
        logging.debug("Command output received (%d bytes)", len(output))
        return output
    finally:
        client.close()
        logging.debug("SSH connection to %s closed", host)


def parse_interface_errors(output: str) -> dict:
    """Parse ``show interfaces`` output and extract error counters.

    The function looks for lines containing ``input errors`` or ``output errors``
    and builds a nested dictionary keyed by interface name.

    Args:
        output: Raw text output from the device.

    Returns:
        A dictionary:
        {
            "timestamp": "<ISO8601>",
            "interfaces": {
                "<interface>": {
                    "input_errors": int,
                    "output_errors": int,
                    "crc_errors": int,
                    "frame_errors": int
                },
                ...
            }
        }
    """
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    report = {"timestamp": timestamp, "interfaces": {}}

    # Example fragment to match:
    # GigabitEthernet0/0 is up, line protocol is up
    #   5 input errors, 2 CRC, 0 frame, 0 overrun, 0 ignored
    #   0 output errors, 0 collisions, 0 interface resets
    iface_regex = re.compile(r"^(?P<intf>\S+)\s+is\s+\S+", re.MULTILINE)
    input_err_regex = re.compile(
        r"(?P<input>\d+)\s+input errors(?:,\s+(?P<crc>\d+)\s+CRC)?"
        r"(?:,\s+(?P<frame>\d+)\s+frame)?",
        re.IGNORECASE,
    )
    output_err_regex = re.compile(r"(?P<output>\d+)\s+output errors", re.IGNORECASE)

    lines = output.splitlines()
    current_intf = None

    for line in lines:
        iface_match = iface_regex.search(line)
        if iface_match:
            current_intf = iface_match.group("intf")
            report["interfaces"][current_intf] = {
                "input_errors": 0,
                "output_errors": 0,
                "crc_errors": 0,
                "frame_errors": 0,
            }
            logging.debug("Found interface: %s", current_intf)
            continue

        if current_intf is None:
            continue

        in_match = input_err_regex.search(line)
        if in_match:
            report["interfaces"][current_intf]["input_errors"] = int(
                in_match.group("input")
            )
            report["interfaces"][current_intf]["crc_errors"] = int(
                in_match.group("crc") or 0
            )
            report["interfaces"][current_intf]["frame_errors"] = int(
                in_match.group("frame") or 0
            )
            logging.debug(
                "Interface %s input errors: %s (CRC %s, frame %s)",
                current_intf,
                in_match.group("input"),
                in_match.group("crc") or "0",
                in_match.group("frame") or "0",
            )
            continue

        out_match = output_err_regex.search(line)
        if out_match:
            report["interfaces"][current_intf]["output_errors"] = int(
                out_match.group("output")
            )
            logging.debug(
                "Interface %s output errors: %s",
                current_intf,
                out_match.group("output"),
            )

    return report


def write_report(report: dict, output_path: Path) -> None:
    """Write the JSON report to a file.

    Args:
        report: The dictionary returned by :func:`parse_interface_errors`.
        output_path: Destination file path.
    """
    try:
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        logging.info("Report written to %s", output_path)
    except OSError as exc:
        logging.error("Failed to write report: %s", exc)
        raise


def parse_args() -> argparse.Namespace:
    """Parse command‑line arguments."""
    parser = argparse.ArgumentParser(
        description="Collect interface error statistics from a network device via SSH.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host", required=True, help="IP address or hostname of the target device"
    )
    parser.add_argument("--user", required=True, help="SSH username")
    parser.add_argument(
        "--password", required=True, help="SSH password"
    )  # In production use getpass or vaults
    parser.add_argument(
        "--command",
        default="show interfaces",
        help="Command to retrieve interface information",
    )
    parser.add_argument(
        "--output",
        help="Path to output JSON report. Default: errors_<host>.json",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable debug‑level logging"
    )
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    configure_logging(args.verbose)

    output_file = (
        Path(args.output)
        if args.output
        else Path(f"errors_{args.host.replace('.', '_')}.json")
    )

    try:
        raw_output = get_ssh_output(
            host=args.host,
            username=args.user,
            password=args.password,
            command=args.command,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logging.error("Failed to retrieve data from %s: %s", args.host, exc)
        return 1

    report = parse_interface_errors(raw_output)
    try:
        write_report(report, output_file)
    except Exception:  # pylint: disable=broad-except
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())