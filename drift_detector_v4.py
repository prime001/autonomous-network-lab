"""drift_detector.py
Network Configuration Drift Detector

Purpose:
    Connect to a network device over SSH, retrieve the current running
    configuration and compare it against a stored baseline configuration.
    Differences are reported as a unified diff, enabling quick identification
    of configuration drift.

Usage:
    python drift_detector.py --host 10.0.0.1 --user admin --password secret \\
        --baseline ./baselines/router1.cfg [--port 22] [--timeout 30] [--quiet]

Prerequisites:
    * Python 3.8+
    * paramiko (pip install paramiko)
    * Access to the target device via SSH with sufficient privileges to
      execute ``show running-config`` (or the device‑specific command defined
      by ``--command``).

The script is self‑contained and follows PEP 8 guidelines.
"""

import argparse
import difflib
import logging
import os
import sys
import socket
from typing import List, Optional

import paramiko


def configure_logging(verbose: bool) -> None:
    """Configure root logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    """Parse command‑line arguments."""
    parser = argparse.ArgumentParser(
        description="Detect configuration drift on network devices."
    )
    parser.add_argument(
        "--host", required=True, help="Hostname or IP address of the device."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port (default: 22).",
    )
    parser.add_argument(
        "--user", required=True, help="Username for SSH authentication."
    )
    parser.add_argument(
        "--password",
        required=True,
        help="Password for SSH authentication.",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        help="Path to the baseline configuration file.",
    )
    parser.add_argument(
        "--command",
        default="show running-config",
        help="Command to retrieve the running configuration (default: show running-config).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="SSH connection timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress diff output, only set exit code.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose (debug) logging.",
    )
    return parser.parse_args()


def load_baseline(path: str) -> List[str]:
    """Read baseline file and return its lines.

    Args:
        path: Path to the baseline configuration file.

    Raises:
        FileNotFoundError: If the file does not exist.
        PermissionError: If the file cannot be read.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Baseline file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    logging.debug("Baseline loaded (%d lines).", len(lines))
    return lines


def ssh_execute(
    host: str,
    port: int,
    username: str,
    password: str,
    command: str,
    timeout: int,
) -> List[str]:
    """Execute a command over SSH and return its output as a list of lines.

    Args:
        host: Device hostname or IP.
        port: SSH port.
        username: SSH user.
        password: SSH password.
        command: CLI command to execute.
        timeout: Connection timeout.

    Returns:
        List of output lines (including newline characters).

    Raises:
        paramiko.SSHException: On generic SSH failure.
        socket.error: On network‑level errors.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        logging.debug("Connecting to %s:%s as %s", host, port, username)
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        logging.debug("Connection established.")
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdout.channel.settimeout(timeout)

        output = stdout.readlines()
        err = stderr.read().decode()
        if err:
            logging.warning("Command stderr: %s", err.strip())
        logging.debug("Command output received (%d lines).", len(output))
        return output
    finally:
        client.close()
        logging.debug("SSH connection closed.")


def generate_diff(
    baseline: List[str], current: List[str], fromfile: str = "baseline", tofile: str = "current"
) -> str:
    """Create a unified diff between baseline and current configurations."""
    diff = difflib.unified_diff(
        baseline,
        current,
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
    )
    return "\n".join(diff)


def main() -> int:
    """Entry point for drift detection."""
    args = parse_args()
    configure_logging(args.verbose)

    try:
        baseline_cfg = load_baseline(args.baseline)
    except (FileNotFoundError, PermissionError) as e:
        logging.error(e)
        return 2

    try:
        current_cfg = ssh_execute(
            host=args.host,
            port=args.port,
            username=args.user,
            password=args.password,
            command=args.command,
            timeout=args.timeout,
        )
    except (paramiko.SSHException, socket.error) as e:
        logging.error("SSH command failed: %s", e)
        return 3

    diff = generate_diff(baseline_cfg, current_cfg, fromfile=args.baseline, tofile=args.host)

    if diff:
        logging.info("Configuration drift detected.")
        if not args.quiet:
            print(diff)
        return 1  # non‑zero indicates drift
    else:
        logging.info("No drift detected – configurations are identical.")
        return 0


if __name__ == "__main__":
    sys.exit(main())