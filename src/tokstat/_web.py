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
    """Yield every cached conversation payload, tagging each with `_account`
    decoded from its `<account>__<id>.json` filename."""
    d = _CACHE_BASE / service
    if not d.exists():
        return
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        name = f.stem
        if "__" in name and not data.get("_account"):
            data["_account"] = name.split("__", 1)[0]
        yield data


def imported_accounts(service: str) -> list[str]:
    """Return the distinct account names present in the on-disk cache."""
    d = _CACHE_BASE / service
    if not d.exists():
        return []
    seen = set()
    for f in d.glob("*.json"):
        name = f.stem
        if "__" in name:
            seen.add(name.split("__", 1)[0])
    return sorted(seen)
