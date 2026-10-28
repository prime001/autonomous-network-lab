"""network_event_handler.py

A lightweight event‑handler that executes an arbitrary command on a network
device over SSH using only the Python standard library.

The script is intended for automation pipelines, CI/CD jobs or ad‑hoc
network operations where pulling in heavy third‑party automation frameworks
is unnecessary.

Features
--------
* Invoke a single command (or a list of commands) on a remote device via SSH.
* Supports password authentication; the password can be supplied on the
  command line or entered securely at runtime.
* Optional per‑command timeout and exit‑code validation.
* Detailed structured logging (INFO, WARNING, ERROR) to stdout and an optional
  log file.
* Returns the command output on success; on failure a comprehensive exception
  is raised and logged.

Prerequisites
-------------
* Python 3.8+.
* An SSH client available on the execution host (the script relies on the
  system ``ssh`` binary via :pyfunc:`subprocess.run`).
* Network device reachable over TCP/22 and credentials with sufficient
  privilege to run the requested commands.

Usage
-----
::

    python network_event_handler.py \\
        --device 10.0.0.1 \\
        --username admin \\
        --command "show version" \\
        [--password secret] \\
        [--timeout 30] \\
        [--log-file handler.log]

If ``--password`` is omitted the script will prompt securely.

"""

import argparse
import getpass
import logging
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


LOGGER = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO, logfile: Optional[Path] = None) -> None:
    """Configure root logger with console and optional file handlers."""
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)

    handlers = [stream_handler]

    if logfile:
        file_handler = logging.FileHandler(logfile, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers)


def build_ssh_command(
    device: str,
    username: str,
    command: str,
    timeout: int,
    password: Optional[str] = None,
) -> List[str]:
    """
    Construct the ``ssh`` command line.

    ``ssh`` is invoked with a minimal set of options for automation:
    * ``-o StrictHostKeyChecking=no`` – auto‑accept host keys.
    * ``-o UserKnownHostsFile=/dev/null`` – do not record the host.
    * ``-o LogLevel=ERROR`` – silence non‑essential output.
    * ``-o ConnectTimeout`` – enforce network timeout.
    * ``-o BatchMode=no`` – allow password prompt when needed.

    The password, when supplied, is passed via ``sshpass`` if it exists on the
    system. The function falls back to interactive password entry when
    ``sshpass`` is unavailable.

    Returns
    -------
    List[str]
        Tokenised command suitable for ``subprocess.run``.
    """
    base_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", f"ConnectTimeout={timeout}",
        f"{username}@{device}",
        command,
    ]

    if password:
        # Prefer sshpass for non‑interactive password injection.
        sshpass_path = shutil.which("sshpass")
        if sshpass_path:
            return [
                sshpass_path,
                "-p", password,
                *base_cmd,
            ]
        # If sshpass missing we rely on the user to have an SSH key or to
        # manually enter the password when prompted.
        LOGGER.warning(
            "sshpass not found; falling back to interactive password entry."
        )

    return base_cmd


def run_ssh_command(
    device: str,
    username: str,
    command: str,
    password: Optional[str] = None,
    timeout: int = 30,
) -> str:
    """
    Execute a command on a remote device via SSH.

    Parameters
    ----------
    device : str
        IP address or hostname of the target device.
    username : str
        SSH login name.
    command : str
        The command string to be executed on the remote host.
    password : str, optional
        Password for SSH authentication. If ``None`` the system will prompt.
    timeout : int, default 30
        Maximum seconds to wait for the SSH session to complete.

    Returns
    -------
    str
        Standard output from the remote command, stripped of trailing whitespace.

    Raises
    ------
    RuntimeError
        If the SSH process exits with a non‑zero return code or times out.
    """
    LOGGER.info("Running command on %s: %s", device, command)
    ssh_cmd = build_ssh_command(device, username, command, timeout, password)

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,  # a small buffer above SSH's own timeout
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        LOGGER.error("SSH command timed out after %s seconds", timeout)
        raise RuntimeError(f"Command timed out: {exc}") from exc
    except Exception as exc:
        LOGGER.exception("Unexpected error while executing SSH command")
        raise RuntimeError(f"SSH execution failed: {exc}") from exc

    if result.returncode != 0:
        LOGGER.error(
            "SSH command failed (rc=%s). Stderr: %s",
            result.returncode,
            result.stderr.strip(),
        )
        raise RuntimeError(
            f"SSH command failed with rc={result.returncode}: {result.stderr.strip()}"
        )

    LOGGER.debug("SSH command succeeded. Output: %s", result.stdout.strip())
    return result.stdout.strip()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Execute a single command on a network device via SSH.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--device",
        required=True,
        help="Hostname or IP address of the target device.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="SSH username.",
    )
    parser.add_argument(
        "--password",
        help="SSH password. If omitted, the script will prompt securely.",
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Command string to execute on the remote device. "
        "Enclose in quotes if it contains spaces.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Connection and command execution timeout in seconds.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Optional path to a file where logs will be written.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        help="Write the command output to the specified file instead of stdout.",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point for the script."""
    args = parse_args()
    configure_logging(
        level=getattr(logging, args.log_level.upper()),
        logfile=args.log_file,
    )

    password = args.password
    if password is None:
        password = getpass.getpass(prompt="SSH Password: ")

    try:
        output = run_ssh_command(
            device=args.device,
            username=args.username,
            command=args.command,
            password=password,
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        LOGGER.critical("Command execution failed: %s", exc)
        sys.exit(1)

    if args.output_file:
        try:
            args.output_file.write_text(output + "\n", encoding="utf-8")
            LOGGER.info("Output written to %s", args.output_file)
        except IOError as exc:
            LOGGER.error("Failed to write output to %s: %s", args.output_file, exc)
            sys.exit(1)
    else:
        print(output)


if __name__ == "__main__":
    main()