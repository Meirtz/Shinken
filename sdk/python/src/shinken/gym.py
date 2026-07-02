"""Fork-native gym adapter — the trainer-facing ``make/reset/step/evaluate`` shape.

RL stacks consume environments through a gym-style surface (cua-bench, CUA-Gym, OSWorld
wrappers all ship one), and in every one of them ``reset()`` re-provisions the sandbox —
cua-bench's own ``Environment.reset()`` closes the session and creates a brand-new VM per
episode (see ``notes/cua-teardown.md`` §4/§7). This module ships the same API shape with
the runtime-state semantics underneath (D5): **reset() restores from the task checkpoint**.
Fidelity is provider-specific: the Docker disk tier preserves filesystem state and
restarts processes, while an explicitly requested memory tier preserves
process/GUI state. Tasks must replay launch/focus after a disk restore instead of assuming a
byte-identical live desktop. Measured restore latencies are in
``docs/engineering/benchmarks.md`` §1.

Providers without a snapshot tier still get the full harness: reset-strategy resolution
(``reset_strategy="auto"``, the default) forks where the provider serves the
checkpoint+resume pair and degrades **loudly** to ``recreate`` — fresh sandbox +
per-episode ``task.setup`` replay, the cold semantics every other gym has — where it does
not (every ``shinken.backends`` provider). Recreate additionally requires a real
create/destroy lifecycle: the attach-only ``ExternalProvider`` stays a typed failure,
because recreate over it would reuse one live desktop across episodes.
``info["reset_strategy"]`` and the trajectory metadata record which path actually ran,
and ``info["reset_ms"]`` stays the honest apples-to-apples re-provision number on both
paths.

Everything here is a *consumer* of the narrow waist (``docs/design/agent-runtime.md``):
sessions come from a provider, episodes are recorded as the existing typed
:class:`~shinken.runtime.trajectory.Trajectory`, and no scorer/reward semantics leak into
the runtime. The pieces:

- :class:`ShinkenGymEnv` — one task on one provider: ``make()`` (boot base → ``task.setup``
  once → golden checkpoint), ``reset()`` (fork-from-golden; ``info["reset_ms"]`` is the
  measured fork→connected latency), ``step(action)`` (canonical ACI dicts **or raw model
  text** routed through :func:`shinken.dialect.parse_actions` — incl. the wild-type XML
  tool-call grammars), ``evaluate()`` (the task verifier, ``shinken.eval`` receipt
  plumbing), ``close()``/``dispose()``.
- :class:`ShinkenGymPool` — N envs sharing ONE golden checkpoint, one
  :class:`~shinken.SharedLoop` (one client thread for all sessions) and one
  :class:`~shinken.FrameCache` (cross-replica frame dedup), with **parallel reset** — the
  fan-out fork.
- :func:`episodes_to_records` / :func:`to_hf_dataset` — the HF-``datasets``-shaped exporter
  (dict-of-lists, one row per step, images as PNG bytes) trainers ingest directly, with the
  ``exit_reason``/failure-taxonomy columns the poll-and-recreate stacks lack.
- :class:`MultiTurnDataloader` — a cua-bench-``MultiTurnDataloader``-shaped iterator
  (duck-typed; **no TRL/torch/tokenizer dependency**) yielding observation batches from the
  pool and applying raw model responses via ``async_step``, auto-resetting (= re-forking)
  finished envs.

A *task* is duck-typed to ``shinken.eval.Task``: ``.name``, optional ``.setup(session)``
(run once into the golden state), optional ``.verify(session)`` (returns a
``VerifierReceipt``-shaped object or a float reward), plus an optional ``.instruction``
string (:class:`GymTask` adds it; ``eval.Task`` works as-is).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ._lifecycle import connect_owned_handle
from .client import FrameCache, SharedLoop
from .dialect import parse_actions
from .errors import SandboxDied, ShinkenError, is_connection_loss
from .eval import Task, coerce_verifier_receipt
from .providers.base import UnsupportedProviderOperation
from .runtime.trajectory import Step, Trajectory

_log = logging.getLogger("shinken.gym")

__all__ = [
    "EXPORT_COLUMNS",
    "Episode",
    "GymError",
    "GymTask",
    "MultiTurnDataloader",
    "ShinkenGymEnv",
    "ShinkenGymPool",
    "Task",
    "episodes_to_records",
    "make",
    "to_hf_dataset",
]

#: Control verbs that terminate an episode instead of dispatching to the runtime
#: (``shinken.dialect.DONE`` and its ``<stop/>`` sibling).
_CONTROL_VERBS = ("done", "stop")

#: terminal sentinel -> trajectory ``exit_reason`` (the rollout-loop mapping, #56).
_EXIT_REASON_FOR_TERMINAL = {
    "done": "task_complete",
    "stopped": "task_complete",
    "max_steps": "max_steps",
}

#: Reset strategies for the gym knob: ``fork`` = golden-checkpoint fork (strict),
#: ``recreate`` = fresh sandbox + ``task.setup`` replay per episode (the semantics every
#: other gym has), ``auto`` = fork when the provider serves the checkpoint+resume pair,
#: loud degrade to recreate when it does not. Distinct from
#: ``ProviderCapabilities.reset_strategy`` (``providers/base.py``), which describes the
#: provider's OWN ``reset()`` verb — the fork-capable Docker provider advertises
#: ``reset_strategy="recreate"`` there — so resolution reads the boolean
#: checkpoint/resume/lifecycle flags, never that field.
_RESET_STRATEGIES = ("auto", "fork", "recreate")


class GymError(ShinkenError):
    """A gym lifecycle/contract violation (no live replica, no verifier, bad reward shape)."""


#: **Deprecated alias of the unified** :class:`shinken.eval.Task` — the one Task
#: dataclass shared by eval and gym (the gym's ``instruction``/``metadata`` are
#: optional fields on it; ``run`` is simply unused here because the *policy* drives
#: ``step()``). ``verify`` may return a ``VerifierReceipt``-shaped object (``.passed``
#: → reward 1.0/0.0) or a float reward directly. Construct extras by KEYWORD
#: (``GymTask("name", instruction="…")``) — positionally, ``instruction`` would land
#: in the unified Task's ``run`` slot.
GymTask = Task


def _reward_from(verdict: Any) -> float:
    """Normalize a verifier's return into a float reward: float/int pass through,
    ``VerifierReceipt``-shaped objects (``.passed`` or ``{"passed": …}``) map to 1.0/0.0.
    Anything else is a typed error — never a silent 0.0."""
    if isinstance(verdict, bool):  # bool before int: True is an int subclass
        return 1.0 if verdict else 0.0
    if isinstance(verdict, int | float):
        reward = float(verdict)
        if not math.isfinite(reward):
            raise GymError(f"verifier returned a non-finite reward: {reward!r}")
        return reward
    try:
        receipt = coerce_verifier_receipt(verdict)
    except Exception as exc:  # noqa: BLE001 — normalize the shared verifier contract here
        raise GymError(
            f"verifier returned an invalid receipt: {exc}; expected a float reward or a "
            "non-empty, internally consistent VerifierReceipt"
        ) from exc
    return 1.0 if receipt.passed else 0.0


@dataclass
class Episode:
    """One finished episode: the typed :class:`Trajectory` plus the consumer-attached
    verdict (reward) and the runtime-state receipt (which checkpoint, how fast the fork
    was). The trajectory itself stays verdict-free by design — the reward lives here."""

    index: int
    task: str
    instruction: str
    trajectory: Trajectory
    reward: float | None
    reset_ms: float
    info: dict = field(default_factory=dict)
    # Stable join key across envs, pools, workers, process restarts, and merged datasets.
    # ``index`` remains the env-local ordinal for backward compatibility.
    episode_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "episode_id": self.episode_id,
            "task": self.task,
            "instruction": self.instruction,
            "trajectory": self.trajectory.to_dict(),
            "reward": self.reward,
            "reset_ms": round(self.reset_ms, 3),
            "info": self.info,
        }


# ------------------------------------------------------------------------------ the env


class ShinkenGymEnv:
    """One task as a gym environment over a Shinken provider, with **fork-native reset**.

    Lifecycle::

        env = shinken.gym.make(task, provider)   # boot base → setup once → golden ckpt
        obs, info = env.reset()                  # fork replica; info["reset_ms"]
        obs, reward, done, info = env.step(model_text_or_actions)
        reward = env.evaluate()                  # the task verifier, on demand
        env.dispose()                            # replica + golden snapshot reclaimed

    ``observation`` knob: ``"screenshot"`` (default — the screenshot dict, with actions
    and the post-step frame fused into ~1 RTT via the pipelined ``Sandbox.step``) or
    ``"structured"`` (the guest a11y tree observation — ``tree_text``/``elements``).

    ``reset_strategy`` knob: ``"auto"`` (default — fork when the provider has a snapshot
    tier, loud degrade to recreate when it does not, so the same harness runs over the
    fork-native providers AND the snapshot-less ``shinken.backends`` providers),
    ``"fork"`` (strict — a snapshot-less provider raises the typed
    ``UnsupportedProviderOperation``), or ``"recreate"`` (fresh sandbox + ``task.setup``
    replay per episode — the cold semantics every other gym has, on purpose).
    ``info["reset_strategy"]`` and the trajectory metadata record what actually ran.

    ``step(action)`` accepts a canonical ACI action dict, a list of them, or **raw model
    text** parsed by :func:`shinken.dialect.parse_actions` with ``action_format``
    (``"auto"`` routes the XML tool-call grammars too); a ``<done/>``/``<stop/>`` control
    action ends the episode without dispatching. A malformed text action raises the typed
    ``DialectError`` (a teaching error for the policy loop — see
    :meth:`MultiTurnDataloader.async_step` for the collection-loop handling).

    Every episode is recorded as a typed :class:`Trajectory` and appended to
    :attr:`episodes` (as an :class:`Episode`, carrying the reward out-of-band) when it
    ends — on ``done``, on the next ``reset()``, on :meth:`abort`, or on ``close()``.

    Provider fidelity is enforced before setup: filesystem checkpoints restart processes;
    process-memory tasks must use a provider that advertises that tier. The former live
    warm-pool graft is intentionally disabled until pool-hit/pool-miss equivalence can be
    proven.
    """

    def __init__(
        self,
        task: Any,
        provider: Any,
        *,
        spec: Any = None,
        observation: str = "screenshot",
        action_format: str = "auto",
        reset_strategy: str = "auto",
        max_steps: int | None = None,
        observe_args: dict | None = None,
        connect_kwargs: dict | None = None,
    ) -> None:
        if observation not in ("screenshot", "structured"):
            raise GymError(f"unknown observation kind {observation!r}")
        if reset_strategy not in _RESET_STRATEGIES:
            raise GymError(
                f"unknown reset_strategy {reset_strategy!r} (one of {_RESET_STRATEGIES})"
            )
        self.task = task
        self.provider = provider
        self.spec = spec
        self.observation = observation
        self.action_format = action_format
        self.reset_strategy = reset_strategy
        self.max_steps = max_steps
        self.observe_args = dict(observe_args or {})
        self.connect_kwargs = dict(connect_kwargs or {})
        #: Latched by :meth:`make` when resolution lands on recreate (explicit knob, or
        #: the honest ``auto`` degrade). Fork is never latched — it is DERIVED from
        #: ``golden_checkpoint`` (see :attr:`resolved_reset_strategy`), so the documented
        #: golden-sharing pattern can never desync from the reported strategy.
        self._recreate_latched = False
        self.golden_checkpoint: str | None = None
        #: False when the checkpoint is shared/borrowed (pool siblings): dispose() then
        #: never deletes the underlying snapshot.
        self.owns_checkpoint = True
        self.episodes: list[Episode] = []
        self.last_receipt: Any = None
        self._handle: Any = None  # current replica
        self._sess: Any = None  # connected session for the current replica
        self._steps: list[Step] = []  # in-flight episode
        # The exact observation returned to the policy for its next decision. A
        # trajectory Step records this as s_t before dispatch and separately retains
        # the post-action observation as next_observation (s_{t+1}).
        self._current_observation: dict | None = None
        self._episode_open = False
        self._episode_id: str | None = None
        self._reset_ms = 0.0
        self._done = False

    # --- lifecycle ---------------------------------------------------------------------

    @property
    def resolved_reset_strategy(self) -> str | None:
        """What this env actually resolved to: ``"fork"`` when a golden checkpoint
        exists (built, borrowed from a pool sibling, or assigned directly — the
        ``benchmarks/bench_agent_quality.py`` sharing pattern), ``"recreate"`` when the
        recreate latch is set, ``None`` before :meth:`make` resolves."""
        if self.golden_checkpoint is not None:
            return "fork"
        if self._recreate_latched:
            return "recreate"
        return None

    def make(self) -> ShinkenGymEnv:
        """Resolve the reset strategy and, on the fork path, build the golden checkpoint
        (idempotent): create a base sandbox, run ``task.setup`` once, ``checkpoint``,
        destroy the base — every subsequent :meth:`reset` forks that single checkpoint.
        On the recreate path there is nothing to prebuild: provisioning (and the
        spec/provider preflight with it) defers to the first :meth:`reset`.

        ``auto`` resolves from the provider's honest ``ProviderCapabilities`` when
        published: the fork path needs the pair the gym drives (``supports_checkpoint``
        AND ``supports_resume``), and recreate additionally needs
        ``supports_lifecycle`` — an attach-only provider (``ExternalProvider``) stays a
        typed failure, because recreate over it would reuse ONE live sandbox across
        episodes. A provider with no such advertisement is probed by the golden build
        itself; a probe that degrades costs one boot and one extra ``task.setup`` run,
        so capability-less providers with side-effectful setups should pass an explicit
        ``reset_strategy``."""
        if self.reset_strategy == "recreate" and self.golden_checkpoint is not None:
            raise GymError(
                "reset_strategy='recreate' conflicts with an assigned golden_checkpoint "
                "— drop the checkpoint sharing or use reset_strategy='auto'/'fork'"
            )
        if self.resolved_reset_strategy is not None:
            return self
        if self.reset_strategy == "recreate":
            self._require_lifecycle()
            self._recreate_latched = True
            return self
        fork_path, lifecycle = self._capability_verdict()
        if fork_path is False or not callable(getattr(self.provider, "checkpoint", None)):
            detail = (
                "capabilities advertise no checkpoint+resume pair"
                if fork_path is False
                else "provider defines no checkpoint verb"
            )
            if self.reset_strategy == "fork":
                raise UnsupportedProviderOperation(
                    f"{type(self.provider).__name__} cannot serve reset_strategy='fork': {detail}"
                )
            if lifecycle is False:
                self._raise_no_lifecycle(detail)
            self._degrade_to_recreate(detail)
            return self
        # Fork advertised (True) or unknown (no ProviderCapabilities-shaped object):
        # build the golden — for the unknown case this build IS the probe.
        base = self.provider.create(self.spec)
        try:
            sess = self.provider.connect(base, **self.connect_kwargs)
            try:
                self._run_setup(sess)
                try:
                    self.golden_checkpoint = self.provider.checkpoint(base)
                except UnsupportedProviderOperation:
                    if self.reset_strategy == "fork":
                        raise
                    self._degrade_to_recreate("the checkpoint probe raised the typed error")
            finally:
                with contextlib.suppress(Exception):
                    sess.close()
        finally:
            with contextlib.suppress(Exception):
                self.provider.destroy(base)
        return self

    def reset(self) -> tuple[dict, dict]:
        """Start a fresh episode and return ``(observation, info)``. On the fork path
        the replica is **forked from the golden checkpoint** (built first if :meth:`make`
        was never called); on the recreate path a fresh sandbox is provisioned and
        ``task.setup`` replays into it — the CUA-Gym semantics, for providers with no
        snapshot tier. ``info["reset_ms"]`` is the measured latency to a policy-ready
        replica — fork→connected on the fork path, create→connected→setup on the
        recreate path — the honest apples-to-apples re-provision number;
        ``info["reset_strategy"]`` says which path this env resolved to."""
        self.make()
        self._finalize_episode(terminal="stopped")  # an un-done episode is "stopped"
        self._teardown_replica()
        recreate = self.resolved_reset_strategy == "recreate"
        t0 = time.perf_counter()
        if recreate:
            handle = self.provider.create(self.spec)
        else:
            handle = self.provider.resume(self.golden_checkpoint)
        sess = connect_owned_handle(self.provider, handle, **self.connect_kwargs)
        self._handle = handle
        self._sess = sess
        self._steps = []
        self._done = False
        try:
            if recreate:
                self._run_setup(sess)  # per-episode replay — what fork amortizes away
            self._reset_ms = (time.perf_counter() - t0) * 1000.0
            obs = self._observe()
        except BaseException:
            self._teardown_replica()
            raise
        self._current_observation = obs
        self._episode_id = uuid.uuid4().hex
        self._episode_open = True
        info = {
            "task": getattr(self.task, "name", ""),
            "instruction": getattr(self.task, "instruction", "") or "",
            "reset_ms": self._reset_ms,
            "reset_strategy": self.resolved_reset_strategy,
            "episode": len(self.episodes),
            "episode_id": self._episode_id,
            "golden_checkpoint": self.golden_checkpoint,
        }
        return obs, info

    # --- the step loop -----------------------------------------------------------------

    def step(self, action: str | dict | list[dict]) -> tuple[dict, float | None, bool, dict]:
        """Apply one policy turn and return ``(observation, reward, done, info)``.

        ``action`` is a canonical ACI dict, an ordered list of them, or raw model text
        (parsed via ``parse_actions(text, format=self.action_format)`` — the Shinken tag
        dialect and the XML tool-call grammars both work). ``reward`` is ``None`` until
        the episode ends; on ``done`` the task verifier runs (when present) and its
        reward is returned and attached to the episode. ``done`` is set by a
        ``<done/>``/``<stop/>`` control action or the ``max_steps`` budget.

        Infrastructure death is typed: a dead replica raises
        :class:`~shinken.errors.SandboxDied` after recording the episode with
        ``exit_reason="sandbox_died"`` (retry by calling :meth:`reset` — a fresh fork)."""
        sess = self._session()
        if self._done:
            raise GymError("episode is done — call reset() for a fresh fork")
        if self._current_observation is None:
            raise GymError("episode has no current observation — call reset() for a fresh fork")
        agent_observation = self._current_observation
        raw_text = action if isinstance(action, str) else None
        actions = self._coerce_actions(action)
        control_index = next(
            (i for i, candidate in enumerate(actions) if candidate.get("verb") in _CONTROL_VERBS),
            None,
        )
        control = dict(actions[control_index]) if control_index is not None else None
        control_verb = control.get("verb") if control is not None else None
        if control_index is not None and control_index + 1 < len(actions):
            raise GymError("actions after a terminal control are not allowed")
        dispatch = actions if control_index is None else actions[:control_index]
        done = control is not None
        terminal = (
            "done" if control_verb == "done" else ("stopped" if control_verb == "stop" else None)
        )
        info: dict = {}
        try:
            obs, results = self._dispatch_and_observe(sess, dispatch)
        except Exception as exc:
            reason = (
                "sandbox_died"
                if isinstance(exc, SandboxDied) or is_connection_loss(exc)
                else "agent_error"
            )
            self._finalize_episode(terminal="aborted", exit_reason=reason, error=f"{reason}: {exc}")
            raise
        if results is not None:
            info["results"] = results["results"]
            if results.get("failure_kind") == "sandbox_died":
                err = next((r.get("error") for r in results["results"] if not r["ok"]), "")
                self._finalize_episode(
                    terminal="aborted", exit_reason="sandbox_died", error=f"sandbox_died: {err}"
                )
                raise SandboxDied(f"replica died mid-step: {err}")
        # ``obs`` is what the policy receives for its NEXT turn. Update the cursor only
        # after dispatch completed, then record the transition as (s_t, a_t, s_{t+1}).
        self._current_observation = obs
        step_info: dict = {}
        if raw_text is not None:
            step_info["raw_text"] = raw_text
        step_index = len(self._steps)
        episode_index = len(self.episodes)
        self._steps.append(
            Step(
                index=step_index,
                observation=agent_observation,
                actions=dispatch,
                info=step_info,
                next_observation=obs,
                control=control,
            )
        )
        if not done and self.max_steps is not None and len(self._steps) >= self.max_steps:
            done, terminal = True, "max_steps"
        reward: float | None = None
        if done:
            reward, scorer_error = self._evaluate_for_done()
            if scorer_error is not None:
                info["scorer_error"] = scorer_error
            self._finalize_episode(
                terminal=terminal,
                reward=reward,
                exit_reason="scorer_error" if scorer_error is not None else None,
                error=scorer_error,
            )
            info["terminal"] = terminal
        info["step"] = step_index
        info["episode"] = episode_index
        info["episode_id"] = self._episode_id
        return obs, reward, done, info

    def evaluate(self) -> float:
        """Run the task verifier against the live replica and return the reward (float
        passthrough, or 1.0/0.0 from a ``VerifierReceipt``-shaped ``.passed``). The raw
        verdict is kept in :attr:`last_receipt`. Typed errors, never a silent 0.0."""
        verify = getattr(self.task, "verify", None)
        if verify is None:
            raise GymError(f"task {getattr(self.task, 'name', '?')!r} has no verifier")
        verdict = verify(self._session())
        self.last_receipt = verdict
        return _reward_from(verdict)

    def abort(self, error: str, *, exit_reason: str = "agent_error") -> None:
        """Record the in-flight episode as aborted (default ``agent_error`` — e.g. the
        policy emitted unparseable actions) without tearing the replica down; the next
        :meth:`reset` forks fresh."""
        self._finalize_episode(terminal="aborted", exit_reason=exit_reason, error=error)

    def close(self) -> None:
        """Finalize any in-flight episode and destroy the current replica. The golden
        checkpoint survives — ``reset()`` works again; :meth:`dispose` reclaims it."""
        self._finalize_episode(terminal="stopped")
        self._teardown_replica()

    def dispose(self) -> None:
        """Full cleanup: the replica AND (when owned) the golden snapshot image. On the
        fork path a later :meth:`reset` rebuilds a fresh golden checkpoint; the recreate
        latch survives dispose (a provider does not grow a snapshot tier), so no
        re-probe is paid."""
        self.close()
        if (
            self.golden_checkpoint is not None
            and self.owns_checkpoint
            and hasattr(self.provider, "delete_snapshot")
        ):
            with contextlib.suppress(Exception):
                self.provider.delete_snapshot(self.golden_checkpoint)
        self.golden_checkpoint = None

    def __enter__(self) -> ShinkenGymEnv:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.dispose()

    @property
    def session(self) -> Any:
        """The live replica's connected session (the same object ``task.verify`` sees).
        Lets a harness run out-of-band readiness probes or auxiliary captures against the
        current fork; raises :class:`GymError` when no replica is live (call ``reset()``).
        """
        return self._session()

    # --- internals ---------------------------------------------------------------------

    def _session(self) -> Any:
        if self._sess is None:
            raise GymError("no live replica — call reset() first")
        return self._sess

    def _capability_verdict(self) -> tuple[bool | None, bool | None]:
        """``(fork_path, lifecycle)`` from the provider's honest advertisement.
        ``fork_path`` is True only for the exact pair the gym drives
        (``supports_checkpoint`` AND ``supports_resume`` — ``supports_fork`` alone
        advertises a verb the gym never calls). Both are ``None`` when the provider
        publishes no ``ProviderCapabilities``-shaped object (absent attribute, or a
        foreign shape such as a dict) — unknown, so the golden-build probe decides."""
        caps = getattr(self.provider, "capabilities", None)
        if caps is None or not hasattr(caps, "supports_checkpoint"):
            return None, None
        fork_path = bool(getattr(caps, "supports_checkpoint", False)) and bool(
            getattr(caps, "supports_resume", False)
        )
        return fork_path, bool(getattr(caps, "supports_lifecycle", False))

    def _run_setup(self, sess: Any) -> None:
        """The one setup contract both strategy paths share (golden build and
        per-episode recreate replay)."""
        setup = getattr(self.task, "setup", None)
        if setup is not None:
            setup(sess)

    def _degrade_to_recreate(self, detail: str) -> None:
        _log.warning(
            "provider %s has no usable fork tier (%s); gym reset degrades to recreate "
            "(fresh sandbox + task.setup replay per episode)",
            type(self.provider).__name__,
            detail,
        )
        self._recreate_latched = True

    def _require_lifecycle(self) -> None:
        _fork_path, lifecycle = self._capability_verdict()
        if lifecycle is False:
            self._raise_no_lifecycle("no fork tier requested or available")

    def _raise_no_lifecycle(self, detail: str) -> None:
        raise UnsupportedProviderOperation(
            f"{type(self.provider).__name__} cannot serve recreate resets ({detail}): it "
            "advertises supports_lifecycle=False (attach-only) — recreate would reuse ONE "
            "live sandbox across episodes; gym needs a real create/destroy lifecycle or a "
            "checkpoint tier"
        )

    def _coerce_actions(self, action: str | dict | list[dict]) -> list[dict]:
        if isinstance(action, str):
            return parse_actions(action, format=self.action_format)
        if isinstance(action, dict):
            return [action]
        if isinstance(action, list):
            return list(action)
        raise GymError(f"unsupported action type {type(action).__name__!r}")

    def _dispatch_and_observe(self, sess: Any, dispatch: list[dict]) -> tuple[dict, dict | None]:
        """Run the actions and capture the post-step observation. On the screenshot knob
        the pipelined ``Sandbox.step`` fuses both into ~1 RTT; the structured knob pays
        ``act_batch`` + a guest ``observe``."""
        if self.observation == "screenshot" and dispatch:
            res = sess.step(dispatch, observe=self.observe_args or {})
            if res.get("observation") is not None:
                return res["observation"], res
            return self._observe(), res  # fused frame denied/missing — observe explicitly
        results = sess.act_batch(dispatch) if dispatch else None
        return self._observe(), results

    def _observe(self) -> dict:
        sess = self._session()
        if self.observation == "structured":
            return sess.observe(structured=True)
        kw = {
            k: v
            for k, v in self.observe_args.items()
            if k in ("format", "quality", "max_long_edge", "dedup")
        }
        return sess.screenshot(**kw)

    def _evaluate_for_done(self) -> tuple[float | None, str | None]:
        """Episode-end reward: the verifier's verdict, or ``(None, error)`` when the
        verifier itself failed (a scorer fault is typed, never a fake 0.0).

        A verifier may need to read the live sandbox.  Losing that transport is still
        infrastructure death, not a scorer fault: record the aborted episode and let the
        typed exception reach the dataloader so it can retry without consuming rollout
        budget.
        """
        if getattr(self.task, "verify", None) is None:
            return None, None
        try:
            return self.evaluate(), None
        except Exception as exc:  # noqa: BLE001 — classify into the episode, never crash
            if isinstance(exc, SandboxDied) or is_connection_loss(exc):
                self._finalize_episode(
                    terminal="aborted",
                    exit_reason="sandbox_died",
                    error=f"sandbox_died: {exc}",
                )
                raise
            return None, f"scorer_error: {exc}"

    def _finalize_episode(
        self,
        *,
        terminal: str | None,
        reward: float | None = None,
        exit_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """Close the in-flight episode into a typed :class:`Trajectory` + :class:`Episode`.
        Marking the env done is part of this transition, so no exception path can close
        the record while leaving the same replica accepting untracked follow-up steps.
        Record creation is a no-op when no episode is open (back-to-back resets stay clean)."""
        self._done = True
        if not self._episode_open:
            return
        self._episode_open = False
        if not self._steps and error is None:
            return  # a reset that saw no steps and no fault leaves no empty record
        reason = exit_reason or _EXIT_REASON_FOR_TERMINAL.get(terminal or "", "task_complete")
        meta = {
            "task": getattr(self.task, "name", ""),
            "instruction": getattr(self.task, "instruction", "") or "",
            "golden_checkpoint": self.golden_checkpoint,
            "reset_ms": round(self._reset_ms, 3),
            "reset_strategy": self.resolved_reset_strategy,
            "episode_id": self._episode_id,
            # Old Gym artifacts omitted this marker and stored post-action frames in
            # Step.observation. Consumers can now reject/explicitly migrate those
            # ambiguous legacy records instead of silently training on (s_{t+1}, a_t).
            "transition_semantics": "s_t_action_s_t_plus_1_v1",
        }
        if error is not None:
            meta["error"] = error
        traj = Trajectory(steps=self._steps, terminal=terminal, exit_reason=reason, metadata=meta)
        self.episodes.append(
            Episode(
                index=len(self.episodes),
                task=meta["task"],
                instruction=meta["instruction"],
                trajectory=traj,
                reward=reward,
                reset_ms=self._reset_ms,
                info={"receipt": _receipt_dict(self.last_receipt)} if reward is not None else {},
                episode_id=self._episode_id or uuid.uuid4().hex,
            )
        )
        self._steps = []

    def _teardown_replica(self) -> None:
        if self._sess is not None:
            with contextlib.suppress(Exception):
                self._sess.close()
            self._sess = None
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self.provider.destroy(self._handle)
            self._handle = None
        self._current_observation = None


def _receipt_dict(receipt: Any) -> Any:
    if receipt is None:
        return None
    to_dict = getattr(receipt, "to_dict", None)
    return to_dict() if callable(to_dict) else receipt


def make(task: Any, provider: Any, **kwargs: Any) -> ShinkenGymEnv:
    """Module-level ``make()`` (the gym entry point trainers expect): construct the env
    and resolve the reset strategy eagerly — on the fork path that builds the golden
    checkpoint (boot base, run ``task.setup`` once, checkpoint); on the recreate path
    resolution is bookkeeping only and provisioning happens per ``reset()``. Provider
    preflight rejects unsupported state-fidelity/fast-reset requests."""
    return ShinkenGymEnv(task, provider, **kwargs).make()


# ------------------------------------------------------------------------------ the pool


class ShinkenGymPool:
    """N gym envs over ONE golden checkpoint with **parallel fork-reset** — the fan-out.

    All sessions share one :class:`~shinken.SharedLoop` (a single client IO thread, the
    measured 16→1 thread collapse) and one :class:`~shinken.FrameCache` (cross-replica
    screenshot dedup over the forked fleet). ``reset()`` forks all N replicas from the
    single golden checkpoint concurrently; ``step()``/``evaluate()`` fan the per-env calls
    out on the same worker threads (the IO itself is multiplexed on the shared loop).

    The provider is caller-owned; the pool owns the golden snapshot and reclaims it on
    :meth:`close`."""

    def __init__(self, task: Any, provider: Any, n: int, **env_kwargs: Any) -> None:
        if n < 1:
            raise GymError("pool needs n >= 1 envs")
        self._loop = SharedLoop()
        self._cache = FrameCache()
        connect_kwargs = dict(env_kwargs.pop("connect_kwargs", None) or {})
        connect_kwargs.setdefault("loop", self._loop)
        connect_kwargs.setdefault("frame_cache", self._cache)
        self.envs = [
            ShinkenGymEnv(task, provider, connect_kwargs=connect_kwargs, **env_kwargs)
            for _ in range(n)
        ]
        for env in self.envs[1:]:
            env.owns_checkpoint = False  # one snapshot, owned by envs[0] / the pool
        self._pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="shinken-gym")
        self._closed = False

    def __len__(self) -> int:
        return len(self.envs)

    @property
    def episodes(self) -> list[Episode]:
        """All finished episodes across the pool (env-major, stable order)."""
        return [ep for env in self.envs for ep in env.episodes]

    def make(self) -> ShinkenGymPool:
        """Resolve the strategy ONCE on env 0 and share the outcome with every sibling:
        on the fork path that is the single golden checkpoint (env 0 runs ``task.setup``
        once — N envs, one setup, one snapshot); on the recreate path siblings inherit
        the latch (no per-sibling probes) and each reset provisions its own fresh
        sandbox with a per-episode ``task.setup`` replay. Fork's build-once guarantee
        does NOT carry to the recreate path: pool resets replay ``task.setup``
        concurrently on the worker threads (one call per sibling sandbox), so a
        recreate-path task's setup must tolerate concurrent runs against independent
        sandboxes."""
        first = self.envs[0].make()
        for env in self.envs[1:]:
            env.golden_checkpoint = first.golden_checkpoint
            env._recreate_latched = first._recreate_latched
        return self

    def reset(self) -> list[tuple[dict, dict]]:
        """Fork all N replicas from the golden checkpoint **in parallel** and return the
        per-env ``(observation, info)`` pairs (each ``info`` carries its ``reset_ms``)."""
        self.make()
        return list(self._pool.map(lambda env: env.reset(), self.envs))

    def step(self, actions: list[Any]) -> list[tuple[dict, float | None, bool, dict]]:
        """Apply one action (dict/list/raw text) per env, in parallel; aligned results."""
        if len(actions) != len(self.envs):
            raise GymError(f"expected {len(self.envs)} actions, got {len(actions)}")
        pairs = zip(self.envs, actions, strict=True)
        return list(self._pool.map(lambda ea: ea[0].step(ea[1]), pairs))

    def evaluate(self) -> list[float]:
        """Run the task verifier on every live replica, in parallel; aligned rewards."""
        return list(self._pool.map(lambda env: env.evaluate(), self.envs))

    def close(self) -> None:
        """Tear down every replica, reclaim the shared golden snapshot, stop the worker
        threads and the shared client loop. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for env in self.envs:
            with contextlib.suppress(Exception):
                env.close()
        with contextlib.suppress(Exception):
            self.envs[0].dispose()  # the snapshot owner reclaims it
        self._pool.shutdown(wait=True)
        self._loop.close()

    def __enter__(self) -> ShinkenGymPool:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ------------------------------------------------------------------- trajectory exporter

#: Exporter columns — one row per step, dict-of-lists (the HF ``Dataset.from_dict`` shape).
#: ``image`` is the raw PNG/JPEG policy input ``s_t`` (or None on the structured knob,
#: which fills ``tree_text`` instead), paired with that row's action(s). The post-action
#: ``s_{t+1}`` remains available on ``Step.next_observation`` / trajectory serialization;
#: the flattened column set stays backward compatible. ``reward`` is the episode reward;
#: ``terminal``/``exit_reason`` carry the typed failure taxonomy (so a trainer drops
#: ``sandbox_died`` rollouts without poisoning the reward signal).
EXPORT_COLUMNS = (
    "episode",
    "episode_id",
    "step",
    "task",
    "instruction",
    "image",
    "tree_text",
    "actions_json",
    "control_json",
    "raw_text",
    "note",
    "reward",
    "done",
    "terminal",
    "exit_reason",
    "reset_ms",
)


def episodes_to_records(episodes: list[Episode]) -> dict[str, list]:
    """Flatten episodes into the training-native dict-of-lists shape (columns:
    :data:`EXPORT_COLUMNS`; one row per step, episode fields broadcast). Pure and
    dependency-free — feed it to ``datasets.Dataset.from_dict`` or any columnar sink."""
    records: dict[str, list] = {c: [] for c in EXPORT_COLUMNS}
    for ep in episodes:
        steps = ep.trajectory.steps
        for step in steps:
            # Step.observation is the observation the agent actually consumed before
            # producing Step.actions: the exported training pair is (s_t, a_t).
            obs = step.observation or {}
            records["episode"].append(ep.index)
            records["episode_id"].append(ep.episode_id)
            records["step"].append(step.index)
            records["task"].append(ep.task)
            records["instruction"].append(ep.instruction)
            records["image"].append(obs.get("png") or obs.get("bytes"))
            records["tree_text"].append(obs.get("tree_text"))
            records["actions_json"].append(json.dumps(step.actions))
            records["control_json"].append(
                json.dumps(step.control) if step.control is not None else None
            )
            records["raw_text"].append(step.info.get("raw_text"))
            records["note"].append(step.note)
            records["reward"].append(ep.reward)
            records["done"].append(step.index == steps[-1].index)
            records["terminal"].append(ep.trajectory.terminal)
            records["exit_reason"].append(ep.trajectory.exit_reason)
            records["reset_ms"].append(round(ep.reset_ms, 3))
    return records


def to_hf_dataset(episodes: list[Episode]) -> Any:
    """Episodes as a Hugging Face ``datasets.Dataset`` (lazy import — ``datasets`` is
    NEVER a dependency). Without the package installed this returns the plain
    dict-of-lists from :func:`episodes_to_records`, which is the same columnar shape."""
    records = episodes_to_records(episodes)
    try:
        import datasets  # noqa: PLC0415 — deliberate lazy import, optional dependency
    except ImportError:
        return records
    return datasets.Dataset.from_dict(records)


# ----------------------------------------------------------------------- the dataloader


class MultiTurnDataloader:
    """A cua-bench-``MultiTurnDataloader``-shaped collection iterator over a
    :class:`ShinkenGymPool` — duck-typed to the surface TRL-GRPO-style loops drive
    (``__iter__``/``__next__`` yielding observation batches, ``async_step`` applying the
    model's responses, an episode buffer to sample from) with **no TRL/torch/tokenizer
    dependency**: batches are plain dict-of-lists carrying raw observations, and
    responses are raw model text (routed through ``parse_actions``) or action dicts.

    The loop::

        loader = MultiTurnDataloader(pool, total_episodes=32)
        for batch in loader:                        # {"env_id", "image", "observation", …}
            responses = policy(batch)               # one raw-text response per row
            loader.async_step({
                "env_id": batch["env_id"],
                "episode_id": batch["episode_id"],
                "step": batch["step"],
                "responses": responses,
            })
        dataset = to_hf_dataset(loader.episodes)

    Where cua-bench's loader spawns a worker subprocess per env and its env re-creates
    the sandbox per episode, here every env rides the pool's shared loop and every
    episode boundary is a **fork from the golden checkpoint** (``auto_reset``). A
    response that fails to parse aborts that env's episode as ``agent_error`` (typed,
    recorded, auto-reset) instead of crashing the collection loop."""

    def __init__(
        self,
        pool: ShinkenGymPool,
        *,
        batch_size: int | None = None,
        total_episodes: int | None = None,
        auto_reset: bool = True,
    ) -> None:
        self.pool = pool
        self.num_envs = len(pool.envs)
        self.batch_size = batch_size or self.num_envs
        if self.batch_size > self.num_envs:
            raise GymError("each env cannot run more than one step per batch")
        self.total_episodes = total_episodes
        self.auto_reset = auto_reset
        self._pending: dict[int, dict] = {}  # env index -> observation awaiting a response
        self._completed = 0
        self._started = False

    @property
    def episodes(self) -> list[Episode]:
        return self.pool.episodes

    def __iter__(self) -> MultiTurnDataloader:
        return self

    def __next__(self) -> dict:
        """The next observation batch (dict-of-lists): ``env_id``, ``observation`` (full
        dicts), ``image`` (PNG/JPEG bytes or None), ``instruction``, ``episode_id``,
        ``step``, ``task``.
        Raises ``StopIteration`` once ``total_episodes`` have completed."""
        if self.total_episodes is not None and self._completed >= self.total_episodes:
            raise StopIteration
        if not self._started:
            self.pool.make()
            self._started = True
        for i, env in enumerate(self.pool.envs):
            if i not in self._pending and (env._sess is None or env._done):
                if self._budget_left() <= self._in_flight():
                    continue  # don't fork episodes the budget can never finish
                obs, _info = env.reset()
                self._pending[i] = obs
        ready = sorted(self._pending)[: self.batch_size]
        if not ready:
            raise StopIteration  # budget exhausted mid-flight
        batch: dict[str, list] = {
            "env_id": [],
            "episode_id": [],
            "observation": [],
            "image": [],
            "instruction": [],
            "step": [],
            "task": [],
        }
        for i in ready:
            obs = self._pending.pop(i)
            env = self.pool.envs[i]
            batch["env_id"].append(i)
            batch["episode_id"].append(env._episode_id)
            batch["observation"].append(obs)
            batch["image"].append(obs.get("png") or obs.get("bytes"))
            batch["instruction"].append(getattr(env.task, "instruction", "") or "")
            batch["step"].append(len(env._steps))
            batch["task"].append(getattr(env.task, "name", ""))
        return batch

    def async_step(self, batch_return: dict) -> list[dict]:
        """Apply the model's responses for a previously yielded batch (their
        ``async_step`` shape: a dict carrying the batch's ids + ``responses``). Accepts
        ``env_id`` (ours) or ``worker_id`` (theirs), plus the yielded ``episode_id`` and
        ``step`` generation. The latter two are mandatory: an env id is reusable, so it
        cannot by itself prevent a delayed/retried response from landing in a later episode.
        Returns one
        ``{"env_id", "reward", "done", "info"}`` row per response; a ``DialectError``
        aborts that env's episode as ``agent_error`` and reports ``done=True``."""
        env_ids = batch_return.get("env_id")
        if env_ids is None:
            env_ids = batch_return.get("worker_id")
        episode_ids = batch_return.get("episode_id")
        expected_steps = batch_return.get("step")
        responses = batch_return.get("responses")
        lengths = [
            len(values)
            for values in (env_ids, episode_ids, expected_steps, responses)
            if values is not None and hasattr(values, "__len__")
        ]
        if (
            env_ids is None
            or episode_ids is None
            or expected_steps is None
            or responses is None
            or len(lengths) != 4
            or len(set(lengths)) != 1
        ):
            raise GymError(
                "batch_return needs aligned env_id (or worker_id), episode_id, step, and responses"
            )
        targets = list(zip(env_ids, episode_ids, expected_steps, responses, strict=True))
        # Validate the entire batch before mutating any env, so one stale row cannot leave
        # an otherwise-valid batch half-applied.
        for env_id, episode_id, expected_step, _response in targets:
            self._validate_response_target(env_id, episode_id, expected_step)
        if len({env_id for env_id, *_rest in targets}) != len(targets):
            raise GymError("batch_return contains duplicate env_id rows")
        rows: list[dict] = []
        for index, (env_id, episode_id, expected_step, response) in enumerate(targets):
            try:
                rows.append(
                    self.step(
                        env_id,
                        response,
                        episode_id=episode_id,
                        expected_step=expected_step,
                    )
                )
            except BaseException:
                # A runtime/harness error may propagate by design. Restore the yielded
                # observations for this row (when it failed before closing the episode)
                # and every not-yet-applied row, otherwise one failure in a multi-env
                # batch strands the remaining live episodes with no pending response.
                self._requeue_unapplied_targets(targets[index:])
                raise
        return rows

    def step(
        self,
        env_id: int,
        response: Any,
        *,
        episode_id: str,
        expected_step: int,
    ) -> dict:
        """Apply one response to one env; on episode end count it (and auto-reset =
        re-fork) — see :meth:`async_step` for the batch form."""
        self._validate_response_target(env_id, episode_id, expected_step)
        env = self.pool.envs[env_id]
        try:
            obs, reward, done, info = env.step(response)
        except Exception as exc:
            if isinstance(exc, SandboxDied) or is_connection_loss(exc):
                # Infra death is typed and retry-eligible: the episode was recorded with
                # exit_reason="sandbox_died"; a fresh fork continues collection. It does not
                # consume the requested count of valid/agent-attempt episodes.
                self._episode_finished(env_id, count_toward_budget=False)
                return {
                    "env_id": env_id,
                    "episode_id": episode_id,
                    "reward": None,
                    "done": True,
                    "info": {"error": str(exc)},
                }
            if isinstance(exc, ValueError):  # DialectError: unparseable model output
                env.abort(f"agent_error: {exc}")
                self._episode_finished(env_id)
                return {
                    "env_id": env_id,
                    "episode_id": episode_id,
                    "reward": None,
                    "done": True,
                    "info": {"error": str(exc)},
                }
            raise
        if done:
            self._episode_finished(env_id)
        else:
            self._pending[env_id] = obs
        return {
            "env_id": env_id,
            "episode_id": episode_id,
            "reward": reward,
            "done": done,
            "info": info,
        }

    def sample_episodes(self, batch_size: int | None = None) -> list[Episode]:
        """A random batch of finished episodes (their ``sample_from_buffer`` analog —
        the buffer here is the pool's typed episode list)."""
        eps = self.episodes
        if batch_size is None or batch_size >= len(eps):
            return list(eps)
        return random.sample(eps, batch_size)

    def close(self) -> None:
        self.pool.close()

    # --- internals ---------------------------------------------------------------------

    def _budget_left(self) -> int:
        if self.total_episodes is None:
            return len(self.pool.envs)  # effectively unbounded per scheduling round
        return max(0, self.total_episodes - self._completed)

    def _in_flight(self) -> int:
        """Episodes currently open (forked and not yet finished) — counts envs awaiting
        a response too, not just the ones holding a pending observation."""
        return sum(1 for env in self.pool.envs if env._episode_open)

    def _validate_response_target(self, env_id: Any, episode_id: Any, expected_step: Any) -> None:
        if (
            not isinstance(env_id, int)
            or isinstance(env_id, bool)
            or not 0 <= env_id < self.num_envs
        ):
            raise GymError(f"invalid env_id {env_id!r}")
        if not isinstance(episode_id, str) or not episode_id:
            raise GymError("response needs a non-empty episode_id")
        if (
            not isinstance(expected_step, int)
            or isinstance(expected_step, bool)
            or expected_step < 0
        ):
            raise GymError(f"invalid response step {expected_step!r}")
        env = self.pool.envs[env_id]
        current_episode = env._episode_id
        current_step = len(env._steps)
        if env_id in self._pending:
            raise GymError(
                f"response for env {env_id} has no outstanding yielded observation "
                "(call next(loader) first)"
            )
        if not env._episode_open or current_episode != episode_id or current_step != expected_step:
            raise GymError(
                f"stale response for env {env_id}: expected episode_id={current_episode!r}, "
                f"step={current_step}; got episode_id={episode_id!r}, step={expected_step}"
            )

    def _requeue_unapplied_targets(self, targets: list[tuple[Any, Any, Any, Any]]) -> None:
        """Return still-current yielded observations to the pending queue after a batch
        aborts. A target whose episode already finalized stays closed and will reset on
        the next iteration; untouched siblings are yielded again instead of being lost."""
        for env_id, episode_id, expected_step, _response in targets:
            env = self.pool.envs[env_id]
            if (
                env._episode_open
                and not env._done
                and env._episode_id == episode_id
                and len(env._steps) == expected_step
                and env._current_observation is not None
            ):
                self._pending[env_id] = env._current_observation

    def _episode_finished(self, env_id: int, *, count_toward_budget: bool = True) -> None:
        if count_toward_budget:
            self._completed += 1
        self._pending.pop(env_id, None)
        if self.auto_reset and self._budget_left() > self._in_flight():
            obs, _info = self.pool.envs[env_id].reset()
            self._pending[env_id] = obs
