"""autonomous_network_lab_auto_remediate.py
================================================

Utility script for **auto‑remediation** of basic Cisco‑like network devices.

The script connects to a device via SSH (using :pypi:`Paramiko`), inspects the
operational status of all physical interfaces and, if any are administratively
down, attempts to bring them up.  The tool can be run in a *check‑only* mode to
report problems without making changes, or in *remediate* mode to apply the fix.

Usage
-----

.. code-block:: console

    python auto_remediate.py \\
        --host 10.0.0.1 \\
        --username admin \\
        --password secret \\
        [--check-only | --remediate] \\
        [--log-level INFO]

Prerequisites
-------------

* Python 3.8+
* ``paramiko`` library (`pip install paramiko`)
* Network device must support standard Cisco‑style CLI commands:
  ``show ip interface brief`` and ``interface <name>`` / ``no shutdown``.
"""

import argparse
import logging
import re
import sys
import time
from typing import List, Tuple

import paramiko
from paramiko.ssh_exception import (
    AuthenticationException,
    SSHException,
    BadHostKeyException,
)


LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Configure root logger."""
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def ssh_connect(
    host: str, username: str, password: str, timeout: int = 10
) -> paramiko.SSHClient:
    """
    Establish an SSH connection using Paramiko.

    Args:
        host: Device hostname or IP address.
        username: Login name.
        password: Login password.
        timeout: Connection timeout in seconds.

    Returns:
        An active ``paramiko.SSHClient`` instance.

    Raises:
        RuntimeError: On any connection/authentication failure.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        LOGGER.debug("Connecting to %s:%s", host, timeout)
        client.connect(
            hostname=host,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
        )
        LOGGER.info("SSH connection established to %s", host)
        return client
    except BadHostKeyException as exc:
        raise RuntimeError(f"Bad host key for {host}: {exc}") from exc
    except AuthenticationException as exc:
        raise RuntimeError(f"Authentication failed for {host}: {exc}") from exc
    except SSHException as exc:
        raise RuntimeError(f"SSH error while connecting to {host}: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Unexpected error connecting to {host}: {exc}") from exc


def _invoke_shell(client: paramiko.SSHClient) -> paramiko.Channel:
    """Open an interactive shell channel."""
    chan = client.invoke_shell()
    # Wait for the prompt
    time.sleep(0.5)
    chan.recv(65535)  # flush
    return chan


def _send_command(chan: paramiko.Channel, command: str, wait: float = 0.5) -> str:
    """Send a command and return its output."""
    LOGGER.debug("Sending command: %s", command.strip())
    chan.send(command + "\n")
    time.sleep(wait)
    output = b""
    while chan.recv_ready():
        output += chan.recv(65535)
        time.sleep(0.1)
    return output.decode(errors="ignore")


def get_interface_status(chan: paramiko.Channel) -> str:
    """
    Retrieve ``show ip interface brief`` output.

    Returns:
        Raw command output as a string.
    """
    return _send_command(chan, "show ip interface brief")


def parse_down_interfaces(output: str) -> List[Tuple[str, str]]:
    """
    Parse the ``show ip interface brief`` output.

    Returns:
        A list of tuples ``(interface, status)`` for interfaces that are
        ``down`` (administratively or operationally).
    """
    down_intfs = []
    # Typical header: Interface   IP-Address  OK? Method Status       Protocol
    for line in output.splitlines():
        if not line or line.startswith("Interface"):
            continue
        parts = re.split(r"\s+", line.strip())
        if len(parts) < 6:
            continue
        iface, _, _, _, status, protocol = parts[:6]
        if status.lower() != "up" or protocol.lower() != "up":
            down_intfs.append((iface, status))
    LOGGER.debug("Parsed down interfaces: %s", down_intfs)
    return down_intfs


def remediate_interface(chan: paramiko.Channel, interface: str) -> str:
    """
    Attempt to bring the interface up with ``no shutdown``.

    Returns:
        The device output of the remediation commands.
    """
    cmds = [
        f"configure terminal",
        f"interface {interface}",
        "no shutdown",
        "end",
        "write memory",
    ]
    output = ""
    for cmd in cmds:
        out = _send_command(chan, cmd)
        output += out
        time.sleep(0.2)
    LOGGER.info("Remediation commands executed for %s", interface)
    return output


def check_and_remediate(
    host: str,
    username: str,
    password: str,
    remediate: bool = False,
) -> None:
    """
    Core workflow: connect, check interfaces, optionally remediate.

    Args:
        host: Device address.
        username: Login name.
        password: Login password.
        remediate: If True, attempt to fix down interfaces.
    """
    client = None
    try:
        client = ssh_connect(host, username, password)
        chan = _invoke_shell(client)

        raw_status = get_interface_status(chan)
        down_intfs = parse_down_interfaces(raw_status)

        if not down_intfs:
            LOGGER.info("All interfaces are up on %s", host)
            return

        LOGGER.warning(
            "Detected %d down interface(s) on %s: %s",
            len(down_intfs),
            host,
            ", ".join(i for i, _ in down_intfs),
        )
        if not remediate:
            LOGGER.info("Run with --remediate to attempt fixing.")
            return

        for iface, _ in down_intfs:
            LOGGER.info("Attempting remediation on interface %s", iface)
            remediation_output = remediate_interface(chan, iface)
            LOGGER.debug("Remediation output for %s:\n%s", iface, remediation_output)

        # Verify again
        final_status = get_interface_status(chan)
        still_down = parse_down_interfaces(final_status)
        if not still_down:
            LOGGER.info("All interfaces are now up on %s after remediation.", host)
        else:
            LOGGER.error(
                "Remediation incomplete; still down: %s",
                ", ".join(i for i, _ in still_down),
            )
    except RuntimeError as exc:
        LOGGER.error("Operation failed: %s", exc)
    finally:
        if client:
            client.close()
            LOGGER.debug("SSH connection closed.")


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Auto‑remediation tool for Cisco‑style devices."
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Device hostname or IP address.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Login username.",
    )
    parser.add_argument(
        "--password",
        required=True,
        help="Login password.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="Only report down interfaces, do not modify the device.",
    )
    mode.add_argument(
        "--remediate",
        action="store_true",
        help="Attempt to bring down interfaces up.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity.",
    )
    return parser


def main() -> None:
    """Entry point."""
    parser = build_arg_parser()
    args = parser.parse_args()

    configure_logging(args.log_level)

    LOGGER.debug("Arguments parsed: %s", args)

    check_and_remediate(
        host=args.host,
        username=args.username,
        password=args.password,
        remediate=args.remediate,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOGGER.error("Script interrupted by user.")
        sys.exit(1)