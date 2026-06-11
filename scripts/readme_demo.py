"""Produce the README demo figures from LIVE sandboxes — real screenshots, no mockups.

Two assets, both written to ``docs/assets/demo/``:

- ``fork_fanout.png`` — the memory-fork story in pixels: a golden desktop with an
  in-memory state (a shell variable + on-screen scrollback), checkpointed LIVE on the
  CRIU tier, then four replicas spawned from that one checkpoint — each wakes up with
  the same shell, same heap, same screen, and types a different continuation.
  Requires the ``shinken/sandbox-linux-criu`` image (privileged tier).

- ``observe_diff.png`` — the structured-observation story: a real GTK dialog observed
  through the guest engine (screenshot + numbered tree with stable ids), then a typed
  edit re-observed as a ``~/+/-`` diff a fraction of the screenshot's size.
  Requires the ``shinken/sandbox-a11y`` image.

Run (Docker up, images built):
    python scripts/readme_demo.py fork      # fork_fanout.png
    python scripts/readme_demo.py observe   # observe_diff.png
    python scripts/readme_demo.py all
"""

from __future__ import annotations

import base64
import io
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "assets" / "demo"
SDK = REPO / "sdk" / "python" / "src"
sys.path.insert(0, str(SDK))

CRIU_IMAGE = "shinken/sandbox-linux-criu:latest"
A11Y_IMAGE = "shinken/sandbox-a11y:latest"

SERIF = "/System/Library/Fonts/Supplemental/Times New Roman.ttf"
SERIF_BOLD = "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"
MONO = "/System/Library/Fonts/Menlo.ttc"

INK = (28, 28, 30)
MUTED = (105, 105, 110)
PAPER = (252, 252, 250)
FRAME = (70, 70, 75)


def _png_image(shot: dict) -> Image.Image:
    raw = shot["png"]
    if isinstance(raw, str):
        raw = base64.b64decode(raw)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _save(img: Image.Image, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img.quantize(colors=256, method=Image.MEDIANCUT).save(OUT / name, optimize=True)
    print(f"wrote {(OUT / name).relative_to(REPO)} ({(OUT / name).stat().st_size // 1024} KiB)")


def _quiet(sbx, argv: list[str]) -> None:
    """Best-effort guest command (cosmetics only — never fail the demo on it)."""
    try:
        sbx.exec(argv, env={"DISPLAY": ":0"}, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------- fork fan-out

XTERM_ARGS = [
    "-geometry", "86x16+60+96",
    "-fa", "DejaVu Sans Mono", "-fs", "17",
    "-bg", "#11151a", "-fg", "#e6e6e6",
]
XTERM_CENTER = (60 + 430, 96 + 200)
CROP = (30, 60, 1130, 565)  # our xterm at +60+96 (~1030x450) plus margin

STRATEGIES = [
    ("replica 1", "try the File menu", "#5b7fa6"),
    ("replica 2", "try the keyboard shortcut", "#6a9955"),
    ("replica 3", "try the search box", "#b0764f"),
    ("replica 4", "try drag-and-drop", "#8e6fae"),
]


def _shell(env, line: str) -> None:
    env.type_text(line)
    env.key("Return")
    time.sleep(0.35)


def fork_grid() -> None:
    from shinken import SandboxSpec
    from shinken.providers.criu import CriuDockerProvider

    provider = CriuDockerProvider(image=CRIU_IMAGE, name_prefix="shinken-demo")
    shots: list[tuple[str, str, Image.Image]] = []
    timing: dict[str, float] = {}

    with provider.session(SandboxSpec()) as env:
        # Drop the boot xterm (xdotool is in the image; pkill is not) — ours is the scene.
        _quiet(env, ["xdotool", "search", "--class", "XTerm", "windowkill", "%@"])
        time.sleep(0.5)
        env.launch_app("xterm", XTERM_ARGS)
        time.sleep(1.2)
        env.click(x=XTERM_CENTER[0], y=XTERM_CENTER[1])
        time.sleep(0.4)
        _shell(env, "clear")
        _shell(env, 'echo "EXPENSIVE SETUP, DONE ONCE: logged in, app open, step 7/30"')
        _shell(env, "ANSWER=42   # lives in this shell's HEAP, not in any file")
        _shell(env, 'echo "in-memory state: ANSWER=$ANSWER"')
        time.sleep(0.6)

        t0 = time.perf_counter()
        ckpt = env.checkpoint("readme-demo-golden")
        timing["checkpoint"] = time.perf_counter() - t0
        shots.append(("golden", "", _png_image(env.screenshot()).crop(CROP)))

        spawn_costs = []
        try:
            for label, strategy, color in STRATEGIES:
                t0 = time.perf_counter()
                rep = ckpt.spawn()
                spawn_costs.append(time.perf_counter() - t0)
                try:
                    rep.click(x=XTERM_CENTER[0], y=XTERM_CENTER[1])
                    time.sleep(0.4)
                    rep.type_text(f'echo "{label}: ANSWER=$ANSWER inherited -> {strategy}"')
                    rep.key("Return")
                    time.sleep(0.6)
                    shots.append((label, f"{strategy}\x00{color}",
                                  _png_image(rep.screenshot()).crop(CROP)))
                finally:
                    rep.destroy()
        finally:
            ckpt.delete()
        timing["spawn_p50"] = sorted(spawn_costs)[len(spawn_costs) // 2]

    _compose_fork_grid(shots, timing)


def _compose_fork_grid(shots: list[tuple[str, str, Image.Image]], timing: dict[str, float]) -> None:
    golden = shots[0][2]
    reps = shots[1:]
    w = 1480
    pad = 26
    cap_h = 44
    head_h = 76
    gw = 1150
    gh = round(gw * golden.height / golden.width)
    rw = (w - 3 * pad) // 2
    rh = round(rw * golden.height / golden.width)
    h = head_h + gh + cap_h + 14 + 2 * (rh + cap_h) + pad

    img = Image.new("RGB", (w, h), PAPER)
    d = ImageDraw.Draw(img)
    f_head = _font(SERIF_BOLD, 30)
    f_cap = _font(SERIF, 22)
    f_cap_i = _font(SERIF, 21)

    d.text((pad, 20), "One golden desktop, four live replicas — the memory fork carries the "
                      "screen, the processes, and the heap", font=f_head, fill=INK)

    y = head_h
    gx = (w - gw) // 2
    img.paste(golden.resize((gw, gh), Image.LANCZOS), (gx, y))
    d.rectangle([gx, y, gx + gw, y + gh], outline=FRAME, width=2)
    y += gh + 8
    cap = (f"golden — set up once, checkpointed LIVE in {timing['checkpoint']:.1f} s; the donor "
           f"keeps running and ANSWER=42 exists only in the shell's memory")
    d.text(((w - d.textlength(cap, font=f_cap)) // 2, y), cap, font=f_cap, fill=INK)
    y += cap_h + 14

    for row in range(2):
        x = pad
        for col in range(2):
            label, strategy_color, shot = reps[row * 2 + col]
            strategy, _, color = strategy_color.partition("\x00")
            img.paste(shot.resize((rw, rh), Image.LANCZOS), (x, y))
            d.rectangle([x, y, x + rw, y + rh], outline=color or FRAME, width=4)
            d.text((x, y + rh + 8),
                   f"{label} — forked in {timing['spawn_p50']:.2f} s, $ANSWER intact; {strategy}",
                   font=f_cap_i, fill=MUTED)
            x += rw + pad
        y += rh + cap_h

    _save(img, "fork_fanout.png")


# ------------------------------------------------------------- observe + diff

ZENITY_TITLE = "Expense report"


def observe_panel() -> None:
    from shinken import DockerLocalProvider, SandboxSpec

    provider = DockerLocalProvider(image=A11Y_IMAGE, name_prefix="shinken-demo")
    with provider.session(SandboxSpec()) as env:
        _quiet(env, ["pkill", "-x", "xterm"])
        _quiet(env, ["xsetroot", "-solid", "#46627f"])
        # The a11y image predates launch_app; start zenity on the image's shared
        # session bus the way scripts/observe_smoke.py does.
        container = subprocess.run(
            ["docker", "ps", "--filter", "name=shinken-demo", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        guest_env = ("DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/shinken-session-bus "
                     "NO_AT_BRIDGE=0")
        subprocess.run(
            ["docker", "exec", "-d", container, "sh", "-c",
             f'{guest_env} zenity --entry --title="{ZENITY_TITLE}" --text="Vendor name:" '
             ">/tmp/zen.log 2>&1"],
            check=True,
        )

        obs1 = None
        for _ in range(30):
            obs1 = env.observe(structured=True, settle_ms=200)
            if ZENITY_TITLE in obs1.get("tree_text", "") and _entry(obs1):
                break
            time.sleep(0.5)
        assert obs1 and _entry(obs1), "zenity never appeared in the AT-SPI tree"

        entry = _entry(obs1)
        env.act_on(entry["ref"], "click")
        env.type_text("Imagine Diffusion KK")
        diff = None
        for _ in range(20):
            diff = env.observe_diff(settle_ms=200)
            if diff.get("tree") == "diff" and "Imagine Diffusion" in diff.get("tree_text", ""):
                break
            time.sleep(0.4)
        assert diff and diff.get("tree") == "diff", "typed text never appeared in a diff"

        shot = env.screenshot()  # the typed state — what the diff just described
        wire_raw = shot["png"]
        wire_size = len(base64.b64decode(wire_raw) if isinstance(wire_raw, str) else wire_raw)
        dialog = next((e for e in obs1.get("elements", []) if e.get("role") == "dialog"), None)

    _compose_observe_panel(_png_image(shot), dialog, obs1, diff, wire_size)


def _entry(obs: dict) -> dict | None:
    return next((e for e in obs.get("elements", []) if e.get("role") in {"text", "entry"}), None)


def _wrap(line: str, budget: int, indent: str = "        ") -> list[str]:
    out, s = [], line
    while len(s) > budget:
        out.append(s[:budget])
        s = indent + s[budget:]
    out.append(s)
    return out[:3]


def _compose_observe_panel(shot: Image.Image, dialog: dict | None, obs1: dict, diff: dict,
                           wire_size: int) -> None:
    if dialog and dialog.get("bbox"):
        x, y, bw, bh = dialog["bbox"]
        m = 56
        box = (max(0, x - m), max(0, y - m - 24),
               min(shot.width, x + bw + m), min(shot.height, y + bh + m))
    else:
        box = (shot.width // 2 - 320, shot.height // 2 - 240,
               shot.width // 2 + 320, shot.height // 2 + 200)
    crop = shot.crop(box)

    w = 1480
    pad = 26
    head_h = 76
    left_w = 620
    lh = round(left_w * crop.height / crop.width)

    f_head = _font(SERIF_BOLD, 30)
    f_cap = _font(SERIF, 22)
    f_mono = _font(MONO, 15)
    line_h = 22
    budget = 80

    tree_lines = [seg for ln in obs1["tree_text"].splitlines()[:16] for seg in _wrap(ln, budget)]
    diff_lines = [seg for ln in diff["tree_text"].splitlines() if ln
                  for seg in _wrap(ln, budget)][:12]
    tree_bytes = len(obs1["tree_text"].encode())
    diff_bytes = len(diff["tree_text"].encode())

    right_h = 34 + len(tree_lines) * line_h + 26 + 34 + len(diff_lines) * line_h
    h = head_h + max(lh + 44, right_h) + pad

    img = Image.new("RGB", (w, h), PAPER)
    d = ImageDraw.Draw(img)
    d.text((pad, 20), "Observe on demand: pixels + a numbered tree with stable ids; "
                      "re-observe is a diff, not a dump", font=f_head, fill=INK)

    y0 = head_h
    img.paste(crop.resize((left_w, lh), Image.LANCZOS), (pad, y0))
    d.rectangle([pad, y0, pad + left_w, y0 + lh], outline=FRAME, width=2)
    d.text((pad, y0 + lh + 8),
           f"the same moment as pixels: {wire_size / 1024:.1f} KiB on the wire",
           font=f_cap, fill=MUTED)

    x1 = pad + left_w + pad
    y = y0
    d.text((x1, y), f"observe(structured=True)  →  tree_text   ({tree_bytes / 1024:.1f} KiB)",
           font=f_cap, fill=INK)
    y += 34
    for ln in tree_lines:
        d.text((x1, y), ln, font=f_mono, fill=(40, 40, 45))
        y += line_h
    y += 26
    d.text((x1, y), f'type_text("Imagine Diffusion KK")  →  observe_diff()   ({diff_bytes} bytes)',
           font=f_cap, fill=INK)
    y += 34
    for ln in diff_lines:
        color = (21, 97, 36) if ln.lstrip().startswith("+") \
            else (152, 82, 14) if ln.lstrip().startswith("~") \
            else (130, 36, 36) if ln.lstrip().startswith("-") else (40, 40, 45)
        d.text((x1, y), ln, font=f_mono, fill=color)
        y += line_h

    _save(img, "observe_diff.png")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("fork", "all"):
        fork_grid()
    if what in ("observe", "all"):
        observe_panel()
