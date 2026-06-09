"""``shinken`` command-line entry point."""

from __future__ import annotations

import argparse
import os

from . import __version__, connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shinken", description="Shinken SDK CLI")
    parser.add_argument("--version", action="version", version=f"shinken {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    connect_cmd = sub.add_parser("connect", help="connect to a shinkend and print its capabilities")
    # Default addr/token from the SDK's SHK_ADDR/SHK_TOKEN convention (see smoke.py) so the
    # CLI can reach a token-protected (non-loopback) shinkend, which requires a token.
    connect_cmd.add_argument(
        "addr", nargs="?", default=os.environ.get("SHK_ADDR", "127.0.0.1:8765")
    )
    connect_cmd.add_argument(
        "--token",
        default=os.environ.get("SHK_TOKEN"),
        help="dev bearer token (defaults to $SHK_TOKEN); required for a non-loopback shinkend",
    )
    args = parser.parse_args(argv)

    if args.cmd == "connect":
        env = connect(args.addr, token=args.token)
        try:
            caps = env.capabilities
            print(f"connected to shinkend @ {args.addr}")
            print(f"  platform : {env.platform}")
            print(f"  rtt      : {env.ping() * 1000:.1f} ms")
            print(f"  screen   : {env.screen_size()}")
            print(f"  ACI      : v{caps.schema_version}")
            print(f"  verbs    : {', '.join(caps.verbs)}")
        finally:
            env.close()
        return 0

    parser.print_help()
    return 1
