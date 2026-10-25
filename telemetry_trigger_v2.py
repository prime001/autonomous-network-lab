"""autonomous_telemetry_trigger.py
================================================

A lightweight utility to poll a network device via SSH, extract telemetry data
(e.g., interface error counters) and optionally trigger an automated remediation
action when a configurable threshold is breached.

Features
--------
* Connects to IOS/JunOS/Arista devices using Paramiko (pure Python SSH client).
* Executes a user‑provided telemetry command (default ``show interfaces``).
* Parses the command output for interface error statistics.
* Logs detailed information and errors to both console and a rotating log file.
* Supports a ``--dry-run`` mode where remediation commands are only logged.
* Fully configurable via command‑line arguments.

Prerequisites
-------------
* Python 3.8+.
* ``paramiko`` library installed (``pip install paramiko``).
* Network device must allow SSH access and the executing user must have
  sufficient privilege to run the telemetry and remediation commands.

Usage
-----
::

    python autonomous_telemetry_trigger.py \\
        --host 10.0.0.1 \\
        --username admin \\
        --password secret \\
        --interface GigabitEthernet0/1 \\
        --error-threshold 100 \\
        --remediate-cmd "shutdown; no shutdown" \\
        --dry-run

The script will fetch interface statistics, evaluate the error count, and if the
count exceeds ``--error-threshold`` will execute the remediation command on the
specified interface (or log the intended action when ``--dry-run`` is set).

"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import paramiko
from paramiko.ssh_exception import AuthenticationException, SSHException

# -------------------------------------------------------------------------
# Logging configuration
# -------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGER = logging.getLogger("TelemetryTrigger")
LOGGER.setLevel(logging.DEBUG)

# Console handler - INFO and above
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(levelname)s: %(message)s")
console_handler.setFormatter(console_formatter)

# File handler - DEBUG and above, rotates daily
file_handler = logging.handlers.TimedRotatingFileHandler(
    LOG_DIR / "telemetry_trigger.log", when="midnight", backupCount=7, encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s",
    "%Y-%m-%d %H:%M:%S",
)
file_handler.setFormatter(file_formatter)

LOGGER.addHandler(console_handler)
LOGGER.addHandler(file_handler)

# -------------------------------------------------------------------------
# Core functionality
# -------------------------------------------------------------------------

def establish_ssh_connection(
    host: str,
    username: str,
    password: Optional[str] = None,
    port: int = 22,
    timeout: int = 10,
    key_file: Optional[Path] = None,
) -> paramiko.SSHClient:
    """Create and return an active Paramiko SSHClient.

    Args:
        host: IP or hostname of the device.
        username: SSH username.
        password: SSH password (optional if key_file is provided).
        port: SSH port number.
        timeout: Connection timeout in seconds.
        key_file: Path to a private key file for key‑based auth.

    Raises:
        AuthenticationException: If authentication fails.
        SSHException: For other SSH related errors.

    Returns:
        An authenticated ``paramiko.SSHClient`` instance.
    """
    LOGGER.debug("Initializing SSH client for %s:%s", host, port)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        connect_kwargs = {"hostname": host, "port": port, "username": username, "timeout": timeout}
        if key_file:
            connect_kwargs["key_filename"] = str(key_file)
        else:
            connect_kwargs["password"] = password

        client.connect(**connect_kwargs)
        LOGGER.info("SSH connection established to %s", host)
        return client
    except AuthenticationException as exc:
        LOGGER.error("Authentication failed for %s: %s", host, exc)
        raise
    except SSHException as exc:
        LOGGER.error("SSH error while connecting to %s: %s", host, exc)
        raise
    except Exception as exc:
        LOGGER.exception("Unexpected error establishing SSH connection to %s", host)
        raise

def run_ssh_command(client: paramiko.SSHClient, command: str, timeout: int = 30) -> str:
    """Execute a command on the remote device and return its output.

    Args:
        client: An active Paramiko SSHClient.
        command: The command string to execute.
        timeout: Command execution timeout.

    Returns:
        The command's stdout as a string.

    Raises:
        RuntimeError: If command execution fails or returns a non‑zero exit status.
    """
    LOGGER.debug("Running command: %s", command)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode()
        err = stderr.read().decode()
        if exit_status != 0:
            LOGGER.error(
                "Command failed with status %s. Stderr: %s", exit_status, err.strip()
            )
            raise RuntimeError(f"Command '{command}' failed with exit status {exit_status}")
        LOGGER.debug("Command output: %s", out[:200])
        return out
    except Exception as exc:
        LOGGER.exception("Failed to execute command '%s': %s", command, exc)
        raise

def parse_interface_errors(output: str, interface: str) -> Optional[int]:
    """Extract the input error count for a given interface from CLI output.

    This parser works with Cisco IOS ``show interfaces`` style output. It looks
    for a line containing ``<interface> is ...`` followed by a line with
    ``<errors> input errors,``.

    Args:
        output: The raw command output.
        interface: The interface name to search for.

    Returns:
        The integer error count, or ``None`` if parsing fails.
    """
    LOGGER.debug("Parsing error count for interface %s", interface)
    # Normalize whitespace and split into sections per interface
    iface_sections = re.split(r"\n(?=\S+ is )", output)
    for section in iface_sections:
        if not section.lower().startswith(interface.lower()):
            continue
        # Search for '<n> input errors,'
        match = re.search(r"(\d+)\s+input errors,", section, re.IGNORECASE)
        if match:
            error_count = int(match.group(1))
            LOGGER.info(
                "Found %d input errors on interface %s", error_count, interface
            )
            return error_count
    LOGGER.warning(
        "Could not locate error statistics for interface %s in command output", interface
    )
    return None

def build_remediation_commands(
    interface: str,
    remediate_cmd: str,
) -> str:
    """Construct the full remediation command string.

    Args:
        interface: Target interface name.
        remediate_cmd: Semicolon‑separated sub‑commands (e.g., ``shutdown; no shutdown``).

    Returns:
        A multi‑line string ready for execution in the device's CLI.
    """
    commands = [
        "configure terminal",
        f"interface {interface}",
    ] + [cmd.strip() for cmd in remediate_cmd.split(";") if cmd.strip()]
    commands.append("end")
    remediation = "\n".join(commands)
    LOGGER.debug("Built remediation command: %s", remediation.replace("\n", " | "))
    return remediation

def execute_remediation(
    client: paramiko.SSHClient,
    remediation: str,
    dry_run: bool = False,
) -> None:
    """Execute remediation commands on the device.

    Args:
        client: Active SSH client.
        remediation: Multi‑line command string.
        dry_run: If ``True`` only logs the remediation without executing.
    """
    if dry_run:
        LOGGER.info("[DRY RUN] Would execute remediation commands:\n%s", remediation)
        return

    LOGGER.info("Executing remediation commands.")
    # Many devices accept config mode commands via a single exec channel.
    # We'll send the whole script at once.
    try:
        channel = client.invoke_shell()
        time.sleep(0.5)  # give prompt time to appear
        output = channel.recv(65535).decode()
        LOGGER.debug("Initial prompt: %s", output.strip())

        for line in remediation.splitlines():
            channel.send(line + "\n")
            time.sleep(0.5)  # wait for command to be processed
            resp = channel.recv(65535).decode()
            LOGGER.debug("Sent: %s | Received: %s", line, resp.strip())

        channel.send("exit\n")
        LOGGER.info("Remediation sequence completed.")
    except Exception as exc:
        LOGGER.exception("Failed during remediation execution: %s", exc)
        raise

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telemetry‑trigger script: poll a device, evaluate thresholds, "
        "and optionally remediate.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="Device IP or hostname")
    parser.add_argument("--port", type=int, default=22, help="SSH port")
    parser.add_argument("-u", "--username", required=True, help="SSH username")
    auth_group = parser.add_mutually_exclusive_group(required=True)
    auth_group.add_argument("-p", "--password", help="SSH password")
    auth_group.add_argument("-k", "--key-file", type=Path, help="Path to private key")
    parser.add_argument(
        "-i",
        "--interface",
        required=True,
        help="Target interface for telemetry collection and remediation",
    )
    parser.add_argument(
        "-c",
        "--command",
        default="show interfaces",
        help="Telemetry command to execute on the device",
    )
    parser.add_argument(
        "-t",
        "--error-threshold",
        type=int,
        default=100,
        help="Error count threshold to trigger remediation",
    )
    parser.add_argument(
        "-r",
        "--remediate-cmd",
        default="shutdown; no shutdown",
        help="Semicolon‑separated commands to run when threshold exceeded",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log remediation actions without executing them",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity",
    )
    args = parser.parse_args()

    # Adjust log level based on CLI
    LOGGER.setLevel(getattr(logging, args.log_level))
    console_handler.setLevel(getattr(logging, args.log_level))

    try:
        client = establish_ssh_connection(
            host=args.host,
            username=args.username,
            password=args.password,
            port=args.port,
            key_file=args.key_file,
        )
        output = run_ssh_command(client, args.command)
        error_count = parse_interface_errors(output, args.interface)

        if error_count is None:
            LOGGER.error("Unable to determine error count; aborting.")
            sys.exit(1)

        if error_count > args.error_threshold:
            LOGGER.warning(
                "Error count %d exceeds threshold %d on %s",
                error_count,
                args.error_threshold,
                args.interface,
            )
            remediation = build_remediation_commands(args.interface, args.remediate_cmd)
            execute_remediation(client, remediation, dry_run=args.dry_run)
        else:
            LOGGER.info(
                "Error count %d is within acceptable limits (threshold %d). No action taken.",
                error_count,
                args.error_threshold,
            )
    except Exception as exc:
        LOGGER.error("Telemetry trigger failed: %s", exc)
        sys.exit(1)
    finally:
        try:
            client.close()
            LOGGER.debug("SSH connection closed.")
        except Exception:
            pass

if __name__ == "__main__":
    main()