"""network_cli_tool.py
Utility script to execute a single command on a network device via Telnet.

Usage:
    python network_cli_tool.py --host 10.1.1.1 --user admin \
        --command "show version" [options]

Options:
    --host HOST           IP address or hostname of the device (required)
    --port PORT           Telnet port, default 23
    --user USER           Username for authentication (required)
    --password PASSWORD   Password for authentication (if omitted, prompted securely)
    --command COMMAND     Command to execute on the device (default: "show version")
    --timeout TIMEOUT     Connection timeout in seconds (default: 10)
    --log LOGFILE         Path to a log file; defaults to STDOUT

Prerequisites:
    * Device must support Telnet access.
    * Python 3.8+ (uses f-strings and typing).
    * No third‑party libraries; uses only the Python standard library.

The script establishes a Telnet session, logs in, runs the specified command,
captures the output, and prints it to STDOUT while also logging key events.
"""

import argparse
import getpass
import logging
import socket
import sys
import telnetlib
from typing import Optional


def setup_logging(logfile: Optional[str] = None) -> None:
    """Configure root logger to output to file or STDOUT."""
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    if logfile:
        logging.basicConfig(
            level=logging.INFO,
            filename=logfile,
            filemode="a",
            format=log_format,
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            stream=sys.stdout,
            format=log_format,
        )


class TelnetDevice:
    """Thin wrapper around telnetlib for network device interaction."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: int = 10,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.tn: Optional[telnetlib.Telnet] = None

    def __enter__(self) -> "TelnetDevice":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    def connect(self) -> None:
        """Open Telnet connection and perform login."""
        logging.info("Connecting to %s:%s", self.host, self.port)
        try:
            self.tn = telnetlib.Telnet(self.host, self.port, self.timeout)
        except (socket.timeout, ConnectionRefusedError) as e:
            logging.error("Unable to connect to %s:%s – %s", self.host, self.port, e)
            raise ConnectionError(f"Connection failed: {e}") from e

        # Expect login prompts; many devices use 'Username:' / 'Password:'
        login_prompts = [b"login:", b"username:", b"user:", b"Password:", b"password:"]
        try:
            idx, _, _ = self.tn.expect(login_prompts, self.timeout)
            if idx in (0, 1, 2):
                self.tn.write(self.username.encode("ascii") + b"\n")
                self.tn.read_until(b"Password:", self.timeout)
            self.tn.write(self.password.encode("ascii") + b"\n")
            # Wait for the command prompt (commonly '>','%','#')
            self.tn.read_until(b">", self.timeout)
            logging.info("Login successful")
        except EOFError as e:
            logging.error("Unexpected EOF during login")
            raise ConnectionError("Login failed: unexpected EOF") from e
        except Exception as e:
            logging.error("Login failed: %s", e)
            raise ConnectionError(f"Login failed: {e}") from e

    def execute(self, command: str) -> str:
        """Send a command and return its raw output."""
        if not self.tn:
            raise RuntimeError("Telnet session not established")
        logging.info("Executing command: %s", command)
        self.tn.write(command.encode("ascii") + b"\n")
        # Read until we see the prompt again
        output = self.tn.read_until(b">", self.timeout).decode("utf-8", errors="ignore")
        # Strip the sent command and prompt from output
        lines = output.splitlines()
        if lines and command.strip() in lines[0]:
            lines = lines[1:]  # remove echoed command
        if lines and lines[-1].endswith(">"):
            lines = lines[:-1]  # remove trailing prompt
        result = "\n".join(lines).strip()
        logging.debug("Command output: %s", result)
        return result

    def disconnect(self) -> None:
        """Close the Telnet session."""
        if self.tn:
            logging.info("Closing connection to %s", self.host)
            try:
                self.tn.write(b"exit\n")
                self.tn.close()
            except Exception as e:
                logging.warning("Error while closing Telnet session: %s", e)
            finally:
                self.tn = None


def parse_arguments() -> argparse.Namespace:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Execute a single command on a network device via Telnet."
    )
    parser.add_argument("--host", required=True, help="Device IP address or hostname")
    parser.add_argument("--port", type=int, default=23, help="Telnet port (default: 23)")
    parser.add_argument("--user", required=True, help="Username for authentication")
    parser.add_argument(
        "--password",
        help="Password for authentication (if omitted, prompted securely)",
    )
    parser.add_argument(
        "--command",
        default="show version",
        help='Command to run on the device (default: "show version")',
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Connection timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--log",
        dest="logfile",
        help="File to write logs to (default: STDOUT)",
    )
    return parser.parse_args()


def main() -> None:
    """Main execution flow."""
    args = parse_arguments()
    setup_logging(args.logfile)

    password = args.password or getpass.getpass(prompt="Password: ")

    try:
        with TelnetDevice(
            host=args.host,
            port=args.port,
            username=args.user,
            password=password,
            timeout=args.timeout,
        ) as device:
            output = device.execute(args.command)
            print(output)
    except ConnectionError as ce:
        logging.error("Connection error: %s", ce)
        sys.exit(1)
    except RuntimeError as re:
        logging.error("Runtime error: %s", re)
        sys.exit(1)
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
        sys.exit(0)
    except Exception as exc:
        logging.exception("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()