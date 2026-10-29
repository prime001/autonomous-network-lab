"""config_drift_audit.py

Utility script to detect configuration drift on network devices by comparing the
running configuration against the startup configuration.

Usage:
    python config_drift_audit.py --host 10.0.0.1 --user admin --password secret
    python config_drift_audit.py -H 10.0.0.1 -u admin -p secret -e enablepw
    python config_drift_audit.py ... --ignore '^!$' '^no logging console$'

Prerequisites:
    * Python 3.8+
    * paramiko library (pip install paramiko)
    * SSH access to the target device with sufficient privileges to run
      "show running-config" and "show startup-config"
"""

import argparse
import difflib
import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import List, Tuple

import paramiko


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class DeviceConfig:
    """Container for device configuration sections."""
    running: List[str]
    startup: List[str]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def configure_logging(verbose: bool) -> None:
    """Configure root logger with console and file handlers."""
    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger()
    logger.setLevel(level)

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler
    fh = logging.FileHandler("config_drift_audit.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)


def build_ssh_client(host: str, username: str, password: str,
                     enable_password: str = None,
                     timeout: int = 30) -> paramiko.SSHClient:
    """Create and return a connected Paramiko SSH client."""
    logger = logging.getLogger("ssh")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        logger.debug("Connecting to %s", host)
        client.connect(
            hostname=host,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
        )
        logger.info("SSH connection established to %s", host)

        if enable_password:
            logger.debug("Attempting enable mode")
            channel = client.invoke_shell()
            time.sleep(0.5)
            channel.recv(65535)  # clean banner

            channel.send("enable\n")
            time.sleep(0.5)
            channel.recv(65535)

            channel.send(f"{enable_password}\n")
            time.sleep(0.5)
            output = channel.recv(65535).decode()
            if "Invalid" in output or "Password" in output:
                raise PermissionError("Enable password rejected")
            logger.info("Entered enable mode")
            channel.close()
    except paramiko.AuthenticationException as exc:
        logger.error("Authentication failed for %s: %s", host, exc)
        raise
    except paramiko.SSHException as exc:
        logger.error("SSH error for %s: %s", host, exc)
        raise
    return client


def execute_command(client: paramiko.SSHClient, command: str,
                    delay: float = 0.5) -> str:
    """Execute a command over an existing SSH client and return its output."""
    logger = logging.getLogger("ssh")
    try:
        logger.debug("Executing command: %s", command)
        transport = client.get_transport()
        channel = transport.open_session()
        channel.exec_command(command)

        # Some network devices need a short pause to start returning output
        time.sleep(delay)
        stdout = channel.makefile("r", -1).read()
        stderr = channel.makefile_stderr("r", -1).read()
        exit_status = channel.recv_exit_status()
        if exit_status != 0:
            logger.warning("Command exited with status %s. Stderr: %s",
                           exit_status, stderr.strip())
        logger.debug("Command output captured (%d bytes)", len(stdout))
        return stdout
    except Exception as exc:
        logger.error("Failed to execute command '%s': %s", command, exc)
        raise


def fetch_configs(client: paramiko.SSHClient,
                 ignore_patterns: List[re.Pattern] = None) -> DeviceConfig:
    """Retrieve running and startup configs, applying ignore regexes if supplied."""
    logger = logging.getLogger("fetch")
    running_raw = execute_command(client, "show running-config")
    startup_raw = execute_command(client, "show startup-config")

    def clean(config: str) -> List[str]:
        lines = config.splitlines()
        filtered = []
        for line in lines:
            stripped = line.rstrip()
            if ignore_patterns and any(p.search(stripped) for p in ignore_patterns):
                continue
            filtered.append(stripped)
        return filtered

    running = clean(running_raw)
    startup = clean(startup_raw)

    logger.info("Fetched running (%d lines) and startup (%d lines) configs",
                len(running), len(startup))
    return DeviceConfig(running=running, startup=startup)


def compute_diff(config: DeviceConfig) -> Tuple[str, bool]:
    """Return a unified diff string and a boolean indicating drift presence."""
    diff = difflib.unified_diff(
        config.startup,
        config.running,
        fromfile="startup-config",
        tofile="running-config",
        lineterm="",
    )
    diff_text = "\n".join(diff)
    drift = bool(diff_text.strip())
    return diff_text, drift


def parse_ignore_args(ignore_args: List[str]) -> List[re.Pattern]:
    """Compile ignore regex patterns supplied via CLI."""
    patterns = []
    for expr in ignore_args:
        try:
            patterns.append(re.compile(expr))
        except re.error as exc:
            logging.getLogger("argparse").error("Invalid regex '%s': %s", expr, exc)
            sys.exit(2)
    return patterns


# --------------------------------------------------------------------------- #
# Main execution
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect configuration drift on a network device."
    )
    parser.add_argument("-H", "--host", required=True, help="Device IP or hostname")
    parser.add_argument("-u", "--user", required=True, help="Login username")
    parser.add_argument("-p", "--password", required=True, help="Login password")
    parser.add_argument("-e", "--enable", help="Enable password (if required)")
    parser.add_argument(
        "-i",
        "--ignore",
        action="append",
        default=[],
        metavar="REGEX",
        help="Regular expression lines to ignore in diff (can be used multiple times)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "--show-diff",
        action="store_true",
        help="Print the unified diff to stdout when drift is detected",
    )
    args = parser.parse_args()
    configure_logging(args.verbose)

    logger = logging.getLogger("main")
    logger.debug("Parsed arguments: %s", args)

    ignore_patterns = parse_ignore_args(args.ignore)

    try:
        client = build_ssh_client(
            host=args.host,
            username=args.user,
            password=args.password,
            enable_password=args.enable,
        )
        device_cfg = fetch_configs(client, ignore_patterns=ignore_patterns)
        diff_text, drift = compute_diff(device_cfg)

        if drift:
            logger.warning("Configuration drift detected on %s", args.host)
            if args.show_diff:
                print("\n--- CONFIG DRIFT ---")
                print(diff_text)
                print("--- END OF DRIFT ---\n")
        else:
            logger.info("No drift detected on %s", args.host)
    except PermissionError as exc:
        logger.error("Privilege escalation failed: %s", exc)
        sys.exit(1)
    except (paramiko.SSHException, paramiko.AuthenticationException) as exc:
        logger.error("SSH failure: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        sys.exit(1)
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()