"""autonomous_intent_engine.py

A lightweight intent‑engine script that connects to a network device over SSH,
executes a list of commands (or an intent JSON file), and returns the output.
The script is written for demonstration purposes in a portfolio repository
and showcases real‑world network automation techniques using only the Python
standard library and **paramiko** for SSH connectivity.

Usage
-----
::

    python autonomous_intent_engine.py \\
        --host 10.0.0.1 \\
        --username admin \\
        --password secret \\
        --command "show version" \\
        --command "show ip interface brief"

Or with an intent file (JSON) containing a ``commands`` key:

::

    python autonomous_intent_engine.py \\
        --host 10.0.0.1 \\
        --username admin \\
        --intent-file intent.json

The JSON format is::

    {
        "commands": [
            "show version",
            "show running-config"
        ],
        "enable_password": "enable-secret"   # optional
    }

Prerequisites
-------------
* Python 3.8+
* paramiko (`pip install paramiko`)

The script logs detailed information to the console and exits with a non‑zero
status code on fatal errors.
"""

import argparse
import getpass
import json
import logging
import os
import sys
from typing import List, Optional, Dict

import paramiko
import socket

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
HANDLER = logging.StreamHandler()
FORMATTER = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
HANDLER.setFormatter(FORMATTER)
LOGGER.addHandler(HANDLER)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class DeviceConnectionError(Exception):
    """Raised when a connection to the device cannot be established."""


class CommandExecutionError(Exception):
    """Raised when a command fails to execute on the remote device."""


# --------------------------------------------------------------------------- #
# Core SSH session handling
# --------------------------------------------------------------------------- #
class DeviceSSH:
    """Encapsulates an SSH session to a network device using Paramiko."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        enable_password: Optional[str] = None,
        timeout: int = 10,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.enable_password = enable_password
        self.timeout = timeout
        self.client: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None

    def __enter__(self) -> "DeviceSSH":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ----------------------------------------------------------------------- #
    # Connection handling
    # ----------------------------------------------------------------------- #
    def connect(self) -> None:
        """Establish an SSH connection and, if required, enter enable mode."""
        LOGGER.debug("Attempting SSH connection to %s:%s", self.host, self.port)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                look_for_keys=False,
                allow_agent=False,
                timeout=self.timeout,
            )
        except (paramiko.SSHException, socket.error) as exc:
            raise DeviceConnectionError(str(exc)) from exc

        LOGGER.info("SSH connection established to %s", self.host)

        # If an enable password is supplied we need an interactive shell.
        if self.enable_password:
            self._enter_enable_mode()

    def _enter_enable_mode(self) -> None:
        """Open a shell channel and enter privileged exec (enable) mode."""
        LOGGER.debug("Opening interactive shell for enable mode")
        self.shell = self.client.invoke_shell()
        self._read_until_prompt()

        self._send_line("enable")
        self._read_until(b"Password:")
        self._send_line(self.enable_password)
        self._read_until_prompt()
        LOGGER.info("Entered enable mode on %s", self.host)

    # ----------------------------------------------------------------------- #
    # Command execution
    # ----------------------------------------------------------------------- #
    def exec_commands(self, commands: List[str]) -> Dict[str, str]:
        """
        Execute a list of commands.

        Returns
        -------
        dict
            Mapping of command string to its output (stdout+stderr).
        """
        results: Dict[str, str] = {}
        if self.shell:
            for cmd in commands:
                results[cmd] = self._exec_interactive(cmd)
        else:
            for cmd in commands:
                results[cmd] = self._exec_non_interactive(cmd)
        return results

    def _exec_non_interactive(self, command: str) -> str:
        """Execute a single command using exec_command."""
        LOGGER.debug("Executing (non‑interactive): %s", command)
        try:
            stdin, stdout, stderr = self.client.exec_command(
                command, timeout=self.timeout
            )
            exit_status = stdout.channel.recv_exit_status()
        except paramiko.SSHException as exc:
            raise CommandExecutionError(str(exc)) from exc

        out = stdout.read().decode()
        err = stderr.read().decode()
        if exit_status != 0:
            LOGGER.warning(
                "Command `%s` returned non‑zero status %s", command, exit_status
            )
        return out + err

    def _exec_interactive(self, command: str) -> str:
        """Execute a command inside an interactive shell."""
        LOGGER.debug("Executing (interactive): %s", command)
        self._send_line(command)
        output = self._read_until_prompt()
        return output

    # ----------------------------------------------------------------------- #
    # Helper methods for interactive shell
    # ----------------------------------------------------------------------- #
    def _send_line(self, line: str) -> None:
        """Send a line followed by newline to the shell."""
        if not self.shell:
            raise RuntimeError("Shell channel not initialized")
        self.shell.send(line + "\n")

    def _read_until(self, marker: bytes, timeout: int = 10) -> str:
        """Read from the shell until the marker bytes are seen."""
        if not self.shell:
            raise RuntimeError("Shell channel not initialized")
        buffer = b""
        self.shell.settimeout(timeout)
        while not buffer.strip().endswith(marker):
            try:
                chunk = self.shell.recv(1024)
                if not chunk:
                    break
                buffer += chunk
            except socket.timeout:
                break
        return buffer.decode(errors="ignore")

    def _read_until_prompt(self, timeout: int = 10) -> str:
        """Read until the device prompt (assumed to end with `#` or `>`)."""
        if not self.shell:
            raise RuntimeError("Shell channel not initialized")
        buffer = b""
        self.shell.settimeout(timeout)
        while True:
            try:
                chunk = self.shell.recv(1024)
                if not chunk:
                    break
                buffer += chunk
                if buffer.rstrip().endswith(b"#") or buffer.rstrip().endswith(b">"):
                    break
            except socket.timeout:
                break
        return buffer.decode(errors="ignore")

    # ----------------------------------------------------------------------- #
    # Cleanup
    # ----------------------------------------------------------------------- #
    def close(self) -> None:
        """Close the SSH client and any opened channels."""
        if self.shell:
            LOGGER.debug("Closing interactive shell")
            self.shell.close()
        if self.client:
            LOGGER.debug("Closing SSH client")
            self.client.close()
        LOGGER.info("SSH session to %s closed", self.host)


# --------------------------------------------------------------------------- #
# Argument parsing utilities
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Intent‑engine: run commands on a network device via SSH."
    )
    parser.add_argument(
        "--host", required=True, help="IP address or hostname of the device."
    )
    parser.add_argument(
        "--port", type=int, default=22, help="SSH port (default: 22)."
    )
    parser.add_argument(
        "--username", required=True, help="SSH username."
    )
    parser.add_argument(
        "--password",
        help="SSH password. If omitted, you will be prompted securely.",
    )
    parser.add_argument(
        "--enable-password",
        help="Enable (privileged exec) password, if the device requires it.",
    )
    parser.add_argument(
        "--command",
        action="append",
        dest="commands",
        help="Command to execute on the device. Can be used multiple times.",
    )
    parser.add_argument(
        "--intent-file",
        type=str,
        help="Path to a JSON file describing an intent (list of commands).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging verbosity.",
    )
    return parser.parse_args()


def load_intent_file(path: str) -> Dict[str, any]:
    """Load a JSON intent file and return its content."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Intent file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "commands" not in data:
        raise ValueError("Intent file must contain a top‑level 'commands' key.")
    return data


# --------------------------------------------------------------------------- #
# Main execution flow
# --------------------------------------------------------------------------- #
def main() -> None:
    args = parse_args()
    LOGGER.setLevel(getattr(logging, args.log_level))

    ssh_password = args.password or getpass.getpass(prompt="SSH Password: ")
    enable_password = args.enable_password

    # Build the command list
    command_list: List[str] = args.commands or []
    if args.intent_file:
        try:
            intent = load_intent_file(args.intent_file)
            intent_cmds = intent.get("commands", [])
            if not isinstance(intent_cmds, list):
                raise ValueError("'commands' must be a list.")
            command_list.extend(intent_cmds)
            if not enable_password:
                enable_password = intent.get("enable_password")
        except Exception as exc:
            LOGGER.error("Failed to load intent file: %s", exc)
            sys.exit(1)

    if not command_list:
        LOGGER.error("No commands supplied. Use --command or --intent-file.")
        sys.exit(1)

    LOGGER.info(
        "Connecting to %s as %s and executing %d command(s)",
        args.host,
        args.username,
        len(command_list),
    )
    try:
        with DeviceSSH(
            host=args.host,
            port=args.port,
            username=args.username,
            password=ssh_password,
            enable_password=enable_password,
        ) as session:
            results = session.exec_commands(command_list)
    except DeviceConnectionError as exc:
        LOGGER.error("Connection failed: %s", exc)
        sys.exit(1)
    except CommandExecutionError as exc:
        LOGGER.error("Command execution failed: %s", exc)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Unexpected error: %s", exc)
        sys.exit(1)

    # Output results
    for cmd, output in results.items():
        separator = f"\n--- Output for: {cmd} ---\n"
        print(separator + output.rstrip() + "\n")

    LOGGER.info("All commands executed successfully.")


if __name__ == "__main__":
    main()