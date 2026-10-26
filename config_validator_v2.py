"""autonomous_network_config_validator.py

A lightweight, pure‑stdlib utility to fetch a network device's running
configuration via SSH and validate it against a set of simple policy
rules.

Features
--------
* Connects to a device using the local ``ssh`` binary (key‑based auth).
* Retrieves the running configuration (or any command) in a non‑interactive
  manner.
* Validates the configuration using regular‑expression based rules
  supplied in a text file.
* Reports pass/fail summary and optionally prints detailed mismatches.
* Command‑line interface built with :pymod:`argparse`.
* Structured logging with :pymod:`logging`.

Prerequisites
-------------
* Python 3.8+.
* An ``ssh`` client available in the system ``PATH``.
* The target device must allow key‑based SSH access for the provided user.
* A rules file where each line is either:
      allow:<regex>   – the line *must* match the regex.
      deny:<regex>    – the line *must not* match the regex.
  Blank lines and lines starting with ``#`` are ignored.

Typical usage
-------------
    $ python autonomous_network_config_validator.py \\
        --host 10.0.1.10 \\
        --user admin \\
        --command "show running-config" \\
        --rules policies.txt \\
        --log-level INFO
"""

import argparse
import logging
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple


def build_logger(level: str) -> logging.Logger:
    """Create and configure a module‑level logger."""
    logger = logging.getLogger("config_validator")
    logger.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s – %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.handlers.clear()
    logger.addHandler(handler)
    return logger


def run_ssh_command(
    host: str,
    user: str,
    remote_cmd: str,
    ssh_key: Path | None = None,
    timeout: int = 30,
) -> str:
    """
    Execute ``remote_cmd`` on ``host`` via the local ``ssh`` client.

    Parameters
    ----------
    host: str
        Destination host or IP address.
    user: str
        Username for SSH.
    remote_cmd: str
        Command to be executed on the remote device.
    ssh_key: pathlib.Path | None
        Path to a private key file. If ``None`` the default ssh key is used.
    timeout: int
        Seconds to wait before aborting.

    Returns
    -------
    str
        Standard output of the remote command.

    Raises
    ------
    RuntimeError
        If ssh exits with a non‑zero status or times out.
    """
    ssh_parts = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if ssh_key:
        ssh_parts.extend(["-i", str(ssh_key)])
    ssh_parts.append(f"{user}@{host}")

    # Use shlex.split to safely handle complex commands.
    ssh_parts.append(remote_cmd)

    cmd_str = " ".join(shlex.quote(part) for part in ssh_parts)
    logger.debug("Executing SSH command: %s", cmd_str)

    try:
        completed = subprocess.run(
            ssh_parts,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"SSH command timed out after {timeout}s") from exc

    if completed.returncode != 0:
        err = completed.stderr.strip() or "unknown error"
        raise RuntimeError(f"SSH command failed: {err}")

    return completed.stdout


def load_rules(rules_path: Path) -> Tuple[List[re.Pattern], List[re.Pattern]]:
    """
    Parse a rules file into allow and deny regular‑expression lists.

    Parameters
    ----------
    rules_path: pathlib.Path
        Path to the rules file.

    Returns
    -------
    Tuple[List[re.Pattern], List[re.Pattern]]
        ``(allow_patterns, deny_patterns)``

    Raises
    ------
    ValueError
        If a line cannot be parsed.
    """
    allow_patterns: List[re.Pattern] = []
    deny_patterns: List[re.Pattern] = []

    with rules_path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            if line.lower().startswith("allow:"):
                pattern_str = line.split(":", 1)[1].strip()
                try:
                    allow_patterns.append(re.compile(pattern_str))
                except re.error as exc:
                    raise ValueError(
                        f"Invalid allow regex on line {line_no}: {pattern_str}"
                    ) from exc
            elif line.lower().startswith("deny:"):
                pattern_str = line.split(":", 1)[1].strip()
                try:
                    deny_patterns.append(re.compile(pattern_str))
                except re.error as exc:
                    raise ValueError(
                        f"Invalid deny regex on line {line_no}: {pattern_str}"
                    ) from exc
            else:
                raise ValueError(f"Unrecognized rule on line {line_no}: {raw}")

    logger.debug(
        "Loaded %d allow and %d deny patterns from %s",
        len(allow_patterns),
        len(deny_patterns),
        rules_path,
    )
    return allow_patterns, deny_patterns


def validate_config(
    config: str,
    allow_patterns: List[re.Pattern],
    deny_patterns: List[re.Pattern],
) -> Tuple[bool, List[str]]:
    """
    Validate the configuration against allow/deny patterns.

    Parameters
    ----------
    config: str
        The raw device configuration.
    allow_patterns: List[re.Pattern]
        Regexes that must each match *at least one* line.
    deny_patterns: List[re.Pattern]
        Regexes that must **not** match any line.

    Returns
    -------
    Tuple[bool, List[str]]
        ``(is_valid, messages)`` where ``messages`` contains any failures.
    """
    is_valid = True
    messages: List[str] = []
    config_lines = config.splitlines()

    # Check denies first – any match is a failure.
    for pattern in deny_patterns:
        for idx, line in enumerate(config_lines, start=1):
            if pattern.search(line):
                is_valid = False
                messages.append(
                    f"Deny rule '{pattern.pattern}' matched line {idx}: {line}"
                )
                logger.debug(
                    "Deny match: pattern=%s line=%d content=%s",
                    pattern.pattern,
                    idx,
                    line,
                )
                break  # No need to scan further for this pattern.

    # Check allows – each pattern must match at least one line.
    for pattern in allow_patterns:
        matched = any(pattern.search(line) for line in config_lines)
        if not matched:
            is_valid = False
            messages.append(f"Allow rule '{pattern.pattern}' not found in config")
            logger.debug("Allow rule missing: %s", pattern.pattern)

    return is_valid, messages


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a network device's running configuration.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Hostname or IP address of the target device.",
    )
    parser.add_argument(
        "--user",
        required=True,
        help="SSH username.",
    )
    parser.add_argument(
        "--ssh-key",
        type=Path,
        default=None,
        help="Path to a private SSH key (defaults to ssh-agent/default key).",
    )
    parser.add_argument(
        "--command",
        default="show running-config",
        help="Command to retrieve configuration (device specific).",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        required=True,
        help="Path to a file containing allow/deny validation rules.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit with status 1 if validation fails.",
    )

    args = parser.parse_args()
    global logger
    logger = build_logger(args.log_level)

    try:
        allow_patterns, deny_patterns = load_rules(args.rules)
    except (OSError, ValueError) as exc:
        logger.error("Failed to load rules: %s", exc)
        sys.exit(2)

    try:
        config_output = run_ssh_command(
            host=args.host,
            user=args.user,
            remote_cmd=args.command,
            ssh_key=args.ssh_key,
        )
        logger.info("Successfully retrieved configuration from %s", args.host)
    except RuntimeError as exc:
        logger.error("SSH command failed: %s", exc)
        sys.exit(2)

    is_valid, messages = validate_config(config_output, allow_patterns, deny_patterns)

    if is_valid:
        logger.info("Configuration validation PASSED.")
        print("✅ Validation passed.")
    else:
        logger.warning("Configuration validation FAILED.")
        print("❌ Validation failed:")
        for msg in messages:
            print(f"  - {msg}")

    if args.fail_on_error and not is_valid:
        sys.exit(1)


if __name__ == "__main__":
    main()