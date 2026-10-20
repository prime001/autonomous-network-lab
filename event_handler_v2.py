"""config_backup.py
~~~~~~~~~~~~~~~~~

A command‑line utility to retrieve and store the running configuration of a
network device via SSH. The script is intended for use in an event‑driven
automation pipeline (e.g., as part of the *autonomous‑network‑lab* suite).

Features
--------
* Connects to a device using the system ``ssh`` client (no third‑party
  libraries required).
* Executes a user‑specified command to obtain the configuration
  (default: ``show run`` for Cisco‑like devices).
* Saves the configuration to a timestamped file in a target directory.
* Supports password authentication (prompted securely) and key‑based
  authentication.
* Provides structured logging and robust error handling.

Prerequisites
-------------
* Python 3.8+.
* ``ssh`` client installed and reachable from the execution environment.
* Network device must allow SSH access for the supplied credentials.

Usage
-----
::

    python config_backup.py \\
        --host 10.0.0.1 \\
        --username admin \\
        [--password]            # Prompt for password interactively
        [--ssh-key /path/to/key] \\
        [--port 22] \\
        [--command "show running-config"] \\
        [--output-dir backups]

The script returns exit code ``0`` on success and a non‑zero code on
failure. Detailed logs are written to ``config_backup.log`` in the current
working directory.
"""

import argparse
import datetime
import getpass
import logging
import os
import pathlib
import shlex
import subprocess
import sys
from typing import Optional

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOG_FILE = "config_backup.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Core functionality
# --------------------------------------------------------------------------- #


def build_ssh_command(
    host: str,
    username: str,
    port: int,
    ssh_key: Optional[pathlib.Path],
    command: str,
) -> list:
    """Create the ``ssh`` command line list.

    Args:
        host: Target device IP or hostname.
        username: SSH username.
        port: SSH port number.
        ssh_key: Path to a private key file (or ``None`` for password auth).
        command: Remote command to execute.

    Returns:
        List suitable for ``subprocess.run``.
    """
    ssh_base = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-p",
        str(port),
    ]

    if ssh_key:
        ssh_base.extend(["-i", str(ssh_key)])

    remote = f"{username}@{host}"
    full_cmd = ssh_base + [remote, command]
    logger.debug("SSH command built: %s", " ".join(shlex.quote(c) for c in full_cmd))
    return full_cmd


def run_ssh_command(
    ssh_cmd: list,
    password: Optional[str] = None,
    timeout: int = 30,
) -> str:
    """Execute the SSH command and return its stdout.

    If ``password`` is provided, ``sshpass`` is used as a wrapper. The function
    prefers key‑based authentication; ``sshpass`` is only a convenience for
    quick testing and should be avoided in production.

    Args:
        ssh_cmd: List produced by :func:`build_ssh_command`.
        password: Optional password string.
        timeout: Execution timeout in seconds.

    Returns:
        The remote command output.

    Raises:
        RuntimeError: When SSH exits with a non‑zero status.
    """
    if password:
        # ``sshpass`` must be installed on the host system.
        ssh_cmd = ["sshpass", "-p", password] + ssh_cmd
        logger.debug("Using sshpass for password authentication.")

    logger.info("Executing SSH command on remote device.")
    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.SubprocessError as exc:
        logger.exception("SSH subprocess failed.")
        raise RuntimeError(f"SSH execution error: {exc}") from exc

    if result.returncode != 0:
        err_msg = result.stderr.strip() or "Unknown SSH error"
        logger.error("SSH command failed: %s", err_msg)
        raise RuntimeError(f"SSH command failed ({result.returncode}): {err_msg}")

    logger.debug("SSH command succeeded. Output length: %d bytes.", len(result.stdout))
    return result.stdout


def store_configuration(
    output_dir: pathlib.Path,
    host: str,
    config: str,
) -> pathlib.Path:
    """Write the configuration to a timestamped file.

    The filename pattern is ``<host>_YYYYmmdd_HHMMSS.cfg``.

    Args:
        output_dir: Directory where the file will be stored.
        host: Device identifier used for the filename.
        config: Configuration text to write.

    Returns:
        Path to the created file.
    """
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{host}_{timestamp}.cfg"
    output_path = output_dir / filename

    try:
        output_path.write_text(config, encoding="utf-8")
        logger.info("Configuration saved to %s", output_path)
    except OSError as exc:
        logger.exception("Failed to write configuration file.")
        raise RuntimeError(f"Unable to write file {output_path}: {exc}") from exc

    return output_path


def backup_device_config(
    host: str,
    username: str,
    password: Optional[str],
    ssh_key: Optional[pathlib.Path],
    port: int,
    command: str,
    output_dir: pathlib.Path,
) -> pathlib.Path:
    """High‑level orchestration function.

    Connects to the device, retrieves its configuration, and persists it.

    Returns:
        Path to the saved configuration file.
    """
    logger.info("Starting backup for device %s", host)

    ssh_cmd = build_ssh_command(host, username, port, ssh_key, command)
    config = run_ssh_command(ssh_cmd, password=password)

    if not config.strip():
        logger.warning("Received empty configuration from %s", host)
        raise RuntimeError("Empty configuration received; aborting backup.")

    saved_path = store_configuration(output_dir, host, config)
    logger.info("Backup completed successfully for %s", host)
    return saved_path


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def parse_arguments() -> argparse.Namespace:
    """Parse command‑line arguments."""
    parser = argparse.ArgumentParser(
        description="Backup running configuration from a network device via SSH.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--host",
        required=True,
        help="IP address or hostname of the target device.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="SSH username.",
    )
    auth_group = parser.add_mutually_exclusive_group()
    auth_group.add_argument(
        "--password",
        action="store_true",
        help="Prompt for an SSH password (non‑interactive authentication).",
    )
    auth_group.add_argument(
        "--ssh-key",
        type=pathlib.Path,
        help="Path to a private SSH key for key‑based authentication.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port number.",
    )
    parser.add_argument(
        "--command",
        default="show run",
        help="Command to retrieve the configuration on the remote device.",
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=pathlib.Path.cwd() / "backups",
        help="Directory where configuration files will be stored.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="SSH command timeout in seconds.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Logging verbosity.",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    """Entry point for the CLI."""
    args = parse_arguments()
    logger.setLevel(args.log_level)

    # Ensure output directory exists
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("Output directory ensured at %s", args.output_dir)
    except OSError as exc:
        logger.error("Failed to create output directory: %s", exc)
        return 1

    password = None
    if args.password:
        password = getpass.getpass(prompt="SSH Password: ")

    try:
        backup_device_config(
            host=args.host,
            username=args.username,
            password=password,
            ssh_key=args.ssh_key,
            port=args.port,
            command=args.command,
            output_dir=args.output_dir,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Backup failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())