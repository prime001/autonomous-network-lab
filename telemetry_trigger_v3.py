"""Telemetry Trigger Script
==========================

This utility continuously pulls telemetry data from a network device via SSH
and triggers an alert when a defined threshold is crossed.  It is meant as a
practical example of network automation using only the Python standard library.

Features
--------
* Periodic SSH command execution using :mod:`subprocess`.
* Simple numeric threshold evaluation.
* Optional HTTP webhook integration for alerting (uses :mod:`urllib`).
* Structured logging with configurable log level.
* Command‑line interface built with :mod:`argparse`.

Prerequisites
-------------
* Python 3.8+.
* SSH access to the target device (key‑based authentication recommended).
* The device must support the supplied command; JSON output is preferred
  (e.g. ``show interfaces | json``) but plain text is also accepted.

Usage
-----
``python telemetry_trigger.py \\
    --device 10.0.0.1 \\
    --user admin \\
    --identity-file ~/.ssh/id_rsa \\
    --command "show interfaces | json" \\
    --metric errors \\
    --threshold 100 \\
    --interval 30 \\
    --webhook-url https://example.com/alert``
"""

import argparse
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


LOGGER = logging.getLogger(__name__)


class TelemetryTrigger:
    """Core class handling telemetry collection and alerting."""

    def __init__(
        self,
        device: str,
        user: str,
        identity_file: Path,
        command: str,
        metric: str,
        threshold: float,
        interval: int,
        webhook_url: Optional[str] = None,
        ssh_options: Optional[Dict[str, str]] = None,
    ) -> None:
        self.device = device
        self.user = user
        self.identity_file = identity_file
        self.command = command
        self.metric = metric
        self.threshold = threshold
        self.interval = interval
        self.webhook_url = webhook_url
        self.ssh_options = ssh_options or {}

    def _build_ssh_command(self) -> list[str]:
        """Construct the SSH command to be executed via subprocess."""
        ssh_base = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-i", str(self.identity_file),
        ]

        for opt, val in self.ssh_options.items():
            ssh_base.extend([f"-o", f"{opt}={val}"])

        target = f"{self.user}@{self.device}"
        ssh_base.append(target)
        ssh_base.append(self.command)

        LOGGER.debug("SSH command: %s", " ".join(ssh_base))
        return ssh_base

    def fetch_telemetry(self) -> Any:
        """
        Execute the SSH command and return parsed telemetry data.

        Returns
        -------
        Any
            Parsed JSON object or raw text if JSON parsing fails.
        """
        cmd = self._build_ssh_command()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            LOGGER.error(
                "SSH command failed: %s (returncode=%s)", exc.stderr.strip(), exc.returncode
            )
            raise
        except subprocess.TimeoutExpired:
            LOGGER.error("SSH command timed out after %s seconds", 30)
            raise

        output = result.stdout.strip()
        LOGGER.debug("Raw device output: %s", output[:200])

        # Attempt to parse JSON; fall back to raw text.
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            LOGGER.info("Device output is not valid JSON; returning raw text")
            return output

    @staticmethod
    def _extract_metric(data: Any, metric_path: str) -> Optional[float]:
        """
        Retrieve a numeric metric from parsed telemetry.

        Parameters
        ----------
        data : Any
            JSON object or raw text.
        metric_path : str
            Dot‑separated path to the metric (e.g. ``interfaces.Gig0/1.errors``).

        Returns
        -------
        Optional[float]
            Metric value if found and convertible, otherwise ``None``.
        """
        if isinstance(data, dict):
            parts = metric_path.split(".")
            current: Any = data
            for part in parts:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return None
            try:
                return float(current)
            except (TypeError, ValueError):
                return None
        # Plain‑text fallback – simple regex for a numeric value.
        import re

        pattern = re.compile(rf"{re.escape(metric_path)}\s*[:=]\s*([0-9\.]+)", re.IGNORECASE)
        match = pattern.search(str(data))
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
        return None

    def _alert(self, value: float) -> None:
        """Log the alert and optionally post to a webhook."""
        timestamp = datetime.utcnow().isoformat()
        message = (
            f"[{timestamp}] ALERT: {self.metric} on {self.device} "
            f"is {value} (threshold={self.threshold})"
        )
        LOGGER.warning(message)

        if self.webhook_url:
            payload = json.dumps(
                {
                    "device": self.device,
                    "metric": self.metric,
                    "value": value,
                    "threshold": self.threshold,
                    "timestamp": timestamp,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    LOGGER.info("Webhook response: %s %s", resp.status, resp.reason)
            except urllib.error.URLError as exc:
                LOGGER.error("Failed to send webhook: %s", exc)

    def run(self) -> None:
        """Main loop – fetch telemetry at the configured interval."""
        LOGGER.info("Starting telemetry collection for %s", self.device)
        while True:
            try:
                data = self.fetch_telemetry()
                value = self._extract_metric(data, self.metric)
                if value is None:
                    LOGGER.debug("Metric %s not found in telemetry data", self.metric)
                elif value > self.threshold:
                    self._alert(value)
                else:
                    LOGGER.debug(
                        "Metric %s=%s within threshold %s",
                        self.metric,
                        value,
                        self.threshold,
                    )
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Error during telemetry cycle: %s", exc)

            time.sleep(self.interval)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously fetch telemetry via SSH and trigger alerts."
    )
    parser.add_argument("--device", required=True, help="IP or hostname of the target device")
    parser.add_argument("--user", required=True, help="SSH username")
    parser.add_argument(
        "--identity-file",
        type=Path,
        required=True,
        help="Path to the private key for SSH authentication",
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Command executed on the device to retrieve telemetry (pref. JSON output)",
    )
    parser.add_argument(
        "--metric",
        required=True,
        help="Dot‑separated path to the numeric metric within the telemetry output",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        required=True,
        help="Numeric threshold that triggers an alert",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--webhook-url",
        help="Optional HTTP endpoint for alert notifications",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO)",
    )
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional SSH options (e.g., ConnectTimeout=10)",
    )
    return parser.parse_args(argv)


def build_ssh_options(option_list: list[str]) -> Dict[str, str]:
    """Convert a list of ``KEY=VALUE`` strings to a dict."""
    opts: Dict[str, str] = {}
    for opt in option_list:
        if "=" not in opt:
            LOGGER.warning("Ignoring malformed SSH option: %s", opt)
            continue
        key, value = opt.split("=", 1)
        opts[key] = value
    return opts


def configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)

    if not args.identity_file.is_file():
        LOGGER.error("Identity file not found: %s", args.identity_file)
        sys.exit(1)

    ssh_opts = build_ssh_options(args.ssh_option)

    trigger = TelemetryTrigger(
        device=args.device,
        user=args.user,
        identity_file=args.identity_file,
        command=args.command,
        metric=args.metric,
        threshold=args.threshold,
        interval=args.interval,
        webhook_url=args.webhook_url,
        ssh_options=ssh_opts,
    )
    try:
        trigger.run()
    except KeyboardInterrupt:
        LOGGER.info("Telemetry trigger stopped by user")
    except Exception:  # pragma: no cover
        LOGGER.exception("Fatal error in telemetry trigger")
        sys.exit(1)


if __name__ == "__main__":
    main()