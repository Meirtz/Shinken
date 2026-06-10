"""E6 proof: an off-the-shelf model drives the live Sandbox unchanged through one adapter.

A real vision model (Kimi K2.6 via SHK_SMOKE_MODEL_*) observes the reference Docker
sandbox's desktop, emits OSWorld pixel-pyautogui, and Shinken actuates it over the typed
ACI — the full model -> parse -> adapter -> ACI -> live GUI chain with a real provider.
Verifies a real guest-state change (a file the model was asked to write), and records the
adapter/model/coordinate-transform metadata. No OSWorld VM needed; this is the on-this-box
slice of the alpha gate (M5 adds the official OSWorld boot+evaluator).

Run (Docker + SHK_SMOKE_MODEL_* required):
    set -a; . ./.env; set +a
    PYTHONPATH=sdk/python/src python scripts/live_model_smoke.py
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path
from tempfile import mkdtemp

from shinken.osworld import parse_model_actions
from shinken.osworld_eval import ChatModelAgent, make_shinken_actuator
from shinken.providers.base import SandboxSpec
from shinken.providers.docker import DockerLocalProvider

MARKER = "k2-6-live"
GUEST_FILE = "/tmp/shinken_e6.txt"
INSTRUCTION = (
    "A terminal window is open. Click inside it, then type exactly this command and press "
    f"Enter: echo {MARKER} > {GUEST_FILE}\nWhen the command has been run, return DONE."
)


def main() -> int:
    prov = DockerLocalProvider(image="shinken/sandbox-linux")
    handle = prov.create(SandboxSpec(screen_geometry="1280x800x24"))
    record: dict = {"instruction": INSTRUCTION, "steps": [], "passed": False, "error": None}
    actuator = None
    try:
        agent = ChatModelAgent.from_env()
        actuator = make_shinken_actuator(handle.addr, handle.token)  # ShinkenDesktopEnv shim
        history: list[str] = []
        actuated = 0
        for i in range(6):
            obs = actuator._observation() if hasattr(actuator, "_observation") else actuator.reset()
            png = obs.get("screenshot")
            raw = agent.act(png, None, INSTRUCTION, history)
            actions = parse_model_actions(raw)
            record["steps"].append({"i": i, "n_actions": len(actions), "actions": actions[:4]})
            if not actions:
                history.append("[no parseable action]")
                continue
            done = False
            for a in actions:
                if a in ("DONE", "FAIL"):
                    done = True
                    break
                actuator.step(a, pause=1.0)  # real model output actuated over the ACI
                actuated += 1
                history.append(a.replace("\n", " ")[:120])
            if done:
                break
        # Verify the real guest state via the provider's docker-cp transport.
        time.sleep(1.0)
        verify_env = prov.connect(handle)
        try:
            out = Path(mkdtemp()) / "e6.txt"
            verify_env.get_file(GUEST_FILE, str(out))
            content = out.read_text().strip()
        finally:
            verify_env.close()
        record.update(
            adapter=agent.__class__.__name__,
            model=ChatModelAgent.from_env()._model,
            coordinate_space="point_px (pixels read off the screenshot)",
            actuated_actions=actuated,
            observed=content,
            passed=(content == MARKER),
        )
    except Exception as exc:  # noqa: BLE001 — record the failure as the receipt
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if actuator is not None:
            with contextlib.suppress(Exception):
                actuator.close()
        with contextlib.suppress(Exception):
            prov.destroy(handle)
    print(json.dumps(record, indent=2))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
