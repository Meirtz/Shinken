"""``shinken`` command-line entry point."""

from __future__ import annotations

import argparse

from . import __version__, connect


def _format_steps(rp) -> str:
    """Render a `.skn` bundle as scrubable steps: each action followed by the
    observations it triggered, with media refs + metadata."""
    out = [
        f"  session {rp.manifest.get('session_id')} · {len(rp)} events · "
        f"{len(rp.steps())} steps",
        "",
    ]
    for i, step in enumerate(rp.steps(), 1):
        act = step["action"]
        if act is not None:
            target = (act["payload"].get("target") or {}).get("kind", "-")
            out.append(
                f"step {i}: action[{act['src']}] #{act.get('action_id', '-')} "
                f"target={target}  (+{act['dt']:.3f}s)"
            )
        else:
            out.append(f"step {i}: (pre-action events)")
        for e in step["events"]:
            if e is act:
                continue
            extra = ""
            if e["kind"] == "observation":
                img = e["payload"].get("image") or {}
                if img:
                    ref = str(img.get("ref", ""))[:12]
                    extra = f"  image={img.get('w')}x{img.get('h')} media={ref}…"
            pair = f" ↳action#{e['action_id']}" if e.get("action_id") else ""
            out.append(f"   [{e['seq']:>3}] +{e['dt']:.3f}s {e['kind']}:{e['src']}{pair}{extra}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shinken", description="Shinken SDK CLI")
    parser.add_argument("--version", action="version", version=f"shinken {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    connect_cmd = sub.add_parser("connect", help="connect to a shinkend and print its capabilities")
    connect_cmd.add_argument("addr", nargs="?", default="127.0.0.1:8765")
    replay_cmd = sub.add_parser("replay", help="print the timeline of a .skn replay bundle")
    replay_cmd.add_argument("bundle")
    replay_cmd.add_argument(
        "--step", action="store_true", help="step through paired action→observation events"
    )
    replay_cmd.add_argument(
        "--validate", action="store_true", help="validate schema + action/observation pairing"
    )
    args = parser.parse_args(argv)

    if args.cmd == "replay":
        from .skn import Replay, summarize

        if args.validate:
            try:
                Replay.load(args.bundle).validate()
            except Exception as exc:
                print(f"replay INVALID: {exc}")
                return 1
            print("replay OK: schema + action/observation pairing valid")
            return 0
        if args.step:
            print(_format_steps(Replay.load(args.bundle)))
            return 0
        print(summarize(args.bundle))
        return 0

    if args.cmd == "connect":
        env = connect(args.addr)
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
