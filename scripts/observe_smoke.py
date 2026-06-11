"""Docker structured-observation smoke (M1b): drive the GUEST a11y engine end-to-end
against a real AT-SPI app (zenity --entry) inside the sandbox image.

Asserts, in order:
  1. the runtime advertises `structured_observation` and the `observe` verb;
  2. zenity's tree appears via `env.observe(structured=True)` (settle honored);
  3. element ids are STABLE across two observations;
  4. clicking the entry BY ELEMENT ID works (guest-side element_ref resolution);
  5. after typing, `env.observe_diff()` reports the change (`~` line w/ the text);
  6. `invoke_action` on the OK button works over the AX path (the dialog closes).

Env: SHK_TOKEN (bearer token), optional SHK_ADDR (default 127.0.0.1:8765),
SHK_CONTAINER (default `shk`) — the running sandbox container, used only to launch
zenity via `docker exec` with the image's shared DISPLAY + session-bus address.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import shinken

ADDR = os.environ.get("SHK_ADDR", "127.0.0.1:8765")
CONTAINER = os.environ.get("SHK_CONTAINER", "shk")
TITLE = "Shinken Observe Smoke"

# Match the image's start.sh: every GUI app must join the SAME session bus so its
# a11y bridge lands on the a11y bus shinkend's worker is connected to.
GUEST_ENV = "DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/shinken-session-bus NO_AT_BRIDGE=0"


def sh(cmd: str) -> None:
    subprocess.run(["docker", "exec", "-d", CONTAINER, "sh", "-c", cmd], check=True)


def wait_for_tree(env, pred, what: str, tries: int = 30, **observe_kw):
    for _ in range(tries):
        obs = env.observe(structured=True, **observe_kw)
        if pred(obs):
            return obs
        time.sleep(0.5)
    print(f"FAIL: {what}; last tree:\n{obs.get('tree_text', '')}", file=sys.stderr)
    sys.exit(1)


# GtkEntry maps to AT-SPI ROLE_TEXT ("text") on GTK3; tolerate "entry" spellings.
ENTRY_ROLES = {"text", "entry"}


def entry_of(obs) -> dict | None:
    return next((e for e in obs.get("elements", []) if e.get("role") in ENTRY_ROLES), None)


env = shinken.connect(ADDR, token=os.environ["SHK_TOKEN"])
try:
    caps = env.capabilities
    assert caps.structured_observation, "runtime must advertise structured_observation"
    assert "observe" in caps.verbs and "element_ref" in caps.targets, caps

    # 1) launch a real AT-SPI app in the sandbox and wait for its tree.
    sh(f'{GUEST_ENV} zenity --entry --title="{TITLE}" --text="Name:" >/tmp/zen.log 2>&1')
    obs1 = wait_for_tree(
        env,
        lambda o: TITLE in o.get("tree_text", "") and entry_of(o) is not None,
        "zenity --entry never appeared in the AT-SPI tree",
        settle_ms=200,
    )
    print(f"observe #1: revision={obs1['revision']} nodes={obs1['node_count']} "
          f"capture_ms={obs1['capture_ms']}")
    print(obs1["tree_text"])

    # 2) id stability across re-observation.
    entry1 = entry_of(obs1)
    obs2 = env.observe(structured=True)
    entry2 = entry_of(obs2)
    assert entry2 is not None and entry2["ref"] == entry1["ref"], (
        f"entry id must be stable across observes: {entry1['ref']} vs "
        f"{entry2 and entry2['ref']}"
    )
    assert obs2["revision"] == obs1["revision"] + 1
    print(f"id stability OK: entry={entry1['ref']} across revisions "
          f"{obs1['revision']}->{obs2['revision']}")

    # 3) element click by id (guest-side element_ref -> bbox centre -> XTEST).
    ack = env.act_on(entry1["ref"], "click")
    assert ack.get("ok") is True, ack
    print(f"element click by id OK: {entry1['ref']}")

    # 4) type, then the diff must carry the change.
    env.type_text("hello shinken")
    diff = wait_for_tree(
        env,
        lambda o: o.get("tree") == "diff" and "hello shinken" in o.get("tree_text", ""),
        "typed text never appeared in an observe diff",
        diff=True,
        settle_ms=200,
    )
    print(f"diff after typing (diff_of={diff.get('diff_of')}):\n{diff['tree_text']}")

    # 5) AX-path actuation: invoke the OK button's action; the dialog should close.
    ok_button = next(
        (
            e
            for e in diff.get("elements", [])
            if e.get("role") == "push button" and e.get("name") == "OK"
        ),
        None,
    )
    assert ok_button is not None, "zenity OK button not in the tree"
    ack = env.invoke_action(ok_button["ref"], "click")
    assert ack.get("ok") is True, ack
    gone = wait_for_tree(
        env,
        lambda o: TITLE not in o.get("tree_text", ""),
        "zenity dialog did not close after invoke_action",
        settle_ms=200,
    )
    assert gone is not None
    print("invoke_action OK: dialog closed via the AX path")

    print("observe smoke OK")
finally:
    env.close()
