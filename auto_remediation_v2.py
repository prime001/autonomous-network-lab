"""interface_remediator.py

Utility script to detect administratively down interfaces on a network device and
optionally bring them up.  It uses Paramiko for SSH interaction, argparse for
CLI handling, and the standard logging module.

Typical usage::

    python interface_remediator.py \\
        --host 10.0.1.10 \\
        --username admin \\
        --password secret \\
        --auto-up

The script connects to the device, runs ``show interfaces status`` (Cisco‑like
output), parses the result and logs any interfaces that are down.  If the
``--auto-up`` flag is provided, the script attempts to enable each down
interface using ``interface <name>`` followed by ``no shutdown``.

Prerequisites
-------------
- Python 3.8+
- Paramiko library (``pip install paramiko``)
- Network device that supports SSH and Cisco‑style ``show interfaces
  status`` output.

"""

import argparse
import getpass
import logging
import re
import sys
import time
from typing import List, Tuple

import paramiko
from paramiko.ssh_exception import SSHException, AuthenticationException

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S"
)
_console_handler.setFormatter(_formatter)
LOGGER.addHandler(_console_handler)


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #


def establish_ssh_connection(
    host: str, username: str, password: str, port: int = 22, timeout: int = 10
) -> paramiko.SSHClient:
    """Create and return an active Paramiko SSHClient.

    Args:
        host: Target device IP or hostname.
        username: SSH username.
        password: SSH password.
        port: SSH port, defaults to 22.
        timeout: Connection timeout in seconds.

    Raises:
        ConnectionError: If the SSH connection cannot be established.
    """
    LOGGER.info("Establishing SSH connection to %s:%s", host, port)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
        )
    except AuthenticationException as exc:
        raise ConnectionError("Authentication failed") from exc
    except SSHException as exc:
        raise ConnectionError(f"SSH error: {exc}") from exc
    except Exception as exc:
        raise ConnectionError(f"Unable to connect to {host}: {exc}") from exc

    LOGGER.debug("SSH connection established")
    return client


def run_command(
    ssh_client: paramiko.SSHClient, command: str, expect: str = "#", timeout: int = 30
) -> str:
    """Execute a command on the remote device and return its output.

    Args:
        ssh_client: An active Paramiko SSHClient.
        command: Command string to execute.
        expect: Prompt string that indicates command completion.
        timeout: Timeout for command execution.

    Returns:
        The command output as a string.

    Raises:
        RuntimeError: If the command does not finish within timeout.
    """
    LOGGER.debug("Running command: %s", command)
    channel = ssh_client.invoke_shell()
    time.sleep(0.5)  # Allow the remote shell to settle

    # Clear any pre‑existing data
    if channel.recv_ready():
        channel.recv(65535)

    channel.send(command + "\n")
    output = ""
    start_time = time.time()
    while True:
        if channel.recv_ready():
            recv = channel.recv(65535).decode("utf-8", errors="ignore")
            output += recv
            if expect in recv:
                break
        if time.time() - start_time > timeout:
            raise RuntimeError(f"Command '{command}' timed out")
        time.sleep(0.2)

    # Strip the sent command and the prompt from the output
    cleaned = re.sub(rf".*{re.escape(command)}\r?\n", "", output, flags=re.DOTALL)
    cleaned = re.sub(rf"{re.escape(expect)}.*", "", cleaned, flags=re.DOTALL)
    LOGGER.debug("Command output captured")
    return cleaned.strip()


def parse_down_interfaces(output: str) -> List[Tuple[str, str]]:
    """Parse ``show interfaces status`` output and return down interfaces.

    The Cisco IOS output format is assumed:

        Port    Name               Status       Vlan    Duplex  Speed Type
        Gi1/0/1                     notconnect   1       auto    auto 10/100/1000BaseTX
        Gi1/0/2                     connected    1       a-full a-100 10/100/1000BaseTX

    Only interfaces whose *Status* column contains ``disabled`` or
    ``notconnect`` are considered down.

    Args:
        output: Raw command output.

    Returns:
        List of tuples ``(interface, status)``.
    """
    down_intfs = []
    lines = output.splitlines()
    header_regex = re.compile(r"^Port\s+Name\s+Status", re.IGNORECASE)
    data_started = False
    for line in lines:
        if not data_started:
            if header_regex.search(line):
                data_started = True
            continue
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        iface, _, status = parts[0], parts[1], parts[2]
        if status.lower() in ("disabled", "notconnect"):
            down_intfs.append((iface, status))
    return down_intfs


def enable_interface(ssh_client: paramiko.SSHClient, interface: str) -> bool:
    """Attempt to enable an interface using ``no shutdown``.

    Returns:
        True if the command succeeded, False otherwise.
    """
    try:
        LOGGER.info("Enabling interface %s", interface)
        # Enter config mode, select interface, issue no shutdown
        commands = [
            "configure terminal",
            f"interface {interface}",
            "no shutdown",
            "end",
            "write memory",
        ]
        for cmd in commands:
            run_command(ssh_client, cmd)
        LOGGER.info("Interface %s enabled successfully", interface)
        return True
    except Exception as exc:  # Catch broad for any command failure
        LOGGER.error("Failed to enable %s: %s", interface, exc)
        return False


def remediate_interfaces(
    host: str,
    username: str,
    password: str,
    auto_up: bool = False,
    port: int = 22,
) -> None:
    """Main remediation workflow.

    Connects to the device, discovers down interfaces, logs them and optionally
    brings them up.

    Args:
        host: Device IP or hostname.
        username: SSH username.
        password: SSH password.
        auto_up: If True, attempt to enable discovered interfaces.
        port: SSH port.
    """
    try:
        ssh = establish_ssh_connection(host, username, password, port=port)
    except ConnectionError as exc:
        LOGGER.critical("Connection failed: %s", exc)
        sys.exit(1)

    try:
        raw_output = run_command(ssh, "show interfaces status")
        down_intfs = parse_down_interfaces(raw_output)

        if not down_intfs:
            LOGGER.info("No administratively down interfaces found on %s", host)
            return

        LOGGER.warning("Found %d down interface(s) on %s:", len(down_intfs), host)
        for iface, status in down_intfs:
            LOGGER.warning(" - %s (status: %s)", iface, status)

        if auto_up:
            for iface, _ in down_intfs:
                success = enable_interface(ssh, iface)
                if not success:
                    LOGGER.error("Could not remediate interface %s", iface)
        else:
            LOGGER.info(
                "Run script with '--auto-up' to automatically enable these interfaces."
            )
    finally:
        ssh.close()
        LOGGER.debug("SSH connection closed")


# --------------------------------------------------------------------------- #
# Command‑line interface
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    """Create and return the top‑level argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Detect and optionally remediate administratively down interfaces on a "
            "network device."
        )
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Device hostname or IP address",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="SSH username",
    )
    parser.add_argument(
        "--password",
        help="SSH password (will prompt if omitted)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port (default: 22)",
    )
    parser.add_argument(
        "--auto-up",
        action="store_true",
        help="Automatically issue 'no shutdown' on down interfaces",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug level logging",
    )
    return parser


def main() -> None:
    """Entry point for script execution."""
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.verbose:
        _console_handler.setLevel(logging.DEBUG)
        LOGGER.debug("Verbose logging enabled")

    password = args.password
    if not password:
        password = getpass.getpass(prompt="SSH Password: ")

    remediate_interfaces(
        host=args.host,
        username=args.username,
        password=password,
        auto_up=args.auto_up,
        port=args.port,
    )


if __name__ == "__main__":
    main()