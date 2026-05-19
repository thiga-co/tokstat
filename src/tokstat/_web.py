"""Shared infrastructure for scraping logged-in claude.ai and chatgpt.com.

These services don't publish an official usage API; we hit the same private
endpoints the web UI uses, authenticated with the user's session cookie.

Cookies live in `~/.config/tokstat/web-auth.json` (mode 0600). Override per
service via env vars:
  TOKSTAT_CLAUDE_AI_SESSION   - claude.ai sessionKey cookie value
  TOKSTAT_CHATGPT_SESSION     - chatgpt.com __Secure-next-auth.session-token

Conversation payloads are cached under `~/.cache/tokstat/web/<service>/<id>.json`
keyed by conversation UUID + last `updated_at`, so subsequent runs only fetch
conversations that changed.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


_CONFIG_PATH = Path.home() / ".config" / "tokstat" / "web-auth.json"
_CACHE_BASE  = Path.home() / ".cache" / "tokstat" / "web"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


# ─── Auth config ──────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(_CONFIG_PATH, 0o600)
    except OSError:
        pass


def get_session(service: str):
    """Return the session secret(s) for the named service, or None.

    May return a plain string (single cookie / token) or a list of strings
    (cookie split across multiple parts, like NextAuth's
    `__Secure-next-auth.session-token.0` / `.1`). Env vars take precedence
    over the on-disk config and are always returned as strings.
    """
    env_key = {
        "claude.ai":   "TOKSTAT_CLAUDE_AI_SESSION",
        "chatgpt.com": "TOKSTAT_CHATGPT_SESSION",
    }.get(service)
    if env_key and os.environ.get(env_key):
        return os.environ[env_key].strip() or None
    val = _load_config().get(service)
    if isinstance(val, list):
        cleaned = [v for v in val if v]
        return cleaned or None
    return val


def set_session(service: str, value) -> None:
    cfg = _load_config()
    cfg[service] = value
    _save_config(cfg)


def clear_session(service: str) -> None:
    cfg = _load_config()
    cfg.pop(service, None)
    _save_config(cfg)


# ─── HTTP ────────────────────────────────────────────────────────────────────

def http_get(url: str, *, headers: dict | None = None,
             timeout: float = 30.0) -> tuple[int, bytes, dict]:
    """Plain urllib GET. Returns (status, body, response_headers)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/json,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b""), dict(e.headers or {})


def http_get_json(url: str, *, headers: dict | None = None,
                  timeout: float = 30.0) -> Any:
    status, body, _ = http_get(url, headers=headers, timeout=timeout)
    if status >= 400:
        raise RuntimeError(f"HTTP {status} fetching {url}: {body[:200]!r}")
    return json.loads(body.decode("utf-8", errors="replace") or "null")


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


def cache_is_fresh(cached: dict | None, updated_at: str | None) -> bool:
    """A cached conv is fresh iff its stored `_updated_at` >= the list's."""
    if not cached or not updated_at:
        return False
    return cached.get("_updated_at") == updated_at
