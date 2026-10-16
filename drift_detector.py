"""drift_detector.py

Detect configuration drift between a network device's running configuration and a
saved baseline configuration.

Usage:
    python drift_detector.py --host 10.1.1.1 --username admin \\
        --baseline ./baselines/router1.cfg [--port 22] [--enable] [--verbose]

Prerequisites:
    * Python 3.8+
    * Paramiko library (`pip install paramiko`)
    * Devices must support SSH and standard CLI commands:
        - `terminal length 0`
        - `show running-config`
        - (optional) `enable` for privileged mode

The script connects via SSH, retrieves the running configuration, compares it
to a baseline file and prints a unified diff. Exit codes:
    0 – No drift detected (configs are identical)
    1 – Drift detected (differences found)
    2 – Connection / execution error
"""

import argparse
import difflib
import getpass
import logging
import os
import sys
import time

import paramiko


def parse_arguments() -> argparse.Namespace:
    """Parse command‑line arguments."""
    parser = argparse.ArgumentParser(
        description="Detect configuration drift on network devices."
    )
    parser.add_argument(
        "--host",
        required=True,
        help="IP address or hostname of the target device.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port (default: 22).",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="SSH username.",
    )
    parser.add_argument(
        "--password",
        help="SSH password (will prompt if omitted).",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Enter enable mode after login (requires enable password).",
    )
    parser.add_argument(
        "--enable-password",
        help="Enable password (will prompt if omitted and --enable is used).",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        help="Path to the baseline configuration file.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging output.",
    )
    return parser.parse_args()


def setup_logging(verbose: bool) -> None:
    """Configure the root logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def read_baseline(path: str) -> str:
    """Return the contents of the baseline configuration file."""
    if not os.path.isfile(path):
        logging.error("Baseline file not found: %s", path)
        sys.exit(2)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        logging.debug("Baseline file %s loaded (%d bytes).", path, len(content))
        return content
    except OSError as exc:
        logging.error("Failed to read baseline file: %s", exc)
        sys.exit(2)


def establish_ssh_connection(host: str, port: int, username: str, password: str) -> paramiko.SSHClient:
    """Create and return an active Paramiko SSH client."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        logging.info("Connecting to %s:%s as %s", host, port, username)
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
        )
        logging.info("SSH connection established.")
        return client
    except paramiko.AuthenticationException:
        logging.error("Authentication failed for %s.", username)
        sys.exit(2)
    except paramiko.SSHException as exc:
        logging.error("SSH error: %s", exc)
        sys.exit(2)
    except Exception as exc:
        logging.error("Unexpected error establishing SSH: %s", exc)
        sys.exit(2)


def invoke_shell_and_execute(
    client: paramiko.SSHClient,
    commands: list[str],
    enable_password: str | None = None,
) -> str:
    """
    Open an interactive shell, optionally enter enable mode, run commands,
    and return the combined output.
    """
    channel = client.invoke_shell()
    time.sleep(1)  # give the shell some time to start
    output = ""

    def _recv():
        """Receive all pending data from the channel."""
        buf = ""
        while channel.recv_ready():
            buf += channel.recv(4096).decode("utf-8", errors="ignore")
        return buf

    # Flush any banner
    output += _recv()

    # Send terminal length 0 to avoid pagination
    channel.send("terminal length 0\n")
    time.sleep(0.5)
    output += _recv()

    if enable_password is not None:
        # Enter enable mode
        channel.send("enable\n")
        time.sleep(0.5)
        output += _recv()
        channel.send(f"{enable_password}\n")
        time.sleep(0.5)
        output += _recv()

    for cmd in commands:
        channel.send(f"{cmd}\n")
        logging.debug("Sent command: %s", cmd)
        time.sleep(1)  # simple wait; could be improved with pattern matching
        output += _recv()

    # Gracefully close the channel
    channel.close()
    return output


def extract_config(raw_output: str) -> str:
    """
    Strip command echoes and prompts from raw SSH output to isolate the config.
    Works for typical Cisco‑like devices.
    """
    lines = raw_output.splitlines()
    config_lines = []
    capture = False
    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("show running-config"):
            capture = True
            continue
        if capture:
            # Detect end of config (usually a prompt like hostname#)
            if stripped.endswith("#") or stripped.endswith(">"):
                break
            config_lines.append(stripped)
    config = "\n".join(config_lines).strip() + "\n"
    logging.debug("Extracted running config (%d lines).", len(config_lines))
    return config


def get_running_config(
    host: str,
    port: int,
    username: str,
    password: str,
    enable: bool,
    enable_password: str | None,
) -> str:
    """Connect to the device and retrieve its running configuration."""
    client = establish_ssh_connection(host, port, username, password)
    try:
        raw = invoke_shell_and_execute(
            client,
            commands=["show running-config"],
            enable_password=enable_password if enable else None,
        )
        return extract_config(raw)
    finally:
        client.close()
        logging.info("SSH connection closed.")


def generate_diff(baseline: str, running: str, fromfile: str, tofile: str) -> str:
    """Return a unified diff string between baseline and running configs."""
    baseline_lines = baseline.splitlines(keepends=True)
    running_lines = running.splitlines(keepends=True)
    diff = difflib.unified_diff(
        baseline_lines,
        running_lines,
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
    )
    return "".join(diff)


def main():
    args = parse_arguments()
    setup_logging(args.verbose)

    password = args.password or getpass.getpass(prompt="SSH password: ")
    enable_password = None
    if args.enable:
        enable_password = args.enable_password or getpass.getpass(prompt="Enable password: ")

    baseline_cfg = read_baseline(args.baseline)
    running_cfg = get_running_config(
        host=args.host,
        port=args.port,
        username=args.username,
        password=password,
        enable=args.enable,
        enable_password=enable_password,
    )

    diff = generate_diff(
        baseline=baseline_cfg,
        running=running_cfg,
        fromfile=f"{args.host}_baseline",
        tofile=f"{args.host}_running",
    )

    if diff:
        print("Configuration drift detected:\n")
        print(diff)
        sys.exit(1)
    else:
        print("No drift detected – configurations match.")
        sys.exit(0)


if __name__ == "__main__":
    main()