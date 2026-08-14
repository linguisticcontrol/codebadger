from __future__ import annotations

import pytest

from scripts import codebadger_mcp_proxy


def test_get_upstream_url_defaults_to_internal_codebadger(monkeypatch):
    monkeypatch.delenv("CODEBADGER_UPSTREAM_URL", raising=False)

    assert codebadger_mcp_proxy.get_upstream_url() == "http://codebadger-mcp:4242/mcp"


def test_get_upstream_url_accepts_absolute_https_and_trims(monkeypatch):
    monkeypatch.setenv("CODEBADGER_UPSTREAM_URL", "  https://codebadger.example/mcp  ")

    assert codebadger_mcp_proxy.get_upstream_url() == "https://codebadger.example/mcp"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "codebadger-mcp:4242/mcp",
        "file:///tmp/codebadger.sock",
    ],
)
def test_get_upstream_url_rejects_invalid_transport(monkeypatch, url):
    monkeypatch.setenv("CODEBADGER_UPSTREAM_URL", url)

    if not url:
        # An empty setting intentionally selects the safe internal default.
        assert (
            codebadger_mcp_proxy.get_upstream_url() == "http://codebadger-mcp:4242/mcp"
        )
        return

    with pytest.raises(ValueError, match="absolute http"):
        codebadger_mcp_proxy.get_upstream_url()


def test_tool_profile_defaults_to_controller(monkeypatch):
    monkeypatch.delenv("CODEBADGER_TOOL_PROFILE", raising=False)
    assert codebadger_mcp_proxy.get_tool_profile() == "controller"


def test_tool_profile_accepts_query_only_and_normalizes(monkeypatch):
    monkeypatch.setenv("CODEBADGER_TOOL_PROFILE", " Query-Only ")
    assert codebadger_mcp_proxy.get_tool_profile() == "query-only"


def test_tool_profile_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("CODEBADGER_TOOL_PROFILE", "review-ish")
    with pytest.raises(ValueError, match="controller, query-only"):
        codebadger_mcp_proxy.get_tool_profile()


def test_main_runs_fastmcp_proxy_over_stdio(monkeypatch):
    calls = {}

    class FakeProxy:
        def run(self, **kwargs):
            calls["run"] = kwargs

    def fake_create_proxy(target, **settings):
        calls["target"] = target
        calls["settings"] = settings
        return FakeProxy()

    monkeypatch.setenv("CODEBADGER_UPSTREAM_URL", "http://codebadger-mcp:4242/mcp")
    monkeypatch.delenv("CODEBADGER_TOOL_PROFILE", raising=False)
    monkeypatch.setattr(codebadger_mcp_proxy, "create_proxy", fake_create_proxy)

    codebadger_mcp_proxy.main()

    assert calls == {
        "target": "http://codebadger-mcp:4242/mcp",
        "settings": {"name": "CodeBadger"},
        "run": {"transport": "stdio", "show_banner": False},
    }


def test_main_applies_query_only_allowlist(monkeypatch):
    calls = {}

    class FakeProxy:
        def enable(self, **kwargs):
            calls["enable"] = kwargs

        def run(self, **kwargs):
            calls["run"] = kwargs

    monkeypatch.setenv("CODEBADGER_TOOL_PROFILE", "query-only")
    monkeypatch.setattr(
        codebadger_mcp_proxy,
        "create_proxy",
        lambda target, **settings: FakeProxy(),
    )

    codebadger_mcp_proxy.main()

    assert calls["enable"] == {
        "names": set(codebadger_mcp_proxy.QUERY_ONLY_TOOLS),
        "components": {"tool"},
        "only": True,
    }
    assert "generate_cpg" not in calls["enable"]["names"]
    assert "remove_cpg" not in calls["enable"]["names"]
    assert calls["run"] == {"transport": "stdio", "show_banner": False}
