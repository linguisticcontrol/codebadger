"""Tests for versioned CPG cache identity and content keying.

Covers the cache-key changes:
  - github/gitlab `branch` is part of the key (two branches must not collide)
  - every effective graph-shaping option is part of the key
  - local sources key on a content fingerprint (dedupe across paths; rebuild on change)
  - local sources fail closed when no fingerprint is available
"""

from types import SimpleNamespace

import pytest

import src.tools.core_tools as core_tools
from src.exceptions import ValidationError
from src.tools.core_tools import _get_cpg_build_spec, get_cpg_cache_key

GH = "https://github.com/owner/repo"
GL = "https://gitlab.com/group/sub/repo"


def test_github_branch_changes_key():
    """Two branches of the same repo must produce distinct CPG hashes."""
    default = get_cpg_cache_key("github", GH, "c")
    main = get_cpg_cache_key("github", GH, "c", branch="main")
    dev = get_cpg_cache_key("github", GH, "c", branch="dev")
    assert default != main, "explicit branch must differ from default"
    assert main != dev, "different branches must not collide on one CPG"


def test_github_default_branch_is_stable():
    """No branch given -> stable key (back-compat with existing default-branch CPGs)."""
    assert get_cpg_cache_key("github", GH, "c") == get_cpg_cache_key("github", GH, "c")


def test_gitlab_branch_changes_key():
    """Branch keying also applies to gitlab URLs (same source_type='github')."""
    a = get_cpg_cache_key("github", GL, "c", branch="v2")
    b = get_cpg_cache_key("github", GL, "c", branch="v3")
    assert a != b


def test_local_ignores_branch():
    """branch is a remote-revision selector; it must not affect local source keys."""
    base = get_cpg_cache_key("local", "/src", "c", content="abc")
    with_branch = get_cpg_cache_key("local", "/src", "c", content="abc", branch="dev")
    assert base == with_branch


def test_extra_build_options_change_key():
    """include paths / defines (extra) produce a distinct CPG."""
    plain = get_cpg_cache_key("local", "/src", "c", content="abc")
    with_inc = get_cpg_cache_key("local", "/src", "c", content="abc", extra="inc=include,_build")
    with_def = get_cpg_cache_key("local", "/src", "c", content="abc", extra="def=LIBXML_CATALOG_ENABLED")
    assert plain != with_inc
    assert with_inc != with_def


def test_local_content_dedupes_across_paths():
    """Identical content at different paths => same key (dedupe)."""
    a = get_cpg_cache_key("local", "/path/one", "c", content="IDENTICAL")
    b = get_cpg_cache_key("local", "/path/two", "c", content="IDENTICAL")
    assert a == b


def test_local_content_change_rebuilds():
    """Changed content => new key (no stale-CPG reuse)."""
    a = get_cpg_cache_key("local", "/src", "c", content="v1")
    b = get_cpg_cache_key("local", "/src", "c", content="v2")
    assert a != b


def test_local_without_content_fails_closed():
    """A path alone can never authorize reuse of a local cached CPG."""
    with pytest.raises(ValidationError, match="verified source fingerprint"):
        get_cpg_cache_key("local", "/path/one", "c")


def test_commit_hash_changes_key():
    a = get_cpg_cache_key("github", GH, "c")
    b = get_cpg_cache_key("github", GH, "c", commit_hash="deadbeef")
    assert a != b


def test_snippet_keys_on_content_not_label():
    """Snippets dedupe on code content regardless of the label/source_path."""
    a = get_cpg_cache_key("snippet", "label-a", "c", content="int main(){}")
    b = get_cpg_cache_key("snippet", "label-b", "c", content="int main(){}")
    c = get_cpg_cache_key("snippet", "label-a", "c", content="int other(){}")
    assert a == b
    assert a != c


def test_key_is_16_hex_chars():
    k = get_cpg_cache_key("github", GH, "c", branch="x")
    assert len(k) == 16
    int(k, 16)  # raises if not hex


def _config(*, autodetect=False, patterns=None, languages=None):
    return SimpleNamespace(
        cpg=SimpleNamespace(
            autodetect_compile_db=autodetect,
            exclusion_patterns=list(patterns or []),
            languages_with_exclusions=list(languages or []),
        )
    )


def _local_key(build_spec):
    return get_cpg_cache_key(
        "local",
        "/src",
        "c",
        content="source-fingerprint",
        build_spec=build_spec,
    )


def test_cache_format_epoch_invalidates_old_namespace(monkeypatch):
    before = get_cpg_cache_key("local", "/src", "c", content="same")
    monkeypatch.setattr(
        core_tools, "CPG_CACHE_FORMAT_VERSION", "cpg-cache-v-next"
    )
    after = get_cpg_cache_key("local", "/src", "c", content="same")
    assert before != after


@pytest.mark.parametrize(
    "changed",
    [
        {"include_paths": ["include"]},
        {"defines": ["FEATURE=1"]},
        {"include_globs": ["src/**"]},
        {"auto_system_headers": True},
        {"compile_commands": "build/compile_commands.json"},
    ],
)
def test_each_caller_graph_option_changes_cache_identity(changed):
    config = _config(autodetect=False)
    base = _get_cpg_build_spec("c", config=config)
    variant = _get_cpg_build_spec("c", config=config, **changed)
    assert _local_key(base) != _local_key(variant)


def test_compile_database_autodetection_behavior_changes_identity():
    disabled = _get_cpg_build_spec("c", config=_config(autodetect=False))
    enabled = _get_cpg_build_spec("c", config=_config(autodetect=True))
    assert disabled["compile_database"] == {"mode": "none", "path": None}
    assert enabled["compile_database"] == {"mode": "auto", "path": None}
    assert _local_key(disabled) != _local_key(enabled)


def test_effective_exclusions_change_cache_identity():
    disabled = _get_cpg_build_spec(
        "c", config=_config(patterns=[r"tests/.*"], languages=[])
    )
    enabled = _get_cpg_build_spec(
        "c", config=_config(patterns=[r"tests/.*"], languages=["c"])
    )
    changed = _get_cpg_build_spec(
        "c", config=_config(patterns=[r"vendor/.*"], languages=["c"])
    )

    assert disabled["exclusions"] == {"enabled": False, "patterns": []}
    assert _local_key(disabled) != _local_key(enabled)
    assert _local_key(enabled) != _local_key(changed)


def test_order_sensitive_build_options_preserve_order_in_identity():
    config = _config(autodetect=False)
    first = _get_cpg_build_spec(
        "c", config=config, include_paths=["first", "second"]
    )
    second = _get_cpg_build_spec(
        "c", config=config, include_paths=["second", "first"]
    )
    assert first["include_paths"] == ["first", "second"]
    assert _local_key(first) != _local_key(second)


def test_source_view_policy_changes_cache_identity():
    config = _config(
        autodetect=False,
        patterns=[r"(?:^|.*/)tool.*/.*"],
        languages=["c"],
    )
    inherited = _get_cpg_build_spec(
        "c", config=config, exclude_globs=None, ignore_globs=[]
    )
    no_defaults = _get_cpg_build_spec(
        "c", config=config, exclude_globs=[], ignore_globs=[]
    )
    ignored_worktree = _get_cpg_build_spec(
        "c", config=config, exclude_globs=[], ignore_globs=[".work/**"]
    )
    assert inherited["exclude_globs"] is None
    assert no_defaults["exclude_globs"] == []
    assert len({
        _local_key(inherited),
        _local_key(no_defaults),
        _local_key(ignored_worktree),
    }) == 3
