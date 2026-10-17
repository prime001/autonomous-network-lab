"""config_validator.py

Validate network device configuration against a baseline.

This script connects to a network device over SSH (key‑based authentication), retrieves the
running configuration, and compares it to a user‑provided baseline file.  The
differences are shown in unified diff format and can optionally be saved to a file.

Prerequisites
-------------
* Python 3.8+
* SSH access to the target device (public key authentication recommended)
* The device must understand the command ``show running-config`` (Cisco‑IOS style)

Usage
-----
    python config_validator.py \\
        --device 10.0.0.1 \\
        --username admin \\
        --baseline baseline.cfg \\
        [--output diff.txt] [--log-level INFO]

Arguments
---------
* ``--device``      : Hostname or IP address of the network device
* ``--username``    : SSH username
* ``--baseline``    : Path to the baseline configuration file
* ``--output``      : Optional file to write the diff output
* ``--log-level``   : Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
* ``--timeout``     : SSH command timeout in seconds (default: 30)

The script exits with code 0 if the configurations match, 1 if differences are
found, and 2 for execution errors.
"""

import argparse
import difflib
import json
import logging
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import List, Tuple

LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Configure the root logger."""
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {level}")
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    LOGGER.debug("Logging configured at %s level", level)


def parse_arguments() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Validate device config against a baseline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--device", required=True, help="Hostname or IP address of the device"
    )
    parser.add_argument(
        "--username", required=True, help="SSH username for device access"
    )
    parser.add_argument(
        "--baseline",
        required=True,
        type=Path,
        help="Path to the baseline configuration file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="File to write diff output (defaults to STDOUT)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="SSH command timeout in seconds",
    )
    return parser.parse_args()


def load_baseline(path: Path) -> List[str]:
    """Read the baseline file and return a list of lines."""
    LOGGER.debug("Loading baseline configuration from %s", path)
    if not path.is_file():
        raise FileNotFoundError(f"Baseline file not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        lines = [line.rstrip() for line in file.readlines()]
    LOGGER.info("Baseline configuration loaded (%d lines)", len(lines))
    return lines


def build_ssh_command(
    username: str, device: str, remote_cmd: str, timeout: int
) -> List[str]:
    """Create the ssh command list for subprocess."""
    ssh_opts = [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"ConnectTimeout={timeout}",
    ]
    full_cmd = [
        "ssh",
        *ssh_opts,
        f"{username}@{device}",
        remote_cmd,
    ]
    LOGGER.debug("SSH command built: %s", " ".join(shlex.quote(part) for part in full_cmd))
    return full_cmd


def fetch_running_config(
    username: str, device: str, timeout: int
) -> List[str]:
    """
    Retrieve the running configuration from the device.

    Returns a list of stripped lines.
    """
    remote_command = "show running-config"
    cmd = build_ssh_command(username, device, remote_command, timeout)

    try:
        LOGGER.info("Connecting to %s to fetch running-config", device)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        LOGGER.exception("Failed to execute SSH command")
        raise RuntimeError(f"SSH execution error: {exc}") from exc

    if result.returncode != 0:
        LOGGER.error(
            "SSH command failed (rc=%d): %s", result.returncode, result.stderr.strip()
        )
        raise RuntimeError(
            f"SSH command failed with exit code {result.returncode}: {result.stderr.strip()}"
        )

    config_lines = [line.rstrip() for line in result.stdout.splitlines()]
    LOGGER.info("Fetched running-config (%d lines)", len(config_lines))
    return config_lines


def compare_configs(
    baseline: List[str], running: List[str]
) -> Tuple[bool, str]:
    """
    Compare baseline and running configurations.

    Returns a tuple ``(is_match, diff_text)`` where ``is_match`` is ``True`` when
    the configurations are identical. ``diff_text`` contains a unified diff.
    """
    LOGGER.debug("Generating diff between baseline and running config")
    diff = difflib.unified_diff(
        baseline,
        running,
        fromfile="baseline",
        tofile="running",
        lineterm="",
    )
    diff_text = "\n".join(diff)
    is_match = diff_text == ""
    if is_match:
        LOGGER.info("Configurations match – no differences found")
    else:
        LOGGER.warning("Configuration drift detected")
    return is_match, diff_text


def write_output(diff: str, output_path: Path | None) -> None:
    """Write diff output to file or STDOUT."""
    if output_path:
        LOGGER.debug("Writing diff to %s", output_path)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("w", encoding="utf-8") as fh:
                fh.write(diff + "\n")
            LOGGER.info("Diff written to %s", output_path)
        except OSError as exc:
            LOGGER.exception("Failed to write diff file")
            raise RuntimeError(f"Unable to write diff to {output_path}: {exc}") from exc
    else:
        print(diff)


def main() -> int:
    """Main entry point."""
    args = parse_arguments()
    configure_logging(args.log_level)

    try:
        baseline_cfg = load_baseline(args.baseline)
        running_cfg = fetch_running_config(
            username=args.username, device=args.device, timeout=args.timeout
        )
        match, diff = compare_configs(baseline_cfg, running_cfg)

        if not match:
            write_output(diff, args.output)
            return 1  # drift detected
        else:
            print("✅ Device configuration matches the baseline.")
            return 0
    except FileNotFoundError as e:
        LOGGER.error(e)
        return 2
    except RuntimeError as e:
        LOGGER.error(e)
        return 2
    except KeyboardInterrupt:
        LOGGER.warning("Execution interrupted by user")
        return 2
    finally:
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())