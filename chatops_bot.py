"""autonomous_chatops_bot.py

A lightweight chat‑ops style network automation script. It can execute arbitrary
commands or retrieve device facts from Cisco IOS, NX‑OS, JunOS, Arista EOS and
similar platforms using either Netmiko (SSH) or NAPALM (API/SSH).

Features
--------
* Command‑line interface built with ``argparse``.
* Secure handling of credentials (plain‑text for demo purposes – use a vault in
  production).
* Detailed logging to console and optional log file.
* Graceful error handling for authentication, timeouts and command failures.
* Ability to dump device facts (interfaces, version, etc.) when ``--facts`` is
  supplied.
* Supports ``--save-config`` to write the running configuration to a local file.

Prerequisites
-------------
* Python 3.8+
* netmiko
* napalm
* (optional) paramiko – pulled in by Netmiko/NAPALM

Install the required packages with::

    pip install netmiko napalm

Usage
-----
Run a single command on a device::

    python autonomous_chatops_bot.py \\
        --device 10.0.0.1 \\
        --username admin \\
        --password secret \\
        --command "show version"

Collect facts and save the running config::

    python autonomous_chatops_bot.py \\
        --device 10.0.0.1 \\
        --username admin \\
        --password secret \\
        --facts \\
        --save-config

"""

import argparse
import getpass
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from netmiko import ConnectHandler, NetMikoAuthenticationException, NetMikoTimeoutException
from napalm import get_network_driver
from napalm.base.exceptions import (
    ConnectionException,
    CommandErrorException,
    MergeConfigException,
)


# --------------------------------------------------------------------------- #
# Logging configuration
# --------------------------------------------------------------------------- #
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)

CONSOLE_HANDLER = logging.StreamHandler()
CONSOLE_HANDLER.setLevel(logging.INFO)
CONSOLE_FORMAT = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
CONSOLE_HANDLER.setFormatter(CONSOLE_FORMAT)

LOGGER.addHandler(CONSOLE_HANDLER)


def configure_file_logging(log_path: str) -> None:
    """Add a rotating file handler to the global logger."""
    file_handler = logging.FileHandler(log_path, mode="a")
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(module)s - %(message)s"
    )
    file_handler.setFormatter(file_format)
    LOGGER.addHandler(file_handler)


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #
def build_netmiko_kwargs(
    device_ip: str,
    username: str,
    password: str,
    device_type: str = "cisco_ios",
) -> Dict[str, Any]:
    """Return a dictionary suitable for Netmiko ConnectHandler."""
    return {
        "device_type": device_type,
        "host": device_ip,
        "username": username,
        "password": password,
        "fast_cli": False,
    }


def run_command_netmiko(
    device_ip: str,
    username: str,
    password: str,
    command: str,
    device_type: str = "cisco_ios",
) -> str:
    """Execute a single command via Netmiko and return the output."""
    kwargs = build_netmiko_kwargs(device_ip, username, password, device_type)
    LOGGER.debug("Netmiko connection kwargs: %s", kwargs)

    try:
        with ConnectHandler(**kwargs) as conn:
            LOGGER.info("Connected to %s via Netmiko", device_ip)
            output = conn.send_command(command, expect_string=r"#|\$", strip_prompt=True)
            LOGGER.debug("Command output: %s", output)
            return output
    except (NetMikoAuthenticationException, NetMikoTimeoutException) as exc:
        LOGGER.error("Netmiko connection error: %s", exc)
        raise
    except Exception as exc:
        LOGGER.error("Unexpected error during Netmiko command execution: %s", exc)
        raise


def get_driver_kwargs(device_type: str) -> Dict[str, Any]:
    """Map generic device_type to NAPALM driver name."""
    mapping = {
        "cisco_ios": "ios",
        "cisco_xr": "iosxr",
        "cisco_nxos": "nxos_ssh",
        "juniper_junos": "junos",
        "arista_eos": "eos",
    }
    return {"optional_args": {"port": 22}}  # basic optional args, extend as needed


def run_facts_napalm(
    device_ip: str,
    username: str,
    password: str,
    device_type: str = "cisco_ios",
) -> Dict[str, Any]:
    """Connect with NAPALM and return a dictionary of device facts."""
    driver_name = mapping_napalm_driver(device_type)
    driver = get_network_driver(driver_name)

    driver_kwargs = get_driver_kwargs(device_type)
    LOGGER.debug("NAPALM driver: %s, kwargs: %s", driver_name, driver_kwargs)

    try:
        device = driver(
            hostname=device_ip,
            username=username,
            password=password,
            optional_args=driver_kwargs.get("optional_args", {}),
        )
        device.open()
        LOGGER.info("Connected to %s via NAPALM (%s)", device_ip, driver_name)

        facts = device.get_facts()
        LOGGER.debug("Collected facts: %s", json.dumps(facts, indent=2))

        interfaces = device.get_interfaces()
        LOGGER.debug("Collected interfaces: %s", json.dumps(interfaces, indent=2))

        device.close()
        return {"facts": facts, "interfaces": interfaces}
    except (ConnectionException, CommandErrorException) as exc:
        LOGGER.error("NAPALM connection/command error: %s", exc)
        raise
    except Exception as exc:
        LOGGER.error("Unexpected NAPALM error: %s", exc)
        raise


def mapping_napalm_driver(device_type: str) -> str:
    """Return NAPALM driver name for a given generic device_type."""
    mapping = {
        "cisco_ios": "ios",
        "cisco_xr": "iosxr",
        "cisco_nxos": "nxos_ssh",
        "juniper_junos": "junos",
        "arista_eos": "eos",
    }
    try:
        return mapping[device_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported device_type '{device_type}'.") from exc


def save_running_config(
    device_ip: str,
    username: str,
    password: str,
    device_type: str = "cisco_ios",
    dest_dir: str = "configs",
) -> str:
    """Retrieve the running configuration via Netmiko and save it locally."""
    os.makedirs(dest_dir, exist_ok=True)
    filename = os.path.join(dest_dir, f"{device_ip}_running_{datetime.now():%Y%m%d_%H%M%S}.cfg")
    command = "show running-config"
    config = run_command_netmiko(device_ip, username, password, command, device_type)

    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(config)
        LOGGER.info("Running configuration saved to %s", filename)
    except OSError as exc:
        LOGGER.error("Failed to write configuration file: %s", exc)
        raise

    return filename


# --------------------------------------------------------------------------- #
# Main execution
# --------------------------------------------------------------------------- #
def parse_arguments() -> argparse.Namespace:
    """Define and parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="ChatOps‑style network automation script.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--device",
        required=True,
        help="IP address or hostname of the target network device.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Login username for the device.",
    )
    parser.add_argument(
        "--password",
        help="Login password. If omitted, the script will prompt securely.",
    )
    parser.add_argument(
        "--device-type",
        default="cisco_ios",
        choices=[
            "cisco_ios",
            "cisco_xr",
            "cisco_nxos",
            "juniper_junos",
            "arista_eos",
        ],
        help="Vendor/OS type used to select the appropriate driver.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--command",
        help="Command string to execute on the device (single command).",
    )
    group.add_argument(
        "--facts",
        action="store_true",
        help="Collect device facts via NAPALM instead of running a command.",
    )
    parser.add_argument(
        "--save-config",
        action="store_true",
        help="Save the running configuration to a local file after command execution.",
    )
    parser.add_argument(
        "--log-file",
        default="autonomous_chatops_bot.log",
        help="Path to the log file for detailed debug output.",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point for script execution."""
    args = parse_arguments()
    configure_file_logging(args.log_file)

    password = args.password or getpass.getpass(prompt="Device password: ")

    try:
        if args.facts:
            facts = run_facts_napalm(
                device_ip=args.device,
                username=args.username,
                password=password,
                device_type=args.device_type,
            )
            print(json.dumps(facts, indent=2))
        else:
            output = run_command_netmiko(
                device_ip=args.device,
                username=args.username,
                password=password,
                command=args.command,
                device_type=args.device_type,
            )
            print(output)

        if args.save_config:
            save_running_config(
                device_ip=args.device,
                username=args.username,
                password=password,
                device_type=args.device_type,
            )
    except (NetMikoAuthenticationException, NetMikoTimeoutException, ConnectionException) as exc:
        LOGGER.error("Connection failure: %s", exc)
        sys.exit(1)
    except ValueError as exc:
        LOGGER.error("Invalid argument: %s", exc)
        sys.exit(2)
    except Exception:
        LOGGER.error("Unhandled exception:\n%s", traceback.format_exc())
        sys.exit(3)


if __name__ == "__main__":
    main()