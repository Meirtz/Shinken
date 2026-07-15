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
(task setup runs once; the reference Docker filesystem tier restores each rollout from
an immutable checkpoint image). The historical live warm-pool graft is disabled because
it could race guest writers. Tasks come from CUA-Gym exported bundles
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
import logging
import math
import os
import secrets
import threading
import time
import weakref
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .cua_gym import CuaGymError, CuaGymTask, ShinkenCuaGymEnv

_LOG = logging.getLogger(__name__)
_SESSION_GENERATION_KEY = "shinken_rollout_generation"
_LOCK_STRIPES = 257

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
    "You are a computer-use agent operating a real sandboxed desktop through tools.\n"
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

    __slots__ = ("env", "task", "generation", "observed_once", "last_used")

    def __init__(self, env: ShinkenCuaGymEnv, task: CuaGymTask, generation: int) -> None:
        self.env = env
        self.task = task
        self.generation = generation
        self.observed_once = False
        self.last_used = time.monotonic()


@dataclass
class _Golden:
    """A cached immutable snapshot plus an eviction-safe reset lease count."""

    snapshot: Any
    last_used: float
    leases: int = 0


class ShinkenComputerEngine:
    """Framework-free resources-server core: per-rollout forked desktops, keyed by an
    opaque session id (NeMo Gym's session cookie lands here verbatim).

    - :meth:`seed` — fork the task's golden checkpoint (built once per task, shared across
      rollouts and protected by a per-task lock) into a fresh replica.
    - :meth:`tool` — dispatch one tool call; returns the string the agent server relays
      to the model.
    - :meth:`verify` — run the bundle's ``reward.py`` in the replica, tear the replica
      down, return the reward.

    Calls for one session are linearized while different sessions remain concurrent.
    After :meth:`start_maintenance` (called by the web adapter), replicas whose rollout never
    reaches ``verify`` are actively reaped once older than ``idle_ttl_s``. Golden snapshots
    are lease-protected during reset and bounded by both ``max_goldens`` and
    ``golden_ttl_s``. :meth:`close` is a terminal lifecycle barrier: it rejects new work,
    waits for admitted work, stops the reaper, and then tears down every remaining replica.
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
        max_goldens: int = 32,
        golden_ttl_s: float = 3600.0,
        reap_interval_s: float = 30.0,
        max_pending_cleanup: int = 64,
        cleanup_retry_batch: int = 16,
        scorer_error_reward: float | None = None,
        env_factory: Any = ShinkenCuaGymEnv,
    ) -> None:
        if type(max_goldens) is not int or max_goldens < 0:
            raise ValueError("max_goldens must be >= 0")
        if type(max_pending_cleanup) is not int or max_pending_cleanup < 1:
            raise ValueError("max_pending_cleanup must be >= 1")
        if type(cleanup_retry_batch) is not int or cleanup_retry_batch < 1:
            raise ValueError("cleanup_retry_batch must be >= 1")
        if not callable(getattr(provider, "delete_snapshot", None)):
            raise ValueError("provider must implement delete_snapshot for bounded golden ownership")
        lifetimes = {
            "idle_ttl_s": float(idle_ttl_s),
            "golden_ttl_s": float(golden_ttl_s),
            "reap_interval_s": float(reap_interval_s),
        }
        for name, value in lifetimes.items():
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if value < 0:
                raise ValueError(f"{name} must be >= 0")
        self.provider = provider
        self.tasks = tasks  # Mapping-like with .get(task_id) -> CuaGymTask
        self.spec = spec
        self.settle_ms = settle_ms
        self.exec_timeout = exec_timeout
        self.idle_ttl_s = lifetimes["idle_ttl_s"]
        self.max_goldens = int(max_goldens)
        self.golden_ttl_s = lifetimes["golden_ttl_s"]
        self.reap_interval_s = lifetimes["reap_interval_s"]
        self.max_pending_cleanup = max_pending_cleanup
        self.cleanup_retry_batch = cleanup_retry_batch
        # None keeps the strict eval contract (a broken scorer is a typed fault). A float
        # makes verify tolerant for RL collection over messy corpora: a reward.py that
        # crashes/emits no reward (e.g. CUA-Gym self-tests that `assert reward == 1.0` on
        # the unsolved state) scores this value with a warning instead of aborting the batch.
        self.scorer_error_reward = (
            None if scorer_error_reward is None else float(scorer_error_reward)
        )
        self.env_factory = env_factory
        self._rollouts: dict[str, _Rollout] = {}
        self._goldens: dict[str, _Golden] = {}
        self._session_inflight: dict[str, int] = {}
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._terminal_cleanup_lock = threading.Lock()
        self._cleanup_reservations = 0
        self._cleanup_inflight = 0
        self._pending_snapshot_deletes: list[Any] = []
        self._pending_env_closes: list[Any] = []
        # Fixed stripes avoid the lock-map leak/ABA problem of one dynamically-created lock
        # per session/task. Hash collisions reduce concurrency but cannot break correctness.
        self._session_locks = tuple(threading.Lock() for _ in range(_LOCK_STRIPES))
        self._golden_locks = tuple(threading.Lock() for _ in range(_LOCK_STRIPES))
        self._lifecycle = threading.Condition()
        self._operation_local = threading.local()
        self._state = "open"
        self._active_operations = 0
        self._reaper_stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None

    def start_maintenance(self) -> None:
        """Start active resource maintenance for a long-lived server (idempotent).

        Construction stays thread-free so the framework-free engine composes safely with
        POSIX fork-based scorers. The web adapter starts this worker from app startup.
        """
        if self.reap_interval_s <= 0:
            return
        with self._lifecycle:
            if self._state != "open":
                raise CuaGymError(f"computer engine is {self._state}")
            if self._reaper_thread is not None and self._reaper_thread.is_alive():
                return
            self._reaper_stop.clear()
            self_ref = weakref.ref(self)
            self._reaper_thread = threading.Thread(
                target=self._run_reaper,
                args=(self_ref, self._reaper_stop, self.reap_interval_s),
                name="shinken-nemo-reaper",
                daemon=True,
            )
            self._reaper_thread.start()

    # -- lifecycle ---------------------------------------------------------------

    def seed(
        self,
        session_id: str,
        task_id: str,
        *,
        generation: int | None,
    ) -> dict:
        """Start a rollout, atomically replacing the prior generation on this session."""
        with self._operation(session_id=session_id):
            with self._session_lock(session_id):
                with self._lock:
                    current = self._rollouts.get(session_id)
                if generation is not None and type(generation) is not int:
                    raise CuaGymError("rollout generation must be an integer or None")
                if current is not None:
                    if generation is None:
                        # An initial seed response may be lost. Retrying the same task without
                        # a generation is idempotent; a different task is ambiguous and unsafe.
                        if current.task.task_id != task_id:
                            raise CuaGymError(
                                f"session {session_id!r} is already seeded for "
                                f"task {current.task.task_id!r}"
                            )
                        current.last_used = time.monotonic()
                        return self._seed_result(current, reset_ms=0.0)
                    if current.generation != generation:
                        raise CuaGymError(
                            f"stale rollout generation for session {session_id!r}; "
                            "seed_session ran again"
                        )
                with self._cleanup_admission():
                    return self._seed_new_generation(session_id, task_id)

    def _seed_new_generation(self, session_id: str, task_id: str) -> dict:
        try:
            task = self.tasks.get(task_id)
        except KeyError:
            # ``CuaGymTaskSource.get`` raises while a plain dict returns None.
            task = None
        if task is None:
            raise CuaGymError(f"unknown task_id {task_id!r}")

        # Mint the restart-safe epoch before creating any resource-owning environment.
        # Entropy failures must not leave an initialized but unpublished replica behind.
        generation = secrets.randbits(128)
        env = self.env_factory(task, self.provider, spec=self.spec, exec_timeout=self.exec_timeout)
        try:
            t0 = time.perf_counter()
            with self._golden_lease(task, env) as golden:
                env.golden_checkpoint = golden
                env.reset()
            # Process setup is replayed after every filesystem-tier fork.
            post_fork = task.config.get("shinken_post_fork") or []
            if post_fork:
                sess, run = env._session(), env._guest_exec()
                for step in post_fork:
                    env._apply_setup_step(step, sess, run)
            # Idle age starts only after expensive reset/setup has completed. Constructing the
            # rollout earlier would let maintenance reap a generation immediately after seed.
            published = _Rollout(env, task, generation)
        except BaseException:
            # The old generation remains current unless initialization fully succeeds.
            self._close_env(env)
            raise

        reset_ms = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            previous = self._rollouts.get(session_id)
            self._rollouts[session_id] = published
        if previous is not None:
            self._close_env(previous.env)
        return self._seed_result(published, reset_ms=reset_ms)

    @contextlib.contextmanager
    def _cleanup_admission(self) -> Iterator[None]:
        # Give a recovered provider one bounded retry batch before applying backpressure.
        self._drain_env_closes()
        self._drain_snapshot_deletes()
        # A prior seed may have left a leased golden temporarily above the cache limit.
        # Re-attempt that eviction before deciding whether ownership is still saturated.
        self._evict_goldens()
        with self._lock:
            if self._cleanup_pressure_locked() >= self.max_pending_cleanup:
                raise CuaGymError(
                    "cleanup backlog is at capacity; cleanup-producing operations are disabled "
                    "until maintenance reclaims pending resources"
                )
            self._cleanup_reservations += 1
        try:
            yield
        finally:
            with self._lock:
                self._cleanup_reservations -= 1

    @staticmethod
    def _seed_result(rollout: _Rollout, *, reset_ms: float) -> dict:
        return {
            "task_id": rollout.task.task_id,
            "instruction": rollout.task.instruction,
            "reset_ms": reset_ms,
            "generation": rollout.generation,
        }

    @contextlib.contextmanager
    def _golden_lease(self, task: CuaGymTask, env: ShinkenCuaGymEnv) -> Iterator[Any]:
        """Get/build one task golden and pin it until the caller's reset completes."""
        task_id = task.task_id
        with self._golden_lock(task_id):
            now = time.monotonic()
            with self._lock:
                entry = self._goldens.get(task_id)
                if entry is not None:
                    entry.leases += 1
                    entry.last_used = now
            if entry is None:
                snapshot = env._build_golden()
                entry = _Golden(snapshot=snapshot, last_used=now, leases=1)
                with self._lock:
                    self._goldens[task_id] = entry
        try:
            yield entry.snapshot
        finally:
            with self._lock:
                current = self._goldens.get(task_id)
                if current is entry:
                    entry.leases -= 1
                    entry.last_used = time.monotonic()
            self._evict_goldens()

    def verify(self, session_id: str, *, generation: int) -> float:
        """Score the rollout with the bundle's ``reward.py`` and tear the replica down.

        A scorer fault is a typed error by default; with ``scorer_error_reward`` set it is
        logged and scored as that value so one badly-authored corpus task cannot abort an
        RL collection batch."""
        with self._operation(session_id=session_id):
            with self._session_lock(session_id):
                rollout = self._current_rollout(session_id, generation)
                with self._cleanup_admission():
                    try:
                        return self._score(rollout)
                    finally:
                        detached = self._detach_rollout(session_id, rollout)
                        if detached is not None:
                            self._close_env(detached.env)

    def _score(self, rollout: _Rollout) -> float:
        if self.scorer_error_reward is None:
            return rollout.env.evaluate()
        try:
            return rollout.env.evaluate()
        except CuaGymError as exc:
            _LOG.warning(
                "reward.py fault on task %r scored as %s (RL-tolerant): %s",
                getattr(rollout.task, "task_id", "?"),
                self.scorer_error_reward,
                exc,
            )
            return self.scorer_error_reward

    def end(self, session_id: str, *, generation: int) -> None:
        """Tear down a rollout's replica without scoring (idempotent)."""
        with self._operation(fail_if_closed=False, session_id=session_id) as admitted:
            if not admitted:
                return
            with self._session_lock(session_id):
                with self._lock:
                    rollout = self._rollouts.get(session_id)
                    if (
                        rollout is None
                        or type(generation) is not int
                        or rollout.generation != generation
                    ):
                        return
                with self._cleanup_admission():
                    with self._lock:
                        self._rollouts.pop(session_id, None)
                    self._close_env(rollout.env)

    def close(self, *, delete_goldens: bool = True) -> None:
        """Terminal barrier: reject new work, drain admitted calls, then reclaim resources."""
        if getattr(self._operation_local, "depth", 0):
            raise CuaGymError("cannot close the computer engine from an active engine operation")
        owner = False
        with self._lifecycle:
            if self._state == "open":
                self._state = "closing"
                owner = True
            elif self._state == "closing":
                self._lifecycle.wait_for(lambda: self._state == "closed")

        if not owner:
            # A later close(delete_goldens=True) may release snapshots retained by an earlier
            # close(delete_goldens=False), while replica teardown remains idempotent.
            # An interrupted owning close may already have published ``closed`` while an
            # admitted operation finishes; preserve the terminal barrier on this retry.
            with self._lifecycle:
                self._lifecycle.wait_for(lambda: self._active_operations == 0)
            self._reclaim_terminal_resources(delete_goldens=delete_goldens)
            self._raise_pending_cleanup(include_retained_goldens=delete_goldens)
            return

        try:
            self._reaper_stop.set()
            thread = self._reaper_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join()

            with self._lifecycle:
                self._lifecycle.wait_for(lambda: self._active_operations == 0)

            self._reclaim_terminal_resources(delete_goldens=delete_goldens)
        finally:
            # Even an interrupt cannot strand concurrent close callers in ``closing``.
            # Any resource not conclusively reclaimed remains in an ownership collection.
            self._reaper_stop.set()
            with self._lifecycle:
                self._state = "closed"
                self._lifecycle.notify_all()
        self._raise_pending_cleanup(include_retained_goldens=delete_goldens)

    def _reclaim_terminal_resources(self, *, delete_goldens: bool) -> None:
        """Reclaim terminal ownership without ever exceeding the cleanup high-water mark."""
        with self._terminal_cleanup_lock:
            # Two fair rounds recover the common fail-once case during the app's only shutdown
            # callback. A poison owner is attempted at most twice per close call, preventing an
            # O(N) series of provider timeouts while other healthy owners are still reclaimed.
            for _round in range(2):
                self._drain_terminal_pending_once()
                self._close_retained_rollouts_once()
                if delete_goldens:
                    self._delete_retained_goldens_once()
                with self._lock:
                    remaining = self._terminal_ownership_locked(
                        include_retained_goldens=delete_goldens
                    )
                if remaining == 0:
                    return

    def _drain_terminal_pending_once(self) -> int:
        """Give every currently queued owner one fair retry; return reclaimed count."""
        with self._lock:
            env_remaining = len(self._pending_env_closes)
            snapshot_remaining = len(self._pending_snapshot_deletes)
        reclaimed = 0
        while env_remaining:
            attempted, succeeded = self._drain_env_closes(limit=env_remaining)
            if attempted == 0:
                break
            env_remaining -= attempted
            reclaimed += succeeded
        while snapshot_remaining:
            attempted, succeeded = self._drain_snapshot_deletes(limit=snapshot_remaining)
            if attempted == 0:
                break
            snapshot_remaining -= attempted
            reclaimed += succeeded
        return reclaimed

    def _close_retained_rollouts_once(self) -> int:
        """Try every terminal rollout in place so one poison owner cannot block the rest."""
        with self._lock:
            retained = list(self._rollouts.items())
        reclaimed = 0
        for session_id, rollout in retained:
            try:
                rollout.env.close()
            except Exception:
                _LOG.warning(
                    "failed to close terminal NeMo Gym rollout; retained for retry",
                    exc_info=True,
                )
                continue
            except BaseException:
                # The rollout remains in the ledger before an interrupt propagates.
                raise
            with self._lock:
                if self._rollouts.get(session_id) is rollout:
                    self._rollouts.pop(session_id)
                    reclaimed += 1
        return reclaimed

    def _delete_retained_goldens_once(self) -> int:
        """Try every terminal golden in place, retaining failures without queue growth."""
        with self._lock:
            retained = list(self._goldens.items())
        reclaimed = 0
        delete_snapshot = self.provider.delete_snapshot
        for task_id, entry in retained:
            try:
                delete_snapshot(entry.snapshot)
            except Exception:
                _LOG.warning(
                    "failed to delete terminal NeMo Gym golden; retained for retry",
                    exc_info=True,
                )
                continue
            except BaseException:
                # The golden remains in the ledger before an interrupt propagates.
                raise
            with self._lock:
                if self._goldens.get(task_id) is entry:
                    self._goldens.pop(task_id)
                    reclaimed += 1
        return reclaimed

    @contextlib.contextmanager
    def _operation(
        self, *, fail_if_closed: bool = True, session_id: str | None = None
    ) -> Iterator[bool]:
        admitted = False
        with self._lifecycle:
            if self._state == "open":
                self._active_operations += 1
                admitted = True
            elif fail_if_closed:
                raise CuaGymError(f"computer engine is {self._state}")
        if not admitted:
            yield False
            return
        if session_id is not None:
            with self._lock:
                self._session_inflight[session_id] = self._session_inflight.get(session_id, 0) + 1
        self._operation_local.depth = getattr(self._operation_local, "depth", 0) + 1
        try:
            yield True
        finally:
            self._operation_local.depth -= 1
            if session_id is not None:
                with self._lock:
                    remaining = self._session_inflight[session_id] - 1
                    if remaining:
                        self._session_inflight[session_id] = remaining
                    else:
                        self._session_inflight.pop(session_id, None)
            with self._lifecycle:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._lifecycle.notify_all()

    def _session_lock(self, session_id: str) -> threading.Lock:
        return self._session_locks[hash(session_id) % len(self._session_locks)]

    def _golden_lock(self, task_id: str) -> threading.Lock:
        return self._golden_locks[hash(task_id) % len(self._golden_locks)]

    def current_generation(self, session_id: str) -> int | None:
        """The generation of the session's live rollout, or ``None`` when none is active.

        Lets a transport that cannot reliably thread the per-rollout generation (e.g. a
        multi-server SessionMiddleware whose ``session`` cookie collides across servers,
        while the ``session_id`` still threads) recover the fence value from the
        reliably-keyed ``session_id``. Single rollout per session, so this is unambiguous."""
        with self._lock:
            rollout = self._rollouts.get(session_id)
            return rollout.generation if rollout is not None else None

    def _current_rollout(self, session_id: str, generation: int) -> _Rollout:
        with self._lock:
            rollout = self._rollouts.get(session_id)
        if rollout is None:
            raise CuaGymError(
                f"no live rollout for session {session_id!r} — seed_session must run first"
            )
        if type(generation) is not int or rollout.generation != generation:
            raise CuaGymError(
                f"stale rollout generation for session {session_id!r}; seed_session ran again"
            )
        return rollout

    def _detach_rollout(self, session_id: str, expected: _Rollout) -> _Rollout | None:
        with self._lock:
            if self._rollouts.get(session_id) is not expected:
                return None
            return self._rollouts.pop(session_id)

    def maintenance_once(self) -> dict[str, int]:
        """Synchronously reap idle replicas and evict expired/LRU golden snapshots."""
        with self._operation(fail_if_closed=False) as admitted:
            if not admitted:
                return {"rollouts_reaped": 0, "goldens_evicted": 0}
            if not self._maintenance_lock.acquire(blocking=False):
                return {"rollouts_reaped": 0, "goldens_evicted": 0}
            try:
                return self._maintenance_once()
            finally:
                self._maintenance_lock.release()

    def _maintenance_once(self) -> dict[str, int]:
        result = {
            "rollouts_reaped": self._reap_idle_rollouts(),
            "goldens_evicted": self._evict_goldens(),
        }
        self._drain_env_closes()
        self._drain_snapshot_deletes()
        return result

    def _reap_idle_rollouts(self) -> int:
        if self.idle_ttl_s <= 0:
            return 0
        cutoff = time.monotonic() - self.idle_ttl_s
        with self._lock:
            candidates = list(self._rollouts)
        reaped = 0
        for session_id in candidates:
            session_lock = self._session_lock(session_id)
            if not session_lock.acquire(blocking=False):
                continue
            reserved = False
            try:
                with self._lock:
                    rollout = self._rollouts.get(session_id)
                    if (
                        rollout is None
                        or self._session_inflight.get(session_id, 0)
                        or rollout.last_used >= cutoff
                    ):
                        continue
                    if self._cleanup_pressure_locked() >= self.max_pending_cleanup:
                        break
                    self._cleanup_reservations += 1
                    reserved = True
                    self._rollouts.pop(session_id, None)
                self._close_env(rollout.env)
                reaped += 1
            finally:
                if reserved:
                    with self._lock:
                        self._cleanup_reservations -= 1
                session_lock.release()
        return reaped

    def _evict_goldens(self) -> int:
        now = time.monotonic()
        cutoff = now - self.golden_ttl_s
        with self._lock:
            removable = [
                (task_id, entry) for task_id, entry in self._goldens.items() if entry.leases == 0
            ]
            evicted = {
                task_id
                for task_id, entry in removable
                if self.golden_ttl_s > 0 and entry.last_used < cutoff
            }
            remaining_count = len(self._goldens) - len(evicted)
            if remaining_count > self.max_goldens:
                for task_id, _entry in sorted(removable, key=lambda item: item[1].last_used):
                    if remaining_count <= self.max_goldens:
                        break
                    if task_id not in evicted:
                        evicted.add(task_id)
                        remaining_count -= 1
            available = max(
                0,
                self.max_pending_cleanup - self._cleanup_pressure_locked(),
            )
            selected = sorted(evicted, key=lambda task_id: self._goldens[task_id].last_used)[
                :available
            ]
            snapshots = [self._goldens.pop(task_id).snapshot for task_id in selected]
            # Selecting and queueing are one transaction. Otherwise two concurrent eviction
            # passes can both observe the same free capacity and overflow the high-water mark.
            self._pending_snapshot_deletes.extend(snapshots)
        self._drain_snapshot_deletes()
        return len(snapshots)

    def _drain_snapshot_deletes(self, *, limit: int | None = None) -> tuple[int, int]:
        if not self._cleanup_lock.acquire(blocking=False):
            return 0, 0
        try:
            with self._lock:
                batch = (
                    self.cleanup_retry_batch
                    if limit is None
                    else min(limit, self.cleanup_retry_batch)
                )
                pending = self._pending_snapshot_deletes[:batch]
                del self._pending_snapshot_deletes[:batch]
                self._cleanup_inflight += len(pending)
            if not pending:
                return 0, 0
            failed = []
            completed = 0
            delete_snapshot = getattr(self.provider, "delete_snapshot", None)
            try:
                if delete_snapshot is None:
                    failed = pending
                    completed = len(pending)
                else:
                    for snapshot in pending:
                        try:
                            delete_snapshot(snapshot)
                        except Exception:
                            failed.append(snapshot)
                            completed += 1
                            _LOG.warning(
                                "failed to delete NeMo Gym golden snapshot; queued for retry",
                                exc_info=True,
                            )
                        else:
                            completed += 1
            finally:
                # If an interrupt escapes a provider call, retain the current and all
                # unattempted handles. Snapshot deletion is required to be idempotent.
                failed.extend(pending[completed:])
                with self._lock:
                    self._pending_snapshot_deletes.extend(failed)
                    self._cleanup_inflight -= len(pending)
            return len(pending), len(pending) - len(failed)
        finally:
            self._cleanup_lock.release()

    def _close_env(self, env: Any) -> None:
        try:
            env.close()
        except BaseException as exc:
            with self._lock:
                if not any(pending is env for pending in self._pending_env_closes):
                    self._pending_env_closes.append(env)
            if isinstance(exc, Exception):
                _LOG.warning(
                    "failed to close NeMo Gym rollout environment; queued for retry",
                    exc_info=True,
                )
                return
            # Interrupts still propagate, but only after the ownership handle is durable.
            raise

    def _drain_env_closes(self, *, limit: int | None = None) -> tuple[int, int]:
        if not self._cleanup_lock.acquire(blocking=False):
            return 0, 0
        try:
            with self._lock:
                batch = (
                    self.cleanup_retry_batch
                    if limit is None
                    else min(limit, self.cleanup_retry_batch)
                )
                pending = self._pending_env_closes[:batch]
                del self._pending_env_closes[:batch]
                self._cleanup_inflight += len(pending)
            failed = []
            completed = 0
            try:
                for env in pending:
                    try:
                        env.close()
                    except Exception:
                        failed.append(env)
                        completed += 1
                        _LOG.warning(
                            "failed to close queued NeMo Gym rollout environment",
                            exc_info=True,
                        )
                    else:
                        completed += 1
            finally:
                # Preserve ownership across interrupts just like the snapshot retry path.
                failed.extend(pending[completed:])
                with self._lock:
                    self._pending_env_closes.extend(failed)
                    self._cleanup_inflight -= len(pending)
            return len(pending), len(pending) - len(failed)
        finally:
            self._cleanup_lock.release()

    def _raise_pending_cleanup(self, *, include_retained_goldens: bool) -> None:
        with self._lock:
            env_count = len(self._pending_env_closes)
            snapshot_count = len(self._pending_snapshot_deletes)
            rollout_count = len(self._rollouts)
            golden_count = len(self._goldens) if include_retained_goldens else 0
        if env_count or snapshot_count or rollout_count or golden_count:
            raise CuaGymError(
                "computer engine closed with retryable cleanup pending: "
                f"{env_count} environment(s), {snapshot_count} snapshot(s), "
                f"{rollout_count} retained rollout(s), {golden_count} retained golden(s); "
                "call close() again"
            )

    def _cleanup_backlog_locked(self) -> int:
        return len(self._pending_env_closes) + len(self._pending_snapshot_deletes)

    def _cleanup_pressure_locked(self) -> int:
        return self._cleanup_backlog_locked() + self._cleanup_inflight + self._cleanup_reservations

    def _terminal_ownership_locked(self, *, include_retained_goldens: bool) -> int:
        return (
            self._cleanup_pressure_locked()
            + len(self._rollouts)
            + (len(self._goldens) if include_retained_goldens else 0)
        )

    @staticmethod
    def _run_reaper(
        engine_ref: "weakref.ReferenceType[ShinkenComputerEngine]",
        stop: threading.Event,
        interval_s: float,
    ) -> None:
        while not stop.wait(interval_s):
            engine = engine_ref()
            if engine is None:
                return
            try:
                engine.maintenance_once()
            except Exception:
                _LOG.exception("NeMo Gym resource maintenance failed")
            finally:
                del engine

    # -- tools -------------------------------------------------------------------

    def tool(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, Any],
        *,
        generation: int,
    ) -> str:
        """Dispatch one tool call; the returned string is what the model sees."""
        with self._operation(session_id=session_id):
            with self._session_lock(session_id):
                rollout = self._current_rollout(session_id, generation)
                rollout.last_used = time.monotonic()
                handler = getattr(self, f"_tool_{name.removeprefix('computer_')}", None)
                if handler is None:
                    raise CuaGymError(f"unknown tool {name!r}")
                try:
                    return handler(rollout, dict(arguments))
                except CuaGymError:
                    raise
                except Exception as exc:  # the model sees a typed, actionable error string
                    return f"error: {type(exc).__name__}: {exc}"
                finally:
                    # TTL is measured from completed activity, so a long call is never reaped.
                    rollout.last_used = time.monotonic()

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


def _install_request_tracer(app: Any, session_id_key: str) -> None:
    """SHINKEN_NG_DEBUG diagnostic: log every inbound request's path, the cookies it
    carried, and the session_id/generation the server resolved — to pin down where a
    multi-server session cookie drops the per-rollout state. Off unless the env is set."""
    import sys

    @app.middleware("http")
    async def _trace(request: Any, call_next: Any) -> Any:
        cookie_names = sorted(request.cookies.keys())
        try:
            session = request.session
            sid = session.get(session_id_key)
            gen = session.get(_SESSION_GENERATION_KEY)
        except Exception as exc:  # noqa: BLE001 — diagnostic must never break the request
            sid, gen = f"<no-session:{exc}>", None
        print(
            f"[NG_TRACE] {request.method} {request.url.path} cookies={cookie_names} "
            f"session_id={sid!r} generation={gen!r}",
            file=sys.stderr,
            flush=True,
        )
        return await call_next(request)


def _install_engine_shutdown(app: Any, engine: ShinkenComputerEngine) -> None:
    """Bind active maintenance and engine-owned resources to the web app lifetime.

    Starlette's classic ``add_event_handler``/``on_event`` surface was removed in newer
    releases (lifespan-only apps), so when the app doesn't expose it, wrap the router's
    lifespan context instead — same lifetime contract on both API generations."""
    import asyncio
    import contextlib

    def start_engine() -> None:
        engine.start_maintenance()

    async def close_engine() -> None:
        await asyncio.to_thread(engine.close)

    add_handler = getattr(app, "add_event_handler", None)
    if callable(add_handler):
        add_handler("startup", start_engine)
        add_handler("shutdown", close_engine)
        return

    router = app.router
    inner = router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan_with_engine(app_: Any) -> Any:
        start_engine()
        try:
            async with inner(app_) as state:
                yield state
        finally:
            await close_engine()

    router.lifespan_context = lifespan_with_engine


def _request_generation(request: Any) -> int:
    generation = request.session.get(_SESSION_GENERATION_KEY)
    if type(generation) is not int:
        raise CuaGymError("session carries no Shinken rollout generation — seed_session first")
    return generation


def _seed_request_generation(request: Any) -> int | None:
    generation = request.session.get(_SESSION_GENERATION_KEY)
    if generation is not None and type(generation) is not int:
        raise CuaGymError("session carries an invalid Shinken rollout generation")
    return generation


def _require_single_worker(config: Any) -> None:
    workers = config.get("num_workers") if isinstance(config, Mapping) else None
    if workers is None:
        workers = getattr(config, "num_workers", 1)
    if workers is not None and (type(workers) is not int or workers != 1):
        raise CuaGymError(
            "ShinkenComputerEngine state is process-local; num_workers must be 1. "
            "Scale with multiple session-routed resources-server instances instead."
        )


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
            _require_single_worker(self.config)
            self._engine = engine_factory(self.config)

        def setup_webserver(self) -> FastAPI:
            app = super().setup_webserver()
            for tool in COMPUTER_TOOLS:
                app.post(f"/{tool['name']}")(self._make_tool_route(tool["name"]))
            # ``SimpleResourcesServer`` owns the FastAPI app, while the engine owns all
            # live replicas and golden snapshots. Tie those lifetimes together so normal
            # server shutdown cannot orphan provider resources.
            _install_engine_shutdown(app, self._engine)
            if os.environ.get("SHINKEN_NG_DEBUG"):
                _install_request_tracer(app, SESSION_ID_KEY)
            return app

        def _make_tool_route(self, name: str):
            async def route(request: Request) -> PlainTextResponse:
                session_id = request.session[SESSION_ID_KEY]
                generation = self._resolve_generation(request, session_id)
                args = await request.json()
                out = await asyncio.to_thread(
                    self._engine.tool,
                    session_id,
                    name,
                    args if isinstance(args, dict) else {},
                    generation=generation,
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
            generation = _seed_request_generation(request)
            seeded = await asyncio.to_thread(
                self._engine.seed, session_id, task_id, generation=generation
            )
            request.session[_SESSION_GENERATION_KEY] = seeded["generation"]
            if os.environ.get("SHINKEN_NG_DEBUG"):
                import sys

                print(
                    f"[NG_TRACE] SEED session_id={session_id!r} task={task_id!r} "
                    f"generation={seeded['generation']!r}",
                    file=sys.stderr,
                    flush=True,
                )
            return BaseSeedSessionResponse()

        async def verify(self, request: Request, body: BaseVerifyRequest) -> BaseVerifyResponse:
            session_id = request.session[SESSION_ID_KEY]
            generation = self._resolve_generation(request, session_id)
            reward = await asyncio.to_thread(self._engine.verify, session_id, generation=generation)
            return BaseVerifyResponse(**body.model_dump(), reward=reward)

        def _resolve_generation(self, request: Request, session_id: str) -> int:
            """The rollout generation for a fenced tool/verify call, re-asserted into the
            session so Starlette re-emits the Set-Cookie.

            Value precedence: the client session's threaded value, else the engine's
            active generation for this ``session_id`` (recovering from a transport that
            drops the per-rollout cookie but keeps ``session_id``). The re-assert is the
            load-bearing part over a multi-server agent: each server namespaces its own
            session cookie and the agent chains ``resources_server_cookies`` from one tool
            response to the next, so a response that does NOT re-emit our cookie breaks
            the chain and the next call arrives session-less. Writing the generation back
            marks the session modified on every call, keeping the cookie alive end to end."""
            generation = request.session.get(_SESSION_GENERATION_KEY)
            if type(generation) is not int:  # missing / non-int (bool) — recover it
                generation = self._engine.current_generation(session_id)
                if generation is None:
                    raise CuaGymError(
                        "session carries no Shinken rollout generation — seed_session first"
                    )
            request.session[_SESSION_GENERATION_KEY] = generation
            return generation

    return ShinkenComputerResourcesServer
