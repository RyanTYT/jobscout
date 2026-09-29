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
from jobscout.core import paths as core_paths
from jobscout.webapp import config_store as cs

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
    return Path(core_paths.config_dir()) / "profile.yaml"


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


# ── line patching: thin recipes over config_store ─────────────────────────

def _set_list_block(lines: list[str], key: str, items: list[str]) -> bool:
    return cs.set_list_block(lines, key, items, indent="  ",
                             comment=_COMMENTS.get(key, ""))


def _set_remote(lines: list[str], *, allowed: bool, preference: str) -> bool:
    i = cs.find_key(lines, "remote", indent="  ")
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

    def _mutate(lines):
        for key, items in (
                ("roles", _split_csv(roles)),
                ("seniorities", levels),
                ("locations", primary + others),
                ("primary_locations", primary),
                ("stack", _split_csv(stack)),
        ):
            cs.require(_set_list_block(lines, key, items),
                       f"target.{key}")
        cs.require(_set_remote(lines, allowed=remote_allowed,
                               preference=remote_preference), "target.remote")
        cs.require(_bump_version(lines), "profile_version")

    def _validate():
        profile = core_config.load_profile()
        t = profile.target
        if (t.seniorities != levels or t.primary_locations != primary
                or t.locations != primary + others):
            raise core_config.ConfigError("round-trip mismatch")
        return {"profile_version": profile.profile_version, "target": t}

    return cs.commit(path, _mutate, _validate, error_cls=TargetingError,
                     structure_msg="profile.yaml structure not recognised "
                     "— the target: block should hold plain keys at "
                     "two-space indent; edit by hand once and retry")
