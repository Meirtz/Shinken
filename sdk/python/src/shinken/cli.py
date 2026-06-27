"""``shinken`` command-line entry point."""

from __future__ import annotations

import argparse
import os
import time

from . import __version__, connect
from .providers import get as get_provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shinken", description="Shinken SDK CLI")
    parser.add_argument("--version", action="version", version=f"shinken {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    connect_cmd = sub.add_parser("connect", help="connect to a shinkend and print its capabilities")
    # Default addr/token from the SDK's SHK_ADDR/SHK_TOKEN convention (see smoke.py).
    # Every TCP shinkend, including loopback, requires a token.
    connect_cmd.add_argument(
        "addr", nargs="?", default=os.environ.get("SHK_ADDR", "127.0.0.1:8765")
    )
    connect_cmd.add_argument(
        "--token",
        default=os.environ.get("SHK_TOKEN"),
        help="dev bearer token (defaults to $SHK_TOKEN); required by every shinkend",
    )
    ps_cmd = sub.add_parser(
        "ps", help="list live Shinken sandboxes rebuilt from substrate labels (Provider.list)"
    )
    ps_cmd.add_argument(
        "--provider", default="docker", help="registered provider name (default: docker)"
    )
    gc_cmd = sub.add_parser(
        "gc", help="reclaim orphaned sandboxes (dead owner process) via Provider.gc"
    )
    gc_cmd.add_argument(
        "--provider", default="docker", help="registered provider name (default: docker)"
    )
    gc_cmd.add_argument(
        "--snapshots",
        action="store_true",
        help="also reclaim labeled snapshot images (shinken.snapshot=true)",
    )
    gc_cmd.add_argument(
        "--force",
        action="store_true",
        help="reclaim live-owner sessions too (the blunt sweep; default skips them)",
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

    if args.cmd == "ps":
        handles = get_provider(args.provider).list()
        if not handles:
            print("no live sandboxes")
            return 0
        print(f"{'SANDBOX':<28} {'ADDR':<22} {'OWNER':>8}  AGE")
        for h in handles:
            owner = h.metadata.get("owner_pid")
            age = f"{time.time() - h.created_at:.0f}s" if h.created_at else "?"
            print(f"{h.sandbox_id:<28} {h.addr or '-':<22} {owner or '?':>8}  {age}")
        return 0

    if args.cmd == "gc":
        report = get_provider(args.provider).gc(snapshots=args.snapshots, force=args.force)
        print(
            f"reclaimed {report.containers} container(s), {report.images} snapshot image(s); "
            f"skipped {report.skipped} live-owner resource(s)"
        )
        return 0

    parser.print_help()
    return 1
