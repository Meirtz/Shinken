"""Typed failure taxonomy (#56).

A consumer — an eval harness, an RL rollout collector, an interactive driver — must be able
to tell **infrastructure death** (the sandbox/runtime went away: retry on a fresh sandbox or
fork) apart from **task failure** (the agent did the wrong thing: score it 0, do not retry).
These exception types make that branch machine-actionable instead of string-matching.

`SandboxDied` is raised by the provider when the substrate exits underneath a live session
(a container that OOM-killed, a runtime that crashed). It is the Python-side counterpart of
the `sandbox_died` failure class: shinkend cannot report its own death over the wire, so the
provider — which owns the substrate lifecycle — classifies it.
"""

from __future__ import annotations


class ShinkenError(RuntimeError):
    """Base class for typed Shinken SDK errors."""


class SessionClosed(ShinkenError):
    """A method was called on a Sandbox/AsyncSandbox after ``close()``.

    Raised IMMEDIATELY and typed — before this existed, a call on a closed sync
    :class:`~shinken.Sandbox` scheduled a coroutine onto a stopped event loop and
    blocked forever (the use-after-close deadlock). ``close()`` itself stays
    idempotent: only the *other* methods raise."""


class ConnectError(ShinkenError, ConnectionError):
    """Could not establish a session to the runtime (dial/handshake transport failure).

    One typed error for the whole zoo a dead address used to surface
    (``ConnectionRefusedError``, ``OSError``, websockets' ``InvalidMessage``/
    ``InvalidHandshake``, a handshake timeout). Subclasses :class:`ConnectionError`
    too, so existing ``except ConnectionError`` callers keep working."""


class UnknownVerb(ShinkenError):
    """The runtime rejected an action because it does not know the verb (an older
    runtime, or a typo'd verb name). Typed so a caller can branch
    capability-negotiation fallback vs a genuine action failure without
    string-matching the runtime's ``unknown verb: …`` message."""


class ProviderRequired(ShinkenError):
    """A runtime-state/lifecycle operation (``checkpoint``/``fork``/``spawn``/
    ``destroy``) was called on a session with no managing provider attached. Open the
    session via a provider's ``connect()``/``session()`` instead of a bare
    ``shinken.connect()``."""


class SandboxDied(ShinkenError):
    """The sandbox/runtime substrate died underneath a live session — infrastructure
    failure, not task failure. Carries the substrate exit detail when the provider can
    recover it (e.g. a container's exit code / OOM signal), so a consumer can branch
    retry-with-more-resources vs mark-trajectory-failed."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        signal: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.signal = signal
        self.detail = detail
        suffix = []
        if exit_code is not None:
            suffix.append(f"exit_code={exit_code}")
        if signal is not None:
            suffix.append(f"signal={signal}")
        full = message if not suffix else f"{message} ({', '.join(suffix)})"
        super().__init__(full)


class ScorerError(ShinkenError):
    """The isolated scorer failed to produce a trustworthy verdict (T-5 scorer isolation;
    see ``shinken.scorer_proc``). Typed so a consumer records the trajectory-level
    ``exit_reason="scorer_error"`` instead of mistaking a scorer fault for a task failure
    (a real 0) or an infrastructure death (a retry signal). ``kind`` is one of
    :data:`ScorerError.KINDS`: ``crash`` (the scorer process exited without writing a
    verdict), ``timeout`` (killed at the deadline with no verdict written), or ``garbage``
    (exited cleanly but produced no parseable verdict)."""

    KINDS = ("crash", "timeout", "garbage")

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        exit_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"unknown ScorerError kind {kind!r}; expected one of {self.KINDS}")
        self.kind = kind
        self.exit_code = exit_code
        self.detail = detail
        suffix = [f"kind={kind}"]
        if exit_code is not None:
            suffix.append(f"exit_code={exit_code}")
        super().__init__(f"{message} ({', '.join(suffix)})")


#: The typed status of a single dispatched action in a batch result (#56). `ok` and `error`
#: come from the runtime's ack (client-side gate denials also classify as `error`);
#: `timeout` is an RPC deadline; `skipped` means the action never ran because an earlier
#: action in the batch failed first; `sandbox_died` means the substrate went away mid-batch
#: (infrastructure failure, distinct from a per-action error).
ACTION_STATUSES = ("ok", "error", "timeout", "skipped", "sandbox_died")


def is_connection_loss(exc: BaseException) -> bool:
    """True for exceptions that mean the transport to the sandbox went away. Covers the
    builtin ``ConnectionError`` family AND the websockets library's ``ConnectionClosed``
    (raised by a send on a dead socket — NOT a ``ConnectionError`` subclass), so the
    common death-while-idle timing (sandbox OOM-killed between actions; the next send
    fails) classifies as infrastructure loss rather than a generic error."""
    if isinstance(exc, ConnectionError):
        return True
    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError:  # pragma: no cover — websockets is a hard SDK dependency
        return False
    return isinstance(exc, ConnectionClosed)


def classify_exception(exc: BaseException) -> str:
    """Map an exception raised while dispatching an action to an :data:`ACTION_STATUSES`
    value, so batch results carry a typed status rather than only a string."""
    if isinstance(exc, SandboxDied):
        return "sandbox_died"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if is_connection_loss(exc):
        # the connection dropping under us is usually the sandbox going away — only the
        # provider can confirm exit detail (check_alive), so stay coarse here
        return "sandbox_died"
    return "error"
