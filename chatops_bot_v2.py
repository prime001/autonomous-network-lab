"""autonomous_network_tool.py

Utility script to execute arbitrary commands on network devices via SSH or Telnet.
It is intended for demonstration of real‑world network automation in a
ChatOps‑style environment.

Usage:
    python autonomous_network_tool.py --host 10.0.0.1 \\
        --user admin --command "show version" [--telnet] [options]

Prerequisites:
* Python 3.8+
* For SSH: the target device must accept key‑based authentication or the
  system's `ssh` client must be configured to allow password login.
* For Telnet: the device must have Telnet enabled.
* No third‑party Python packages are required; the script relies solely on
  the Python standard library.

The script logs its activity, validates inputs and handles common network
errors gracefully.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import shlex
import socket
import subprocess
import sys
import telnetlib
import time
from typing import List, Optional

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)


def configure_logging(verbose: bool) -> None:
    """Configure root logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --------------------------------------------------------------------------- #
# Telnet interaction
# --------------------------------------------------------------------------- #
def run_telnet(
    host: str,
    username: str,
    password: str,
    commands: List[str],
    timeout: int = 30,
) -> str:
    """
    Connect to a device via Telnet, authenticate, run commands, and collect
    the combined output.

    Args:
        host: Target IPv4/IPv6 address or hostname.
        username: Login name.
        password: Login password.
        commands: List of commands to execute (in order).
        timeout: Seconds to wait for network operations.

    Returns:
        Full output from all commands as a single string.

    Raises:
        ConnectionError: When Telnet connection cannot be established.
        RuntimeError: Authentication failed or command execution error.
    """
    logger.debug("Opening Telnet connection to %s", host)
    try:
        tn = telnetlib.Telnet(host, 23, timeout)
    except socket.error as exc:
        raise ConnectionError(f"Could not connect to {host} via Telnet: {exc}") from exc

    try:
        # Login sequence – handle multiple possible prompts.
        def expect_prompt(prompt: bytes) -> None:
            logger.debug("Waiting for prompt: %s", prompt.decode())
            tn.read_until(prompt, timeout)

        # Most devices use "Username:" / "login:" – try both.
        output = tn.read_until(b"login:", timeout)
        if b"login:" in output:
            tn.write(username.encode("ascii") + b"\n")
            expect_prompt(b"Password:")
        else:
            tn.read_until(b"Username:", timeout)
            tn.write(username.encode("ascii") + b"\n")
            tn.read_until(b"Password:", timeout)

        tn.write(password.encode("ascii") + b"\n")
        # Some devices show a prompt after login; wait a moment.
        time.sleep(1)
        # Flush any banner.
        tn.read_very_eager()

        # Enter enable mode if needed – optional.
        # tn.write(b"enable\n")
        # tn.write(b"\n")  # assuming no enable password.

        # Run each command.
        full_output = ""
        for cmd in commands:
            logger.info("Executing command: %s", cmd)
            tn.write(cmd.encode("ascii") + b"\n")
            # Allow some time for the device to process.
            time.sleep(0.5)
            cmd_output = tn.read_until(b"#", timeout)  # assume # prompt
            full_output += cmd_output.decode(errors="ignore")
        tn.write(b"exit\n")
        tn.close()
        return full_output
    except EOFError as exc:
        raise RuntimeError("Telnet session terminated unexpectedly") from exc
    except socket.timeout as exc:
        raise RuntimeError("Telnet operation timed out") from exc


# --------------------------------------------------------------------------- #
# SSH interaction (subprocess wrapper)
# --------------------------------------------------------------------------- #
def run_ssh(
    host: str,
    username: str,
    commands: List[str],
    timeout: int = 30,
) -> str:
    """
    Execute commands on a remote host via the system SSH client.

    Args:
        host: Target host.
        username: Login user (used as user@host).
        commands: List of commands to run (joined with ' ; ').
        timeout: Command timeout in seconds.

    Returns:
        Combined stdout and stderr from the SSH session.

    Raises:
        RuntimeError: When SSH command fails.
    """
    ssh_target = f"{username}@{host}"
    remote_cmd = " ; ".join(commands)
    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        ssh_target,
        remote_cmd,
    ]
    logger.debug("Running SSH command: %s", " ".join(shlex.quote(arg) for arg in ssh_cmd))

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"SSH execution failed: {exc}") from exc

    if result.returncode != 0:
        logger.warning("SSH returned non-zero exit status %s", result.returncode)
        logger.debug("SSH stderr: %s", result.stderr.strip())

    combined = result.stdout + result.stderr
    return combined


# --------------------------------------------------------------------------- #
# Argument handling
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command‑line arguments."""
    parser = argparse.ArgumentParser(
        description="Execute commands on a network device via SSH or Telnet."
    )
    parser.add_argument(
        "--host",
        required=True,
        help="IP address or hostname of the target device.",
    )
    parser.add_argument(
        "--user",
        required=True,
        help="Username for authentication.",
    )
    parser.add_argument(
        "--password",
        help="Password for authentication. If omitted, you will be prompted.",
    )
    parser.add_argument(
        "--command",
        action="append",
        required=True,
        help="Command to execute on the device. Can be specified multiple times.",
    )
    parser.add_argument(
        "--telnet",
        action="store_true",
        help="Use Telnet instead of SSH.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Network operation timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Increase output verbosity for debugging.",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    """Execute the script based on supplied CLI arguments."""
    args = parse_args()
    configure_logging(args.verbose)

    password = args.password
    if not password:
        password = getpass.getpass(prompt="Device password: ")

    try:
        if args.telnet:
            logger.info("Using Telnet to connect to %s", args.host)
            output = run_telnet(
                host=args.host,
                username=args.user,
                password=password,
                commands=args.command,
                timeout=args.timeout,
            )
        else:
            logger.info("Using SSH to connect to %s", args.host)
            output = run_ssh(
                host=args.host,
                username=args.user,
                commands=args.command,
                timeout=args.timeout,
            )
        print(output)
        logger.info("Command execution completed successfully.")
        return 0
    except (ConnectionError, RuntimeError) as exc:
        logger.error("Operation failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Execution interrupted by user.")
        return 1


if __name__ == "__main__":
    sys.exit(main())