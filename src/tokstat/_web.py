"""Local cache helpers for imported claude.ai / chatgpt.com data exports.

The web tools no longer scrape provider endpoints — the cookie-based
approach was too fragile (Anthropic / OpenAI rate-limit at ~30 s per
request, anti-bot checks reject urllib, ToS gray area). They now read
exclusively from the local cache populated by `--import`.

Conversation payloads live under `~/.cache/tokstat/web/<service>/`,
keyed by `<account>__<conv_id>.json` so a single user can import
multiple accounts side-by-side (perso + work, …).

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
from pathlib import Path


_CACHE_BASE = Path.home() / ".cache" / "tokstat" / "web"


# ─── Conversation cache ──────────────────────────────────────────────────────

def cache_dir(service: str) -> Path:
    p = _CACHE_BASE / service
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_load(service: str, conv_id: str) -> dict | None:
    p = cache_dir(service) / f"{conv_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def cache_save(service: str, conv_id: str, data: dict) -> None:
    p = cache_dir(service) / f"{conv_id}.json"
    p.write_text(json.dumps(data))


def cache_iter(service: str):
    """Yield cached conversation payloads. Only files named
    `<account>__<id>.json` are emitted — bare-uuid files from the
    pre-multi-account scraper era are ignored to avoid double-counting
    conversations that have since been re-imported under an account."""
    d = _CACHE_BASE / service
    if not d.exists():
        return
    for f in d.glob("*.json"):
        if "__" not in f.stem:
            continue
        try:
            data = json.loads(f.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if not data.get("_account"):
            data["_account"] = f.stem.split("__", 1)[0]
        yield data


def imported_accounts(service: str) -> list[str]:
    """Return the distinct account names present in the on-disk cache."""
    d = _CACHE_BASE / service
    if not d.exists():
        return []
    seen = set()
    for f in d.glob("*.json"):
        if "__" in f.stem:
            seen.add(f.stem.split("__", 1)[0])
    return sorted(seen)


def cache_orphans(service: str) -> list[Path]:
    """Return bare-uuid cache files (no `<account>__` prefix). These are
    leftovers from older versions that wrote conversations without
    account namespacing — they're invisible to `cache_iter`."""
    d = _CACHE_BASE / service
    if not d.exists():
        return []
    return [f for f in d.glob("*.json") if "__" not in f.stem]


def clean_orphans(service: str) -> int:
    """Delete every orphaned (un-namespaced) cache file. Returns the count."""
    orphans = cache_orphans(service)
    for f in orphans:
        try:
            f.unlink()
        except OSError:
            pass
    return len(orphans)


def clear_imports(service: str, account: str | None = None) -> int:
    """Delete imported cache files. With `account=None`, wipe every
    imported account. With a name, only that account's files. Returns
    the number of files removed."""
    d = _CACHE_BASE / service
    if not d.exists():
        return 0
    n = 0
    for f in d.glob("*.json"):
        name = f.stem
        if "__" not in name:
            continue  # orphan — leave to clean_orphans
        if account is not None and name.split("__", 1)[0] != account:
            continue
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
