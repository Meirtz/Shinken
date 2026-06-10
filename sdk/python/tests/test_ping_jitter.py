"""Keepalive phase decorrelation (docs/engineering/many-sandbox-concurrency.md §3.2).

The websockets library pings every ``ping_interval`` (20 s) from the moment of connect,
so a fleet dialed together pings in the same tick forever. ``ping_jitter`` draws a
per-connection interval once, at dial time: ``20.0 + uniform(0, ping_jitter)``.
"""

from __future__ import annotations

import asyncio

import shinken
import shinken.client as client


def test_default_keeps_library_ping_interval(mock_shinkend):
    async def run():
        sb = await shinken.aconnect(mock_shinkend)
        try:
            return sb._ws.ping_interval
        finally:
            await sb.close()

    assert asyncio.run(run()) == 20  # websockets' own default, untouched


def test_ping_jitter_offsets_interval_at_dial(mock_shinkend, monkeypatch):
    drawn: list[tuple[float, float]] = []

    def fake_uniform(a: float, b: float) -> float:
        drawn.append((a, b))
        return b  # deterministic: the max of the jitter range

    monkeypatch.setattr(client.random, "uniform", fake_uniform)

    async def run():
        sb = await shinken.aconnect(mock_shinkend, ping_jitter=7.5)
        try:
            return sb._ws.ping_interval
        finally:
            await sb.close()

    assert asyncio.run(run()) == 20.0 + 7.5
    assert drawn == [(0.0, 7.5)]  # one draw, at dial time


def test_ping_jitter_draws_within_range(mock_shinkend):
    async def run():
        sb = await shinken.aconnect(mock_shinkend, ping_jitter=5.0)
        try:
            return sb._ws.ping_interval
        finally:
            await sb.close()

    interval = asyncio.run(run())
    assert 20.0 <= interval <= 25.0


def test_sync_connect_plumbs_ping_jitter(mock_shinkend, monkeypatch):
    monkeypatch.setattr(client.random, "uniform", lambda a, b: b)
    with shinken.connect(mock_shinkend, ping_jitter=3.0) as env:
        assert env._inner._ws.ping_interval == 23.0
