"""Provider-neutral smoke workflow: agent (#91), egress proxy (#93), hygiene (#92)."""

from __future__ import annotations

from pathlib import Path

import shinken
from shinken.egress import proxy_status, redact_proxy_url, resolve_task_egress_proxy
from shinken.skn import Replay
from shinken.smoke import SmokeConfig, run_smoke_agent


class _StubAgent:
    """Deterministic agent — no model/network/secret needed (CI-safe)."""

    def __init__(self):
        self.calls = 0

    def act(self, observation, instruction):
        assert "screenshot" in observation
        self.calls += 1
        if self.calls == 1:
            return {"verb": "click", "x": 5, "y": 7}
        if self.calls == 2:
            return {"verb": "type_text", "text": "smoke"}
        return "DONE"


def test_skips_cleanly_without_model_config():
    res = run_smoke_agent(config=SmokeConfig())  # no model env → clean skip
    assert res.status == "skipped" and not res.ok
    assert "SHK_SMOKE_MODEL" in res.reason
    assert res.proxy["task_egress_proxy"] == "skipped"


def test_stub_agent_one_task_flow(mock_shinkend, tmp_path):
    res = run_smoke_agent(
        agent=_StubAgent(),
        connect_factory=lambda: shinken.connect(mock_shinkend, record=True),
        out_path=str(tmp_path / "smoke.skn"),
    )
    assert res.ok and res.status == "pass"
    assert res.steps == 3  # click, type_text, DONE
    assert res.bundle and Path(res.bundle).exists()
    srcs = [e["src"] for e in Replay.load(res.bundle).events if e["kind"] == "action"]
    assert "click" in srcs and "type_text" in srcs


def test_egress_proxy_status_hides_secrets():
    cfg = resolve_task_egress_proxy(
        {"SHK_TASK_EGRESS_PROXY": "http://user:pass@proxy.example:8080"}
    )
    assert cfg is not None and cfg.scheme == "http"
    status = proxy_status(cfg)
    assert status["task_egress_proxy"] == "requested" and status["applied_to"] == "task_egress"
    # no host/user/password anywhere in the surfaced status
    blob = repr(status)
    assert "user" not in blob and "pass" not in blob and "proxy.example" not in blob
    assert redact_proxy_url("http://user:pass@proxy.example:8080") == "http://<redacted>"
    assert proxy_status(None)["task_egress_proxy"] == "skipped"
    assert proxy_status(None, failed=True)["task_egress_proxy"] == "failed"


def test_smoke_config_from_env():
    cfg = SmokeConfig.from_env(
        {
            "SHK_SMOKE_MODEL_BASE_URL": "https://api.example/v1",
            "SHK_SMOKE_MODEL_API_KEY": "k",
            "SHK_SMOKE_MODEL_NAME": "m",
            "SHK_ADDR": "1.2.3.4:9",
        }
    )
    assert cfg.model_available and cfg.model == "m" and cfg.address == "1.2.3.4:9"
    assert not SmokeConfig().model_available  # nothing set → unavailable
