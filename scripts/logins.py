"""The one rule every loader uses to turn a GitHub login into our Person key."""
from pathlib import Path

import yaml

ALIAS_FILE = Path(__file__).resolve().parent.parent / "data" / "aliases.yaml"
ROBOTS = {"k8s-ci-robot", "k8s-triage-robot", "k8s-infra-cherrypick-robot"}
# Wrong spelling -> right login, checked by a person (see data/aliases.yaml).
ALIASES = {str(k).lower(): str(v).lower()
           for k, v in (yaml.safe_load(ALIAS_FILE.read_text(encoding="utf-8")) or {}).items()}


def is_bot(login):
    return login.endswith("bot") or login.endswith("[bot]") or login in ROBOTS


def clean_login(raw):
    """'Liggit' -> 'liggitt'. Returns None for empty logins and bots."""
    if not raw:
        return None
    login = str(raw).strip().lower()
    login = ALIASES.get(login, login)
    return None if is_bot(login) else login
