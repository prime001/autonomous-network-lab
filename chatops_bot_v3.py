"""
autonomous_network_ssh.py

A lightweight command‑line utility for executing arbitrary commands on network
devices over SSH using only the Python standard library.  The script spawns the
system ``ssh`` client via ``subprocess.run`` so it works without third‑party
dependencies such as paramiko or netmiko.

Typical usage::

    python autonomous_network_ssh.py \\
        --device 10.0.0.1 \\
        --user admin \\
        --command "show version" \\
        --timeout 30

Prerequisites
-------------
* The host running the script must have the OpenSSH client installed and
  accessible via the ``ssh`` executable in the PATH.
* Password‑less (key‑based) authentication is recommended.  If a password is
  supplied, the script will attempt to use ``sshpass`` if it is available.
* Network device must accept SSH connections on port 22 (or the port supplied
  with ``--port``).

Features
--------
* Argument parsing with ``argparse`` (device, user, command, port, timeout,
  optional password).
* Detailed logging to STDOUT (INFO) and a rotating file handler (DEBUG).
* Robust error handling – returns a non‑zero exit code on failure and prints
  the cause to the log.
* Optional retry logic for transient connectivity problems.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOG_DIR = Path.cwd() / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGER = logging.getLogger("autonomous_network_ssh")
LOGGER.setLevel(logging.DEBUG)

# Console handler (INFO+)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_console_handler.setFormatter(_console_formatter)

# File handler (DEBUG)
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "autonomous_network_ssh.log", maxBytes=1_048_576, backupCount=5
)
_file_handler.setLevel(logging.DEBUG)
_file_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s"
)
_file_handler.setFormatter(_file_formatter)

LOGGER.addHandler(_console_handler)
LOGGER.addHandler(_file_handler)


# --------------------------------------------------------------------------- #
# Core functionality
# --------------------------------------------------------------------------- #
class SSHExecutor:
    """
    Executes a command on a remote device using the local ``ssh`` binary.
    If ``password`` is provided, the function will try to locate ``sshpass``
    and prepend it to the command line.
    """

    def __init__(
        self,
        device: str,
        username: str,
        password: Optional[str] = None,
        port: int = 22,
        timeout: int = 30,
        retries: int = 0,
        retry_delay: int = 5,
    ) -> None:
        self.device = device
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay

    def _build_ssh_command(self, remote_cmd: str) -> List[str]:
        """Return the command list to be passed to ``subprocess.run``."""
        base_cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",  # fail fast if key auth is not possible
            "-o",
            "ConnectTimeout={}".format(self.timeout),
            "-p",
            str(self.port),
            f"{self.username}@{self.device}",
            remote_cmd,
        ]

        if self.password:
            sshpass_path = shutil.which("sshpass")
            if not sshpass_path:
                raise FileNotFoundError(
                    "Password supplied but 'sshpass' is not installed or not in PATH."
                )
            # sshpass -p 'password' ssh ...
            base_cmd = [
                sshpass_path,
                "-p",
                self.password,
                *base_cmd,
            ]

        LOGGER.debug("Built SSH command: %s", " ".join(base_cmd))
        return base_cmd

    def execute(self, remote_cmd: str) -> subprocess.CompletedProcess:
        """
        Run ``remote_cmd`` on the device.  Retries are performed according to the
        ``retries`` attribute.

        Returns the ``CompletedProcess`` instance from ``subprocess.run``.
        """
        attempt = 0
        while True:
            attempt += 1
            LOGGER.info(
                "Attempt %d/%d: executing command on %s", attempt, self.retries + 1, self.device
            )
            cmd = self._build_ssh_command(remote_cmd)

            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout + 10,  # buffer for SSH overhead
                )
                LOGGER.debug("SSH stdout: %s", completed.stdout.strip())
                LOGGER.debug("SSH stderr: %s", completed.stderr.strip())
                if completed.returncode == 0:
                    LOGGER.info("Command executed successfully.")
                    return completed
                else:
                    LOGGER.warning(
                        "Command returned non‑zero exit status %d.", completed.returncode
                    )
            except subprocess.TimeoutExpired:
                LOGGER.error("SSH command timed out after %d seconds.", self.timeout + 10)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Unexpected error while executing SSH: %s", exc)

            if attempt > self.retries:
                break
            LOGGER.info(
                "Retrying in %d seconds... (remaining attempts: %d)",
                self.retry_delay,
                self.retries - (attempt - 1),
            )
            time.sleep(self.retry_delay)

        raise RuntimeError(
            f"Failed to execute command on {self.device} after {attempt} attempts."
        )


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute a remote command over SSH on a network device."
    )
    parser.add_argument(
        "--device",
        required=True,
        help="IP address or hostname of the target device.",
    )
    parser.add_argument(
        "--user",
        required=True,
        dest="username",
        help="Username for SSH authentication.",
    )
    parser.add_argument(
        "--password",
        help="Password for SSH authentication (requires 'sshpass' installed).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port number (default: 22).",
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Command string to run on the remote device. Enclose in quotes.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Connection timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        help="Number of retry attempts on failure (default: 0).",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=5,
        dest="retry_delay",
        help="Delay between retries in seconds (default: 5).",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Set the console log level (default: INFO).",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Main execution flow
# --------------------------------------------------------------------------- #
def main() -> int:
    args = parse_arguments()

    # Adjust console log level according to user preference
    _console_handler.setLevel(getattr(logging, args.log_level))

    LOGGER.info("Starting SSH command execution.")
    LOGGER.debug("Parsed arguments: %s", args)

    executor = SSHExecutor(
        device=args.device,
        username=args.username,
        password=args.password,
        port=args.port,
        timeout=args.timeout,
        retries=args.retries,
        retry_delay=args.retry_delay,
    )

    try:
        result = executor.execute(args.command)
        # Print the remote command output to STDOUT for easy consumption.
        print(result.stdout.strip())
        return 0
    except Exception as exc:  # pragma: no cover
        LOGGER.error("Execution failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())