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


def get_accounts(service: str) -> dict[str, object]:
    """Return {account_name: session_value} for the named service.

    `session_value` is either a string (single cookie / token) or a list
    (NextAuth-style split cookie). The env var override is always exposed
    under the account name "env".

    Backward compat: if the on-disk config has a bare string/list under
    `service` instead of a dict, treat it as a single "default" account.
    """
    out: dict[str, object] = {}
    env_key = {
        "claude.ai":   "TOKSTAT_CLAUDE_AI_SESSION",
        "chatgpt.com": "TOKSTAT_CHATGPT_SESSION",
    }.get(service)
    if env_key and os.environ.get(env_key):
        out["env"] = os.environ[env_key].strip()
        return out
    val = _load_config().get(service)
    if val is None:
        return out
    if isinstance(val, dict):
        for name, v in val.items():
            if not v:
                continue
            if isinstance(v, list):
                v = [x for x in v if x]
                if not v:
                    continue
            out[name] = v
        return out
    # Legacy: single string or list under the service key.
    if isinstance(val, list):
        cleaned = [v for v in val if v]
        if cleaned:
            out["default"] = cleaned
    elif isinstance(val, str) and val:
        out["default"] = val
    return out


def get_session(service: str, account: str = "default"):
    """Return the session secret(s) for a single (service, account)."""
    return get_accounts(service).get(account)


def set_session(service: str, value, account: str = "default") -> None:
    """Store the session secret(s) for one account, migrating the legacy
    flat representation to the dict form on first multi-account write."""
    cfg = _load_config()
    existing = cfg.get(service)
    if not isinstance(existing, dict):
        bucket: dict[str, object] = {}
        if isinstance(existing, (str, list)) and existing:
            bucket["default"] = existing
        existing = bucket
    existing[account] = value
    cfg[service] = existing
    _save_config(cfg)


def clear_session(service: str, account: str | None = None) -> None:
    """Forget one account, or every account when `account` is None."""
    cfg = _load_config()
    if account is None or not isinstance(cfg.get(service), dict):
        cfg.pop(service, None)
    else:
        cfg[service].pop(account, None)
        if not cfg[service]:
            cfg.pop(service)
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


class RateLimited(Exception):
    """Raised when the server returned HTTP 429. `retry_after` is in seconds."""
    def __init__(self, retry_after: float, body: bytes = b""):
        super().__init__(f"HTTP 429 (retry after {retry_after:g}s)")
        self.retry_after = retry_after
        self.body = body


def http_get_json(url: str, *, headers: dict | None = None,
                  timeout: float = 30.0) -> Any:
    status, body, resp_headers = http_get(url, headers=headers, timeout=timeout)
    if status == 429:
        try:
            retry_after = float(resp_headers.get("Retry-After", "0"))
        except (ValueError, TypeError):
            retry_after = 0.0
        raise RateLimited(retry_after or 30.0, body)
    if status >= 400:
        raise RuntimeError(f"HTTP {status} fetching {url}: {body[:200]!r}")
    return json.loads(body.decode("utf-8", errors="replace") or "null")


# ─── Conversation cache ──────────────────────────────────────────────────────

def cache_dir(service: str) -> Path:
    p = _CACHE_BASE / service
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_iter(service: str):
    """Yield every cached conversation payload for `service`, tagging each
    with `_account` decoded from its `<account>__<id>.json` filename.
    Used to support offline mode after an import."""
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


def cache_is_fresh(cached: dict | None, updated_at: str | None,
                   strict: bool = False) -> bool:
    """A cached conv is considered fresh.

    In the default (non-strict) mode any cached payload counts as fresh —
    this matches users' expectation that once `--import` has populated the
    cache, subsequent runs don't re-fetch silently. Set `strict=True` (e.g.
    `--refresh` flag) to require the cached `_updated_at` to match the
    list endpoint's value.
    """
    if not cached:
        return False
    if not strict:
        return True
    if not updated_at:
        return False
    return cached.get("_updated_at") == updated_at
