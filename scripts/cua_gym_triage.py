"""Triage a CUA-Gym task-bundle export for Shinken runnability.

Scans every bundle under a task root (``$CUA_GYM_TASKS`` or argv[1]) and classifies it by
what its setup/reward actually need, so a training run can start from the subset that runs
on the current image and grow outward:

- ``file_cli``   — no mock-app URLs, no GUI app beyond the base desktop: runnable as-is.
- ``mock_web``   — references ``__CUA_GYM_*_URL__`` placeholders: needs the CUA-Gym-Hub
  mock app deployed and the placeholder materialized (their
  ``scripts/materialize_dataset_urls.py``), plus a browser in the image.
- ``desktop_app`` — instruction/setup references a desktop application: needs that app
  installed in the sandbox image (OSWorld-style image work).

Usage:
    python scripts/cua_gym_triage.py [task_root] [--json out.json] [--sample N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))

from shinken.integrations.cua_gym import CuaGymTaskSource  # noqa: E402

PLACEHOLDER = re.compile(r"__CUA_GYM_[A-Z0-9_]+_URL__")

# Desktop apps that imply image requirements beyond the lean base (xterm/zenity/chromium).
DESKTOP_APP_HINTS = (
    "libreoffice",
    "soffice",
    "gimp",
    "vlc",
    "thunderbird",
    "inkscape",
    "blender",
    "audacity",
    "okular",
    "kdenlive",
    "obs",
    "code ",
    "vscode",
    "nautilus",
    "evince",
    "firefox",
    "gnome-",
    "kate",
    "dolphin",
)


def classify(bundle: Path, cfg: dict) -> tuple[str, list[str]]:
    text = ""
    for p in sorted(bundle.iterdir()):
        if p.is_file() and p.suffix in (".json", ".py", ".sh", ".txt", ".yaml", ".yml"):
            text += p.read_text(errors="replace")
    placeholders = sorted(set(PLACEHOLDER.findall(text)))
    if placeholders:
        return "mock_web", placeholders
    hay = (
        cfg.get("instruction", "") + " " + cfg.get("app_type", "") + " " + text
    ).lower()
    hits = sorted({h.strip() for h in DESKTOP_APP_HINTS if h in hay})
    if hits:
        return "desktop_app", hits
    return "file_cli", []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=os.environ.get("CUA_GYM_TASKS"))
    ap.add_argument("--json", dest="json_out")
    ap.add_argument(
        "--sample", type=int, default=5, help="example task ids to print per class"
    )
    args = ap.parse_args()
    if not args.root:
        ap.error("pass a task root or set $CUA_GYM_TASKS")

    src = CuaGymTaskSource(args.root)
    classes: dict[str, list[str]] = defaultdict(list)
    app_types: Counter = Counter()
    difficulty: Counter = Counter()
    needs: dict[str, Counter] = {"mock_web": Counter(), "desktop_app": Counter()}

    for task in src:
        cls, detail = classify(task.path, task.config)
        classes[cls].append(task.task_id)
        app_types[task.app_type] += 1
        difficulty[str(task.config.get("difficulty"))] += 1
        for d in detail:
            needs[cls][d] += 1 if cls in needs else 0

    total = len(src)
    print(f"bundles: {total} loaded, {len(src.skipped)} skipped")
    print(f"difficulty: {dict(difficulty.most_common())}")
    print("\nrunnability classes:")
    for cls in ("file_cli", "mock_web", "desktop_app"):
        ids = classes.get(cls, [])
        print(
            f"  {cls:12s} {len(ids):6d}  ({100 * len(ids) / max(total, 1):.1f}%)"
            f"   e.g. {ids[: args.sample]}"
        )
    print(f"\ntop app_types: {app_types.most_common(15)}")
    if needs["mock_web"]:
        print(
            f"\nmock-web URL placeholders (top 15): {needs['mock_web'].most_common(15)}"
        )
    if needs["desktop_app"]:
        print(f"desktop-app needs: {needs['desktop_app'].most_common(15)}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "total": total,
                    "skipped": len(src.skipped),
                    "classes": {k: len(v) for k, v in classes.items()},
                    "class_ids": classes,
                    "app_types": dict(app_types),
                    "difficulty": dict(difficulty),
                    "mock_web_placeholders": dict(needs["mock_web"]),
                    "desktop_app_needs": dict(needs["desktop_app"]),
                },
                indent=2,
            )
        )
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
