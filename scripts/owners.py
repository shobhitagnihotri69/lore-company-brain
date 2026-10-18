"""Official code owners of kubernetes/kubernetes, read from its OWNERS files.

    from owners import owners_for_path
    owners_for_path("pkg/scheduler/framework")
    -> {"approvers": {"macsko": "pkg/scheduler/OWNERS", ..., "dims": "OWNERS"},
        "reviewers": {...},
        "source": "pkg/scheduler/OWNERS"}

`approvers` and `reviewers` map each login to the OWNERS file that lists it (use set(...) for
just the logins). `source` is the nearest OWNERS file. Works for file and folder paths.

Reads data/k8s-owners, a sparse clone that holds only OWNERS and OWNERS_ALIASES (see README).
Import it with scripts/ on sys.path; running `python scripts/<name>.py` does that.

Rules (the ones Prow, Kubernetes' merge bot, uses):
- Start at the nearest OWNERS file (the path's own folder, else the closest parent), then add
  every OWNERS file above it up to the repo root. Stop after a file with
  `options: no_parent_owners: true`. If a login is listed twice, the closest file wins.
- Each file's `approvers` and `reviewers` are merged with every list under its `filters:`.
  `emeritus_*` (former owners) and `required_reviewers` (GitHub teams) are ignored.
- Team names are expanded with OWNERS_ALIASES. Logins then go through logins.clean_login:
  lowercased, fixed with data/aliases.yaml, bots dropped.
"""
from functools import lru_cache
from pathlib import Path

import yaml

from logins import clean_login

OWNERS_DIR = Path(__file__).resolve().parent.parent / "data" / "k8s-owners"
TEAM_SUFFIXES = ("-approvers", "-reviewers", "-maintainers", "-leads")

# Team names used in OWNERS files but missing from OWNERS_ALIASES (e.g. 'sig-testing-leads').
# They are not people, so they are skipped; loaders can print this set.
unknown_teams = set()


@lru_cache(maxsize=None)
def _aliases():
    data = yaml.safe_load((OWNERS_DIR / "OWNERS_ALIASES").read_text(encoding="utf-8")) or {}
    return {str(k).lower(): [str(x) for x in (v or [])] for k, v in (data.get("aliases") or {}).items()}


def _people(names):
    """Expand team aliases, then clean each login. Returns logins in file order, no repeats."""
    out = []
    for name in names:
        name = str(name).strip().lower()
        if name in _aliases():
            members = _aliases()[name]
        elif name.endswith(TEAM_SUFFIXES):
            unknown_teams.add(name)
            continue
        else:
            members = [name]
        out += [login for login in map(clean_login, members) if login]
    return list(dict.fromkeys(out))


@lru_cache(maxsize=None)
def owners_files():
    """{folder: {"approvers": [...], "reviewers": [...], "stop": bool}} for every OWNERS file.
    The repo root is ''. `stop` means no_parent_owners."""
    if not (OWNERS_DIR / "OWNERS").exists():
        raise FileNotFoundError(f"No OWNERS files in {OWNERS_DIR}: run the sparse clone from the README.")
    files = {}
    for f in OWNERS_DIR.rglob("OWNERS"):
        rel = f.relative_to(OWNERS_DIR)
        if ".git" in rel.parts:
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        blocks = [data] + [b or {} for b in (data.get("filters") or {}).values()]
        entry = {role: _people([name for b in blocks for name in b.get(role) or []])
                 for role in ("approvers", "reviewers")}
        entry["stop"] = bool((data.get("options") or {}).get("no_parent_owners"))
        folder = rel.parent.as_posix()  # forward slashes on Windows too
        files["" if folder == "." else folder] = entry
    return files


def owners_for_path(path):
    """Official approvers and reviewers of a file or folder, e.g. 'pkg/scheduler/framework'."""
    files = owners_files()
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    result = {"approvers": {}, "reviewers": {}, "source": None}
    for depth in range(len(parts), -1, -1):  # the path itself, then each parent, then the root
        folder = "/".join(parts[:depth])
        if folder not in files:
            continue
        src = f"{folder}/OWNERS" if folder else "OWNERS"
        result["source"] = result["source"] or src
        for role in ("approvers", "reviewers"):
            for login in files[folder][role]:
                result[role].setdefault(login, src)
        if files[folder]["stop"]:
            break
    return result
