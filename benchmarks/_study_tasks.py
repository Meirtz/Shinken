"""Task corpus for the agent-quality study (Arm 2 of the codec-vs-task-quality design).

Sixteen **read-from-screen-critical** tasks — 8 templates x {normal, small} font — where
success requires transcribing/locating information that exists ONLY in the pixels (an
access code, a two-line note, a button label, a key-value row, ...). Every task is a
``shinken.gym.GymTask`` whose verifier is **deterministic guest state** read back over the
typed in-guest ``exec`` channel (``cat /tmp/answer.txt`` etc.) and judged into a
``shinken.eval.VerifierReceipt`` — never a pixel match, never a model judge.

Layout per task (all self-contained in the standard ``shinken/sandbox-linux`` image —
xterm/zenity/exec, no network):

- ``task.setup`` (runs ONCE, into the golden checkpoint) writes the task's files under
  ``/home/shinken/task``: the display content, an idempotent ``launch.sh`` (xterm/zenity
  windows with deterministic geometry + ``DejaVu Sans Mono`` font sizes), and the answer
  collector (an xterm ``read`` loop appending lines to ``/tmp/answer.txt``).
- The Docker disk-tier fork restores the FILESYSTEM byte-identically but boots fresh
  processes, so the harness runs ``launch.sh`` once per fork — the same bytes, the same
  geometry, every episode (see ``bench_agent_quality.py``'s module doc).
- ``task.verify`` reads the answer/click/run artifact back via ``exec`` and compares it
  to the ground truth that was baked into the checkpoint.

The small-font variants (font size 7 vs 13 on the read-critical window; Pango
``font="7"`` on the zenity dialog text) are the designed breaking case: legible at the
lossless control tier, increasingly damaged down the JPEG/downscale ladder.

Ground truth is derived from a fixed per-task seed, so the corpus is identical across
runs and machines. ``task.metadata`` carries everything a driver needs: ``ready_titles``
(window readiness), ``truth``, ``launch``, and an ``oracle(sess)`` callable producing a
scripted ground-truth action plan (raw Shinken-dialect text — it exercises the same
``parse_actions`` path as a model, but reads the truth from metadata, not pixels:
the plumbing validator, not a study subject).
"""

from __future__ import annotations

import random
from typing import Any

from shinken.eval import VerifierReceipt, check
from shinken.gym import GymTask

TASK_DIR = "/home/shinken/task"
ANSWER_FILE = "/tmp/answer.txt"
CLICKED_FILE = "/tmp/clicked.txt"
OPENED_FILE = "/tmp/opened.txt"

#: Window titles (used for readiness polling and by the oracle to aim its clicks).
TASK_TITLE = "TASK"
ANSWER_TITLE = "ANSWER"
SHELL_TITLE = "SHELL"
DIALOG_TITLE = "PICK"

#: Read-critical font size per variant (DejaVu Sans Mono, Xft points). 13 pt is
#: comfortably legible at the lossless control tier; 7 pt is the breaking case.
FONT_PT = {"normal": 13, "small": 7}

#: Code alphabet excludes the classic confusable glyphs (0/O, 1/I/l, 2/Z, 5/S, 8/B, G/6
#: ambiguity kept low) so a CONTROL-tier failure means agent error, not font ambiguity.
CODE_ALPHABET = "ACDEFHJKLMNPRTUVWXY34679"

_WORDS = (
    "amber bridge candle delta ember falcon garden harbor island juniper kettle lantern "
    "meadow nectar orchard pebble quartz river saddle timber umber velvet willow zephyr"
).split()


def _rng(name: str) -> random.Random:
    return random.Random(f"shinken-agent-quality:{name}")


def _code(rng: random.Random, groups: int = 2, n: int = 4) -> str:
    return "-".join("".join(rng.choice(CODE_ALPHABET) for _ in range(n)) for _ in range(groups))


def _short_code(rng: random.Random, n: int = 3) -> str:
    return "".join(rng.choice(CODE_ALPHABET) for _ in range(n))


# ------------------------------------------------------------------- guest file plumbing


def _put(sess: Any, path: str, content: str, mode: str = "644") -> None:
    """Write one file into the guest over the typed exec channel (stdin-fed, no quoting
    risk), creating parents. Raises on a nonzero exit so a broken setup fails loudly."""
    res = sess.exec(
        shell=f'mkdir -p "$(dirname {path})" && cat > {path} && chmod {mode} {path}',
        stdin=content,
        timeout=20,
    )
    if res.get("exit_code") != 0:
        raise RuntimeError(f"guest write of {path} failed: {res}")


_ANSWER_SH = f"""#!/bin/sh
# Answer collector: each line typed at the prompt is appended to {ANSWER_FILE}.
: > {ANSWER_FILE}
while :; do
  printf 'answer> '
  IFS= read -r line || exit 0
  printf '%s\\n' "$line" >> {ANSWER_FILE}
done
"""


def _xterm(title: str, font_pt: int, geometry: str, command: str) -> str:
    """One detached xterm line for launch.sh (setsid: survives the launcher's exit and
    the exec channel's process-group bookkeeping)."""
    return (
        f"setsid xterm -T {title} -fa 'DejaVu Sans Mono' -fs {font_pt} "
        f"-geometry {geometry} -e {command} >/dev/null 2>&1 &\n"
    )


def _launch_script(body: str) -> str:
    return "#!/bin/sh\n# Launched once per fork by the study harness.\n" + body


def _display_task_setup(display_text: str, font_pt: int, *, answer: bool = True):
    """Common setup: a TASK display window (read-critical, variant font size) and the
    ANSWER collector window (always 13 pt — the agent's own typing is not under test)."""

    def setup(sess: Any) -> None:
        _put(sess, f"{TASK_DIR}/display.txt", display_text)
        _put(sess, f"{TASK_DIR}/answer.sh", _ANSWER_SH, mode="755")
        body = _xterm(
            TASK_TITLE,
            font_pt,
            "70x14+392+40",
            f"sh -c 'cat {TASK_DIR}/display.txt; exec sleep 3600'",
        )
        if answer:
            body += _xterm(ANSWER_TITLE, 13, "60x10+392+470", f"{TASK_DIR}/answer.sh")
        _put(sess, f"{TASK_DIR}/launch.sh", _launch_script(body), mode="755")

    return setup


def _read_guest(sess: Any, path: str) -> tuple[Any, str]:
    res = sess.exec(["cat", path], timeout=20)
    return res.get("exit_code"), res.get("stdout") or ""


def _answer_lines(sess: Any) -> list[str]:
    _rc, out = _read_guest(sess, ANSWER_FILE)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _first_answer_verifier(truth: str):
    def verify(sess: Any) -> VerifierReceipt:
        lines = _answer_lines(sess)
        got = lines[0] if lines else ""
        return VerifierReceipt.from_checks(
            [check("answer matches", got == truth, {"expected": truth, "observed": lines[:3]})]
        )

    return verify


# --------------------------------------------------------------------------- the oracle


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _win_center(sess: Any, title: str) -> tuple[int, int]:
    for w in sess.list_windows():
        if w.get("title") == title:
            return w["x"] + w["w"] // 2, w["y"] + w["h"] // 2
    raise RuntimeError(f"oracle: window titled {title!r} not found")


def _oracle_type_lines(lines: list[str], window: str = ANSWER_TITLE):
    """A scripted plan: focus the collector window, type each truth line + Return as its
    own turn (click and type deliberately split across turns, like a careful agent)."""

    def oracle(sess: Any) -> list[str]:
        x, y = _win_center(sess, window)
        plan = [f'<actions><click x="{x}" y="{y}"/></actions>']
        plan += [
            f'<actions><type_text text="{_esc(ln)}"/><key combo="Return"/></actions>'
            for ln in lines
        ]
        return [*plan, "<done/>"]

    return oracle


def _oracle_zenity(label: str):
    """Click the target dialog button by its accessible name (structured observation +
    guest-resolved element_ref — geometry-free, label-exact)."""

    def oracle(sess: Any) -> list[Any]:
        obs = sess.observe(structured=True)
        ref = next(
            (
                e.get("ref")
                for e in obs.get("elements", [])
                if e.get("name") == label and "button" in (e.get("role") or "")
            ),
            None,
        )
        if ref is None:
            raise RuntimeError(f"oracle: no button named {label!r} in the a11y tree")
        return [[{"verb": "click", "target": {"kind": "element_ref", "ref": ref}}], "<done/>"]

    return oracle


# ------------------------------------------------------------------------ the templates


def _task(
    template: str,
    font: str,
    instruction: str,
    setup,
    verify,
    truth: str,
    oracle,
    ready_titles: list[str],
) -> GymTask:
    return GymTask(
        name=f"{template}-{font}",
        instruction=instruction,
        setup=setup,
        verify=verify,
        metadata={
            "template": template,
            "font": font,
            "truth": truth,
            "oracle": oracle,
            "launch": f"{TASK_DIR}/launch.sh",
            "ready_titles": ready_titles,
        },
    )


def _code_prompt(font: str) -> GymTask:
    rng = _rng(f"code_prompt:{font}")
    code = _code(rng)
    display = (
        "ACCESS CODE\n===========\n\n"
        f"    {code}\n\n"
        "Type this code into the ANSWER terminal below, then press Enter.\n"
    )
    return _task(
        "code_prompt",
        font,
        "A window titled TASK shows an access code. Click the ANSWER terminal, type the "
        "code exactly as shown, and press Enter. Then finish.",
        _display_task_setup(display, FONT_PT[font]),
        _first_answer_verifier(code),
        code,
        _oracle_type_lines([code]),
        [TASK_TITLE, ANSWER_TITLE],
    )


def _key_value_lookup(font: str) -> GymTask:
    rng = _rng(f"key_value_lookup:{font}")
    names = ["alpha", "bravo", "delta", "kilo", "sierra", "tango", "victor", "zulu"]
    rng.shuffle(names)
    table = {n: _code(rng, groups=1, n=6) for n in names}
    target = rng.choice(names)
    rows = "\n".join(f"  {n:<8} {table[n]}" for n in names)
    display = f"REGISTRY\n========\n{rows}\n\nFind the row named '{target}'.\n"
    return _task(
        "key_value_lookup",
        font,
        f"The TASK window lists name/code rows. Find the row named '{target}', then type "
        "its code into the ANSWER terminal and press Enter. Then finish.",
        _display_task_setup(display, FONT_PT[font]),
        _first_answer_verifier(table[target]),
        table[target],
        _oracle_type_lines([table[target]]),
        [TASK_TITLE, ANSWER_TITLE],
    )


def _zenity_button(font: str) -> GymTask:
    rng = _rng(f"zenity_button:{font}")
    labels = []
    while len(labels) < 4:
        c = _short_code(rng)
        if c not in labels:
            labels.append(c)
    ok, cancel, extra_a, extra_b = labels
    target = rng.choice([extra_a, extra_b, ok])  # never Cancel: rc-1-no-stdout is ambiguous
    pango = f'<span font="{FONT_PT[font]}">Press the button labeled <b>{target}</b></span>'
    zenity_sh = f"""#!/bin/sh
# The clicked button's label lands in {CLICKED_FILE} (extra buttons print their label;
# OK is exit 0; Cancel is exit 1 with no output).
out=$(zenity --question --title {DIALOG_TITLE} --text '{pango}' \\
  --ok-label '{ok}' --cancel-label '{cancel}' \\
  --extra-button '{extra_a}' --extra-button '{extra_b}' 2>/dev/null)
rc=$?
if [ -n "$out" ]; then lab="$out"; elif [ $rc -eq 0 ]; then lab='{ok}'; else lab='{cancel}'; fi
printf '%s\\n' "$lab" > {CLICKED_FILE}
"""

    def setup(sess: Any) -> None:
        _put(sess, f"{TASK_DIR}/zenity.sh", zenity_sh, mode="755")
        _put(
            sess,
            f"{TASK_DIR}/launch.sh",
            _launch_script(f"setsid {TASK_DIR}/zenity.sh >/dev/null 2>&1 &\n"),
            mode="755",
        )

    def verify(sess: Any) -> VerifierReceipt:
        _rc, out = _read_guest(sess, CLICKED_FILE)
        got = out.strip()
        return VerifierReceipt.from_checks(
            [check("clicked the named button", got == target, {"expected": target, "got": got})]
        )

    return _task(
        "zenity_button",
        font,
        f"A dialog titled {DIALOG_TITLE} tells you which of its buttons to press. Press "
        "exactly that button (read the label from the dialog text). Then finish.",
        setup,
        verify,
        target,
        _oracle_zenity(target),
        [DIALOG_TITLE],
    )


def _transcribe_note(font: str) -> GymTask:
    rng = _rng(f"transcribe_note:{font}")
    line1 = " ".join(rng.sample(_WORDS, 4))
    line2 = " ".join(rng.sample(_WORDS, 4))
    display = (
        "NOTE\n====\n"
        f"  {line1}\n"
        f"  {line2}\n\n"
        "Type BOTH lines into the ANSWER terminal, one per line.\n"
    )

    def verify(sess: Any) -> VerifierReceipt:
        lines = _answer_lines(sess)
        return VerifierReceipt.from_checks(
            [
                check("line 1 matches", bool(lines) and lines[0] == line1, {"expected": line1}),
                check("line 2 matches", len(lines) > 1 and lines[1] == line2, {"expected": line2}),
            ]
        )

    return _task(
        "transcribe_note",
        font,
        "The TASK window shows a two-line note. Type the two lines into the ANSWER "
        "terminal, in order, pressing Enter after each line. Then finish.",
        _display_task_setup(display, FONT_PT[font]),
        verify,
        f"{line1}\n{line2}",
        _oracle_type_lines([line1, line2]),
        [TASK_TITLE, ANSWER_TITLE],
    )


def _larger_number(font: str) -> GymTask:
    rng = _rng(f"larger_number:{font}")
    a = rng.randint(3000, 9000)
    b = a + rng.choice([-1, 1]) * rng.randint(30, 90)  # close: middle digits differ
    display = (
        "READINGS\n========\n"
        f"  A = {a}\n"
        f"  B = {b}\n\n"
        "Type the LARGER of the two numbers into the ANSWER terminal.\n"
    )
    truth = str(max(a, b))
    return _task(
        "larger_number",
        font,
        "The TASK window shows two numbers, A and B. Type the larger of the two into the "
        "ANSWER terminal and press Enter. Then finish.",
        _display_task_setup(display, FONT_PT[font]),
        _first_answer_verifier(truth),
        truth,
        _oracle_type_lines([truth]),
        [TASK_TITLE, ANSWER_TITLE],
    )


def _run_script_by_code(font: str) -> GymTask:
    rng = _rng(f"run_script_by_code:{font}")
    codes = []
    while len(codes) < 4:
        c = _short_code(rng, n=4)
        if c not in codes:
            codes.append(c)
    target = rng.choice(codes)
    listing = "  ".join(f"run_{c}.sh" for c in codes)
    display = (
        "RUN CODE\n========\n\n"
        f"    {target}\n\n"
        f"Scripts in {TASK_DIR}/bin:\n  {listing}\n\n"
        "Execute the matching script in the SHELL terminal.\n"
    )

    def setup(sess: Any) -> None:
        _display_task_setup(display, FONT_PT[font], answer=False)(sess)
        for c in codes:
            _put(
                sess,
                f"{TASK_DIR}/bin/run_{c}.sh",
                f"#!/bin/sh\nprintf '%s\\n' '{c}' >> {OPENED_FILE}\necho ran {c}\n",
                mode="755",
            )
        shell_line = _xterm(SHELL_TITLE, 13, "60x10+392+470", "bash --noprofile --norc -i")
        res = sess.exec(shell=f"cat >> {TASK_DIR}/launch.sh", stdin=shell_line, timeout=20)
        if res.get("exit_code") != 0:
            raise RuntimeError(f"appending SHELL window to launch.sh failed: {res}")

    def verify(sess: Any) -> VerifierReceipt:
        _rc, out = _read_guest(sess, OPENED_FILE)
        ran = [ln.strip() for ln in out.splitlines() if ln.strip()]
        ok = bool(ran) and all(r == target for r in ran)
        return VerifierReceipt.from_checks(
            [check("ran only the matching script", ok, {"expected": target, "ran": ran})]
        )

    return _task(
        "run_script_by_code",
        font,
        f"The TASK window shows a RUN code. In the SHELL terminal, execute the script "
        f"{TASK_DIR}/bin/run_<code>.sh whose name matches that code (type its full path "
        "and press Enter). Then finish.",
        setup,
        verify,
        target,
        _oracle_type_lines([f"{TASK_DIR}/bin/run_{target}.sh"], window=SHELL_TITLE),
        [TASK_TITLE, SHELL_TITLE],
    )


def _long_serial(font: str) -> GymTask:
    rng = _rng(f"long_serial:{font}")
    serial = _code(rng, groups=4, n=4)
    display = (
        "SERIAL NUMBER\n=============\n\n"
        f"    {serial}\n\n"
        "Type the full serial into the ANSWER terminal.\n"
    )
    return _task(
        "long_serial",
        font,
        "The TASK window shows a serial number. Type it into the ANSWER terminal exactly "
        "as shown (including hyphens) and press Enter. Then finish.",
        _display_task_setup(display, FONT_PT[font]),
        _first_answer_verifier(serial),
        serial,
        _oracle_type_lines([serial]),
        [TASK_TITLE, ANSWER_TITLE],
    )


def _find_extension(font: str) -> GymTask:
    rng = _rng(f"find_extension:{font}")
    stems = rng.sample(_WORDS, 6)
    exts = [".txt", ".log", ".txt", ".dat", ".log", ".cfg"]
    rng.shuffle(exts)
    files = [s + e for s, e in zip(stems, exts, strict=True)]
    target = next(f for f in files if f.endswith(".cfg"))
    rows = "\n".join(
        f"  -rw-r--r--  1 shinken shinken  {rng.randint(120, 9800):>5} {f}" for f in files
    )
    display = (
        "DIRECTORY LISTING\n=================\n"
        f"{rows}\n\n"
        "Exactly one file ends in .cfg — type its full name into the ANSWER terminal.\n"
    )
    return _task(
        "find_extension",
        font,
        "The TASK window shows a directory listing. Exactly one file name ends in .cfg; "
        "type that file's full name into the ANSWER terminal and press Enter. Then finish.",
        _display_task_setup(display, FONT_PT[font]),
        _first_answer_verifier(target),
        target,
        _oracle_type_lines([target]),
        [TASK_TITLE, ANSWER_TITLE],
    )


_TEMPLATES = (
    _code_prompt,
    _key_value_lookup,
    _zenity_button,
    _transcribe_note,
    _larger_number,
    _run_script_by_code,
    _long_serial,
    _find_extension,
)


def build_tasks() -> list[GymTask]:
    """The full 16-task corpus: every template in {normal, small} font, deterministic
    content (fixed seeds), ordered template-major."""
    return [tmpl(font) for tmpl in _TEMPLATES for font in ("normal", "small")]
