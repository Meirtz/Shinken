"""OSWorld evaluation as a Shinken Workload — OSWorld is *one example* over the runtime.

A hosted chat model (e.g. Kimi K2.6) sees an OSWorld observation and emits OSWorld-format
pyautogui code (parsed by :func:`shinken.osworld.parse_model_actions`); the Shinken
``DesktopEnv`` shim actuates it over the typed ACI; OSWorld's *own* evaluator scores. The
substrate/provider is orthogonal — resolved by name through the provider registry, so an
out-of-tree (private) substrate works without any reference here. This module names no
provider and embeds no task content (configs load from ``OSWORLD_PATH``).

Registered as the ``osworld-eval`` workload, so ``shinken.runtime.workloads.get("osworld-eval")``
resolves it; adding another benchmark is a sibling module + one ``register()`` call.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from typing import Any

from shinken.osworld import parse_model_actions
from shinken.runtime import workloads

# The model contract — OSWorld's screenshot→pyautogui form (pixel coords + WAIT/DONE/FAIL).
SYSTEM_PROMPT = (
    "You are an agent performing desktop tasks. Each step you get a screenshot. Use "
    "`pyautogui` with PIXEL coordinates read off the screenshot (top-left is (0,0)). Do NOT "
    "use pyautogui.locateCenterOnScreen or pyautogui.screenshot. Return one or more lines of "
    "python in a single ```python``` block. Return ```WAIT``` to wait, ```FAIL``` if "
    "impossible, ```DONE``` when complete. Give a one-line reflection, then ONLY the block."
)
_TERMINAL = {"DONE", "FAIL"}
# A valid 1x1 PNG — the mock observation used only by the dry-run fake env.
_DRY_RUN_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)


# --- agents (return RAW model text; parse_model_actions turns it into OSWorld actions) ---
class ChatModelAgent:
    """OpenAI-compatible *vision* chat client → raw text. K2.6 is OpenAI-compatible. Key/URL/
    model come from the environment; nothing is hardcoded or logged."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self._base_url, self._api_key, self._model, self._timeout = (
            base_url,
            api_key,
            model,
            timeout,
        )

    @classmethod
    def from_env(cls) -> ChatModelAgent:
        b, k, m = (
            os.environ.get("SHK_SMOKE_MODEL_BASE_URL"),
            os.environ.get("SHK_SMOKE_MODEL_API_KEY"),
            os.environ.get("SHK_SMOKE_MODEL_NAME"),
        )
        if not (b and k and m):
            raise SystemExit(
                "model endpoint not configured: set SHK_SMOKE_MODEL_BASE_URL / "
                "SHK_SMOKE_MODEL_API_KEY / SHK_SMOKE_MODEL_NAME (ignored local env)."
            )
        return cls(b, k, m)

    def act(self, png: bytes | None, a11y: str | None, instruction: str, history: list[str]) -> str:
        prev = "\n".join(history[-10:]) or "(none)"
        text = f"Task: {instruction}\nPrevious actions:\n{prev}\nWhat is the next action?"
        if a11y:
            text += f"\n\nAccessibility tree (truncated):\n{a11y[:4000]}"
        content: list[dict] = [{"type": "text", "text": text}]
        if png is not None:
            uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": uri, "detail": "high"}})
        body = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                "max_tokens": 1024,
                "temperature": 0.0,
            }
        ).encode()
        req = urllib.request.Request(
            self._base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            msg = json.loads(resp.read())["choices"][0]["message"]
        # Thinking models (e.g. K2.6) may return content=null with the action in
        # reasoning_content; fall back so the parser always gets a string.
        return msg.get("content") or msg.get("reasoning_content") or ""


class ScriptedAgent:
    """Fixed OSWorld-format outputs — exercises the real parser + workload with no model."""

    def __init__(self, outputs: list[str]):
        self._outputs, self._i = list(outputs), 0

    def act(self, png: bytes | None, a11y: str | None, instruction: str, history: list[str]) -> str:
        out = self._outputs[self._i] if self._i < len(self._outputs) else "```DONE```"
        self._i += 1
        return out


# --- actuators (execute one OSWorld action string) ---
class RecordingActuator:
    """Dry-run/test only: records executed action strings for the fake evaluator."""

    def __init__(self) -> None:
        self.actions: list[str] = []

    def step(self, action: str, pause: float = 0.0) -> None:
        self.actions.append(action)

    def close(self) -> None:
        pass


def make_shinken_actuator(addr: str | None = None) -> Any:
    """Alpha path: the Shinken DesktopEnv shim → a shinkend in the VM (pixel pyautogui → ACI)."""
    from shinken.osworld import DesktopEnv as ShinkenDesktopEnv

    env = ShinkenDesktopEnv(
        addr or os.environ.get("SHK_ADDR", "127.0.0.1:8765"), action_space="pyautogui"
    )
    env.reset()
    return env


def make_osworld_env(
    provider: str, width: int, height: int, observation: str, headless: bool = True
) -> Any:
    """Lazily import + construct the official OSWorld ``DesktopEnv`` (boots + scores).

    ``OSWORLD_PATH`` is prepended to ``sys.path`` if set; ``provider`` is OSWorld's own
    provider name (docker/vmware/aws/…)."""
    path = os.environ.get("OSWORLD_PATH")
    if path and path not in sys.path:
        sys.path.insert(0, path)
    try:
        from desktop_env.desktop_env import DesktopEnv as OSWorldDesktopEnv
    except ImportError as exc:  # pragma: no cover - external install
        raise SystemExit(
            f"OSWorld not importable; install it or set OSWORLD_PATH ({exc})."
        ) from exc
    return OSWorldDesktopEnv(
        provider_name=provider,
        action_space="pyautogui",
        screen_size=(width, height),
        headless=headless,
        require_a11y_tree=(observation != "screenshot"),
        os_type="Ubuntu",
    )


# A fake OSWorld env for dry-run/tests: no VM, scores from the recorded actions.
class FakeOSWorldEnv:
    def __init__(self, recorder: RecordingActuator):
        self._recorder = recorder

    def reset(self, task_config: dict | None = None) -> dict:
        return self._get_obs()

    def _get_obs(self) -> dict:
        return {"screenshot": _DRY_RUN_PNG, "accessibility_tree": None}

    def evaluate(self) -> float:
        joined = "\n".join(self._recorder.actions)
        return 1.0 if ("click(" in joined and "write(" in joined) else 0.0

    def close(self) -> None:
        pass


def _screenshot_bytes(obs: dict) -> bytes | None:
    shot = obs.get("screenshot")
    if shot is None:
        return None
    return base64.b64decode(shot) if isinstance(shot, str) else shot


def run_episode(
    env: Any,
    agent: Any,
    actuator: Any,
    instruction: str,
    *,
    max_steps: int = 15,
    observation: str = "screenshot",
    pause: float = 2.0,
) -> dict:
    """observe → model → parse_model_actions → actuate (to DONE/FAIL), then OSWorld scores."""
    history: list[str] = []
    terminal: str | None = None
    steps = 0
    for _ in range(max_steps):
        steps += 1
        obs = env._get_obs()
        png = _screenshot_bytes(obs) if "screenshot" in observation else None
        a11y = obs.get("accessibility_tree") if "a11y" in observation else None
        for action in parse_model_actions(agent.act(png, a11y, instruction, history)) or [
            "__none__"
        ]:
            if action == "__none__":
                history.append("[no parseable action]")
                continue
            if action in _TERMINAL:
                terminal = action
                break
            history.append(action.replace("\n", " ")[:120])
            actuator.step(action, pause=pause)
        if terminal is not None:
            break
    score = float(env.evaluate())
    return {
        "steps": steps,
        "terminal": terminal,
        "score": score,
        "passed": score >= 1.0 and terminal != "FAIL",
    }


class OSWorldEvalWorkload:
    """Run one OSWorld task with a chat agent over Shinken; score with OSWorld's evaluator.

    ``run(rt, *, env, agent, actuator, instruction, ...)`` — the OSWorld env (boot+score) and
    the actuator are assembled by the caller (live: official OSWorld env + a shinkend actuator;
    dry-run: a fake env + recording actuator). Returns ``{steps, terminal, score, passed}``."""

    name = "osworld-eval"

    def run(
        self,
        rt: Any = None,
        *,
        env: Any,
        agent: Any,
        actuator: Any,
        instruction: str = "",
        max_steps: int = 15,
        observation: str = "screenshot",
        pause: float = 2.0,
    ) -> dict:
        return run_episode(
            env,
            agent,
            actuator,
            instruction,
            max_steps=max_steps,
            observation=observation,
            pause=pause,
        )


workloads.register("osworld-eval", OSWorldEvalWorkload)
