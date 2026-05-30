"""First-party smoke-agent harness validation (#91) — provider-neutral, env-driven.

A minimal one-task smoke that exercises the whole loop — observe (screenshot) → agent
→ typed action → record → score — and writes a `.skn` replay. It uses only
Shinken-owned code, public deps, and environment configuration, and **skips cleanly**
when optional prerequisites are absent. The built-in :class:`HttpModelAgent` speaks an
OpenAI-compatible chat API (base URL + key + model from env) so any compatible provider
works; tests inject a deterministic stub agent, so no network or secret is needed in CI.

Config (by role — set real values only in ignored local env, never in the repo):
  ``SHK_SMOKE_MODEL_BASE_URL``  model endpoint base URL
  ``SHK_SMOKE_MODEL_API_KEY``   model API key
  ``SHK_SMOKE_MODEL_NAME``      model name
  ``SHK_ADDR`` / ``SHK_TOKEN``  shinkend address + dev token
  ``SHK_TASK_EGRESS_PROXY``     optional task-egress proxy (see shinken.egress)
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .client import connect
from .egress import ProxyConfig, proxy_status, resolve_task_egress_proxy

MODEL_BASE_URL_ENV = "SHK_SMOKE_MODEL_BASE_URL"
MODEL_API_KEY_ENV = "SHK_SMOKE_MODEL_API_KEY"
MODEL_NAME_ENV = "SHK_SMOKE_MODEL_NAME"


@dataclass
class SmokeConfig:
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    address: str = "127.0.0.1:8765"
    token: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> SmokeConfig:
        e = os.environ if env is None else env
        return cls(
            base_url=e.get(MODEL_BASE_URL_ENV) or None,
            api_key=e.get(MODEL_API_KEY_ENV) or None,
            model=e.get(MODEL_NAME_ENV) or None,
            address=e.get("SHK_ADDR", "127.0.0.1:8765"),
            token=e.get("SHK_TOKEN") or None,
        )

    @property
    def model_available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass
class SmokeResult:
    status: str  # "pass" | "fail" | "skipped" | "error"
    reason: str = ""
    steps: int = 0
    bundle: str | None = None
    proxy: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "steps": self.steps,
            "bundle": self.bundle,
            "proxy": self.proxy,
        }


class HttpModelAgent:
    """A minimal OpenAI-compatible smoke agent: one chat round-trip proves the model is
    reachable + the key works, then it emits a benign action and finishes. (Harness
    validation — not task-solving.) Honors the task-egress proxy if configured."""

    def __init__(
        self, config: SmokeConfig, proxy: ProxyConfig | None = None, timeout: float = 30.0
    ):
        self._config = config
        self._proxy = proxy
        self._timeout = timeout
        self._round = 0

    def act(self, observation: dict, instruction: str) -> Any:
        self._round += 1
        if self._round == 1:
            self._chat(instruction)  # raises on connectivity/auth failure
            return {"verb": "move", "x": 100, "y": 100}  # benign, recordable
        return "DONE"

    def _chat(self, instruction: str) -> str:
        body = json.dumps(
            {
                "model": self._config.model,
                "messages": [
                    {"role": "user", "content": f"{instruction}\nReply with the word READY."}
                ],
                "max_tokens": 8,
            }
        ).encode()
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
        )
        opener = urllib.request.build_opener()
        if self._proxy is not None:
            handler = urllib.request.ProxyHandler(self._proxy.as_handlers())
            opener = urllib.request.build_opener(handler)
        with opener.open(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]


def _apply(env: Any, action: dict) -> None:
    verb = action.get("verb")
    if verb in ("click", "double_click", "right_click", "move"):
        getattr(env, verb)(x=action["x"], y=action["y"])
    elif verb == "type_text":
        env.type_text(action.get("text", ""))
    elif verb == "key":
        env.key(action.get("keys", ""))
    elif verb == "scroll":
        env.scroll(dy=action.get("dy", 0))
    else:
        raise ValueError(f"smoke agent emitted unsupported verb: {verb!r}")


def run_smoke_agent(
    agent: Any = None,
    *,
    instruction: str = "Validate the Shinken harness end to end.",
    max_steps: int = 4,
    config: SmokeConfig | None = None,
    connect_factory: Callable[[], Any] | None = None,
    out_path: str | None = None,
) -> SmokeResult:
    """Run one smoke episode: observe→agent→act→record→score. Returns a SmokeResult.

    Skips cleanly (``status="skipped"``) when no agent is given and the model env is
    absent. Pass an ``agent`` (e.g. a deterministic stub) to run without a model."""
    config = config or SmokeConfig.from_env()
    proxy_cfg = resolve_task_egress_proxy()
    proxy = proxy_status(proxy_cfg)
    if agent is None:
        if not config.model_available:
            return SmokeResult(
                status="skipped", reason="no SHK_SMOKE_MODEL_* configuration", proxy=proxy
            )
        agent = HttpModelAgent(config, proxy_cfg)

    factory = connect_factory or (
        lambda: connect(config.address, record=True, token=config.token)
    )
    env = factory()
    steps = 0
    try:
        for _ in range(max_steps):
            observation = {"screenshot": env.screenshot()["png"]}
            action = agent.act(observation, instruction)
            steps += 1
            if action is None or action in ("DONE", "FAIL"):
                break
            _apply(env, action)
        bundle = env.save_replay(
            out_path or os.path.join(tempfile.mkdtemp(prefix="shinken-smoke-"), "smoke.skn")
        )
        return SmokeResult(status="pass", steps=steps, bundle=bundle, proxy=proxy)
    except Exception as exc:
        return SmokeResult(status="error", reason=str(exc), steps=steps, proxy=proxy)
    finally:
        with contextlib.suppress(Exception):
            env.close()
