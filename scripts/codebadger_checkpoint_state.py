#!/usr/bin/env python3
"""Manage disposable, repository-local CodeBadger checkpoint pointers.

Polly is the single writer. After every requested CPG reports ready, it replaces
an analysis target's complete language-to-hash mapping. When source writes resume,
it invalidates that target before dispatching another graph consumer.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Mapping, Sequence

STATE_SCHEMA_VERSION = 1
DEFAULT_STATE_RELATIVE_PATH = Path(".codebadger") / "active-cpgs.json"
CODEBASE_HASH_PATTERN = re.compile(r"^[0-9a-f]{16}$")
LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_.+-]+$")


class CheckpointStateError(ValueError):
    """The active-checkpoint state or a requested transition is invalid."""


class InactiveTargetError(CheckpointStateError):
    """No active CPG mapping exists for the requested analysis target."""


def _validate_target_id(analysis_target_id: str) -> str:
    target = str(analysis_target_id).strip()
    if not target:
        raise CheckpointStateError("analysis_target_id must not be empty")
    if len(target) > 256 or any(ord(char) < 0x20 for char in target):
        raise CheckpointStateError("analysis_target_id is invalid")
    return target


def _validate_codebases(codebases: Mapping[str, str]) -> dict[str, str]:
    if not codebases:
        raise CheckpointStateError("at least one language=codebase_hash is required")

    normalized: dict[str, str] = {}
    for raw_language, raw_hash in codebases.items():
        language = str(raw_language).strip()
        codebase_hash = str(raw_hash).strip().lower()
        if not LANGUAGE_PATTERN.fullmatch(language):
            raise CheckpointStateError(f"invalid language identifier: {raw_language!r}")
        if not CODEBASE_HASH_PATTERN.fullmatch(codebase_hash):
            raise CheckpointStateError(
                f"invalid CodeBadger codebase hash for {language!r}: {raw_hash!r}"
            )
        if language in normalized:
            raise CheckpointStateError(f"duplicate language mapping: {language}")
        normalized[language] = codebase_hash
    return normalized


def _empty_state() -> dict:
    return {"schema_version": STATE_SCHEMA_VERSION, "targets": {}}


def find_repository_root(start: Path | None = None) -> Path:
    """Find the nearest Git root without invoking Git or changing process state."""
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise CheckpointStateError("could not find a Git repository root")


def resolve_state_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an explicit/env path, otherwise use the current repository root."""
    selected = explicit or os.getenv("CODEBADGER_ACTIVE_STATE")
    if selected:
        return Path(selected).expanduser().resolve()
    return find_repository_root() / DEFAULT_STATE_RELATIVE_PATH


def load_state(path: Path) -> dict:
    """Load and validate active state; a missing file means no active targets."""
    if not path.exists():
        return _empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointStateError(f"failed to read checkpoint state: {exc}") from exc

    if not isinstance(state, dict):
        raise CheckpointStateError("checkpoint state must be a JSON object")
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise CheckpointStateError(
            f"unsupported checkpoint-state schema: {state.get('schema_version')!r}"
        )
    targets = state.get("targets")
    if not isinstance(targets, dict):
        raise CheckpointStateError("checkpoint state targets must be an object")

    normalized_targets = {}
    for raw_target, entry in targets.items():
        target = _validate_target_id(raw_target)
        if not isinstance(entry, dict) or not isinstance(entry.get("codebases"), dict):
            raise CheckpointStateError(
                f"checkpoint target {target!r} must contain a codebases object"
            )
        normalized_targets[target] = {
            "codebases": _validate_codebases(entry["codebases"])
        }
    return {"schema_version": STATE_SCHEMA_VERSION, "targets": normalized_targets}


def save_state(path: Path, state: dict) -> None:
    """Atomically replace the active-state file in its destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def activate_target(
    path: Path, analysis_target_id: str, codebases: Mapping[str, str]
) -> dict:
    """Atomically replace one target's complete active language/hash mapping."""
    target = _validate_target_id(analysis_target_id)
    normalized = _validate_codebases(codebases)
    state = load_state(path)
    state["targets"][target] = {"codebases": normalized}
    save_state(path, state)
    return state["targets"][target]


def invalidate_target(path: Path, analysis_target_id: str) -> bool:
    """Remove a target's active mapping. Returns whether one existed."""
    target = _validate_target_id(analysis_target_id)
    state = load_state(path)
    existed = state["targets"].pop(target, None) is not None
    if existed:
        save_state(path, state)
    return existed


def invalidate_all(path: Path) -> int:
    """Remove every active target mapping and return the number removed."""
    state = load_state(path)
    removed = len(state["targets"])
    if removed:
        state["targets"] = {}
        save_state(path, state)
    return removed


def resolve_target(path: Path, analysis_target_id: str) -> dict:
    """Return a copy of an active mapping or fail instead of serving stale state."""
    target = _validate_target_id(analysis_target_id)
    entry = load_state(path)["targets"].get(target)
    if entry is None:
        raise InactiveTargetError(
            f"analysis target {target!r} has no active CodeBadger checkpoint"
        )
    return {"codebases": dict(entry["codebases"])}


def _parse_codebase_assignments(values: Sequence[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise CheckpointStateError(
                f"invalid --codebase value {value!r}; expected LANGUAGE=HASH"
            )
        language, codebase_hash = value.split("=", 1)
        language = language.strip()
        if language in parsed:
            raise CheckpointStateError(f"duplicate language mapping: {language}")
        parsed[language] = codebase_hash
    return _validate_codebases(parsed)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage active CodeBadger hashes at explicit source checkpoints."
    )
    parser.add_argument(
        "--state-file",
        help=(
            "State path (default: CODEBADGER_ACTIVE_STATE or "
            ".codebadger/active-cpgs.json at the Git root)."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    activate = commands.add_parser(
        "activate", help="Replace one target's complete active mapping."
    )
    activate.add_argument("analysis_target_id")
    activate.add_argument(
        "--codebase",
        action="append",
        required=True,
        metavar="LANGUAGE=HASH",
        help="Ready CPG mapping; repeat for a polyglot target.",
    )

    resolve = commands.add_parser("resolve", help="Read an active target mapping.")
    resolve.add_argument("analysis_target_id")
    resolve.add_argument("--language")

    invalidate = commands.add_parser(
        "invalidate", help="Invalidate a target before source writes resume."
    )
    invalidate.add_argument("analysis_target_id")

    commands.add_parser("invalidate-all", help="Invalidate every active target.")
    commands.add_parser("list", help="List all active target mappings.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        state_path = resolve_state_path(args.state_file)
        if args.command == "activate":
            entry = activate_target(
                state_path,
                args.analysis_target_id,
                _parse_codebase_assignments(args.codebase),
            )
            output = {
                "analysis_target_id": _validate_target_id(args.analysis_target_id),
                **entry,
            }
        elif args.command == "resolve":
            entry = resolve_target(state_path, args.analysis_target_id)
            if args.language:
                language = args.language.strip()
                try:
                    output = entry["codebases"][language]
                except KeyError as exc:
                    raise InactiveTargetError(
                        f"analysis target {args.analysis_target_id!r} has no active "
                        f"CodeBadger hash for language {language!r}"
                    ) from exc
            else:
                output = {
                    "analysis_target_id": _validate_target_id(args.analysis_target_id),
                    **entry,
                }
        elif args.command == "invalidate":
            output = {
                "analysis_target_id": _validate_target_id(args.analysis_target_id),
                "invalidated": invalidate_target(state_path, args.analysis_target_id),
            }
        elif args.command == "invalidate-all":
            output = {"invalidated_targets": invalidate_all(state_path)}
        else:
            output = load_state(state_path)["targets"]
    except CheckpointStateError as exc:
        print(f"codebadger-checkpoint: {exc}", file=sys.stderr)
        return 2

    if isinstance(output, str):
        print(output)
    else:
        print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
