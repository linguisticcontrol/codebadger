"""Expose the running CodeBadger HTTP server as an MCP STDIO server.

This module runs inside a disposable, least-privilege container created by
start_codebadger_mcp.ps1. FastMCP owns both protocol transports so the bridge
preserves MCP session, cancellation, and concurrent-request semantics.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastmcp.server import create_proxy

DEFAULT_UPSTREAM_URL = "http://codebadger-mcp:4242/mcp"
DEFAULT_TOOL_PROFILE = "controller"

# An allowlist, not a denylist: a newly-added backend tool is unavailable to
# Audit/review clients until it is deliberately classified. Lifecycle mutation
# remains with the controller (Polly), while status and graph queries are exposed.
QUERY_ONLY_TOOLS = frozenset(
    {
        "get_cpg_status",
        "get_backend_status",
        "list_methods",
        "list_calls",
        "get_call_graph",
        "list_parameters",
        "run_cpgql_query",
        "find_bounds_checks",
        "get_cpgql_syntax_help",
        "get_cfg",
        "get_type_definition",
        "find_taint_sources",
        "find_taint_sinks",
        "find_taint_flows",
        "get_program_slice",
        "get_variable_flow",
        "find_use_after_free",
        "find_double_free",
        "find_null_pointer_deref",
        "find_integer_overflow",
        "find_format_string_vulns",
        "find_heap_overflow",
        "find_stack_overflow",
        "find_toctou",
        "find_uninitialized_reads",
        "find_command_injection_sinks",
    }
)
TOOL_PROFILES = frozenset({"controller", "query-only"})


def get_upstream_url() -> str:
    """Return a validated HTTP(S) MCP endpoint from the bridge environment."""
    url = os.getenv("CODEBADGER_UPSTREAM_URL", "").strip() or DEFAULT_UPSTREAM_URL
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(
            "CODEBADGER_UPSTREAM_URL must be an absolute http:// or https:// URL"
        )
    return url


def get_tool_profile() -> str:
    """Return the validated bridge-side tool visibility profile."""
    profile = (
        os.getenv("CODEBADGER_TOOL_PROFILE", "").strip().lower() or DEFAULT_TOOL_PROFILE
    )
    if profile not in TOOL_PROFILES:
        choices = ", ".join(sorted(TOOL_PROFILES))
        raise ValueError(f"CODEBADGER_TOOL_PROFILE must be one of: {choices}")
    return profile


def apply_tool_profile(proxy, profile: str) -> None:
    """Apply transport-neutral tool visibility before serving the client."""
    if profile == "query-only":
        proxy.enable(
            names=set(QUERY_ONLY_TOOLS),
            components={"tool"},
            only=True,
        )


def main() -> None:
    """Run the FastMCP proxy over stdin/stdout until the client disconnects."""
    proxy = create_proxy(get_upstream_url(), name="CodeBadger")
    apply_tool_profile(proxy, get_tool_profile())
    proxy.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
