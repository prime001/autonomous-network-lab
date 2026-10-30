"""auto_remediation.py

Command‑line utility that connects to a network device over SSH, verifies a
pre‑condition, and executes a remediation command if needed.  It is intended
as a simple, reusable building block for automated network operations
(e.g. interface shutdown recovery, ACL fix‑ups, NTP re‑sync).

Features
--------
* Uses only the Python standard library and *paramiko* for SSH.
* Rich logging (INFO for normal operation, DEBUG for raw command output,
  ERROR for failures).
* Fully typed functions with docstrings.
* ``argparse`` based CLI – device address, credentials, verification and
  remediation commands are supplied at runtime.
* Optional “dry‑run” mode to preview actions without applying changes.
* Graceful error handling – network/SSH errors are caught and reported.

Prerequisites
-------------
* Python 3.8+
* ``paramiko`` package (install with ``pip install paramiko``)
* SSH access to the target device (username/password or key‑based auth).

Example
-------
```
python auto_remediation.py \\
    --device 10.1.1.1 \\
    --username admin \\
    --check-cmd "show interface Gig0/1 | include line protocol" \\
    --expected "up" \\
    --remediate-cmd "interface Gig0/1\n no shutdown" \\
    --dry-run
```
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from getpass import getpass
from typing import Optional, Tuple

import paramiko
from paramiko.ssh_exception import SSHException, AuthenticationException

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
)
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Core helpers
# --------------------------------------------------------------------------- #
def establish_ssh_connection(
    hostname: str,
    username: str,
    password: Optional[str] = None,
    timeout: int = 30,
) -> paramiko.SSHClient:
    """Create and return an active paramiko SSHClient.

    Args:
        hostname: Destination host or IP address.
        username: SSH login name.
        password: Password for password‑based auth. If ``None`` key‑based auth
                  is attempted.
        timeout: Connection timeout in seconds.

    Raises:
        AuthenticationException: Wrong credentials.
        SSHException: General SSH error.
    """
    logger.debug("Establishing SSH connection to %s", hostname)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname,
            username=username,
            password=password,
            look_for_keys=True,
            allow_agent=True,
            timeout=timeout,
        )
    except AuthenticationException as exc:
        logger.error("Authentication failed for %s", hostname)
        raise
    except SSHException as exc:
        logger.error("SSH error while connecting to %s: %s", hostname, exc)
        raise
    logger.info("SSH connection established to %s", hostname)
    return client


def run_command(
    client: paramiko.SSHClient, command: str, timeout: int = 30
) -> Tuple[int, str, str]:
    """Execute a command on the remote device.

    Returns a tuple of (exit_status, stdout, stderr).

    Args:
        client: Active SSHClient.
        command: Command string (may contain newlines for multi‑line configs).
        timeout: Command execution timeout.

    Raises:
        SSHException: On transport errors.
    """
    logger.debug("Running command:\n%s", command)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        # Wait for command to finish
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore")
        err = stderr.read().decode(errors="ignore")
        logger.debug("Command exit status: %s", exit_status)
        logger.debug("STDOUT:\n%s", out)
        logger.debug("STDERR:\n%s", err)
        return exit_status, out, err
    except SSHException as exc:
        logger.error("Failed executing command: %s", exc)
        raise


def verify_precondition(
    client: paramiko.SSHClient,
    check_cmd: str,
    expected: str,
) -> bool:
    """Run *check_cmd* and compare its output to *expected*.

    Returns ``True`` when the condition is already satisfied.
    """
    logger.info("Verifying pre‑condition with command: %s", check_cmd)
    _, out, _ = run_command(client, check_cmd)
    # Normalise whitespace for comparison
    normalized_out = " ".join(out.strip().split())
    normalized_expected = " ".join(expected.strip().split())
    if normalized_expected in normalized_out:
        logger.info("Pre‑condition satisfied.")
        return True
    logger.warning("Pre‑condition NOT satisfied. Expected '%s' in output.", expected)
    return False


def apply_remediation(
    client: paramiko.SSHClient,
    remediate_cmd: str,
    dry_run: bool = False,
) -> bool:
    """Execute the remediation command (or log it if dry‑run)."""
    if dry_run:
        logger.info("[DRY‑RUN] Would execute remediation command:\n%s", remediate_cmd)
        return True

    logger.info("Applying remediation command.")
    exit_status, out, err = run_command(client, remediate_cmd)
    if exit_status == 0:
        logger.info("Remediation succeeded.")
        return True
    else:
        logger.error(
            "Remediation failed with exit status %s.\nSTDOUT:\n%s\nSTDERR:\n%s",
            exit_status,
            out,
            err,
        )
        return False


# --------------------------------------------------------------------------- #
# Main orchestration
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automated network remediation utility."
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
        "--check-cmd",
        required=True,
        help="Command that verifies the desired state (e.g. 'show interface...').",
    )
    parser.add_argument(
        "--expected",
        required=True,
        help="String that must appear in ``check-cmd`` output to indicate success.",
    )
    parser.add_argument(
        "--remediate-cmd",
        required=True,
        help="Command (or multiline string) to run when pre‑condition fails.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity.",
    )
    args = parser.parse_args()

    logger.setLevel(getattr(logging, args.log_level))

    password = args.password or getpass(prompt="SSH Password: ")

    try:
        client = establish_ssh_connection(
            hostname=args.device,
            username=args.username,
            password=password,
        )
        satisfied = verify_precondition(
            client, check_cmd=args.check_cmd, expected=args.expected
        )
        if not satisfied:
            success = apply_remediation(
                client, remediate_cmd=args.remediate_cmd, dry_run=args.dry_run
            )
            if not success:
                sys.exit(1)
            # Optional: re‑verify after remediation
            logger.info("Re‑checking pre‑condition after remediation.")
            time.sleep(2)  # give device a moment to apply changes
            if verify_precondition(
                client, check_cmd=args.check_cmd, expected=args.expected
            ):
                logger.info("Remediation validated successfully.")
            else:
                logger.error("Post‑remediation verification failed.")
                sys.exit(2)
        else:
            logger.info("No remediation required.")
    except (AuthenticationException, SSHException) as exc:
        logger.error("Connection failed: %s", exc)
        sys.exit(3)
    finally:
        try:
            client.close()
            logger.debug("SSH connection closed.")
        except Exception:
            pass


if __name__ == "__main__":
    main()