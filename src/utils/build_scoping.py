"""include_globs scoping → c2cpg/frontend `--exclude-regex` construction.

Scoping a large repo by re-rooting `source_path` at a subdirectory silently
drops cross-directory header/macro resolution. Instead we keep the repo root as
the parse base and use the universally-supported `--exclude-regex` to drop the
SOURCE translation units that fall outside the requested globs — while leaving
headers (and in-scope sources) fully resolvable.

Frontends may match `--exclude-regex` against either a repository-relative path
or the absolute path below their input directory. Generated patterns therefore
accept only those two exact forms and use full-match semantics.

Tradeoff (documented for callers): a call from an in-scope file into a function
defined in an out-of-scope file still resolves the *name*, but that callee's
body won't be in the CPG (its TU wasn't compiled). Scope widely enough to cover
the call targets you care about.
"""

import re
from typing import List, Optional

from ..exceptions import ValidationError


def normalize_path_globs(globs: Optional[List[str]], kind: str) -> Optional[List[str]]:
    """Validate and canonicalize repo-relative POSIX glob sets."""
    if globs is None:
        return None
    normalized = set()
    for raw in globs:
        value = str(raw).strip()
        if not value:
            continue
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValidationError(f"Invalid {kind}: control characters not allowed")
        if "\\" in value:
            raise ValidationError(f"Invalid {kind}: use '/' as the path separator")
        if value.startswith("./"):
            value = value[2:]
        if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
            raise ValidationError(f"Invalid {kind}: value must be relative to the source root")
        segments = value.rstrip("/").split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise ValidationError(f"Invalid {kind}: '.', '..', and empty path segments are not allowed")
        if "***" in value:
            raise ValidationError(f"Invalid {kind}: malformed '**' wildcard")
        normalized.add(value)
    return sorted(normalized)


def glob_to_path_regex(glob: str) -> str:
    """Translate a path glob to a full-match regex against a relative path.

    Supported: ``**`` (any chars incl. ``/``), ``*`` (any non-``/``), ``?`` (one
    non-``/``). A pattern with no wildcard and no ``.`` is treated as a directory
    prefix (keeps everything beneath it). A trailing ``/`` is also a directory.
    All other characters are regex-escaped.
    """
    g = glob.strip()
    if g.startswith("./"):
        g = g[2:]
    if not g:
        return ""

    is_dir_prefix = g.endswith("/") or ("*" not in g and "?" not in g and "." not in g)
    g = g.rstrip("/")

    # Tokenize so we can escape literals but translate wildcards. Order matters:
    # match ** before *.
    out = []
    i = 0
    while i < len(g):
        if g.startswith("**/", i):
            # zero-or-more leading directories (so **/*.c also matches root files)
            out.append("(.*/)?")
            i += 3
        elif g.startswith("**", i):
            out.append(".*")
            i += 2
        elif g[i] == "*":
            out.append("[^/]*")
            i += 1
        elif g[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(g[i]))
            i += 1
    regex = "".join(out)
    if is_dir_prefix:
        # Keep the directory's whole subtree.
        regex = regex + "/.*"
    return regex


def path_matches_globs(path: str, globs: List[str], *, is_dir: bool = False) -> bool:
    """Return whether a repo-relative POSIX path matches a normalized glob."""
    relative = str(path).replace("\\", "/")
    if relative.startswith("./"):
        relative = relative[2:]
    candidates = [relative, relative.rstrip("/") + "/"] if is_dir else [relative]
    return any(
        re.fullmatch(regex, candidate) is not None
        for glob in globs
        if (regex := glob_to_path_regex(glob))
        for candidate in candidates
    )


def _frontend_path_forms(
    path_regexes: List[str], frontend_input_root: Optional[str]
) -> str:
    """Match repo-relative paths and paths below one exact frontend input root."""
    relative = "|".join(f"(?:{regex})" for regex in path_regexes)
    root = str(frontend_input_root or "").rstrip("/")
    if not root:
        return relative
    return f"(?:{relative}|{re.escape(root)}/(?:{relative}))"


def scope_exclude_regex(
    include_globs: List[str],
    source_exts: List[str],
    frontend_input_root: Optional[str] = None,
) -> Optional[str]:
    """Build an `--exclude-regex` that drops out-of-scope SOURCE files.

    A path is excluded iff it (a) ends in one of `source_exts` AND (b) does not
    match any of `include_globs`. Header files (any extension not in
    `source_exts`) are never excluded, so #include resolution still works.

    Returns None if there are no usable globs (caller should then not scope).
    """
    keeps = [r for r in (glob_to_path_regex(g) for g in include_globs) if r]
    if not keeps:
        return None
    exts = [re.escape(e.lstrip(".")) for e in source_exts if e and e.strip()]
    if not exts:
        return None
    ext_alt = "|".join(exts)
    keep_forms = _frontend_path_forms(keeps, frontend_input_root)
    # Full-match (.matches) anchored: must be a source file (positive lookahead)
    # AND not in scope (negative lookahead), then consume the whole path.
    return f"^(?=.*\\.(?:{ext_alt})$)(?!(?:{keep_forms})$).*$"


def exclude_globs_regex(
    exclude_globs: List[str],
    source_exts: List[str],
    frontend_input_root: Optional[str] = None,
) -> Optional[str]:
    """Exclude matching source translation units while retaining support files."""
    drops = [r for r in (glob_to_path_regex(g) for g in exclude_globs) if r]
    exts = [re.escape(e.lstrip(".")) for e in source_exts if e and e.strip()]
    if not drops or not exts:
        return None
    drop_forms = _frontend_path_forms(drops, frontend_input_root)
    return f"^(?=.*\\.(?:{'|'.join(exts)})$)(?:{drop_forms})$"


def combine_exclude_regexes(parts: List[Optional[str]]) -> Optional[str]:
    """OR several full-match exclude-regex alternatives into one.

    A file is excluded if it matches ANY part (e.g. it is default-junk OR is
    out of scope). Returns None when there is nothing to exclude.
    """
    usable = [p for p in parts if p]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    return "|".join(f"(?:{p})" for p in usable)
