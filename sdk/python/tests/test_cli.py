"""`shinken` CLI entry point (was at 0% coverage)."""

from __future__ import annotations

from shinken import cli


def test_connect_subcommand_prints_capabilities(mock_shinkend, capsys):
    rc = cli.main(["connect", mock_shinkend])
    out = capsys.readouterr().out
    assert rc == 0
    assert "connected to shinkend" in out
    assert "platform" in out and "verbs" in out


def test_no_subcommand_prints_help_and_returns_1(capsys):
    rc = cli.main([])
    assert rc == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_connect_token_defaults_from_env(monkeypatch, mock_shinkend):
    # --token defaults to $SHK_TOKEN so the CLI can reach a token-protected runtime; the
    # mock ignores the token, so this just exercises the env-default plumbing end to end.
    monkeypatch.setenv("SHK_TOKEN", "shk_fromenv")
    captured: dict = {}
    real_connect = cli.connect

    def spy(addr, token=None):
        captured["token"] = token
        return real_connect(addr, token=token)

    monkeypatch.setattr(cli, "connect", spy)
    rc = cli.main(["connect", mock_shinkend])
    assert rc == 0
    assert captured["token"] == "shk_fromenv"
