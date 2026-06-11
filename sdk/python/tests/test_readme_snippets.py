"""Doc-tests for the repo-root README's ```python fenced blocks.

Why this exists: README snippets rotted silently for weeks (an adapter example dropped
action payloads; the checkpoint/fork example raised RuntimeError on a non-provider
session). Every fenced ```python block in README.md is now extracted and held to three
tiers, so a snippet that names a nonexistent API, passes a wrong kwarg, or no longer
runs against the real SDK fails CI:

1. **compile** — every block must be real Python.
2. **api surface** (always, no Docker/network) — the block's imports are executed and
   every attribute chain / call it makes is resolved against the real SDK objects
   (getattr walk + signature binding), so even blocks whose execution needs Docker are
   checked for API drift in a plain ``pytest`` run.
3. **execution** — blocks with no Docker dependency run for real against the
   in-process mock ``shinkend`` (tests/conftest.py); Docker-dependent blocks
   (``DockerLocalProvider`` / ``run_eval_forked``) run for real only when
   ``SHINKEN_DOCTEST_DOCKER=1`` (CI's Docker job can opt in).

Snippets are allowed to reference narrative names they never define
(``model_tool_call``, ``task``, ``endpoints``); those are supplied from the stand-in
fixtures below. A snippet referencing a name with *no* stand-in fails the api-surface
tier instead of silently skipping.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import inspect
import os
from pathlib import Path

import pytest

import shinken
from shinken import eval as shinken_eval
from shinken.adapters import AnthropicComputerUseAdapter
from shinken.client import Sandbox
from shinken.providers import DockerLocalProvider
from shinken.providers.base import SandboxHandle

_README = Path(__file__).resolve().parents[3] / "README.md"

#: A block whose AST touches one of these names needs a live Docker substrate, so it is
#: api-surface-checked always and executed only behind SHINKEN_DOCTEST_DOCKER=1.
_DOCKER_MARKERS = {"DockerLocalProvider", "run_eval_forked"}

#: Anthropic computer-use ``tool_use`` *input* (what a model emits for the versioned
#: ``computer`` tool — see shinken/adapters/anthropic.py). A ``type`` action is
#: load-bearing here: its payload lives OUTSIDE verb/target, so a consumption pattern
#: that drops payload keys (the original README bug) sends ``type_text`` without
#: ``text``, which the ACI schema rejects — the executed snippet then fails instead of
#: passing vacuously (a click-only fixture would mask that whole bug class).
_MODEL_TOOL_CALL = {"action": "type", "text": "real desktops, one typed interface"}


def _doc_task() -> shinken_eval.Task:
    """A tiny eval Task usable against both the mock and a real sandbox: act, then
    verify from an observed effect (screenshot bytes), not from the task's own input."""

    def run(env: Sandbox) -> None:
        env.click(x=8, y=8)
        env.type_text("doc")

    def verify(env: Sandbox) -> shinken_eval.VerifierReceipt:
        shot = env.screenshot()
        return shinken_eval.VerifierReceipt.from_checks(
            [shinken_eval.check("screenshot has bytes", bool(shot.get("bytes")))]
        )

    return shinken_eval.Task(name="readme-doc-task", run=run, verify=verify)


# --- extraction --------------------------------------------------------------------


def _extract_blocks() -> list[tuple[int, int, str]]:
    """Every ```python fenced block in README.md as (index, 1-based first code line,
    code). Other fences (```bash, ```text, ```mermaid) are not Python and are skipped."""
    blocks: list[tuple[int, int, str]] = []
    lines = _README.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == "```python":
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            blocks.append((len(blocks), i + 2, "\n".join(lines[i + 1 : j])))
            i = j + 1
        else:
            i += 1
    return blocks


def _needs_docker(code: str) -> bool:
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Name) and node.id in _DOCKER_MARKERS:
            return True
        if isinstance(node, ast.alias) and node.name.split(".")[-1] in _DOCKER_MARKERS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _DOCKER_MARKERS:
            return True
    return False


_BLOCKS = _extract_blocks()
_MOCK_BLOCKS = [b for b in _BLOCKS if not _needs_docker(b[2])]
_DOCKER_BLOCKS = [b for b in _BLOCKS if _needs_docker(b[2])]


def _ids(blocks: list[tuple[int, int, str]]) -> list[str]:
    return [f"block{i}-L{ln}" for i, ln, _ in blocks]


def _free_names(tree: ast.AST) -> set[str]:
    """Names a block loads but never binds (and that aren't builtins) — the narrative
    names a stand-in fixture must supply."""
    bound: set[str] = set()
    loaded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (loaded if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return loaded - bound - set(dir(builtins))


# --- tier 2: api-surface check (no sandbox, no Docker, no network) -------------------

_UNKNOWN = object()
_P = inspect.Parameter


def _api_standins() -> dict[str, object]:
    """Representative objects for names snippets use without defining. Classes stand in
    for instances: attribute existence and method signatures are checked on the class."""
    return {
        "shinken": shinken,
        "env": Sandbox,
        "model_tool_call": dict(_MODEL_TOOL_CALL),
        "task": _doc_task(),
        "endpoints": [("127.0.0.1:0", "doc-test-token")],
        # class stands in for the instance the checkpoint/fork block constructs
        "provider": shinken.DockerLocalProvider,
    }


class _ApiSurfaceChecker:
    """AST-level existence/signature audit of one snippet against the real SDK.

    Imports are executed for real; simple bindings are tracked (``x = SomeClass()``
    maps ``x`` to the class); every attribute chain rooted in a known object must
    getattr-resolve; every call on a known callable must bind — unknown keyword,
    excess positional, and missing required arguments all fail."""

    def __init__(self, code: str, readme_lineno: int) -> None:
        self.tree = ast.parse(code)
        self.base = readme_lineno
        self.ns: dict[str, object] = dict(_api_standins())
        # Stand-in names stay authoritative even when the snippet rebinds them through
        # an opaque call (e.g. `env = provider.connect(...)` keeps env -> Sandbox).
        self.pinned = set(self.ns)
        self.errors: list[str] = []

    def run(self) -> list[str]:
        for name in sorted(_free_names(self.tree) - set(self.ns)):
            self.errors.append(
                f"README.md:{self.base}: snippet references undefined name {name!r} "
                "with no stand-in (add it to _api_standins/_exec_fixture)"
            )
        self._visit_stmts(self.tree.body)
        return self.errors

    def _fail(self, node: ast.AST, msg: str) -> None:
        entry = f"README.md:{self.base + getattr(node, 'lineno', 1) - 1}: {msg}"
        if entry not in self.errors:
            self.errors.append(entry)

    def _resolve(self, node: ast.expr) -> object:
        if isinstance(node, ast.Name):
            if node.id in self.ns:
                return self.ns[node.id]
            return getattr(builtins, node.id, _UNKNOWN)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            if base is _UNKNOWN:
                return _UNKNOWN
            try:
                return getattr(base, node.attr)
            except AttributeError:
                owner = getattr(base, "__name__", None) or type(base).__name__
                self._fail(node, f"`{ast.unparse(node)}`: {owner} has no attribute {node.attr!r}")
                return _UNKNOWN
        return _UNKNOWN

    def _infer(self, node: ast.expr) -> object:
        """Calling a class yields an instance — checked against the class. Anything
        else (method results, subscripts, comprehensions) is opaque."""
        if isinstance(node, ast.Call):
            fn = self._resolve(node.func)
            if inspect.isclass(fn):
                return fn
        return _UNKNOWN

    def _bind(self, tgt: ast.expr, value: object) -> None:
        if isinstance(tgt, ast.Name):
            if tgt.id not in self.pinned:
                self.ns[tgt.id] = value
        elif isinstance(tgt, ast.Tuple | ast.List):
            for elt in tgt.elts:
                self._bind(elt, _UNKNOWN)

    def _visit_stmts(self, stmts: list[ast.stmt]) -> None:
        for s in stmts:
            if isinstance(s, ast.Import | ast.ImportFrom):
                try:
                    mod = ast.Module(body=[s], type_ignores=[])
                    exec(compile(mod, "<readme-import>", "exec"), self.ns)  # noqa: S102
                except Exception as exc:
                    self._fail(s, f"import failed: {exc}")
            elif isinstance(s, ast.Assign):
                self._check_expr(s.value)
                result = self._infer(s.value)
                for tgt in s.targets:
                    self._bind(tgt, result)
            elif isinstance(s, ast.With):
                for item in s.items:
                    self._check_expr(item.context_expr)
                    if item.optional_vars is not None:
                        self._bind(item.optional_vars, self._infer(item.context_expr))
                self._visit_stmts(s.body)
            elif isinstance(s, ast.For):
                self._bind(s.target, _UNKNOWN)
                self._check_expr(s.iter)
                self._visit_stmts(s.body)
            elif isinstance(s, ast.Expr):
                self._check_expr(s.value)
            else:
                for child in ast.iter_child_nodes(s):
                    if isinstance(child, ast.expr):
                        self._check_expr(child)

    def _check_expr(self, expr: ast.expr) -> None:
        for node in ast.walk(expr):
            if isinstance(node, ast.Attribute):
                self._resolve(node)
            elif isinstance(node, ast.Call):
                self._check_call(node)

    def _check_call(self, call: ast.Call) -> None:
        fn = self._resolve(call.func)
        if fn is _UNKNOWN or not callable(fn):
            return
        unbound_method = (
            isinstance(call.func, ast.Attribute)
            and inspect.isclass(self._resolve(call.func.value))
            and inspect.isfunction(fn)
        )
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return  # some builtins expose no signature; execution tiers cover them
        params = list(sig.parameters.values())
        if unbound_method:
            params = params[1:]  # the snippet calls it bound; drop `self`
        label = ast.unparse(call.func)
        byname = {p.name: p for p in params}
        has_varkw = any(p.kind is _P.VAR_KEYWORD for p in params)
        has_varpos = any(p.kind is _P.VAR_POSITIONAL for p in params)
        kw_named = [kw.arg for kw in call.keywords if kw.arg is not None]
        for name in kw_named:
            p = byname.get(name)
            bindable = p is not None and p.kind in (_P.POSITIONAL_OR_KEYWORD, _P.KEYWORD_ONLY)
            if not bindable and not has_varkw:
                accepted = sorted(
                    n
                    for n, q in byname.items()
                    if q.kind in (_P.POSITIONAL_OR_KEYWORD, _P.KEYWORD_ONLY)
                )
                self._fail(call, f"`{label}` got unexpected keyword {name!r} (accepts {accepted})")
        starred = any(isinstance(a, ast.Starred) for a in call.args)
        if not has_varpos and not starred:
            max_pos = sum(p.kind in (_P.POSITIONAL_ONLY, _P.POSITIONAL_OR_KEYWORD) for p in params)
            if len(call.args) > max_pos:
                self._fail(
                    call,
                    f"`{label}` takes at most {max_pos} positional arguments, "
                    f"snippet passes {len(call.args)}",
                )
        if starred or any(kw.arg is None for kw in call.keywords):
            return  # *args/**kwargs in the snippet: required-arg coverage is unprovable
        pos_filled = {
            p.name
            for p in params[: len(call.args)]
            if p.kind in (_P.POSITIONAL_ONLY, _P.POSITIONAL_OR_KEYWORD)
        }
        for p in params:
            if p.kind in (_P.VAR_POSITIONAL, _P.VAR_KEYWORD) or p.default is not _P.empty:
                continue
            if p.name not in pos_filled and p.name not in kw_named:
                self._fail(call, f"`{label}` missing required argument {p.name!r}")


# --- tier 3: execution stand-ins -----------------------------------------------------


class _LoopbackRuntimeStateProvider:
    """In-memory checkpoint/fork/resume so a mock-tier ``env`` stand-in counts as a
    provider-managed session (Sandbox runtime-state methods refuse to run otherwise)."""

    def __init__(self) -> None:
        self.checkpoints: dict[str, str] = {}

    def checkpoint(
        self,
        handle: object,
        *,
        name: str | None = None,
        event_seq: int | None = None,
        agent_state_ref: str | None = None,
    ) -> str:
        ckpt_id = f"ckpt-{name or len(self.checkpoints)}"
        self.checkpoints[ckpt_id] = str(handle)
        return ckpt_id

    def fork(self, handle: object) -> str:
        return f"{handle}-fork"

    def resume(self, handle_or_checkpoint: object) -> str:
        return f"{handle_or_checkpoint}-live"


def _exec_fixture(name: str, make_servers, cleanups: list) -> object:
    """A working stand-in for a narrative name, for real execution of a snippet."""
    if name == "shinken":
        return shinken  # blocks build on the quickstart's `import shinken`
    if name == "model_tool_call":
        return dict(_MODEL_TOOL_CALL)
    if name == "task":
        return _doc_task()
    if name == "endpoints":
        return [(addr, "doc-test-token") for addr in make_servers(2)]
    if name == "env":
        env = shinken.connect(make_servers(1)[0])
        env._set_provider_context(_LoopbackRuntimeStateProvider(), "sbx-doc")
        cleanups.append(env.close)
        return env
    if name == "provider":
        return shinken.DockerLocalProvider()  # docker-gated tier only
    pytest.fail(f"README snippet references {name!r} but the doc-test has no stand-in for it")


# --- the tests -----------------------------------------------------------------------


def test_readme_snippet_inventory():
    """Guard the extractor + classifier: if this goes empty, every other test here
    would silently collect nothing and the README could rot again."""
    assert len(_BLOCKS) >= 4, f"README.md should keep >=4 python snippets, found {len(_BLOCKS)}"
    assert _MOCK_BLOCKS, "no README snippet is runnable against the mock shinkend"
    assert _DOCKER_BLOCKS, "no README snippet exercises the Docker/runtime-state path"


def test_model_tool_call_standin_carries_payload():
    """The stand-in must map to an ACI action whose payload lives outside verb/target —
    that is what makes a payload-dropping consumption pattern in the README *fail* the
    executed adapter snippet instead of passing vacuously."""
    action = AnthropicComputerUseAdapter().to_aci_action(dict(_MODEL_TOOL_CALL))
    payload = {k: v for k, v in action.items() if k not in ("verb", "target")}
    assert payload, f"model_tool_call stand-in translated to a payload-free action: {action}"


@pytest.mark.parametrize(("idx", "lineno", "code"), _BLOCKS, ids=_ids(_BLOCKS))
def test_snippet_compiles(idx, lineno, code):
    compile(code, f"README.md:L{lineno}", "exec")


@pytest.mark.parametrize(("idx", "lineno", "code"), _BLOCKS, ids=_ids(_BLOCKS))
def test_snippet_api_surface(idx, lineno, code):
    errors = _ApiSurfaceChecker(code, lineno).run()
    assert not errors, "README snippet drifted from the real SDK API:\n" + "\n".join(errors)


@pytest.mark.parametrize(("idx", "lineno", "code"), _MOCK_BLOCKS, ids=_ids(_MOCK_BLOCKS))
def test_snippet_executes_against_mock(idx, lineno, code, mock_shinkend_many, monkeypatch):
    """Run the snippet for real against the in-process mock shinkend: handshake, typed
    actions, screenshots, and SharedLoop fan-out all go over a live WebSocket, and the
    mock schema-validates every frame — wrong wire shapes fail here."""
    primary = mock_shinkend_many(1)[0]
    real_connect = shinken.connect

    def connect_to_mock(addr=None, *args, **kwargs):
        return real_connect(addr if addr is not None else primary, *args, **kwargs)

    # Snippets dial the default loopback shinkend; route that to the mock. Explicit
    # addrs (the SharedLoop block's `endpoints`) already point at mock servers.
    monkeypatch.setattr(shinken, "connect", connect_to_mock)
    cleanups: list = []
    ns: dict[str, object] = {"__name__": "__shinken_readme__"}
    for name in sorted(_free_names(ast.parse(code))):
        ns[name] = _exec_fixture(name, mock_shinkend_many, cleanups)
    try:
        exec(compile(code, f"README.md:L{lineno}", "exec"), ns)  # noqa: S102
    finally:
        for value in ns.values():
            if isinstance(value, Sandbox):
                with contextlib.suppress(Exception):
                    value.close()
        for fn in cleanups:
            with contextlib.suppress(Exception):
                fn()


def _no_servers(n: int) -> list[str]:
    raise RuntimeError(
        "a Docker-tier snippet asked for a mock server stand-in; give it a Docker-backed one"
    )


def _docker_cleanup(ns: dict) -> None:
    """Best-effort reclamation of everything a Docker-tier snippet created: sessions,
    handles bound by the snippet (fork/revived/...), snapshot images, and orphans."""
    providers = [v for v in ns.values() if isinstance(v, DockerLocalProvider)]
    for value in ns.values():
        if isinstance(value, Sandbox):
            handle = getattr(value._inner, "_handle", None)
            with contextlib.suppress(Exception):
                value.close()
            if handle is not None:
                for p in providers:
                    with contextlib.suppress(Exception):
                        p.destroy(handle)
        elif isinstance(value, SandboxHandle):
            for p in providers:
                with contextlib.suppress(Exception):
                    p.destroy(value)
    for p in providers:
        with contextlib.suppress(Exception):
            p.cleanup_snapshots()
        with contextlib.suppress(Exception):
            p.destroy_all()  # the blunt label sweep (cleanup_orphans is owner-aware now)


@pytest.mark.skipif(
    os.environ.get("SHINKEN_DOCTEST_DOCKER") != "1",
    reason="Docker-backed README snippets run live only with SHINKEN_DOCTEST_DOCKER=1 "
    "(the api-surface tier still checks them in every run)",
)
@pytest.mark.parametrize(("idx", "lineno", "code"), _DOCKER_BLOCKS, ids=_ids(_DOCKER_BLOCKS))
def test_snippet_executes_against_docker(idx, lineno, code):
    """Opt-in live tier for provider/Docker snippets (checkpoint/fork/resume +
    run_eval_forked) — real containers, real `docker commit` snapshots."""
    cleanups: list = []
    ns: dict[str, object] = {"__name__": "__shinken_readme__"}
    for name in sorted(_free_names(ast.parse(code))):
        ns[name] = _exec_fixture(name, _no_servers, cleanups)
    try:
        exec(compile(code, f"README.md:L{lineno}", "exec"), ns)  # noqa: S102
        summary = ns.get("summary")
        if summary is not None:  # the forked-eval loop must actually score replicas
            assert summary.results, "run_eval_forked returned no replica results"
            assert summary.passed > 0, f"no forked replica passed: kinds={summary.kinds}"
    finally:
        _docker_cleanup(ns)
        for fn in cleanups:
            with contextlib.suppress(Exception):
                fn()
