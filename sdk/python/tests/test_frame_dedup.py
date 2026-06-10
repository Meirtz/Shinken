"""Content-negotiated observation (screenshot ``if_none_match`` / ``frame_hash``).

A dedup-enabled screenshot offers the last seen frame_hash; on a hash hit the
runtime answers a compact ``not_modified`` (no payload) and the SDK transparently
returns the cached frame with ``deduped=True``. The mock's frame hash is a
deterministic function of its observed effects (typing/clicking "changes the
screen"), so hit/miss/divergence are all exercisable without a real desktop.
Covers: per-session cache hit/miss, the env/ctor default knobs, the shared
:class:`FrameCache` across sessions (the forked-fleet case), divergence and
re-convergence, the capability-gate fallback against a pre-dedup runtime, and the
FrameCache LRU bound.
"""

from __future__ import annotations

import base64
import threading

import shinken
from shinken import FrameCache

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)


# ---- per-session dedup ----


def test_screenshot_dedup_hits_after_first_full_frame(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.capabilities.frame_dedup is True
        first = env.screenshot(dedup=True)
        # miss: full payload + the hash to offer next time
        assert first["deduped"] is False
        assert first["bytes"] == _PNG_1X1
        assert isinstance(first["frame_hash"], str)
        second = env.screenshot(dedup=True)
        # hit: served from the session cache, payload identical, tiny wire footprint
        assert second["deduped"] is True
        assert second["bytes"] == _PNG_1X1
        assert second["png"] == _PNG_1X1  # the back-compat alias survives dedup
        assert second["frame_hash"] == first["frame_hash"]
        assert second["wire_len"] < 256  # the not_modified frame, not the payload
        assert second["wire_len"] < first["wire_len"]


def test_screenshot_dedup_off_by_default(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        a = env.screenshot()
        b = env.screenshot()
        assert a["deduped"] is False and b["deduped"] is False
        # the runtime still hands out frame_hash; the SDK just never offered one back
        assert "frame_hash" in b


def test_screenshot_dedup_misses_when_the_screen_changes(mock_shinkend):
    """Divergence: an action changes the mock's content hash, so the next dedup
    screenshot is an honest miss (full frame, new hash) — then hits again."""
    with shinken.connect(mock_shinkend) as env:
        first = env.screenshot(dedup=True)
        env.type_text("diverge")  # changes the mock's frame hash
        second = env.screenshot(dedup=True)
        assert second["deduped"] is False
        assert second["frame_hash"] != first["frame_hash"]
        third = env.screenshot(dedup=True)  # re-converged on its own new frame
        assert third["deduped"] is True
        assert third["frame_hash"] == second["frame_hash"]


def test_dedup_is_scoped_to_capture_params(mock_shinkend):
    """A hash minted under one (scope, format, quality) is not offered for another:
    the cached bytes are codec-specific even though the hash is not."""
    with shinken.connect(mock_shinkend) as env:
        png = env.screenshot(dedup=True, format="png")
        jpeg = env.screenshot(dedup=True, format="jpeg")
        # different params_key → no cross-codec serve, even with equal hashes
        assert png["deduped"] is False and jpeg["deduped"] is False
        assert jpeg["format"] == "jpeg"
        again = env.screenshot(dedup=True, format="jpeg")
        assert again["deduped"] is True and again["format"] == "jpeg"


# ---- default knobs (ctor + env) ----


def test_connect_screenshot_dedup_default(mock_shinkend):
    with shinken.connect(mock_shinkend, screenshot_dedup=True) as env:
        env.screenshot()
        assert env.screenshot()["deduped"] is True
        # per-call override beats the session default
        assert env.screenshot(dedup=False)["deduped"] is False


def test_env_var_sets_the_dedup_default(mock_shinkend, monkeypatch):
    monkeypatch.setenv("SHINKEN_SCREENSHOT_DEDUP", "1")
    with shinken.connect(mock_shinkend) as env:
        env.screenshot()
        assert env.screenshot()["deduped"] is True
    # and the ctor knob overrides the env
    monkeypatch.setenv("SHINKEN_SCREENSHOT_DEDUP", "1")
    with shinken.connect(mock_shinkend, screenshot_dedup=False) as env:
        env.screenshot()
        assert env.screenshot()["deduped"] is False


# ---- the forked-fleet case: one shared FrameCache across sessions ----


def test_shared_frame_cache_dedups_across_sessions(mock_shinkend_many):
    """Two 'replicas' (fresh mock servers — identical state, like forks of one
    checkpoint) share one FrameCache: the first session pays the full frame ONCE,
    the second session's very first screenshot is already a dedup hit."""
    addr_a, addr_b = mock_shinkend_many(2)
    cache = FrameCache()
    with (
        shinken.connect(addr_a, frame_cache=cache, screenshot_dedup=True) as a,
        shinken.connect(addr_b, frame_cache=cache, screenshot_dedup=True) as b,
    ):
        first = a.screenshot()
        assert first["deduped"] is False
        cross = b.screenshot()  # b never fetched a frame — the shared cache serves it
        assert cross["deduped"] is True
        assert cross["bytes"] == first["bytes"]
        assert cache.hits == 1 and cache.misses == 1
        assert cache.hit_rate == 0.5


def test_shared_cache_diverged_replica_reconverges_on_its_own_frame(mock_shinkend_many):
    """After one replica diverges it pays ONE miss, then dedups against its OWN
    last frame (the session-local candidate is preferred over the shared one) —
    while the clean replica keeps hitting the shared entry."""
    addr_a, addr_b = mock_shinkend_many(2)
    cache = FrameCache()
    with (
        shinken.connect(addr_a, frame_cache=cache, screenshot_dedup=True) as a,
        shinken.connect(addr_b, frame_cache=cache, screenshot_dedup=True) as b,
    ):
        a.screenshot()  # seed the shared cache
        assert b.screenshot()["deduped"] is True
        b.type_text("now I'm different")  # replica b diverges from the fleet
        miss = b.screenshot()
        assert miss["deduped"] is False
        # b re-converges on its own content; a keeps deduping the fleet frame
        assert b.screenshot()["deduped"] is True
        assert a.screenshot()["deduped"] is True


# ---- back-compat: a pre-dedup runtime never sees if_none_match ----


def test_dedup_request_against_pre_dedup_runtime_falls_back(mock_shinkend_no_dedup):
    """The mock hard-nacks any screenshot carrying if_none_match (deny_unknown_fields,
    like the real old runtime) — so plain success here proves the SDK's capability
    gate kept the field off the wire entirely."""
    with shinken.connect(mock_shinkend_no_dedup) as env:
        assert env.capabilities.frame_dedup is False
        first = env.screenshot(dedup=True)
        second = env.screenshot(dedup=True)
        assert first["bytes"] == _PNG_1X1 and second["bytes"] == _PNG_1X1
        assert first["deduped"] is False and second["deduped"] is False
        assert "frame_hash" not in second  # old runtime computes no hash


# ---- FrameCache itself ----


def test_frame_cache_lru_cap_bounds_entries():
    cache = FrameCache(max_entries=2)
    cache.put("k", "h1", {"bytes": b"1"})
    cache.put("k", "h2", {"bytes": b"2"})
    cache.put("k", "h3", {"bytes": b"3"})  # evicts h1 (least recently used)
    assert len(cache) == 2
    assert cache.lookup("k", "h1") is None
    assert cache.lookup("k", "h2") == {"bytes": b"2"}
    assert cache.lookup("k", "h3") == {"bytes": b"3"}
    # the candidate pointer follows the latest put for the params key
    assert cache.candidate("k") == ("h3", {"bytes": b"3"})


def test_frame_cache_candidate_survives_eviction_of_other_keys():
    cache = FrameCache(max_entries=2)
    cache.put("a", "ha", {"bytes": b"a"})
    cache.put("b", "hb", {"bytes": b"b"})
    cache.lookup("a", "ha")  # refresh ha so hb is the LRU victim
    cache.put("c", "hc", {"bytes": b"c"})  # evicts hb
    assert cache.candidate("b") is None  # dangling pointer cleaned, not a stale hit
    assert cache.candidate("a") == ("ha", {"bytes": b"a"})


def test_frame_cache_keys_entries_by_params_not_hash_alone():
    """The raw-pixel hash is codec-independent, so png and jpeg of the same screen
    share a hash — the cache must keep BOTH payloads, keyed by params."""
    cache = FrameCache()
    cache.put("screen|png|None", "h", {"bytes": b"png-bytes", "format": "png"})
    cache.put("screen|jpeg|80", "h", {"bytes": b"jpeg-bytes", "format": "jpeg"})
    assert cache.candidate("screen|png|None")[1]["format"] == "png"
    assert cache.candidate("screen|jpeg|80")[1]["format"] == "jpeg"


def test_frame_cache_is_thread_safe_under_concurrent_puts_and_reads():
    cache = FrameCache(max_entries=16)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for n in range(200):
                cache.put(f"k{i}", f"h{i}-{n}", {"bytes": bytes([i])})
                cache.candidate(f"k{i}")
                cache.lookup(f"k{i}", f"h{i}-{n}")
                cache.record(hit=n % 2 == 0)
        except Exception as exc:  # pragma: no cover - only on a real race
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(cache) <= 16
    assert cache.hits + cache.misses == 8 * 200
