"""capacity_analysis.py
====================

A lightweight capacity‑planning utility that connects to a network device via SSH,
retrieves interface statistics, and reports interfaces that exceed a configurable
utilization threshold.

Usage
-----
    python capacity_analysis.py --device 10.1.1.1 --username admin \
        [--password PASS] [--threshold 75] [--log-file plan.log]

If ``--password`` is omitted you will be prompted securely.

Prerequisites
-------------
* Python 3.8+
* ``paramiko`` library (install with ``pip install paramiko``)
* Network device must support standard Cisco‑like ``show interface`` output
  (the parser is deliberately simple and may need adjustments for other OSes).

The script uses only the Python standard library plus ``paramiko`` for SSH.
It logs actions to the console and optionally to a file, handling connection
errors and malformed output gracefully.
"""

import argparse
import getpass
import logging
import re
import sys
from typing import Dict, List, Tuple

import paramiko


# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
def setup_logging(log_file: str | None = None) -> None:
    """Configure root logger."""
    log_format = "%(asctime)s %(levelname)-8s %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=log_format, handlers=handlers)


# --------------------------------------------------------------------------- #
# SSH helper
# --------------------------------------------------------------------------- #
def ssh_execute(host: str, username: str, password: str, command: str) -> str:
    """
    Execute a single command on a remote host via SSH and return its output.

    Raises:
        paramiko.SSHException: For any SSH‑related error.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    logging.debug("Connecting to %s as %s", host, username)
    try:
        client.connect(hostname=host, username=username, password=password, timeout=10)
        stdin, stdout, stderr = client.exec_command(command)
        output = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")
        if err:
            logging.warning("Command produced stderr: %s", err.strip())
        return output
    finally:
        client.close()
        logging.debug("SSH connection to %s closed", host)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
INTERFACE_BLOCK_RE = re.compile(
    r"^(?P<if_name>\S+).*?Hardware is .+?, address is .+?,\n"
    r".*?MTU (?P<mtu>\d+) bytes, BW (?P<bw>\d+) Kbit.+?\n"
    r".*?input rate (?P<in_rate>\d+) bits/sec, output rate (?P<out_rate>\d+) bits/sec",
    re.MULTILINE | re.DOTALL,
)


def parse_interfaces(raw_output: str) -> List[Dict[str, int]]:
    """
    Extract interface metrics from ``show interface`` output.

    Returns a list of dictionaries with keys:
        - name
        - bandwidth_kbit
        - input_rate_bps
        - output_rate_bps
    """
    interfaces = []
    for match in INTERFACE_BLOCK_RE.finditer(raw_output):
        name = match.group("if_name")
        bandwidth = int(match.group("bw")) * 1000  # convert Kbit to bits
        in_rate = int(match.group("in_rate"))
        out_rate = int(match.group("out_rate"))
        interfaces.append(
            {
                "name": name,
                "bandwidth_bps": bandwidth,
                "input_rate_bps": in_rate,
                "output_rate_bps": out_rate,
            }
        )
    if not interfaces:
        logging.error("No interfaces could be parsed. Verify device output format.")
    return interfaces


def calculate_utilization(iface: Dict[str, int]) -> Tuple[float, float]:
    """
    Return input and output utilization percentages for a given interface.
    """
    bw = iface["bandwidth_bps"]
    if bw == 0:
        return 0.0, 0.0
    in_util = (iface["input_rate_bps"] / bw) * 100
    out_util = (iface["output_rate_bps"] / bw) * 100
    return in_util, out_util


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #
def evaluate_device(
    host: str,
    username: str,
    password: str,
    threshold: float,
    command: str = "show interface",
) -> None:
    """
    Connect to a device, retrieve interface data, and report any interfaces that
    exceed the utilization ``threshold`` (percentage).
    """
    logging.info("Collecting interface data from %s", host)
    try:
        raw = ssh_execute(host, username, password, command)
    except paramiko.SSHException as exc:
        logging.critical("SSH failure for %s: %s", host, exc)
        return

    interfaces = parse_interfaces(raw)
    if not interfaces:
        return

    overutilized: List[Tuple[str, float, float]] = []

    for iface in interfaces:
        in_util, out_util = calculate_utilization(iface)
        if in_util > threshold or out_util > threshold:
            overutilized.append((iface["name"], in_util, out_util))

    if overutilized:
        logging.warning("Interfaces exceeding %.1f%% utilization:", threshold)
        for name, in_u, out_u in overutilized:
            logging.warning(
                "  %s  Input: %.1f%%  Output: %.1f%%", name, in_u, out_u
            )
    else:
        logging.info("All interfaces are below the %.1f%% threshold.", threshold)


# --------------------------------------------------------------------------- #
# CLI handling
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Network device capacity planner – report high‑utilization interfaces."
    )
    parser.add_argument(
        "--device",
        required=True,
        help="IP address or hostname of the target device",
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
        "--threshold",
        type=float,
        default=80.0,
        help="Utilization percentage that triggers a warning (default: 80)",
    )
    parser.add_argument(
        "--command",
        default="show interface",
        help="Command to retrieve interface statistics (default: 'show interface')",
    )
    parser.add_argument(
        "--log-file",
        help="Optional file to write log output to",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG level logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.log_file)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    password = args.password or getpass.getpass(prompt="SSH Password: ")
    evaluate_device(
        host=args.device,
        username=args.username,
        password=password,
        threshold=args.threshold,
        command=args.command,
    )


if __name__ == "__main__":
    main()