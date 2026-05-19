#!/usr/bin/env python3
"""
chatgpt-web-token-usage — Usage from logged-in chatgpt.com (web UI).

ChatGPT requires exchanging the `__Secure-next-auth.session-token` cookie
for a short-lived bearer access token via `/api/auth/session`. We cache
the access token in `~/.config/tokstat/web-auth.json` and refresh it
when it expires.

Tokens are **estimated** from text length (chars / 4).

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tokstat.cli import __version__
from tokstat._core import (
    BOLD, DIM, RESET, YELLOW, RED, GREEN,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    export_conversations, _parse_period, print_update_notice,
)
from tokstat._web import (
    get_accounts, get_session, set_session, clear_session,
    http_get_json, cache_load, cache_save, cache_is_fresh,
    _CONFIG_PATH, _load_config, _save_config,
)

TOOL_NAME = "ChatGPT"
TOOL_COLORS[TOOL_NAME] = GREEN

_SERVICE = "chatgpt.com"
_BASE    = "https://chatgpt.com"


def _project_label(account: str) -> str:
    return f"chatgpt.com ({account})"


# ─── Access-token cache ──────────────────────────────────────────────────────
# We piggyback on the same web-auth.json file as the session cookie. Token
# entry shape: {"chatgpt_access_token": "...", "chatgpt_access_token_exp": 0}

def _tok_key(account: str) -> str:
    return f"chatgpt_access_token__{account}"


def _load_access_token(account: str) -> str | None:
    cfg = _load_config()
    tok = cfg.get(_tok_key(account))
    exp = cfg.get(_tok_key(account) + "_exp") or 0
    if not tok:
        return None
    if exp and time.time() > exp - 60:
        return None
    return tok


def _save_access_token(account: str, token: str, expires_at_epoch: float) -> None:
    cfg = _load_config()
    cfg[_tok_key(account)] = token
    cfg[_tok_key(account) + "_exp"] = int(expires_at_epoch)
    _save_config(cfg)


def _clear_access_token(account: str | None = None) -> None:
    cfg = _load_config()
    if account is None:
        for k in list(cfg.keys()):
            if k.startswith("chatgpt_access_token__"):
                cfg.pop(k, None)
    else:
        cfg.pop(_tok_key(account), None)
        cfg.pop(_tok_key(account) + "_exp", None)
    _save_config(cfg)


def _cookie_header(account: str) -> str:
    """Build the Cookie header for the given account. Supports a single
    string, a list (NextAuth-split parts), or a pre-baked "name=val; ..."
    string under the account entry.
    """
    sess = get_session(_SERVICE, account)
    if not sess:
        raise RuntimeError(
            f"No chatgpt.com session cookie configured for account '{account}'. "
            f"Run: chatgpt-web-token-usage --set-cookie <value-0> [<value-1>] "
            f"[--account {account}]"
        )
    if isinstance(sess, list):
        parts = [(f"__Secure-next-auth.session-token.{i}", v.strip())
                 for i, v in enumerate(sess) if v]
    else:
        if "=" in sess and ";" in sess:
            return sess
        parts = [("__Secure-next-auth.session-token", sess.strip())]
    return "; ".join(f"{name}={val}" for name, val in parts)


def _refresh_access_token(account: str) -> str:
    data = http_get_json(
        f"{_BASE}/api/auth/session",
        headers={"Cookie": _cookie_header(account),
                 "Referer": f"{_BASE}/"},
    )
    if not data or not data.get("accessToken"):
        raise RuntimeError(
            f"chatgpt.com /api/auth/session returned no access token for "
            f"account '{account}' — cookie likely expired; re-run --set-cookie."
        )
    expires_str = data.get("expires") or ""
    try:
        epoch = datetime.fromisoformat(expires_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        epoch = time.time() + 3600
    _save_access_token(account, data["accessToken"], epoch)
    return data["accessToken"]


def _bearer(account: str) -> str:
    return _load_access_token(account) or _refresh_access_token(account)


def _auth_headers(account: str) -> dict:
    return {"Authorization": f"Bearer {_bearer(account)}",
            "Referer": f"{_BASE}/"}


# ─── Fetchers ────────────────────────────────────────────────────────────────

def _list_conversations(account: str) -> list[dict]:
    items: list[dict] = []
    offset = 0
    page_size = 100
    while True:
        url = (f"{_BASE}/backend-api/conversations"
               f"?offset={offset}&limit={page_size}&order=updated")
        try:
            data = http_get_json(url, headers=_auth_headers(account))
        except RuntimeError as e:
            if "HTTP 401" in str(e) and offset == 0:
                _refresh_access_token(account)
                data = http_get_json(url, headers=_auth_headers(account))
            else:
                raise
        batch = (data or {}).get("items") or []
        items.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
        if offset > 5000:
            break
    return items


def _get_conversation(account: str, conv_id: str) -> dict:
    return http_get_json(
        f"{_BASE}/backend-api/conversation/{conv_id}",
        headers=_auth_headers(account),
    )


def _cache_id(account: str, conv_id: str) -> str:
    return f"{account}__{conv_id}"


def _sync_account(account: str) -> list[dict]:
    entries = _list_conversations(account)
    out: list[dict] = []
    for entry in entries:
        conv_id = entry.get("id")
        if not conv_id:
            continue
        updated = str(entry.get("update_time", ""))
        cache_id = _cache_id(account, conv_id)
        cached = cache_load(_SERVICE, cache_id)
        if cache_is_fresh(cached, updated):
            cached["_account"] = account
            out.append(cached)
            continue
        try:
            detail = _get_conversation(account, conv_id)
        except Exception:
            if cached:
                cached["_account"] = account
                out.append(cached)
            continue
        detail["_updated_at"] = updated
        detail["_account"] = account
        cache_save(_SERVICE, cache_id, detail)
        out.append(detail)
    return out


def _sync_all() -> list[dict]:
    accounts = get_accounts(_SERVICE)
    out: list[dict] = []
    for name in sorted(accounts.keys()):
        try:
            out.extend(_sync_account(name))
        except Exception as e:
            print(f"  {RED}[{name}] {e}{RESET}", file=sys.stderr)
    return out


# ─── Parsing ────────────────────────────────────────────────────────────────

def _parse_dt(epoch) -> datetime | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def _content_text(content: dict | None) -> str:
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if isinstance(parts, list):
        return "\n".join(str(p) for p in parts if isinstance(p, str))
    return content.get("text", "") or ""


def _conv_messages(conv: dict) -> list[dict]:
    """Walk the conversation `mapping` tree, yielding chronologically-sorted
    visible messages (skip system, tool, hidden)."""
    mapping = conv.get("mapping") or {}
    msgs = []
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        m = node.get("message")
        if not isinstance(m, dict):
            continue
        author = (m.get("author") or {}).get("role", "")
        if author not in ("user", "assistant"):
            continue
        ts = _parse_dt(m.get("create_time"))
        if ts is None:
            continue
        meta = m.get("metadata") or {}
        text = _content_text(m.get("content"))
        if not text.strip():
            continue
        msgs.append({
            "ts":     ts,
            "sender": author,
            "text":   text,
            "model":  meta.get("model_slug") or conv.get("default_model_slug") or "gpt-unknown",
        })
    msgs.sort(key=lambda m: m["ts"])
    return msgs


def _to_records(convs: list[dict]) -> list[dict]:
    records = []
    for conv in convs:
        account = conv.get("_account") or "default"
        for msg in _conv_messages(conv):
            if msg["sender"] != "assistant":
                continue
            out_tokens = max(len(msg["text"]) // 4, 0)
            if out_tokens == 0:
                continue
            tokens = {"input": 0, "output": out_tokens,
                      "cache_read": 0, "cache_write": 0}
            model = msg["model"] + " [est]"
            records.append({
                "tool":    TOOL_NAME,
                "model":   model,
                "project": _project_label(account),
                "ts":      msg["ts"],
                **tokens,
                "cost":    compute_cost(tokens, msg["model"]),
            })
    return records


def scan_chatgpt_web() -> list[dict]:
    if not get_accounts(_SERVICE):
        return []
    try:
        convs = _sync_all()
    except Exception as e:
        print(f"  {RED}chatgpt.com fetch failed: {e}{RESET}", file=sys.stderr)
        return []
    return _to_records(convs)


def _extract_exchanges_chatgpt_web() -> list[dict]:
    if not get_accounts(_SERVICE):
        return []
    try:
        convs = _sync_all()
    except Exception:
        return []

    exchanges: list[dict] = []
    for conv in convs:
        account = conv.get("_account") or "default"
        project = _project_label(account)
        msgs = _conv_messages(conv)
        current = None
        for m in msgs:
            if m["sender"] == "user":
                if current:
                    exchanges.append(current)
                current = {
                    "user_text":       m["text"][:500],
                    "assistant_texts": [],
                    "tool_errors":     [],
                    "tools_used":      defaultdict(int),
                    "num_turns":       0,
                    "model":           m["model"] + " [est]",
                    "project":         project,
                    "ts":              m["ts"],
                    "tokens":          {"input": max(len(m["text"]) // 4, 0),
                                        "output": 0,
                                        "cache_read": 0, "cache_write": 0},
                    "cost":            0.0,
                }
            elif m["sender"] == "assistant" and current is not None:
                current["num_turns"] += 1
                out_tokens = max(len(m["text"]) // 4, 0)
                current["tokens"]["output"] += out_tokens
                if m["text"]:
                    current["assistant_texts"].append(m["text"][:500])
                current["model"] = m["model"] + " [est]"
                current["cost"] += compute_cost(
                    {"input": 0, "output": out_tokens,
                     "cache_read": 0, "cache_write": 0},
                    m["model"],
                )
        if current:
            exchanges.append(current)
    return exchanges


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    all_exchanges: list[dict] = []
    tool_counts: dict[str, int] = {}
    if tool_filter and tool_filter != TOOL_NAME:
        return all_exchanges, tool_counts
    exchanges = _extract_exchanges_chatgpt_web()
    filtered = [ex for ex in exchanges
                if ex["ts"] and ex["ts"] >= cutoff
                and (cutoff_end is None or ex["ts"] < cutoff_end)]
    for ex in filtered:
        ex["tool"] = TOOL_NAME
    if filtered:
        all_exchanges.extend(filtered)
        tool_counts[TOOL_NAME] = len(filtered)
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main ────────────────────────────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — {TOOL_NAME} (web){RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")

    accounts = get_accounts(_SERVICE)
    if not accounts:
        print(f"\n  {YELLOW}No chatgpt.com session cookie configured.{RESET}")
        print(f"  Open chatgpt.com in a logged-in browser, DevTools → "
              f"Application → Cookies → copy {BOLD}__Secure-next-auth.session-token{RESET}"
              f" (or its .0 / .1 split), then:\n")
        print(f"    {BOLD}chatgpt-web-token-usage --set-cookie <v0> [<v1>]"
              f" [--account <name>]{RESET}\n")
        return

    names = ", ".join(sorted(accounts.keys()))
    print(f"{DIM}  Syncing chatgpt.com accounts: {names}...{RESET}\n")
    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    records = scan_chatgpt_web()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {GREEN}●{RESET} {TOOL_NAME:<10} {len(records):>6} assistant messages from chatgpt.com")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No usage data found in the given period.{RESET}\n")
        return

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, [], cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges)
    print(f"  {DIM}⚠ Token counts are estimated from text length "
          f"(chatgpt.com does not expose usage).{RESET}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"chatgpt": TOOL_NAME, "chatgpt.com": TOOL_NAME, "chatgpt-web": TOOL_NAME}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--export", "--period", "--since", "--tool",
    "--set-cookie", "--clear-cookie", "--account", "--list-accounts",
}


def _parse_account(args: list[str]) -> str:
    if "--account" not in args:
        return "default"
    idx = args.index("--account")
    if idx + 1 >= len(args):
        return "default"
    return args[idx + 1].strip() or "default"


def _parse_tool(args: list[str]) -> str | None:
    if "--tool" not in args:
        return None
    idx = args.index("--tool")
    if idx + 1 >= len(args):
        return None
    raw = args[idx + 1].lower().strip()
    if raw in ("all", "tous", "*"):
        return None
    return _TOOL_ALIASES.get(raw) or None


def show_help():
    print(f"""
{BOLD}chatgpt-web-token-usage{RESET} — Usage from the chatgpt.com web UI (estimated).

{BOLD}SETUP{RESET}
  In chatgpt.com (logged in), open DevTools → Application → Cookies.
  Copy the value of {BOLD}__Secure-next-auth.session-token{RESET}. If you see
  two cookies named {BOLD}.session-token.0{RESET} and {BOLD}.session-token.1{RESET}
  (NextAuth splits the token when it overflows 4 KB), pass {BOLD}both{RESET}:

    chatgpt-web-token-usage --set-cookie <value-0> [<value-1>] [--account <name>]

  Multiple accounts (e.g. perso + work) are supported — each shows up as
  a separate row under CONSUMPTION BY PROJECT.

  Or export {BOLD}TOKSTAT_CHATGPT_SESSION{RESET} = the raw Cookie header
  string ("name=val; name=val") for a one-shot single-account run.

{BOLD}MODES{RESET}
  chatgpt-web-token-usage                          Aggregated overview
  chatgpt-web-token-usage --prompts  [-p]          Per-exchange detail
  chatgpt-web-token-usage --anomalies              Anomaly detection
  chatgpt-web-token-usage --plan                   Cost breakdown + tips
  chatgpt-web-token-usage --export   [file]        Export exchanges to JSON
  chatgpt-web-token-usage --set-cookie <v0> [<v1>] Store session cookie
                              [--account <name>]   ...for a named account
  chatgpt-web-token-usage --clear-cookie           Forget all accounts
                              [--account <name>]   ...or just one
  chatgpt-web-token-usage --list-accounts          Show configured accounts

{BOLD}FILTERS{RESET}
  --period <p>    all, hour, "5 hours", today, "7 days", "30 days", year

{BOLD}DATA SOURCE{RESET}
  {GREEN}chatgpt.com{RESET}  private endpoints under https://chatgpt.com/backend-api/
                ✓ Conversations · ✓ Models · ~ Tokens (estimated)
""")


def cli():
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print(f"tokstat {__version__}")
        return
    if "--help" in args or "-h" in args:
        show_help()
        return

    if "--list-accounts" in args:
        accounts = get_accounts(_SERVICE)
        if not accounts:
            print(f"  {DIM}No chatgpt.com accounts configured.{RESET}")
        else:
            print(f"  {BOLD}chatgpt.com accounts:{RESET}")
            for name in sorted(accounts.keys()):
                print(f"    {GREEN}●{RESET} {name}")
        return

    if "--set-cookie" in args:
        idx = args.index("--set-cookie")
        if idx + 1 >= len(args):
            print(f"  {RED}--set-cookie requires a value.{RESET}")
            sys.exit(1)
        account = _parse_account(args)
        # Treat the value immediately following --set-cookie as part 0, and
        # an optional second positional as part 1, ignoring --account/<name>.
        positional: list[str] = []
        i = idx + 1
        while i < len(args) and not args[i].startswith("-"):
            positional.append(args[i].strip())
            i += 1
        if not positional:
            print(f"  {RED}--set-cookie requires at least one value.{RESET}")
            sys.exit(1)
        value: object = positional if len(positional) > 1 else positional[0]
        set_session(_SERVICE, value, account=account)
        _clear_access_token(account)  # force refresh next run
        parts_msg = "1 cookie" if len(positional) == 1 else "2 cookie parts (.0 + .1)"
        print(f"  {BOLD}chatgpt.com{RESET} session stored for account "
              f"{BOLD}{account}{RESET} — {parts_msg} "
              f"({DIM}~/.config/tokstat/web-auth.json{RESET}).")
        return
    if "--clear-cookie" in args:
        if "--account" in args:
            account = _parse_account(args)
            clear_session(_SERVICE, account=account)
            _clear_access_token(account)
            print(f"  chatgpt.com account {BOLD}{account}{RESET} cleared.")
        else:
            clear_session(_SERVICE)
            _clear_access_token()
            print(f"  chatgpt.com: all accounts cleared.")
        return

    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"\n  {RED}Unknown option(s): {', '.join(unknown)}{RESET}")
        print(f"  Run {BOLD}chatgpt-web-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)
    tool = _parse_tool(args)

    if "--prompts" in args or "-p" in args:
        show_prompts(_collect_all_exchanges, period, tool)
    elif "--anomalies" in args:
        show_anomalies(_collect_all_exchanges, period, tool)
    elif "--plan" in args:
        show_plan(_collect_all_exchanges, period, tool)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(_collect_all_exchanges, out, period, tool)
    else:
        main(period, tool)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
