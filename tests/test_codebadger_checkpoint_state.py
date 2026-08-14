from __future__ import annotations

import json

import pytest

from scripts import codebadger_checkpoint_state as checkpoint

HASH_C = "0123456789abcdef"
HASH_PYTHON = "fedcba9876543210"


def test_activate_and_resolve_polyglot_target(tmp_path):
    state_path = tmp_path / "active.json"

    activated = checkpoint.activate_target(
        state_path,
        "post-implementation-review",
        {"c": HASH_C, "python": HASH_PYTHON},
    )

    assert activated == {"codebases": {"c": HASH_C, "python": HASH_PYTHON}}
    assert (
        checkpoint.resolve_target(state_path, "post-implementation-review") == activated
    )
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk == {
        "schema_version": 1,
        "targets": {"post-implementation-review": activated},
    }


def test_activate_replaces_complete_target_mapping(tmp_path):
    state_path = tmp_path / "active.json"
    checkpoint.activate_target(state_path, "audit", {"c": HASH_C})

    checkpoint.activate_target(state_path, "audit", {"python": HASH_PYTHON})

    assert checkpoint.resolve_target(state_path, "audit") == {
        "codebases": {"python": HASH_PYTHON}
    }


def test_invalidate_removes_mapping_and_resolve_fails_closed(tmp_path):
    state_path = tmp_path / "active.json"
    checkpoint.activate_target(state_path, "audit", {"c": HASH_C})

    assert checkpoint.invalidate_target(state_path, "audit") is True
    assert checkpoint.invalidate_target(state_path, "audit") is False
    with pytest.raises(checkpoint.InactiveTargetError, match="no active"):
        checkpoint.resolve_target(state_path, "audit")


def test_invalidate_all_clears_every_target(tmp_path):
    state_path = tmp_path / "active.json"
    checkpoint.activate_target(state_path, "audit", {"c": HASH_C})
    checkpoint.activate_target(state_path, "review", {"python": HASH_PYTHON})

    assert checkpoint.invalidate_all(state_path) == 2
    assert checkpoint.load_state(state_path)["targets"] == {}


@pytest.mark.parametrize("bad_hash", ["", "abc", "g" * 16, "a" * 17])
def test_invalid_hash_never_writes_state(tmp_path, bad_hash):
    state_path = tmp_path / "active.json"
    with pytest.raises(checkpoint.CheckpointStateError, match="invalid CodeBadger"):
        checkpoint.activate_target(state_path, "audit", {"c": bad_hash})
    assert not state_path.exists()


def test_corrupt_or_unknown_state_is_not_silently_replaced(tmp_path):
    state_path = tmp_path / "active.json"
    state_path.write_text('{"schema_version": 99, "targets": {}}', encoding="utf-8")

    with pytest.raises(checkpoint.CheckpointStateError, match="unsupported"):
        checkpoint.activate_target(state_path, "audit", {"c": HASH_C})

    assert json.loads(state_path.read_text(encoding="utf-8"))["schema_version"] == 99


def test_cli_resolves_one_language_hash(tmp_path, capsys):
    state_path = tmp_path / "active.json"
    assert (
        checkpoint.main(
            [
                "--state-file",
                str(state_path),
                "activate",
                "review",
                "--codebase",
                f"c={HASH_C}",
                "--codebase",
                f"python={HASH_PYTHON}",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        checkpoint.main(
            [
                "--state-file",
                str(state_path),
                "resolve",
                "review",
                "--language",
                "python",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == HASH_PYTHON
