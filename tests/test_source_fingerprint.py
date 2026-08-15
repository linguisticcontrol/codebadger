"""Tests for _hash_tree_in_process — the content fingerprint behind local CPG cache keying.

Properties that matter for the cache key: deterministic, content-sensitive, path-prefix
independent (so identical trees dedupe), file-order independent, and .git is excluded."""

import os
from unittest.mock import MagicMock

import src.tools.core_tools as core_tools
from src.tools.core_tools import _copy_local_source_tree, _hash_tree_in_process


def _write(path, data=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(data)


def test_deterministic(tmp_path):
    _write(str(tmp_path / "a.c"), "int a;")
    _write(str(tmp_path / "sub" / "b.c"), "int b;")
    assert _hash_tree_in_process(str(tmp_path)) == _hash_tree_in_process(str(tmp_path))


def test_content_change_changes_hash(tmp_path):
    f = tmp_path / "a.c"
    _write(str(f), "int a;")
    before = _hash_tree_in_process(str(tmp_path))
    _write(str(f), "int a; int extra;")
    assert _hash_tree_in_process(str(tmp_path)) != before


def test_identical_trees_at_different_roots_match(tmp_path):
    """Path-prefix independent => identical content dedupes regardless of location."""
    r1, r2 = tmp_path / "one", tmp_path / "two"
    for r in (r1, r2):
        _write(str(r / "a.c"), "int a;")
        _write(str(r / "inc" / "h.h"), "#define X 1")
    assert _hash_tree_in_process(str(r1)) == _hash_tree_in_process(str(r2))


def test_git_metadata_absent_from_fingerprint_and_in_process_snapshot(tmp_path):
    source, snapshot = tmp_path / "source", tmp_path / "snapshot"
    _write(str(source / "a.c"), "int a;")
    _write(str(source / "submodule" / "dep.c"), "int dep;")
    before = _hash_tree_in_process(str(source))

    _write(str(source / ".git" / "HEAD"), "ref: refs/heads/main")
    _write(str(source / ".git" / "objects" / "pack" / "x.pack"), "binary-ish")
    _write(str(source / "vendor" / "nested" / ".git" / "HEAD"), "nested")
    _write(str(source / "submodule" / ".git"), "gitdir: ../.git/modules/submodule")
    _copy_local_source_tree(str(source), str(snapshot))

    assert _hash_tree_in_process(str(source)) == before
    assert (snapshot / "a.c").exists()
    assert (snapshot / "submodule" / "dep.c").exists()
    assert not any(path.name == ".git" for path in snapshot.rglob(".git"))


def test_daemon_snapshot_and_fingerprint_share_git_pruning(monkeypatch):
    client = MagicMock()
    client.containers.get.return_value.image.id = "joern-image"
    client.containers.run.return_value = b"f" * 64
    monkeypatch.setenv("JOERN_PLAYGROUND_HOST_PATH", "/host/playground")
    monkeypatch.setattr(core_tools.docker, "from_env", lambda: client)

    core_tools._copy_local_source_tree_via_daemon("/repo/source", "a" * 16)
    copy_script = client.containers.run.call_args.kwargs["entrypoint"][2]
    client.containers.run.reset_mock()
    core_tools._copy_local_source_tree_via_daemon(
        "/repo/source", "b" * 16, ignore_globs=[".work/**"]
    )
    filtered_copy_script = client.containers.run.call_args.kwargs["entrypoint"][2]
    client.containers.run.reset_mock()
    core_tools._fingerprint_local_source_via_daemon("/repo/source")
    fingerprint_script = client.containers.run.call_args.kwargs["entrypoint"][2]

    assert "find . -mindepth 1 -name .git -prune" in copy_script
    assert "find . -mindepth 1 -name .git -prune" in filtered_copy_script
    assert "is_ignored" in filtered_copy_script
    assert "find . -name .git -prune" in fingerprint_script
    assert "cp -a" not in copy_script


def test_new_file_changes_hash(tmp_path):
    _write(str(tmp_path / "a.c"), "int a;")
    before = _hash_tree_in_process(str(tmp_path))
    _write(str(tmp_path / "b.c"), "int b;")
    assert _hash_tree_in_process(str(tmp_path)) != before


def test_returns_hex_digest(tmp_path):
    _write(str(tmp_path / "a.c"), "x")
    h = _hash_tree_in_process(str(tmp_path))
    assert isinstance(h, str) and len(h) == 64
    int(h, 16)


def test_ignored_content_is_absent_from_fingerprint_and_snapshot(tmp_path):
    source, snapshot = tmp_path / "source", tmp_path / "snapshot"
    _write(str(source / "src" / "main.c"), "int main(void) { return 0; }")
    ignored = source / ".work" / "cache.bin"
    _write(str(ignored), "first")

    ignore_globs = [".work/**"]
    before = _hash_tree_in_process(str(source), ignore_globs)
    _copy_local_source_tree(str(source), str(snapshot), ignore_globs)
    _write(str(ignored), "second")

    assert _hash_tree_in_process(str(source), ignore_globs) == before
    assert (snapshot / "src" / "main.c").exists()
    assert not (snapshot / ".work").exists()
