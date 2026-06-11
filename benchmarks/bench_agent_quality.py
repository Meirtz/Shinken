"""Agent-quality study harness — codec tier x task success, with fork-eliminated env
variance (Arm 2 of the codec-vs-task-quality design; Arm 1 is the per-tier legibility
scoring a sibling produces — joined on tier labels in ``analyze_agent_quality.py``).

The question: how far down the observation codec ladder (JPEG quality x downscale — the
bandwidth levers measured in ``bench_codec_ladder.py``) can a real vision agent go before
**task success** degrades? Success is judged by deterministic guest-state verifiers
(``_study_tasks.py``), never by pixels or a judge model.

**Why fork is the methodology, not just the subject.** Per task the harness builds ONE
golden checkpoint (``shinken.gym.ShinkenGymEnv.make``: boot -> ``task.setup`` once ->
checkpoint), then materializes EVERY episode — every codec tier x seed — as a fork of
that single checkpoint. All conditions therefore start from **byte-identical disk
state**: the same task content, the same launcher bytes, the same fonts and geometry.
A measured between-tier difference cannot come from per-episode environment
re-provisioning variance (different boot timing, different task instantiation), because
there is none — the platform's own checkpoint/fork wedge eliminates that variance
class by construction. (Processes do not survive the Docker disk tier, so the task UI
is re-materialized per fork by the SAME ``launch.sh`` captured in the checkpoint, with
deterministic window geometry.)

Codec tiers (the observation served to the model — both the post-step fused frame and
the harness's explicit captures use the tier's settings):

    png-native   PNG, native 1280x800            (the lossless control)
    q80-1024     JPEG q80, max_long_edge 1024
    q50-1024     JPEG q50, max_long_edge 1024
    q50-512      JPEG q50, max_long_edge 512
    q10-768      JPEG q10, max_long_edge 768

Model layer: ONE pluggable OpenAI-compatible vision endpoint, env-configured —
``SHINKEN_STUDY_BASE_URL`` / ``SHINKEN_STUDY_API_KEY`` / ``SHINKEN_STUDY_MODEL``
(falling back to the repo's existing ``SHK_SMOKE_MODEL_*`` convention). Temperature 0,
one fixed minimal computer-use prompt, actions parsed by
``shinken.dialect.parse_actions(format="auto")`` (the native tag dialect and the wild
XML tool-call grammars both work). Model pixel coordinates are interpreted in the
SERVED image's space and rescaled to the native screen by the harness. Without a
configured endpoint the harness runs the same plumbing with the **scripted oracle**
(ground truth from task metadata — validates fork/observe/parse/verify, says nothing
about legibility).

This lives in ``benchmarks/`` because it follows the suite conventions (rerunnable,
``results/<suite>.json`` + ``_common`` plumbing), but it is a STUDY, not a latency
suite: it needs a model endpoint and ~hours of wall time, so it is deliberately NOT in
``run_all.sh``.

Run (Docker + the shinken/sandbox-linux image; model env optional):

    python benchmarks/bench_agent_quality.py --mode smoke            # oracle, 1 task
    python benchmarks/bench_agent_quality.py --mode pilot            # 2 tasks x 3 tiers x 2 seeds
    python benchmarks/bench_agent_quality.py --mode full --seeds 8   # the full study

Design + budget: docs/engineering/agent-quality-study.md.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# _common bootstraps this checkout's SDK onto sys.path before the shinken imports.
from _common import GEOMETRY, IMAGE, write_result
from _study_tasks import build_tasks
from shinken.dialect import DialectError, parse_actions
from shinken.errors import SandboxDied, is_connection_loss
from shinken.gym import ShinkenGymEnv
from shinken.providers.base import ProviderError, SandboxSpec
from shinken.providers.docker import DockerLocalProvider

SUITE = "agent_quality"

#: tier label -> screenshot-parameter dict (also the pipelined step() observe dict).
#: The LABELS are the join key shared with Arm 1 (legibility) — keep them stable.
TIERS: list[tuple[str, dict]] = [
    ("png-native", {}),
    ("q80-1024", {"format": "jpeg", "quality": 80, "max_long_edge": 1024}),
    ("q50-1024", {"format": "jpeg", "quality": 50, "max_long_edge": 1024}),
    ("q50-512", {"format": "jpeg", "quality": 50, "max_long_edge": 512}),
    ("q10-768", {"format": "jpeg", "quality": 10, "max_long_edge": 768}),
]
PILOT_TIERS = ("png-native", "q50-1024", "q10-768")

MAX_STEPS_DEFAULT = 10

SYSTEM_PROMPT = """You are a computer-use agent operating a Linux desktop through screenshots.

Each turn you receive the current screenshot. Reply with ONE short reasoning line, then
exactly ONE <actions> block containing one or more actions:

<actions>
  <click x="640" y="420"/>
  <type_text text="hello"/>
  <key combo="Return"/>
  <wait ms="500"/>
</actions>

Available actions: click, double_click, right_click, move, scroll (dy), type_text,
key (combo, e.g. "Return" or "ctrl+s"), wait (ms), done.

Rules:
- Coordinates are pixels in the PROVIDED screenshot (its size is stated each turn).
- Click a window to focus it BEFORE typing into it, in an earlier turn than the typing.
- Type text exactly as read from the screen; press Return with <key combo="Return"/>.
- When the task is fully complete, reply with <actions><done/></actions>.
"""


# ------------------------------------------------------------------------- model driver


class StudyModel:
    """Minimal OpenAI-compatible vision chat driver (urllib, no SDK dependency): one
    fixed system prompt, the latest screenshot attached as a data URL, temperature 0,
    transient-status retry. Tracks token usage from the API's ``usage`` field."""

    RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 240.0):
        self.base_url, self._api_key, self.model = base_url.rstrip("/"), api_key, model
        self.timeout = timeout
        self.max_tokens = int(
            os.environ.get("SHINKEN_STUDY_MAX_TOKENS")
            or os.environ.get("SHK_SMOKE_MODEL_MAX_TOKENS")
            or 4096
        )
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    @classmethod
    def from_env(cls) -> StudyModel | None:
        """The study env first (SHINKEN_STUDY_*), then the repo's existing model-smoke
        convention (SHK_SMOKE_MODEL_*). None when no endpoint is configured."""

        def pick(suffix: str) -> str | None:
            return os.environ.get(f"SHINKEN_STUDY_{suffix}") or os.environ.get(
                f"SHK_SMOKE_MODEL_{suffix}"
            )

        base, key, model = pick("BASE_URL"), pick("API_KEY"), pick("MODEL") or pick("NAME")
        if base and key and model:
            return cls(base, key, model)
        return None

    def turn(self, instruction: str, obs: dict, history: list[str]) -> str:
        prev = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(history[-8:])) or "(none)"
        mime = "image/jpeg" if obs.get("format") == "jpeg" else "image/png"
        uri = f"data:{mime};base64," + base64.b64encode(obs["bytes"]).decode("ascii")
        text = (
            f"Task: {instruction}\n"
            f"Screenshot size: {obs.get('w')}x{obs.get('h')} pixels.\n"
            f"Previous turns:\n{prev}\n"
            "What is the next action?"
        )
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": text},
                            {"type": "image_url", "image_url": {"url": uri, "detail": "high"}},
                        ],
                    },
                ],
                "max_tokens": self.max_tokens,
                "temperature": 0.0,
            }
        ).encode()
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        reply = self._post_with_retry(req)
        self.calls += 1
        usage = reply.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        msg = reply["choices"][0]["message"]
        # Reasoning endpoints may put the action block in reasoning_content when the
        # visible content is empty — fall back so the parser always gets a string.
        return msg.get("content") or msg.get("reasoning_content") or ""

    def _post_with_retry(self, req, attempts: int = 5) -> dict:
        last: Exception | None = None
        for i in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in self.RETRY_STATUS:
                    raise
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
            time.sleep(min(2.0**i, 20.0))
        raise RuntimeError(f"model endpoint failed after {attempts} attempts: {last}")


class ModelAgent:
    """One model turn per env step; remembers its own raw outputs as history."""

    kind = "model"
    #: the model reads pixels off the SERVED image — its coordinates need rescaling
    coordinate_space = "served"

    def __init__(self, model: StudyModel):
        self.model = model

    def begin(self, sess: Any, task: Any) -> None:
        self.history: list[str] = []

    def decide(self, instruction: str, obs: dict) -> str:
        text = self.model.turn(instruction, obs, self.history)
        self.history.append(" ".join(text.split())[:160])
        return text

    def note(self, msg: str) -> None:
        self.history.append(msg)


class OracleAgent:
    """The scripted ground-truth plan from task metadata (window-aimed at episode start).
    Emits raw dialect text so it exercises the same parse path as a model — but it READS
    THE TRUTH, not the pixels: it validates plumbing, never legibility."""

    kind = "oracle"
    #: the oracle aims with list_windows geometry — already native-screen pixels
    coordinate_space = "native"

    def __init__(self) -> None:
        self._plan: list[Any] = []

    def begin(self, sess: Any, task: Any) -> None:
        self._plan = list(task.metadata["oracle"](sess))

    def decide(self, instruction: str, obs: dict) -> Any:
        return self._plan.pop(0) if self._plan else "<done/>"

    def note(self, msg: str) -> None:  # pragma: no cover - oracle output always parses
        pass


# ---------------------------------------------------------------------- episode plumbing


def _wait_ready(sess: Any, titles: list[str], timeout_s: float = 25.0) -> float:
    """Poll list_windows until every expected task window is mapped, then give the
    toolkit a short paint beat. Returns the readiness latency (s)."""
    t0 = time.time()
    deadline = t0 + timeout_s
    while time.time() < deadline:
        try:
            seen = {w.get("title") for w in sess.list_windows()}
        except Exception:
            seen = set()
        if all(t in seen for t in titles):
            time.sleep(0.8)  # content paint beat (cat + zenity layout)
            return time.time() - t0
        time.sleep(0.25)
    raise RuntimeError(f"task windows {titles} never mapped within {timeout_s}s (saw {seen})")


def _observe_tier(sess: Any, tier: dict) -> dict:
    """A fresh observation at the tier's exact settings (format/quality/max_long_edge),
    via the pipelined step's fused-observation path (empty action list)."""
    res = sess.step([], observe=tier or {})
    obs = res.get("observation")
    if obs is None:
        raise RuntimeError(f"tier observation failed: {res.get('observation_error')}")
    return obs


def _rescale_targets(actions: list[dict], obs: dict, screen: tuple[int, int]) -> list[dict]:
    """Model pixel coordinates live in the SERVED image space; the ACI executes in
    native screen space. Rescale point_px targets by the served->native ratio (identity
    at native tiers; point_norm passes through untouched)."""
    ow, oh = obs.get("w"), obs.get("h")
    sw, sh = screen
    if not ow or not oh or (ow == sw and oh == sh):
        return actions
    out = []
    for a in actions:
        t = a.get("target")
        if isinstance(t, dict) and t.get("kind") == "point_px":
            a = {
                **a,
                "target": {
                    "kind": "point_px",
                    "x": round(t["x"] * sw / ow),
                    "y": round(t["y"] * sh / oh),
                },
            }
        out.append(a)
    return out


def _settle_click_before_type(actions: list[dict]) -> list[dict]:
    """Insert a short wait between a click and a type/key that follows it in the SAME
    turn: focus-follows-click is a WM round trip, and keystrokes racing it are silently
    dropped. Applied uniformly across every tier (an actuation-determinism guard,
    orthogonal to the codec under study)."""
    out: list[dict] = []
    for a in actions:
        if (
            out
            and out[-1].get("verb") in ("click", "double_click")
            and a.get("verb") in ("type_text", "key")
        ):
            out.append({"verb": "wait", "ms": 300})
        out.append(a)
    return out


def run_episode(
    env: ShinkenGymEnv,
    task: Any,
    tier_label: str,
    tier: dict,
    agent: Any,
    seed: int,
    max_steps: int,
    screen: tuple[int, int],
) -> dict:
    """One episode = one fork of the task's golden checkpoint, observed at one tier."""
    t0 = time.time()
    row: dict = {
        "task": task.name,
        "template": task.metadata["template"],
        "font": task.metadata["font"],
        "tier": tier_label,
        "seed": seed,
        "agent": agent.kind,
    }
    pt0 = agent.model.prompt_tokens if isinstance(agent, ModelAgent) else 0
    ct0 = agent.model.completion_tokens if isinstance(agent, ModelAgent) else 0
    try:
        _obs, info = env.reset()
        row["reset_ms"] = round(info["reset_ms"], 1)
        sess = env.session
        res = sess.exec(["/bin/sh", task.metadata["launch"]], timeout=30)
        if res.get("exit_code") != 0:
            raise RuntimeError(f"launch.sh failed: {res}")
        row["ready_s"] = round(_wait_ready(sess, task.metadata["ready_titles"]), 2)
        obs = _observe_tier(sess, tier)
        agent.begin(sess, task)
        frame_bytes = [len(obs.get("bytes") or b"")]
        parse_errors = 0
        for _turn in range(max_steps):
            decision = agent.decide(task.instruction, obs)
            if isinstance(decision, str):
                try:
                    actions = parse_actions(decision, format="auto")
                except DialectError as exc:
                    parse_errors += 1
                    agent.note(f"[unparseable action: {exc}]")
                    actions = [{"verb": "wait", "ms": 50}]  # consumes a step, honestly
            else:
                actions = list(decision)
            if agent.coordinate_space == "served":
                actions = _rescale_targets(actions, obs, screen)
            actions = _settle_click_before_type(actions)
            obs, _reward, done, _sinfo = env.step(actions)
            frame_bytes.append(len(obs.get("bytes") or b""))
            if done:
                break
        ep = env.episodes[-1]
        row.update(
            reward=ep.reward,
            success=ep.reward == 1.0,
            steps=len(ep.trajectory.steps),
            exit_reason=ep.trajectory.exit_reason,
            kind="pass" if ep.reward == 1.0 else ("fail" if ep.reward is not None else "error"),
            parse_errors=parse_errors,
            frame_bytes_mean=round(sum(frame_bytes) / len(frame_bytes)),
            receipt=(ep.info.get("receipt") if isinstance(ep.info, dict) else None),
        )
    except (SandboxDied, ProviderError) as exc:
        # Infra death (replica died / warm-pool graft lost its container): typed,
        # excluded from the denominators, retry-eligible — never scored as a failure.
        row.update(success=False, kind="sandbox_died", error=f"{type(exc).__name__}: {exc}"[:300])
    except Exception as exc:  # noqa: BLE001 — classify; one episode never kills the study
        kind = "sandbox_died" if is_connection_loss(exc) else "error"
        row.update(success=False, kind=kind, error=f"{type(exc).__name__}: {exc}"[:300])
    if isinstance(agent, ModelAgent):
        row["prompt_tokens"] = agent.model.prompt_tokens - pt0
        row["completion_tokens"] = agent.model.completion_tokens - ct0
    row["wall_s"] = round(time.time() - t0, 1)
    return row


# ------------------------------------------------------------------------------ the run


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["smoke", "pilot", "full"], default="pilot")
    ap.add_argument(
        "--agent",
        choices=["auto", "model", "oracle"],
        default="auto",
        help="auto = model when an endpoint is configured, else oracle",
    )
    ap.add_argument("--seeds", type=int, default=None, help="replicates per task x tier")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS_DEFAULT)
    ap.add_argument("--tasks", type=int, default=None, help="cap the task list (debug)")
    ap.add_argument("--out", default=None, help="results file stem (default per mode)")
    args = ap.parse_args()

    model = StudyModel.from_env()
    if args.agent == "model" and model is None:
        sys.exit(
            "no model endpoint configured: set SHINKEN_STUDY_BASE_URL / _API_KEY / _MODEL "
            "(or the SHK_SMOKE_MODEL_* equivalents)"
        )
    use_model = model is not None and args.agent in ("auto", "model")

    tasks = build_tasks()
    tiers = TIERS
    if args.mode == "smoke":
        tasks, tiers, seeds = tasks[:1], [TIERS[0], TIERS[-1]], 1
    elif args.mode == "pilot":
        # one normal-font task + one small-font task (the breaking case), spread tiers
        tasks = [t for t in tasks if t.name == "code_prompt-normal"] + [
            t for t in tasks if t.name == "key_value_lookup-small"
        ]
        tiers = [t for t in TIERS if t[0] in PILOT_TIERS]
        seeds = args.seeds or 2
    else:
        seeds = args.seeds or 8
    if args.tasks:
        tasks = tasks[: args.tasks]
    if args.seeds:
        seeds = args.seeds

    screen = tuple(int(v) for v in GEOMETRY.split("x")[:2])
    agent = ModelAgent(model) if use_model else OracleAgent()
    label = model.model.rsplit("/", 1)[-1] if use_model else "scripted-oracle"
    print(
        f"mode={args.mode} agent={agent.kind} ({label}) tasks={len(tasks)} "
        f"tiers={[t[0] for t in tiers]} seeds={seeds} max_steps={args.max_steps}",
        flush=True,
    )

    provider = DockerLocalProvider(
        image=IMAGE,
        name_prefix="shinken-bench",
        startup_timeout=120.0,
        warm_pool_size=2,
        warm_pool_spec=SandboxSpec(screen_geometry=GEOMETRY),
        warm_pool_claim_timeout=0.25,
    )
    episodes: list[dict] = []
    try:
        for task in tasks:
            print(f"--- {task.name}: building golden checkpoint", flush=True)
            golden = ShinkenGymEnv(
                task, provider, spec=SandboxSpec(screen_geometry=GEOMETRY)
            ).make()
            try:
                for tier_label, tier in tiers:
                    env = ShinkenGymEnv(
                        task,
                        provider,
                        observe_args=dict(tier),
                        max_steps=args.max_steps,
                    )
                    env.golden_checkpoint = golden.golden_checkpoint
                    env.owns_checkpoint = False
                    try:
                        for seed in range(seeds):
                            row = run_episode(
                                env, task, tier_label, tier, agent, seed, args.max_steps, screen
                            )
                            if row.get("kind") == "sandbox_died":
                                # ONE infra retry on a fresh fork (the typed retry-eligible
                                # class); the dead episode stays in the ledger, excluded
                                # from the denominators.
                                row["retried"] = True
                                episodes.append(row)
                                row = run_episode(
                                    env, task, tier_label, tier, agent, seed, args.max_steps, screen
                                )
                            episodes.append(row)
                            print(
                                f"    {task.name} {tier_label} seed={seed}: "
                                f"{'PASS' if row.get('success') else row.get('kind')} "
                                f"steps={row.get('steps', '-')} wall={row['wall_s']}s",
                                flush=True,
                            )
                    finally:
                        env.close()
            finally:
                golden.dispose()
    finally:
        provider.shutdown_pool()

    verdicts = [e for e in episodes if e.get("kind") in ("pass", "fail")]
    by_tier: dict[str, dict] = {}
    for label_, _ in tiers:
        rows = [e for e in verdicts if e["tier"] == label_]
        wins = [e for e in rows if e["success"]]
        by_tier[label_] = {
            "n": len(rows),
            "successes": len(wins),
            "success_rate": round(len(wins) / len(rows), 4) if rows else None,
            "mean_steps_to_success": (
                round(sum(e["steps"] for e in wins) / len(wins), 2) if wins else None
            ),
            "frame_bytes_mean": (
                round(sum(e["frame_bytes_mean"] for e in rows) / len(rows)) if rows else None
            ),
        }
    payload: dict = {
        "mode": args.mode,
        "agent": {
            "kind": agent.kind,
            "model": label if use_model else None,
            "endpoint": (
                "openai-compatible chat completions (env-configured)" if use_model else None
            ),
            "temperature": 0.0 if use_model else None,
        },
        "study_ready": True,
        "note": (
            "real-model episodes"
            if use_model
            else "scripted-oracle plumbing run — study ready-to-run, awaiting endpoint"
        ),
        "max_steps": args.max_steps,
        "seeds": seeds,
        "tiers": {label_: tier for label_, tier in tiers},
        "per_tier": by_tier,
        "episodes": episodes,
    }
    if use_model:
        payload["tokens"] = {
            "prompt": model.prompt_tokens,
            "completion": model.completion_tokens,
            "model_calls": model.calls,
        }
    out = args.out or f"{SUITE}_{args.mode}"
    write_result(out, payload)
    print(json.dumps({"per_tier": by_tier, "tokens": payload.get("tokens")}, indent=2))
    failures = [e for e in episodes if e.get("kind") not in ("pass", "fail")]
    if failures:
        print(f"note: {len(failures)} episode(s) without a verdict (infra/agent error)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
