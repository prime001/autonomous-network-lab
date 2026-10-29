"""health_audit.py

Network device health audit utility.

This script performs basic, non‑intrusive health checks on a network device:

* **ICMP ping** – verifies the device is reachable.
* **TCP port test** – confirms a specific service port (default SSH) is open.
* **Telnet command execution** – optionally logs into the device via Telnet
  (using the standard library ``telnetlib``) and runs one or more commands,
  returning the output.

The tool is intended for quick, automated monitoring in CI/CD pipelines
or as a standalone CLI utility for network engineers.

Prerequisites
-------------
* Python 3.8+
* The target device must allow ICMP echo, the selected TCP port, and Telnet
  (if command execution is requested).
* No third‑party packages are required; only the Python standard library is used.

Usage
-----
```
python health_audit.py --host 10.0.0.1 \\
    --username admin --password secret \\
    --port 22 --ping --ssh --telnet-cmd "show version,show interface status"
```

Arguments
---------
* ``--host``          Target device IP or hostname (required).
* ``--username``      Username for Telnet login (optional, required if ``--telnet-cmd`` is used).
* ``--password``      Password for Telnet login (optional, required if ``--telnet-cmd`` is used).
* ``--port``          TCP port to test (default: 22).
* ``--timeout``       Seconds to wait for network operations (default: 5).
* ``--ping``          Perform ICMP ping test.
* ``--ssh``           Perform TCP port check (default SSH port 22).
* ``--telnet-cmd``    Comma‑separated list of commands to execute over Telnet.
"""

import argparse
import logging
import socket
import subprocess
import sys
import telnetlib
import time
from typing import List, Optional

LOGGER = logging.getLogger(__name__)


def configure_logging(verbose: bool) -> None:
    """Configure root logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def ping_host(host: str, timeout: int) -> bool:
    """Send a single ICMP echo request to *host*.

    Returns ``True`` if the host replies within *timeout* seconds, else ``False``.
    """
    LOGGER.debug("Pinging %s with timeout %s seconds", host, timeout)
    # Platform‑specific ping arguments
    if sys.platform.startswith("win"):
        count_arg = "-n"
        wait_arg = "-w"
        timeout_ms = str(timeout * 1000)
    else:
        count_arg = "-c"
        wait_arg = "-W"
        timeout_ms = str(timeout)

    cmd = ["ping", count_arg, "1", wait_arg, timeout_ms, host]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
        LOGGER.debug("Ping succeeded: returncode %s", result.returncode)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        LOGGER.debug("Ping failed: %s", exc)
        return False


def check_tcp_port(host: str, port: int, timeout: int) -> bool:
    """Attempt to open a TCP connection to *host*:*port*.

    Returns ``True`` if the connection succeeds, else ``False``.
    """
    LOGGER.debug("Testing TCP port %s on %s (timeout %s s)", port, host, timeout)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            LOGGER.debug("TCP connection to %s:%s succeeded", host, port)
            return True
        except (socket.timeout, socket.error) as exc:
            LOGGER.debug("TCP connection to %s:%s failed: %s", host, port, exc)
            return False


def telnet_execute(
    host: str,
    username: str,
    password: str,
    commands: List[str],
    timeout: int,
    port: int = 23,
) -> Optional[str]:
    """Log into *host* via Telnet and run *commands*.

    Returns the concatenated command output on success, or ``None`` on failure.
    """
    LOGGER.info("Starting Telnet session to %s:%s", host, port)
    try:
        tn = telnetlib.Telnet(host, port, timeout)
    except socket.error as exc:
        LOGGER.error("Unable to open Telnet connection: %s", exc)
        return None

    def read_until(prompt: bytes) -> bool:
        try:
            tn.read_until(prompt, timeout)
            return True
        except EOFError:
            return False

    # Typical Cisco-like login flow; adapt as needed for other platforms.
    if not read_until(b"Username:"):
        LOGGER.error("Did not receive 'Username:' prompt")
        tn.close()
        return None
    tn.write(username.encode("ascii") + b"\n")
    if not read_until(b"Password:"):
        LOGGER.error("Did not receive 'Password:' prompt")
        tn.close()
        return None
    tn.write(password.encode("ascii") + b"\n")

    # Wait for command prompt (assume '>' or '#')
    if not read_until(b">"):
        if not read_until(b"#"):
            LOGGER.error("Login failed or unexpected prompt")
            tn.close()
            return None

    output = []
    for cmd in commands:
        LOGGER.debug("Sending command: %s", cmd)
        tn.write(cmd.encode("ascii") + b"\n")
        time.sleep(0.5)  # Small pause for the device to process
        resp = tn.read_very_eager().decode("utf-8", errors="ignore")
        output.append(resp.strip())

    tn.write(b"exit\n")
    tn.close()
    full_output = "\n".join(output)
    LOGGER.debug("Telnet command output collected")
    return full_output


def parse_arguments() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Network device health audit utility.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="IP address or hostname of the device")
    parser.add_argument(
        "--username", help="Username for Telnet authentication (required with --telnet-cmd)"
    )
    parser.add_argument(
        "--password", help="Password for Telnet authentication (required with --telnet-cmd)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="TCP port to test (default SSH port 22)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="Network operation timeout in seconds",
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="Enable ICMP ping test",
    )
    parser.add_argument(
        "--ssh",
        action="store_true",
        help="Enable TCP port test (default checks SSH port)",
    )
    parser.add_argument(
        "--telnet-cmd",
        help="Comma‑separated list of commands to execute over Telnet",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    configure_logging(args.verbose)

    overall_success = True

    if args.ping:
        LOGGER.info("Running ping test against %s", args.host)
        if ping_host(args.host, args.timeout):
            LOGGER.info("Ping successful")
        else:
            LOGGER.warning("Ping failed")
            overall_success = False

    if args.ssh:
        LOGGER.info("Testing TCP port %s on %s", args.port, args.host)
        if check_tcp_port(args.host, args.port, args.timeout):
            LOGGER.info("TCP port %s is open", args.port)
        else:
            LOGGER.warning("TCP port %s is closed or filtered", args.port)
            overall_success = False

    if args.telnet_cmd:
        if not args.username or not args.password:
            LOGGER.error(
                "Username and password are required when using --telnet-cmd"
            )
            return 1
        commands = [cmd.strip() for cmd in args.telnet_cmd.split(",") if cmd.strip()]
        if not commands:
            LOGGER.error("No commands supplied to --telnet-cmd")
            return 1

        telnet_output = telnet_execute(
            host=args.host,
            username=args.username,
            password=args.password,
            commands=commands,
            timeout=args.timeout,
        )
        if telnet_output is None:
            LOGGER.warning("Telnet command execution failed")
            overall_success = False
        else:
            LOGGER.info("Telnet command output:\n%s", telnet_output)

    if overall_success:
        LOGGER.info("All requested health checks passed")
        return 0
    else:
        LOGGER.warning("One or more health checks failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())