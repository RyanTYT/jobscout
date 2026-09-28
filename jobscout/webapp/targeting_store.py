"""webapp/targeting_store.py — UI editor for the discovery targeting
(config/profile.yaml, target: block).

Edits are line-level textual patches (the profile_store convention): every
untouched key, its comments, and the file header survive. A save that would
break pydantic validation is rolled back and the error is shown. Every
successful save bumps profile_version — that's the LLM score-cache key, so
re-targeting re-scores active postings.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from jobscout.core import config as core_config

# tokens the rule gate can actually produce (rules._SENIORITY_PATTERNS)
LEVEL_TOKENS = ("junior", "senior", "staff", "lead",
                "principal", "manager", "intern")
REMOTE_PREFS = ("onsite", "hybrid", "remote")

# short semantic comment each key carries after a rewrite — the card shows
# the long explanation; this keeps the YAML self-documenting
_COMMENTS = {
    "roles": "rule gate keyword-matches titles/descriptions",
    "seniorities": "exclusion list — senior/lead/... titles fail; unlabeled pass",
    "locations": "substring match on posting location; remote passes",
    "primary_locations": "preferred first; rules treat both lists the same",
    "stack": "LLM rubric stack keywords",
}


class TargetingError(Exception):
    pass


def _profile_path() -> Path:
    return Path(core_config.config_dir()) / "profile.yaml"


def current():
    return core_config.load_profile().target


# ── form parsing ─────────────────────────────────────────────────────────────


def _split_csv(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[,\n]", raw or ""):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


# ── yaml rendering (inline lists, minimal quoting) ──────────────────────────


def _yaml_inline(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .\-+/&]*", s or ""):
        return s
    return '"' + (s or "").replace('"', '\\"') + '"'


def _render_list(items: list[str]) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(_yaml_inline(i) for i in items) + "]"


# ── line patching (comments preserved) ───────────────────────────────────────

def _target_key_index(lines: list[str], key: str) -> int | None:
    pat = re.compile(rf"^  {re.escape(key)}:")
    for i, line in enumerate(lines):
        if pat.match(line):
            return i
    return None


def _set_list_block(lines: list[str], key: str, items: list[str]) -> bool:
    """Replace `  key:` plus its multi-line continuation block (list items
    and deeper-indented comments) with one rendered inline-list line."""
    i = _target_key_index(lines, key)
    if i is None:
        return False
    end = i + 1
    while end < len(lines):
        nxt = lines[end]
        if not nxt.strip():
            break
        if re.match(r"^  \w[\w-]*:", nxt):      # next sibling key
            break
        if nxt[0] not in " \t":                  # column-0 comment/section
            break
        end += 1
    comment = f"   # {_COMMENTS[key]}" if items and key in _COMMENTS else ""
    lines[i:end] = [f"  {key}: {_render_list(items)}{comment}"]
    return True


def _set_remote(lines: list[str], *, allowed: bool, preference: str) -> bool:
    i = _target_key_index(lines, "remote")
    if i is None:
        return False
    patched = {"allowed": False, "preference": False}
    for j in range(i + 1, min(i + 12, len(lines))):
        s = lines[j]
        if re.match(r"^  \w[\w-]*:", s) or (s and s[0] not in " \t"):
            break
        if s.strip().startswith("allowed:"):
            lines[j] = re.sub(r"allowed:.*", f"allowed: {str(allowed).lower()}", s)
            patched["allowed"] = True
        elif s.strip().startswith("preference:"):
            lines[j] = re.sub(
                r"preference:.*", f'preference: "{preference}"', s)
            patched["preference"] = True
    return all(patched.values())


def _next_version(cur: str) -> str:
    today = date.today().isoformat()
    m = re.fullmatch(rf"{re.escape(today)}\.(\d+)", (cur or "").strip())
    n = int(m.group(1)) + 1 if m else 1
    return f"{today}.{n}"


def _bump_version(lines: list[str]) -> bool:
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)profile_version:\s*(\S+)(.*)$", line)
        if m and not line.lstrip().startswith("#"):
            lines[i] = (f"{m.group(1)}profile_version: "
                        f'"{_next_version(m.group(2).strip(chr(34)))}"'
                        f"{m.group(3)}")
            return True
    return False


# ── the save ─────────────────────────────────────────────────────────────────


def save(*, seniorities: list[str], primary_locations: str,
          other_locations: str, roles: str, stack: str,
          remote_preference: str, remote_allowed: bool) -> dict:
    """Patch profile.yaml from form values. Returns the new TargetCfg
    summary; raises TargetingError (file untouched) on anything invalid."""
    path = _profile_path()
    if not path.is_file():
        raise TargetingError(f"missing config file: {path}")
    if remote_preference not in REMOTE_PREFS:
        raise TargetingError(f"invalid remote preference: {remote_preference!r}")

    levels = [s for s in seniorities if s in LEVEL_TOKENS]
    primary = _split_csv(primary_locations)
    others = [loc for loc in _split_csv(other_locations) if loc not in primary]

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    if not all([
        _set_list_block(lines, "roles", _split_csv(roles)),
        _set_list_block(lines, "seniorities", levels),
        _set_list_block(lines, "locations", primary + others),
        _set_list_block(lines, "primary_locations", primary),
        _set_list_block(lines, "stack", _split_csv(stack)),
        _set_remote(lines, allowed=remote_allowed,
                    preference=remote_preference),
        _bump_version(lines),
    ]):
        raise TargetingError(
            "profile.yaml structure not recognised — the target: block "
            "should hold plain keys at two-space indent; edit by hand once "
            "and retry")

    new_text = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
    path.write_text(new_text, encoding="utf-8")
    try:
        profile = core_config.load_profile()
        t = profile.target
        if (t.seniorities != levels or t.primary_locations != primary
                or t.locations != primary + others):
            raise core_config.ConfigError("round-trip mismatch")
        return {"profile_version": profile.profile_version,
                "target": t}
    except Exception as e:                       # rollback — file restored
        path.write_text(original, encoding="utf-8")
        raise TargetingError(
            f"rejected by validation — file restored: {e}") from e
