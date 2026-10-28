"""capacity_analyzer.py

A command‑line utility that connects to a network device via SSH,
retrieves interface statistics, and provides a simple capacity
planning recommendation.

Typical usage:
    python capacity_analyzer.py --host 10.0.0.1 --username admin
        [--password secret] [--command "show interfaces"] [--threshold 80]

Prerequisites:
    * Python 3.8+
    * netmiko (`pip install netmiko`)
    * A device that supports the supplied command (Cisco‑style CLI is
      assumed for parsing).

The script logs its progress to the console and to a file
`capacity_analyzer.log` in the current working directory.
"""

import argparse
import getpass
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from netmiko import ConnectHandler, NetMikoAuthenticationException, NetMikoTimeoutException

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOG_FILE = Path.cwd() / "capacity_analyzer.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #
def parse_interface_stats(output: str) -> List[Dict[str, str]]:
    """
    Parse a Cisco‑style 'show interfaces' output.

    Returns a list of dicts with keys:
        - name
        - input_rate (bits per second)
        - output_rate (bits per second)
        - bandwidth (bits per second)

    The parser looks for lines like:
        GigabitEthernet0/1 is up, line protocol is up
          ...
          5 minute input rate 123456 bits/sec, 789 packets/sec
          5 minute output rate 654321 bits/sec, 987 packets/sec
          (some other line) 1000000
    """
    interfaces = []
    current_int = {}
    # Regular expressions
    int_name_re = re.compile(r"^(\S+?) is .+$")
    rate_re = re.compile(
        r"^\s*5 minute (input|output) rate (\d+) bits/sec, \d+ packets/sec"
    )
    bandwidth_re = re.compile(r"^\s*BW (\d+) Kbit")
    for line in output.splitlines():
        line = line.rstrip()
        m_name = int_name_re.match(line)
        if m_name:
            if current_int:
                interfaces.append(current_int)
                current_int = {}
            current_int["name"] = m_name.group(1)
            continue
        m_rate = rate_re.match(line)
        if m_rate and current_int is not None:
            direction, rate = m_rate.groups()
            key = f"{direction}_rate"
            current_int[key] = int(rate)
            continue
        m_bw = bandwidth_re.match(line)
        if m_bw and current_int is not None:
            # Bandwidth in Kbit -> convert to bits
            current_int["bandwidth"] = int(m_bw.group(1)) * 1000
    if current_int:
        interfaces.append(current_int)
    return interfaces


def evaluate_capacity(
    interfaces: List[Dict[str, str]], threshold: int
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    Split interfaces into two groups:
        - safe: utilization below threshold
        - risky: utilization above threshold

    Utilization is calculated as max(input_rate, output_rate) / bandwidth * 100.
    Returns (safe, risky) lists.
    """
    safe = []
    risky = []
    for intf in interfaces:
        bw = intf.get("bandwidth")
        if not bw:
            # Skip interfaces without declared bandwidth
            continue
        in_rate = intf.get("input_rate", 0)
        out_rate = intf.get("output_rate", 0)
        util = max(in_rate, out_rate) / bw * 100
        intf["utilization"] = round(util, 2)
        if util >= threshold:
            risky.append(intf)
        else:
            safe.append(intf)
    return safe, risky


def generate_report(
    safe: List[Dict[str, str]],
    risky: List[Dict[str, str]],
    host: str,
    threshold: int,
) -> str:
    """
    Build a human‑readable report string.
    """
    lines = [
        f"Capacity Planning Report for {host}",
        f"Generated: {datetime.now().isoformat()}",
        f"Utilization threshold: {threshold}%\n",
    ]
    if risky:
        lines.append(f"⚠️  Interfaces exceeding {threshold}% utilization:")
        for intf in risky:
            lines.append(
                f"  - {intf['name']}: {intf['utilization']}% (in {intf.get('input_rate',0)} bps, out {intf.get('output_rate',0)} bps, bw {intf['bandwidth']} bps)"
            )
    else:
        lines.append("✅  No interfaces exceed the utilization threshold.\n")

    if safe:
        lines.append("\n✅  Interfaces within safe limits:")
        for intf in safe:
            lines.append(
                f"  - {intf['name']}: {intf['utilization']}% utilization"
            )
    else:
        lines.append("\n⚠️  No interfaces with usable bandwidth were detected.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main execution
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="SSH‑based capacity analyzer for network devices"
    )
    parser.add_argument(
        "--host",
        required=True,
        help="IP address or hostname of the target device",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Login username",
    )
    parser.add_argument(
        "--password",
        help="Login password (will prompt if omitted)",
    )
    parser.add_argument(
        "--command",
        default="show interfaces",
        help="CLI command to retrieve interface statistics (default: %(default)s)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=80,
        help="Utilization percentage threshold for warnings (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port (default: %(default)s)",
    )
    args = parser.parse_args()

    password = args.password or getpass.getpass(prompt="Device password: ")

    device_params = {
        "device_type": "cisco_ios",  # Common type; adjust as needed
        "host": args.host,
        "username": args.username,
        "password": password,
        "port": args.port,
        "fast_cli": False,
        "banner_timeout": 200,
        "session_timeout": 60,
    }

    logger.info("Connecting to %s:%s", args.host, args.port)
    try:
        with ConnectHandler(**device_params) as conn:
            logger.info("Connection established – executing command")
            output = conn.send_command(args.command, expect_string=r"#")
    except (NetMikoAuthenticationException, NetMikoTimeoutException) as exc:
        logger.error("Connection failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("Unexpected error while communicating with device")
        sys.exit(1)

    logger.info("Parsing command output")
    interfaces = parse_interface_stats(output)
    if not interfaces:
        logger.warning("No interface data could be parsed")
        sys.exit(1)

    safe, risky = evaluate_capacity(interfaces, args.threshold)

    report = generate_report(safe, risky, args.host, args.threshold)
    print("\n" + report)

    logger.info("Analysis complete – %d risky, %d safe interfaces", len(risky), len(safe))


if __name__ == "__main__":
    main()