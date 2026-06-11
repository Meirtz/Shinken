"""NeMo Gym interop — Shinken computer-use environments as a resources server.

`NeMo Gym <https://github.com/NVIDIA-NeMo/Gym>`__ (Apache-2.0; docs:
<https://docs.nvidia.com/nemo/gym/>) standardizes RL rollout collection: an *agent server*
drives a model through OpenAI **Responses API** turns, tool calls are POSTed to a
*resources server* (one HTTP endpoint per tool, per-rollout state keyed by a session
cookie), and a final ``/verify`` call scores the rollout (``reward: float``). Trainers
(NeMo RL GRPO, OpenRLHF, …) consume the resulting rollout JSONL directly.

This module puts a real desktop behind that contract — and the per-rollout resource the
docs ask for ("initialization, isolation, and cleanup … per rollout") is exactly the
runtime-state primitive: **every rollout is a fork from the task's golden checkpoint**
(task setup runs once; reset p50 ~60 ms on the warm-pool tier vs the re-provision-per-
episode pattern). Tasks come from CUA-Gym exported bundles
(:mod:`shinken.integrations.cua_gym`), whose ``reward.py`` (``REWARD: X.X`` last-line
contract) becomes the ``/verify`` scorer.

The observation channel is **text-first by design**: ``computer_observe`` returns the
guest a11y engine's legible numbered tree (stable ``e<N>`` ids that never rebind) and
``mode="diff"`` returns the ``~/+/-`` delta against the previous observation — a few
hundred bytes per turn, trainable with any tool-calling LLM, no VLM image plumbing
required (a 2.0 KiB tree vs a 76.5 KiB screenshot, measured). Pixels stay available for
future VLM agent servers.

Layering (no hard dependency on ``nemo_gym`` — same discipline as the other adapters):

- :data:`COMPUTER_TOOLS` + :func:`rollout_rows` — the OpenAI function-tool schemas and a
  dataset-row emitter for ``ng_collect_rollouts`` (``metadata.task_id`` routes each row to
  its bundle).
- :class:`ShinkenComputerEngine` — the framework-free core: ``seed`` (golden-fork),
  ``tool`` (dispatch), ``verify`` (reward + teardown), keyed by an opaque session id.
  Drivable without any web server (see ``examples/nemo_gym/local_loop.py``).
- :func:`build_resources_server_cls` — the thin ``nemo_gym`` adapter (lazy import):
  returns a ``SimpleResourcesServer`` subclass wiring the engine to FastAPI routes, for an
  ``ng_run`` entrypoint (``examples/nemo_gym/app.py``).
"""

# NOTE: no `from __future__ import annotations` here — the nemo_gym adapter class is
# built inside a factory, and FastAPI must resolve its method annotations (Request,
# pydantic models imported in that factory scope) as live objects, not strings.
import base64
import contextlib
import json
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from .cua_gym import CuaGymError, CuaGymTask, ShinkenCuaGymEnv

__all__ = [
    "COMPUTER_TOOLS",
    "SYSTEM_PROMPT",
    "rollout_rows",
    "ShinkenComputerEngine",
    "build_resources_server_cls",
]


# ------------------------------------------------------------------ tool contract

#: The closed computer-use tool set, as OpenAI Responses ``function`` tools. Deliberately
#: small (observe / act / exec); ``computer_observe`` is the one observation primitive.
COMPUTER_TOOLS: list[dict] = [
    {
        "type": "function",
        "strict": True,
        "name": "computer_observe",
        "description": (
            "Observe the desktop as a numbered accessibility tree with STABLE element ids "
            "(e1, e2, …; an id never moves to a different control). mode='tree' returns the "
            "full tree; mode='diff' returns only the ~changed/+added/-removed lines since "
            "your previous observation. Observe before acting."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["tree", "diff"],
                    "description": "tree=full, diff=changes only",
                },
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "strict": True,
        "name": "computer_click",
        "description": (
            "Click on the desktop. target is either a stable element id from "
            "computer_observe (e.g. 'e7') or pixel coordinates 'X,Y' (e.g. '640,420')."
        ),
        "parameters": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "'e<N>' or 'X,Y'"}},
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "strict": True,
        "name": "computer_type_text",
        "description": "Type literal text into the focused control (click a field first).",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "strict": True,
        "name": "computer_key",
        "description": "Press a key or chord, e.g. 'Return', 'Tab', 'ctrl+s', 'alt+F4'.",
        "parameters": {
            "type": "object",
            "properties": {"keys": {"type": "string"}},
            "required": ["keys"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "strict": True,
        "name": "computer_scroll",
        "description": "Scroll vertically by dy notches (positive = down, negative = up).",
        "parameters": {
            "type": "object",
            "properties": {"dy": {"type": "integer"}},
            "required": ["dy"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "strict": True,
        "name": "computer_exec",
        "description": (
            "Run a shell command inside the sandbox (sh -c). Returns JSON with "
            "returncode/stdout/stderr. Use for file and CLI work; use the GUI tools for "
            "application work."
        ),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "strict": True,
        "name": "computer_screenshot",
        "description": (
            "Capture the screen as PNG. Over this text channel it returns capture metadata "
            "only (size/bytes) — use computer_observe for actionable structure."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
]

SYSTEM_PROMPT = (
    "You are a computer-use agent operating a real Linux desktop through tools.\n"
    "Loop: observe -> act -> observe the diff. Rules:\n"
    "1. Call computer_observe (mode='tree') once before your first action; afterwards "
    "prefer mode='diff' to see what changed.\n"
    "2. Prefer acting on stable element ids ('e7') over pixel coordinates.\n"
    "3. After typing into a field, the diff will show the field's new Value — verify it.\n"
    "4. Use computer_exec for shell/file work; GUI tools for application work.\n"
    "5. When the task is complete, reply with a short summary and NO tool call."
)


def rollout_rows(
    tasks: Iterable[CuaGymTask],
    *,
    system_prompt: str = SYSTEM_PROMPT,
    tools: list[dict] | None = None,
) -> Iterator[dict]:
    """Emit ``ng_collect_rollouts``-ready dataset rows, one per task: the instruction as
    the user turn, :data:`COMPUTER_TOOLS` as the tool surface, and ``metadata.task_id``
    routing the rollout to its bundle at ``seed_session`` time."""
    for task in tasks:
        yield {
            "responses_create_params": {
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task.instruction},
                ],
                "tools": list(tools if tools is not None else COMPUTER_TOOLS),
                "metadata": {"task_id": task.task_id},
            }
        }


# ------------------------------------------------------------------ engine

def _parse_click_target(target: str) -> dict:
    """``'e7'`` → element ref; ``'640,420'`` → pixel point. Typed error otherwise."""
    t = target.strip()
    if "," in t:
        x_s, _, y_s = t.partition(",")
        try:
            return {"x": int(float(x_s.strip())), "y": int(float(y_s.strip()))}
        except ValueError:
            raise CuaGymError(f"bad pixel target {target!r}; expected 'X,Y'") from None
    if t.startswith("e") and t[1:].isdigit():
        return {"ref": t}
    raise CuaGymError(f"bad click target {target!r}; expected 'e<N>' or 'X,Y'")


class _Rollout:
    """One live rollout: a forked replica + its observation revision state."""

    __slots__ = ("env", "task", "observed_once", "last_used")

    def __init__(self, env: ShinkenCuaGymEnv, task: CuaGymTask) -> None:
        self.env = env
        self.task = task
        self.observed_once = False
        self.last_used = time.monotonic()


class ShinkenComputerEngine:
    """Framework-free resources-server core: per-rollout forked desktops, keyed by an
    opaque session id (NeMo Gym's session cookie lands here verbatim).

    - :meth:`seed` — fork the task's golden checkpoint (built once per task, shared across
      rollouts and protected by a per-task lock) into a fresh replica.
    - :meth:`tool` — dispatch one tool call; returns the string the agent server relays
      to the model.
    - :meth:`verify` — run the bundle's ``reward.py`` in the replica, tear the replica
      down, return the reward.

    Replicas whose rollout never reaches ``verify`` are reaped after ``idle_ttl_s``.
    All methods are blocking; async servers wrap them in a thread (see
    :func:`build_resources_server_cls`).
    """

    def __init__(
        self,
        provider: Any,
        tasks: Mapping[str, CuaGymTask] | Any,
        *,
        spec: Any = None,
        settle_ms: int = 200,
        exec_timeout: float = 60.0,
        idle_ttl_s: float = 900.0,
        env_factory: Any = ShinkenCuaGymEnv,
    ) -> None:
        self.provider = provider
        self.tasks = tasks  # Mapping-like with .get(task_id) -> CuaGymTask
        self.spec = spec
        self.settle_ms = settle_ms
        self.exec_timeout = exec_timeout
        self.idle_ttl_s = idle_ttl_s
        self.env_factory = env_factory
        self._rollouts: dict[str, _Rollout] = {}
        self._goldens: dict[str, str] = {}
        self._golden_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------------

    def seed(self, session_id: str, task_id: str) -> dict:
        """Start a rollout: fork the task's golden checkpoint into a fresh replica."""
        self._reap_idle()
        task = self.tasks.get(task_id)
        if task is None:
            raise CuaGymError(f"unknown task_id {task_id!r}")
        env = self.env_factory(task, self.provider, spec=self.spec, exec_timeout=self.exec_timeout)
        env.golden_checkpoint = self._golden_for(task, env)
        t0 = time.perf_counter()
        env.reset()
        # Bundle extension: steps under config.json's "shinken_post_fork" replay on EVERY
        # replica after the fork. The golden checkpoint carries file state on the disk
        # tier; setup that must exist as a *running process* (an open dialog, a launched
        # app) belongs here, not in the golden build. (On the CRIU memory tier the golden
        # itself carries processes and this list is typically empty.)
        post_fork = task.config.get("shinken_post_fork") or []
        if post_fork:
            sess, run = env._session(), env._guest_exec()
            for step in post_fork:
                env._apply_setup_step(step, sess, run)
        reset_ms = (time.perf_counter() - t0) * 1000.0
        self.end(session_id)  # a re-seeded session replaces its old replica
        with self._lock:
            self._rollouts[session_id] = _Rollout(env, task)
        return {"task_id": task.task_id, "instruction": task.instruction, "reset_ms": reset_ms}

    def _golden_for(self, task: CuaGymTask, env: ShinkenCuaGymEnv) -> str:
        with self._lock:
            lock = self._golden_locks.setdefault(task.task_id, threading.Lock())
        with lock:
            golden = self._goldens.get(task.task_id)
            if golden is None:
                golden = env._build_golden()
                with self._lock:
                    self._goldens[task.task_id] = golden
            return golden

    def verify(self, session_id: str) -> float:
        """Score the rollout with the bundle's ``reward.py`` and tear the replica down."""
        rollout = self._get(session_id)
        try:
            return rollout.env.evaluate()
        finally:
            self.end(session_id)

    def end(self, session_id: str) -> None:
        """Tear down a rollout's replica without scoring (idempotent)."""
        with self._lock:
            rollout = self._rollouts.pop(session_id, None)
        if rollout is not None:
            with contextlib.suppress(Exception):
                rollout.env.close()

    def close(self, *, delete_goldens: bool = True) -> None:
        """Tear down every live replica (and, by default, the shared golden snapshots)."""
        with self._lock:
            ids = list(self._rollouts)
        for sid in ids:
            self.end(sid)
        if delete_goldens and hasattr(self.provider, "delete_snapshot"):
            with self._lock:
                goldens, self._goldens = dict(self._goldens), {}
            for snap in goldens.values():
                with contextlib.suppress(Exception):
                    self.provider.delete_snapshot(snap)

    def _get(self, session_id: str) -> _Rollout:
        with self._lock:
            rollout = self._rollouts.get(session_id)
        if rollout is None:
            raise CuaGymError(
                f"no live rollout for session {session_id!r} — seed_session must run first"
            )
        rollout.last_used = time.monotonic()
        return rollout

    def _reap_idle(self) -> None:
        if self.idle_ttl_s <= 0:
            return
        cutoff = time.monotonic() - self.idle_ttl_s
        with self._lock:
            stale = [sid for sid, r in self._rollouts.items() if r.last_used < cutoff]
        for sid in stale:
            self.end(sid)

    # -- tools -------------------------------------------------------------------

    def tool(self, session_id: str, name: str, arguments: Mapping[str, Any]) -> str:
        """Dispatch one tool call; the returned string is what the model sees."""
        rollout = self._get(session_id)
        handler = getattr(self, f"_tool_{name.removeprefix('computer_')}", None)
        if handler is None:
            raise CuaGymError(f"unknown tool {name!r}")
        try:
            return handler(rollout, dict(arguments))
        except CuaGymError:
            raise
        except Exception as exc:  # the model sees a typed, actionable error string
            return f"error: {type(exc).__name__}: {exc}"

    def _sess(self, rollout: _Rollout) -> Any:
        return rollout.env._session()

    def _tool_observe(self, rollout: _Rollout, args: dict) -> str:
        sess = self._sess(rollout)
        mode = args.get("mode", "tree")
        if mode == "diff" and rollout.observed_once:
            obs = sess.observe_diff(settle_ms=self.settle_ms)
        else:
            obs = sess.observe(structured=True, settle_ms=self.settle_ms)
        rollout.observed_once = True
        tree_text = obs.get("tree_text") or "(no accessible application tree on screen)"
        focus = obs.get("focus")
        suffix = f"\nfocus: {focus}" if focus and f"focus: {focus}" not in tree_text else ""
        return f"{tree_text}{suffix}"

    def _tool_click(self, rollout: _Rollout, args: dict) -> str:
        sess = self._sess(rollout)
        target = _parse_click_target(str(args.get("target", "")))
        if "ref" in target:
            sess.act_on(target["ref"], "click")
            return f"clicked {target['ref']}"
        sess.click(x=target["x"], y=target["y"])
        return f"clicked at ({target['x']}, {target['y']})"

    def _tool_type_text(self, rollout: _Rollout, args: dict) -> str:
        self._sess(rollout).type_text(str(args.get("text", "")))
        return "typed"

    def _tool_key(self, rollout: _Rollout, args: dict) -> str:
        keys = str(args.get("keys", ""))
        self._sess(rollout).key(keys)
        return f"pressed {keys}"

    def _tool_scroll(self, rollout: _Rollout, args: dict) -> str:
        dy = int(args.get("dy", 0))
        self._sess(rollout).scroll(dy=dy)
        return f"scrolled dy={dy}"

    def _tool_exec(self, rollout: _Rollout, args: dict) -> str:
        command = str(args.get("command", ""))
        result = rollout.env.execute(command)
        payload = {
            "returncode": result.get("returncode"),
            "stdout": str(result.get("output", ""))[:2000],
            "stderr": str(result.get("error", ""))[:500],
        }
        return json.dumps(payload, ensure_ascii=False)

    def _tool_screenshot(self, rollout: _Rollout, args: dict) -> str:
        png = rollout.env.screenshot()
        if not png:
            return "error: screenshot unavailable"
        size = self._sess(rollout).screen_size()
        head = base64.b64encode(png[:24]).decode()
        return (
            f"screenshot captured: {size.get('w')}x{size.get('h')} PNG, "
            f"{len(png) / 1024:.1f} KiB (b64 head {head}…). Pixels are not legible over "
            f"this text channel — use computer_observe for actionable structure."
        )


# ------------------------------------------------------------------ nemo_gym adapter

def extract_task_id(payload: Mapping[str, Any]) -> str | None:
    """Pull ``metadata.task_id`` out of a seed-session body (the agent forwards its run
    request, whose ``responses_create_params`` is the dataset row's)."""
    params = payload.get("responses_create_params")
    if isinstance(params, Mapping):
        meta = params.get("metadata")
        if isinstance(meta, Mapping) and meta.get("task_id"):
            return str(meta["task_id"])
    return None


def build_resources_server_cls(engine_factory: Any) -> type:
    """Build the ``nemo_gym`` ``SimpleResourcesServer`` subclass lazily (the ``nemo_gym``
    package is only required at server runtime, never for importing this module).

    ``engine_factory(config) -> ShinkenComputerEngine`` receives the server's pydantic
    config (extra YAML keys ride along) and owns provider/task wiring — see
    ``examples/nemo_gym/app.py`` for the canonical entrypoint.
    """
    import asyncio

    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse
    from nemo_gym.base_resources_server import (
        BaseSeedSessionRequest,
        BaseSeedSessionResponse,
        BaseVerifyRequest,
        BaseVerifyResponse,
        SimpleResourcesServer,
    )
    from nemo_gym.server_utils import SESSION_ID_KEY

    class ShinkenSeedSessionRequest(BaseSeedSessionRequest):
        model_config = {"extra": "allow"}

    class ShinkenComputerResourcesServer(SimpleResourcesServer):
        """CUA-Gym tasks on Shinken sandboxes: every rollout forks the golden state."""

        def model_post_init(self, context: Any) -> None:
            super().model_post_init(context)
            self._engine = engine_factory(self.config)

        def setup_webserver(self) -> FastAPI:
            app = super().setup_webserver()
            for tool in COMPUTER_TOOLS:
                app.post(f"/{tool['name']}")(self._make_tool_route(tool["name"]))
            return app

        def _make_tool_route(self, name: str):
            async def route(request: Request) -> PlainTextResponse:
                session_id = request.session[SESSION_ID_KEY]
                args = await request.json()
                out = await asyncio.to_thread(
                    self._engine.tool, session_id, name, args if isinstance(args, dict) else {}
                )
                return PlainTextResponse(out)

            route.__name__ = f"tool_{name}"
            return route

        async def seed_session(  # type: ignore[override]
            self, request: Request, body: ShinkenSeedSessionRequest
        ) -> BaseSeedSessionResponse:
            session_id = request.session[SESSION_ID_KEY]
            task_id = extract_task_id(body.model_dump())
            if task_id is None:
                raise CuaGymError(
                    "seed_session body carries no responses_create_params.metadata.task_id "
                    "— generate datasets with shinken.integrations.nemo_gym.rollout_rows"
                )
            await asyncio.to_thread(self._engine.seed, session_id, task_id)
            return BaseSeedSessionResponse()

        async def verify(self, request: Request, body: BaseVerifyRequest) -> BaseVerifyResponse:
            session_id = request.session[SESSION_ID_KEY]
            reward = await asyncio.to_thread(self._engine.verify, session_id)
            return BaseVerifyResponse(**body.model_dump(), reward=reward)

    return ShinkenComputerResourcesServer
