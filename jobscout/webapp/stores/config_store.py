"""webapp/config_store.py — the ONE engine behind every config editor.

Every webapp store (profile, targeting, key, models, settings, watchlist)
edits a user-editable text file (yaml or .env) with the same contract:

  1. comment-preserving line patches — untouched keys, inline comments,
     and file headers survive every save;
  2. validate-with-rollback — the mutated file is re-parsed (pydantic or
     domain check); anything invalid restores the original bytes and the
     store's specific error carries the reason;
  3. structure errors are loud, not silent.

File-specific recipes (which keys, which blocks, entry moves) stay in
their stores; the primitives live here.
"""

from __future__ import annotations

import re
from pathlib import Path


class StoreError(Exception):
    """Base for every store's error type (subclassed for API clarity)."""


# ── inline YAML value rendering ─────────────────────────────────────────────


def yaml_inline(value: str) -> str:
    """Render a scalar for an inline list/assignment; quote only when the
    plain form would parse as something else."""
    if value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .\-+/&;,'()]*",
                              value or ""):
        return value
    return '"' + (value or "").replace('"', '\\"') + '"'


def render_list(items: list[str]) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(yaml_inline(i) for i in items) + "]"


def quoted(value: str) -> str:
    return '"' + (value or "").replace('"', '\\"') + '"'


# ── comment-preserving line patches ─────────────────────────────────────────


def set_key(lines: list[str], key: str, value: str, *, indent: str = "  ",
            start: int = 0, end: int | None = None) -> bool:
    """Replace `key: <anything>` with `key: <value>`, keeping the line's
    trailing comment. Bounded to [start, end) so nested keys patch only
    inside their block. Returns False when the key isn't there."""
    limit = end if end is not None else len(lines)
    pat = re.compile(
        rf"^{re.escape(indent)}{re.escape(key)}:(\s*[^\n#]*)(\s+#.*)?$")
    for i in range(start, min(limit, len(lines))):
        m = pat.match(lines[i])
        if m:
            comment = m.group(2) or ""
            lines[i] = f"{indent}{key}: {value}{comment}"
            return True
    return False


def block_end(lines: list[str], start: int, *,
              child_indent: str = "    ") -> int:
    """Index where the block starting at `start` (a key line) ends — the
    next line that is shallower than the block's children."""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(child_indent) and line[0] not in " \t":
            end = j
            break
        if line.startswith(child_indent) is False and line.startswith(" "):
            end = j
            break
    return end


def find_key(lines: list[str], key: str, *, indent: str = "") -> int | None:
    """Index of the `key:` line at exactly `indent` depth (first match)."""
    pat = re.compile(rf"^{re.escape(indent)}{re.escape(key)}:\s*(\[\])?\s*$")
    for i, line in enumerate(lines):
        if pat.match(line):
            return i
    return None


def set_list_block(lines: list[str], key: str, items: list[str], *,
                   indent: str = "  ", comment: str = "") -> bool:
    """Replace `key:` plus its whole continuation block (list items and
    deeper-indented comments) with one inline-list line. The key line may
    already hold an inline list + comment (lenient match)."""
    pat = re.compile(rf"^{re.escape(indent)}{re.escape(key)}:")
    i = next((k for k, line in enumerate(lines) if pat.match(line)), None)
    if i is None:
        return False
    end = i + 1
    while end < len(lines):
        nxt = lines[end]
        if not nxt.strip():
            break
        if re.match(rf"^{re.escape(indent)}\w[\w-]*:", nxt):
            break
        if nxt[0] not in " \t":
            break
        end += 1
    c = f"   # {comment}" if (items and comment) else ""
    lines[i:end] = [f"{indent}{key}: {render_list(items)}{c}"]
    return True


def append_section(lines: list[str], section_lines: list[str]) -> None:
    """Append a top-level block (keeping a blank separator)."""
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(section_lines)


# ── validate-with-rollback save ─────────────────────────────────────────────


def commit(path: Path, mutate, validate, *, error_cls=StoreError,
            structure_msg: str = "file structure not recognised"):
    """The save contract: mutate(lines) patches the file in place;
    validate() re-reads (or parses) the result and returns a payload or
    raises. On any failure the original bytes are restored and
    error_cls carries the reason. Structure failures (a patch returning
    False / raising StructureError) never write at all."""
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = original.splitlines()
    try:
        mutate(lines)
    except StructureError as e:
        raise error_cls(f"{structure_msg}: {e}") from None
    new_text = "\n".join(lines) + ("\n" if original.endswith("\n")
                                   or not original else "")
    if new_text == original + ("\n" if (original and not
                                        original.endswith("\n")) else ""):
        new_text = "\n".join(lines) + ("\n" if original.endswith("\n")
                                       else "")
    path.write_text(new_text, encoding="utf-8")
    try:
        return validate()
    except Exception as e:                       # noqa: BLE001 — rollback path
        path.write_text(original, encoding="utf-8")
        raise error_cls(
            f"rejected by validation — file restored: {e}") from e


class StructureError(Exception):
    """Raised by mutate() when the file doesn't match the expected shape."""


def require(patched: bool, what: str) -> None:
    """Turn a False return from a primitive into a loud structure error."""
    if not patched:
        raise StructureError(what)


# ── .env primitives ─────────────────────────────────────────────────────────


def env_upsert(path: Path, env_name: str, value: str) -> None:
    lines = (path.read_text(encoding="utf-8").splitlines()
             if path.is_file() else [])
    pat = re.compile(rf"^{re.escape(env_name)}\s*=", re.I)
    if any(pat.match(line.strip()) for line in lines if line.strip()):
        lines = [
            f"{env_name}={value}" if pat.match(line.strip()) else line
            for line in lines
        ]
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{env_name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def env_drop(path: Path, env_name: str) -> None:
    if not path.is_file():
        return
    pat = re.compile(rf"^{re.escape(env_name)}\s*=", re.I)
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not pat.match(line.strip())
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
