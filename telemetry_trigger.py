"""telemetry_trigger.py
Utility script that polls a network device for a telemetry value and optionally
executes a remediation command when a threshold is crossed.

Usage
-----
    python telemetry_trigger.py \\
        --device 10.0.0.1 \\
        --user admin \\
        [--key ~/.ssh/id_rsa] \\
        --query "show interfaces GigabitEthernet0/1 | include input rate" \\
        --parser "regex:(\\d+)\\s+bits/sec" \\
        --threshold 1000000 \\
        [--action "configure terminal; interface GigabitEthernet0/1; shutdown"] \\
        [--interval 30]

The script connects to the device using OpenSSH (via the standard ``ssh`` client)
and runs the supplied ``--query`` command. The output is processed by a simple
parser (currently only a regular‑expression parser is supported). If the numeric
value extracted from the output exceeds ``--threshold`` the optional ``--action``
command is sent to the device.

Prerequisites
-------------
* Python 3.8+
* OpenSSH client installed and reachable via the ``ssh`` command.
* Password‑less authentication (SSH key or ssh-agent) for the target device.
  When a key file is supplied via ``--key`` it will be passed to ``ssh`` with
  ``-i``. Password authentication is not implemented for security reasons.

The script is intentionally self‑contained and uses only the Python standard
library. It can be used as a building block for larger automation frameworks
or scheduled via cron/Task Scheduler.
"""

import argparse
import logging
import re
import shlex
import subprocess
import sys
import time
from typing import Callable, Optional, Tuple

# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s - %(message)s", "%Y-%m-%d %H:%M:%S"
)
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #
def run_ssh_command(
    device: str,
    user: str,
    command: str,
    key_file: Optional[str] = None,
    timeout: int = 30,
) -> str:
    """Execute a command on a remote device via SSH.

    Args:
        device: IP address or hostname of the target device.
        user: Username for SSH authentication.
        command: The command string to execute on the device.
        key_file: Optional path to a private key for key‑based auth.
        timeout: Seconds to wait for the command to complete.

    Returns:
        The stdout returned by the remote command.

    Raises:
        RuntimeError: If SSH exits with a non‑zero status.
    """
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"]
    if key_file:
        ssh_cmd.extend(["-i", key_file])
    ssh_cmd.append(f"{user}@{device}")
    ssh_cmd.append(command)

    logger.debug("Running SSH command: %s", " ".join(shlex.quote(arg) for arg in ssh_cmd))

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.SubprocessError as exc:
        raise RuntimeError(f"SSH execution failed: {exc}") from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"SSH command failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    logger.debug("SSH command output: %s", result.stdout.strip())
    return result.stdout.strip()


def build_parser(parser_def: str) -> Callable[[str], int]:
    """Create a callable that extracts an integer from command output.

    Supported parser definitions:
        regex:<pattern>
            The first capturing group must contain the integer value.

    Args:
        parser_def: Parser definition string.

    Returns:
        Callable that receives the raw command output and returns an integer.

    Raises:
        ValueError: If the parser definition is unknown or malformed.
    """
    if not parser_def.startswith("regex:"):
        raise ValueError("Only regex parser is supported, e.g. regex:(\\d+)")

    pattern = parser_def.split("regex:", 1)[1]
    regex = re.compile(pattern)

    def _parser(output: str) -> int:
        match = regex.search(output)
        if not match:
            raise ValueError(f"Regex pattern did not match any part of the output.")
        try:
            value = int(match.group(1))
            logger.debug("Parsed value %d from output.", value)
            return value
        except (IndexError, ValueError) as exc:
            raise ValueError("Failed to convert captured group to int.") from exc

    return _parser


# --------------------------------------------------------------------------- #
# Core class
# --------------------------------------------------------------------------- #
class TelemetryTrigger:
    """Polls a device for telemetry and optionally runs a remediation command."""

    def __init__(
        self,
        device: str,
        user: str,
        query: str,
        parser: Callable[[str], int],
        threshold: int,
        action: Optional[str] = None,
        key_file: Optional[str] = None,
        interval: int = 30,
    ):
        self.device = device
        self.user = user
        self.key_file = key_file
        self.query = query
        self.parser = parser
        self.threshold = threshold
        self.action = action
        self.interval = max(1, interval)

    def poll(self) -> Tuple[int, bool]:
        """Run the query, parse the value and decide if remediation is needed.

        Returns:
            Tuple of (extracted_value, trigger_fired)
        """
        try:
            raw_output = run_ssh_command(
                device=self.device,
                user=self.user,
                command=self.query,
                key_file=self.key_file,
            )
            value = self.parser(raw_output)
            trigger_fired = value > self.threshold
            logger.info(
                "Telemetry value=%d, threshold=%d, trigger=%s",
                value,
                self.threshold,
                "FIRE" if trigger_fired else "clear",
            )
            return value, trigger_fired
        except Exception as exc:
            logger.error("Failed to poll telemetry: %s", exc)
            raise

    def remediate(self) -> None:
        """Execute the remediation command on the device, if defined."""
        if not self.action:
            logger.info("No remediation action configured.")
            return

        logger.info("Executing remediation action on %s", self.device)
        try:
            # Many network OSes accept multi‑line commands separated by newline.
            # Quote the entire command string to let the remote shell interpret it.
            run_ssh_command(
                device=self.device,
                user=self.user,
                command=self.action,
                key_file=self.key_file,
            )
            logger.info("Remediation command completed successfully.")
        except Exception as exc:
            logger.error("Remediation failed: %s", exc)

    def run(self) -> None:
        """Main loop: poll at the configured interval and remediate when needed."""
        logger.info(
            "Starting telemetry monitor for %s (interval=%ds)", self.device, self.interval
        )
        try:
            while True:
                try:
                    _, trigger = self.poll()
                    if trigger:
                        self.remediate()
                except Exception:
                    # Errors already logged; continue loop.
                    pass
                time.sleep(self.interval)
        except KeyboardInterrupt:
            logger.info("Telemetry monitor interrupted by user; exiting.")
        except Exception as exc:
            logger.exception("Unexpected error: %s", exc)
            sys.exit(1)


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Telemetry‑trigger script for network devices."
    )
    parser.add_argument(
        "--device", required=True, help="IP address or hostname of the target device."
    )
    parser.add_argument(
        "--user", required=True, help="Username for SSH authentication."
    )
    parser.add_argument(
        "--key",
        dest="key_file",
        help="Path to private SSH key (if not using ssh-agent).",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Command to run on the device that yields telemetry data.",
    )
    parser.add_argument(
        "--parser",
        default="regex:(\\d+)",
        help=(
            "Parser definition. Currently only regex:<pattern> is supported. "
            "The first capture group must be an integer."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=int,
        required=True,
        help="Numeric threshold that triggers remediation when exceeded.",
    )
    parser.add_argument(
        "--action",
        help=(
            "Command (or semicolon‑separated commands) to execute when the "
            "threshold is crossed. If omitted, only logging occurs."
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    args = parse_args()
    logger.setLevel(getattr(logging, args.log_level.upper()))

    try:
        parser_func = build_parser(args.parser)
    except ValueError as exc:
        logger.error("Invalid parser definition: %s", exc)
        sys.exit(2)

    trigger = TelemetryTrigger(
        device=args.device,
        user=args.user,
        key_file=args.key_file,
        query=args.query,
        parser=parser_func,
        threshold=args.threshold,
        action=args.action,
        interval=args.interval,
    )
    trigger.run()


if __name__ == "__main__":
    main()