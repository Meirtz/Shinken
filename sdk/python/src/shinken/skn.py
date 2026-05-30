"""`.skn` v0 — the event-sourced replay bundle (see docs/05-tech-decisions.md D5).

A bundle is a ZIP (Playwright-trace model): ``manifest.json`` + append-only
``events.jsonl`` (one event per line) + content-addressed ``media/<sha256>``. The
event stream *is* the replay log. :class:`Recorder` accumulates events during a
session and writes a bundle; :class:`Replay` loads one back for scrubbing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

SKN_VERSION = 0

#: v0 session **capability envelope** — what a Sandbox session is permitted to do.
#: This is *reference* semantics for replay/audit (recorded, inspectable), **not**
#: Cedar/ocap/OS enforcement — that lands in a later milestone. See #83 / #7.
DEFAULT_CAPABILITIES: dict[str, Any] = {
    "input_automation": True,  # synthetic pointer/keyboard
    "screenshot": True,
    "a11y": True,  # accessibility-tree capture
    "fs_scope": "session",  # local filesystem scope
    "egress": False,  # network egress
    "credentials": False,
    "clipboard": False,
    "privileged_install": False,
}


@lru_cache(maxsize=1)
def skn_schema() -> dict:
    """Load the `.skn` v0 JSON Schema (packaged copy, with a repo-root fallback)."""
    try:
        from importlib.resources import files

        text = files("shinken").joinpath("schemas", "skn.schema.json").read_text(encoding="utf-8")
        return json.loads(text)
    except (FileNotFoundError, ModuleNotFoundError, NotADirectoryError):
        root = Path(__file__).resolve().parents[4]
        return json.loads((root / "schema" / "skn.schema.json").read_text())


def _validate_bundle(manifest: dict, events: list[dict]) -> None:
    """Validate the manifest + each event against `schema/skn.schema.json`.

    Best-effort: silently skips if the optional ``jsonschema`` dependency is absent
    (so recording works without the ``[dev]`` extra); when it is present, raises
    ``jsonschema.ValidationError`` on a malformed bundle.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - exercised only without the dev extra
        return
    base = {"$defs": skn_schema().get("$defs", {})}
    jsonschema.validate(manifest, {**base, "$ref": "#/$defs/Manifest"})
    for ev in events:
        jsonschema.validate(ev, {**base, "$ref": "#/$defs/Event"})


class Recorder:
    """Accumulates timestamped events + content-addressed media for one session.

    A `.skn` bundle can contain screenshots, typed text, and boundary context, so it
    is a **sensitive artifact** — store and share it accordingly. For sensitive runs,
    ``redact_media`` drops captured screenshot/media bytes (recording only dimensions,
    marked redacted) and ``redact_text`` strips typed text from actions, so plaintext
    secrets are never persisted (#88).
    """

    def __init__(
        self,
        session_id: str | None = None,
        platform: str = "linux",
        aci_version: int = 0,
        capabilities: dict | None = None,
        redact_media: bool = False,
        redact_text: bool = False,
    ):
        self.session_id = session_id or f"sbx-{uuid.uuid4().hex[:12]}"
        self.run_id = f"run-{uuid.uuid4().hex[:12]}"
        self.platform = platform
        self.aci_version = aci_version
        self.capabilities = {**DEFAULT_CAPABILITIES, **(capabilities or {})}
        self.redact_media = redact_media
        self.redact_text = redact_text
        self._events: list[dict] = []
        self._media: dict[str, bytes] = {}
        self._seq = 0
        self._t0 = time.monotonic()
        self._t0_wall = datetime.now(timezone.utc).isoformat()

    def _emit(self, kind: str, src: str, payload: dict, action_id: str | None = None) -> dict:
        ev: dict[str, Any] = {
            "seq": self._seq,
            "dt": round(time.monotonic() - self._t0, 6),
            "kind": kind,
            "src": src,
            "payload": payload,
        }
        if action_id is not None:
            ev["action_id"] = action_id
        self._seq += 1
        self._events.append(ev)
        return ev

    def action(
        self, verb: str, action: dict, action_id: str, *, batch_id: str | None = None
    ) -> dict:
        if self.redact_text and "text" in action:
            action = {**action, "text": "[redacted]"}  # never persist typed secrets
        ev = self._emit("action", verb, action, action_id)
        if batch_id is not None:
            ev["batch_id"] = batch_id  # groups actions dispatched as one ordered batch (#73)
        return ev

    def observation(
        self, payload: dict, *, action_id: str | None = None, png: bytes | None = None
    ) -> dict:
        payload = dict(payload)
        src = "a11y"
        if png is not None:
            image = dict(payload.get("image") or {})
            if self.redact_media:
                # record dimensions only — no raw bytes, no content-addressed media
                image.pop("ref", None)
                image["redacted"] = True
            else:
                sha = hashlib.sha256(png).hexdigest()
                self._media[sha] = png
                image["ref"] = sha
            payload["image"] = image
            src = "image"
        return self._emit("observation", src, payload, action_id)

    def capability_envelope(self) -> dict:
        """Emit the session capability envelope as a `meta` event at session start.
        Reference semantics for replay/audit — not enforcement (#83)."""
        return self._emit("meta", "capability_envelope", {"capabilities": self.capabilities})

    def meta(self, subtype: str, payload: dict) -> dict:
        """Record arbitrary run metadata as a ``meta`` event — e.g. a CU adapter's
        identity / tool version / coordinate space (#75/#76). Reference semantics."""
        return self._emit("meta", subtype, dict(payload))

    def permission(self, payload: dict) -> dict:
        """Record a boundary decision (grant / deny / narrow) as a permission event."""
        return self._emit("permission", payload.get("decision", "ask"), payload)

    def marker(self, name: str) -> dict:
        return self._emit("marker", name, {})

    def verifier_receipt(self, receipt: dict) -> dict:
        """Record an eval verifier verdict as a first-class ``verifier_receipt`` event
        (#149) — so a `.skn` is self-contained eval evidence (pass/fail + checks), not
        just a side summary. ``src`` is ``pass``/``fail`` for quick timeline scanning."""
        return self._emit(
            "verifier_receipt", "pass" if receipt.get("passed") else "fail", dict(receipt)
        )

    def file_transfer(self, ref: dict, data: bytes | None = None) -> dict:
        """Record a file transfer (#85) by content hash/path/scope — never inlining the
        bytes into the event. When ``data`` is supplied (and media isn't redacted) the
        bytes are content-addressed into ``media/<sha256>`` so a replay can reproduce the
        artifact; otherwise only the ref is kept (the default — large payloads stay out
        of the bundle)."""
        payload = dict(ref)
        if data is not None and not self.redact_media:
            sha = ref.get("sha256") or hashlib.sha256(data).hexdigest()
            self._media[sha] = data
            payload["stored"] = True
        return self._emit("file_transfer", ref.get("direction", "put"), payload)

    def manifest(self) -> dict:
        return {
            "skn_version": SKN_VERSION,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "t0_wall": self._t0_wall,
            "t0_mono": 0.0,
            "platform": self.platform,
            "aci_version": self.aci_version,
            "capabilities": self.capabilities,
            "redaction": {"media": self.redact_media, "text": self.redact_text},
            "channels": sorted({e["kind"] for e in self._events}),
        }

    def save(self, path: str, *, validate: bool = True) -> str:
        """Write the bundle to ``path`` (a ``.skn`` ZIP), **atomically**.

        The bundle is built in a temp file in the same directory and ``os.replace``d
        into place, so a crash mid-write can never leave a partial/corrupt ``.skn``.
        When ``validate`` is set (and the optional ``jsonschema`` dep is available) the
        manifest and every event are checked against ``schema/skn.schema.json`` first.
        """
        manifest = self.manifest()
        if validate:
            _validate_bundle(manifest, self._events)
        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(suffix=".skn.tmp", dir=directory)
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("manifest.json", json.dumps(manifest, indent=2))
                z.writestr("events.jsonl", "".join(json.dumps(e) + "\n" for e in self._events))
                for sha, data in self._media.items():
                    z.writestr(f"media/{sha}", data)
            os.replace(tmp, path)  # atomic on the same filesystem
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
        return path

    @property
    def events(self) -> list[dict]:
        return self._events


class Replay:
    """A loaded `.skn` bundle: manifest + ordered events + media accessor."""

    def __init__(self, manifest: dict, events: list[dict], path: str):
        self.manifest = manifest
        self._events = events
        self._path = path

    @classmethod
    def load(cls, path: str) -> Replay:
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json"))
            lines = z.read("events.jsonl").decode("utf-8").splitlines()
        events = [json.loads(line) for line in lines if line.strip()]
        return cls(manifest, events, path)

    @property
    def events(self) -> list[dict]:
        return self._events

    def media(self, sha: str) -> bytes:
        with zipfile.ZipFile(self._path) as z:
            return z.read(f"media/{sha}")

    def media_keys(self) -> list[str]:
        """Content hashes of every blob in the bundle's ``media/`` store (screenshots,
        archived artifacts) — without reading their bytes."""
        with zipfile.ZipFile(self._path) as z:
            names = [n for n in z.namelist() if n.startswith("media/") and n != "media/"]
        return sorted(n[len("media/") :] for n in names)

    def validate(self) -> None:
        """Schema-validate the manifest + events and check action/observation pairing
        integrity. Raises on a malformed bundle or a dangling ``action_id``."""
        _validate_bundle(self.manifest, self._events)
        check_pairing(self._events)

    def steps(self) -> list[dict]:
        """Group the timeline into steps for scrubbing: each ``action`` plus the
        events that follow it until the next action. Returns ``[{action, events}]``
        (``action`` is ``None`` for any leading pre-action events)."""
        steps: list[dict] = []
        cur: dict = {"action": None, "events": []}
        for e in self._events:
            if e.get("kind") == "action":
                if cur["events"]:
                    steps.append(cur)
                cur = {"action": e, "events": [e]}
            else:
                cur["events"].append(e)
        if cur["events"]:
            steps.append(cur)
        return steps

    def __len__(self) -> int:
        return len(self._events)


def check_pairing(events: list[dict]) -> None:
    """Verify action/observation pairing integrity: every non-action event that
    references an ``action_id`` must point at a real recorded ``action``. Raises
    :class:`ValueError` on a dangling/broken link."""
    action_ids = {e.get("action_id") for e in events if e.get("kind") == "action"}
    action_ids.discard(None)
    for e in events:
        aid = e.get("action_id")
        if aid is not None and e.get("kind") != "action" and aid not in action_ids:
            raise ValueError(
                f"event seq={e.get('seq')} ({e.get('kind')}) references action_id "
                f"{aid!r} with no matching action"
            )


def summarize(path: str) -> str:
    """Human-readable timeline summary of a `.skn` bundle (used by the CLI)."""
    rp = Replay.load(path)
    m = rp.manifest
    out = [
        f"{path}",
        f"  session {m.get('session_id')} · run {m.get('run_id')} · platform {m.get('platform')}",
        f"  {len(rp)} events · channels: {', '.join(m.get('channels', []))}",
        "",
    ]
    for e in rp.events:
        extra = ""
        if e["kind"] == "action":
            extra = f" target={(e['payload'].get('target') or {}).get('kind', '-')}"
        elif e["kind"] == "observation":
            img = e["payload"].get("image") or {}
            if img:
                extra = f" image={img.get('w')}x{img.get('h')}"
        out.append(f"  [{e['seq']:>3}] +{e['dt']:.3f}s {e['kind']}:{e['src']}{extra}")
    return "\n".join(out)
