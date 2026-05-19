#!/usr/bin/env python3
"""
claude-web-token-usage — Usage from logged-in claude.ai (web UI).

Authenticates with the user's `sessionKey` cookie and walks the same
private endpoints the web app uses. Token counts are **estimated** from
text length (chars / 4) — claude.ai does not expose per-message usage to
the client.

  Cookie source: copy the value of `sessionKey` from claude.ai DevTools.
  Storage:       `~/.config/tokstat/web-auth.json` (mode 0600), or env
                 `TOKSTAT_CLAUDE_AI_SESSION`.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import os
import sys
import time as _time_mod
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from tokstat.cli import __version__
from tokstat._core import (
    BOLD, DIM, RESET, YELLOW, RED, MAGENTA,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    export_conversations, _parse_period, print_update_notice,
)
from tokstat._web import (
    get_accounts, get_session, set_session, clear_session,
    http_get_json, cache_iter, cache_load, cache_save, cache_is_fresh,
)

TOOL_NAME = "Claude.ai"
TOOL_COLORS[TOOL_NAME] = MAGENTA

_SERVICE = "claude.ai"
_BASE    = "https://claude.ai/api"


def _project_label(account: str) -> str:
    """Project label distinguishing per-account scans in the breakdown."""
    return f"claude.ai ({account})"


def _import_from_path(path: str, account: str) -> int:
    """Populate the local cache from a claude.ai data export.

    Accepts a directory containing conversations.json, the JSON file
    itself, or a ZIP archive. Each conversation is stored under the
    same cache layout the live scraper writes to.
    """
    from zipfile import ZipFile
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file or directory: {p}")
    if p.is_dir():
        jp = p / "conversations.json"
        if not jp.exists():
            raise FileNotFoundError(f"{p} contains no conversations.json")
        raw_text = jp.read_text(errors="replace")
    elif p.suffix.lower() == ".zip":
        with ZipFile(p) as z:
            try:
                with z.open("conversations.json") as f:
                    raw_text = f.read().decode("utf-8", errors="replace")
            except KeyError:
                raise FileNotFoundError(
                    f"{p} doesn't contain conversations.json at its root")
    else:
        raw_text = p.read_text(errors="replace")
    data = json.loads(raw_text)
    if not isinstance(data, list):
        raise ValueError("conversations.json must be a JSON array")
    n = 0
    for conv in data:
        if not isinstance(conv, dict):
            continue
        conv_id = conv.get("uuid") or conv.get("id")
        if not conv_id:
            continue
        updated = conv.get("updated_at") or conv.get("created_at") or ""
        conv["_updated_at"] = updated
        conv["_account"]    = account
        cache_save(_SERVICE, _cache_id(account, conv_id), conv)
        n += 1
    return n


def _anon_id() -> str:
    """Stable per-install UUID used as Anthropic-Anonymous-Id, matching what
    the web UI stores in localStorage. Persisted in web-auth.json."""
    import uuid as _uuid
    from tokstat._web import _load_config, _save_config
    cfg = _load_config()
    aid = cfg.get("claude_ai_anonymous_id")
    if not aid:
        aid = str(_uuid.uuid4())
        cfg["claude_ai_anonymous_id"] = aid
        _save_config(cfg)
    return aid


def _auth_headers(account: str) -> dict:
    sess = get_session(_SERVICE, account)
    if not sess:
        raise RuntimeError(
            f"No claude.ai session cookie configured for account '{account}'. "
            f"Run: claude-web-token-usage --set-cookie <sessionKey> [--account {account}]"
        )
    return {
        "Cookie":                   f"sessionKey={sess}",
        "Origin":                   "https://claude.ai",
        "Referer":                  "https://claude.ai/",
        "Anthropic-Anonymous-Id":   _anon_id(),
        "Anthropic-Client-Sha":     "unknown",
        "Anthropic-Client-Version": "unknown",
    }


# ─── Fetchers ────────────────────────────────────────────────────────────────

def _list_organizations(account: str) -> list[dict]:
    return http_get_json(f"{_BASE}/organizations",
                         headers=_auth_headers(account)) or []


def _list_conversations(account: str, org_uuid: str) -> list[dict]:
    return http_get_json(
        f"{_BASE}/organizations/{org_uuid}/chat_conversations",
        headers=_auth_headers(account),
    ) or []


def _get_conversation(account: str, org_uuid: str, conv_uuid: str) -> dict:
    return http_get_json(
        f"{_BASE}/organizations/{org_uuid}/chat_conversations/{conv_uuid}"
        "?tree=True&rendering_mode=raw",
        headers=_auth_headers(account),
    )


def _get_conversation_with_retry(account: str, org_uuid: str,
                                 conv_uuid: str, max_attempts: int = 4) -> dict:
    import time as _time
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return _get_conversation(account, org_uuid, conv_uuid)
        except RuntimeError as e:
            msg = str(e)
            last_err = e
            if "HTTP 429" in msg or "HTTP 5" in msg:
                _time.sleep(min(2 ** attempt, 8) + 0.25 * attempt)
            else:
                raise
        except Exception as e:
            last_err = e
            _time.sleep(min(2 ** attempt, 8))
    if last_err:
        raise last_err
    raise RuntimeError("unreachable")


def _cache_id(account: str, conv_id: str) -> str:
    # Namespace cache files by account so two accounts can't collide on the
    # same conversation UUID space.
    return f"{account}__{conv_id}"


_MAX_WORKERS = int(os.environ.get("TOKSTAT_WEB_WORKERS", "8") or "8")


def _sync_account(account: str) -> list[dict]:
    """Walk every org/conversation for a single account, returning the
    cached or freshly-fetched conversation payloads. Orgs returning 403
    (or any other error) are skipped. Conversation details are fetched in
    parallel for speed.
    """
    orgs = _list_organizations(account)
    out: list[dict] = []
    ok_orgs: list[str] = []
    failed_orgs: list[tuple[str, str]] = []

    # Collect everything to fetch (across all orgs) before kicking off
    # the thread pool, so progress reflects total work.
    to_fetch: list[tuple[str, str, str, str, dict | None]] = []
    for org in orgs:
        org_uuid = org.get("uuid")
        org_name = org.get("name") or org_uuid or "?"
        if not org_uuid:
            continue
        try:
            convs = _list_conversations(account, org_uuid)
        except Exception as e:
            failed_orgs.append((org_name, str(e).split(":", 1)[0]))
            continue
        ok_orgs.append(org_name)
        for entry in convs:
            conv_id = entry.get("uuid")
            if not conv_id:
                continue
            updated = entry.get("updated_at")
            cache_id = _cache_id(account, conv_id)
            cached = cache_load(_SERVICE, cache_id)
            if cache_is_fresh(cached, updated):
                cached["_account"] = account
                out.append(cached)
                continue
            to_fetch.append((org_uuid, conv_id, updated, cache_id, cached))

    if to_fetch:
        total = len(to_fetch)
        print(f"  {DIM}[{account}] fetching {total} conversation(s), "
              f"{_MAX_WORKERS} in parallel...{RESET}", flush=True)

        def _fetch_one(item):
            org_uuid, conv_id, updated, cache_id, cached = item
            try:
                detail = _get_conversation_with_retry(account, org_uuid, conv_id)
            except Exception as e:
                return ("fail", cached, str(e)[:200])
            detail["_updated_at"] = updated
            detail["_account"] = account
            cache_save(_SERVICE, cache_id, detail)
            return ("ok", detail, None)

        def _fmt_eta(secs: float) -> str:
            secs = max(int(secs), 0)
            if secs < 60:
                return f"{secs}s"
            if secs < 3600:
                return f"{secs // 60}m{secs % 60:02d}s"
            return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"

        done = failed = 0
        sample_errors: list[str] = []
        start = _time_mod.monotonic()
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futures = [ex.submit(_fetch_one, it) for it in to_fetch]
            try:
                for f in as_completed(futures):
                    done += 1
                    status, r, err = f.result()
                    if status == "fail":
                        failed += 1
                        if err and len(sample_errors) < 3:
                            print(f"\n  {DIM}  e{len(sample_errors) + 1}: "
                                  f"{err}{RESET}", flush=True)
                            sample_errors.append(err)
                    if r is not None:
                        out.append(r)
                    pct = done * 100 // total
                    elapsed = _time_mod.monotonic() - start
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    print(f"\r  {DIM}[{account}] {done}/{total} ({pct}%) "
                          f"— {failed} failed · {rate:.2f}/s · "
                          f"ETA {_fmt_eta(eta)}    {RESET}",
                          end="", flush=True)
            except KeyboardInterrupt:
                print(f"\n  {YELLOW}Interrupted at {done}/{total} "
                      f"({failed} failed).{RESET}")
                raise
        print()
        if failed:
            print(f"  {YELLOW}[{account}] {failed}/{total} conversation(s) "
                  f"failed after retries.{RESET}")
            for i, err in enumerate(sample_errors, 1):
                print(f"  {DIM}  e{i}: {err}{RESET}")

    if failed_orgs and not ok_orgs:
        names = ", ".join(n for n, _ in failed_orgs)
        raise RuntimeError(
            f"account '{account}': all listed organizations refused access "
            f"({names}). First error: {failed_orgs[0][1]}. "
            f"The sessionKey cookie may be stale — re-copy it from claude.ai."
        )
    if failed_orgs:
        skipped = ", ".join(f"{n} ({e})" for n, e in failed_orgs)
        print(f"  {DIM}[{account}] Skipped orgs: {skipped}{RESET}")
    return out


def _sync_all() -> list[dict]:
    """Sync every configured account; each conversation is tagged with the
    `_account` it came from for downstream display."""
    accounts = get_accounts(_SERVICE)
    out: list[dict] = []
    for name in sorted(accounts.keys()):
        try:
            out.extend(_sync_account(name))
        except Exception as e:
            print(f"  {RED}[{name}] {e}{RESET}", file=sys.stderr)
    return out


# ─── Parsing ────────────────────────────────────────────────────────────────

def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _message_text(msg: dict) -> str:
    """Reassemble a message body from its `content` parts (or fallback)."""
    parts = msg.get("content") or []
    chunks: list[str] = []
    if isinstance(parts, list):
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                t = p.get("text", "")
                if t:
                    chunks.append(t)
    if not chunks and msg.get("text"):
        chunks.append(msg["text"])
    return "\n".join(chunks)


def _conv_messages(conv: dict) -> list[dict]:
    msgs = conv.get("chat_messages") or []
    out = []
    for m in msgs:
        ts = _parse_dt(m.get("created_at"))
        if ts is None:
            continue
        out.append({
            "ts":     ts,
            "sender": m.get("sender", ""),
            "text":   _message_text(m),
            "model":  m.get("model") or conv.get("model") or "claude-unknown",
        })
    out.sort(key=lambda m: m["ts"])
    return out


def _to_records(convs: list[dict]) -> list[dict]:
    """One record per assistant message, tokens estimated from char count."""
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
                "cost":    compute_cost({"input": 0, "output": out_tokens,
                                         "cache_read": 0, "cache_write": 0},
                                        msg["model"]),
            })
    return records


def scan_claude_web() -> list[dict]:
    if get_accounts(_SERVICE):
        try:
            convs = _sync_all()
        except Exception as e:
            print(f"  {RED}claude.ai fetch failed: {e}{RESET}", file=sys.stderr)
            convs = list(cache_iter(_SERVICE))
    else:
        convs = list(cache_iter(_SERVICE))
    return _to_records(convs)


def _extract_exchanges_claude_web() -> list[dict]:
    if get_accounts(_SERVICE):
        try:
            convs = _sync_all()
        except Exception:
            convs = list(cache_iter(_SERVICE))
    else:
        convs = list(cache_iter(_SERVICE))
    if not convs:
        return []

    exchanges: list[dict] = []
    for conv in convs:
        account = conv.get("_account") or "default"
        project = _project_label(account)
        msgs = _conv_messages(conv)
        current = None
        for m in msgs:
            if m["sender"] == "human":
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
    exchanges = _extract_exchanges_claude_web()
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
    cached_any = any(True for _ in cache_iter(_SERVICE))
    if not accounts and not cached_any:
        print(f"\n  {YELLOW}No claude.ai session cookie configured "
              f"and no imported data found.{RESET}")
        print(f"  Two ways to feed this tool:")
        print(f"    {BOLD}1. Live scrape{RESET} — copy the sessionKey cookie "
              f"from claude.ai (DevTools → Application → Cookies), then:")
        print(f"       claude-web-token-usage --set-cookie <value>"
              f" [--account <name>]")
        print(f"    {BOLD}2. Official export{RESET} — Settings → Privacy → "
              f"Export Data on claude.ai, wait for the email, then:")
        print(f"       claude-web-token-usage --import <export.zip>"
              f" [--account <name>]")
        return

    if accounts:
        names = ", ".join(sorted(accounts.keys()))
        print(f"{DIM}  Syncing claude.ai accounts: {names}...{RESET}\n")
    else:
        print(f"{DIM}  Reading from local cache (offline, no scraping)...{RESET}\n")
    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    records = scan_claude_web()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {MAGENTA}●{RESET} {TOOL_NAME:<10} {len(records):>6} assistant messages from claude.ai")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No usage data found in the given period.{RESET}\n")
        return

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, [], cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges)
    print(f"  {DIM}⚠ Token counts are estimated from text length "
          f"(claude.ai does not expose usage).{RESET}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"claude.ai": TOOL_NAME, "claude-web": TOOL_NAME, "claudeai": TOOL_NAME}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--export", "--period", "--since", "--tool",
    "--set-cookie", "--clear-cookie", "--account", "--list-accounts",
    "--import",
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
{BOLD}claude-web-token-usage{RESET} — Usage from the claude.ai web UI (estimated).

{BOLD}SETUP{RESET}
  Copy the {BOLD}sessionKey{RESET} cookie value from claude.ai in your browser, then:
    claude-web-token-usage --set-cookie <value> [--account <name>]
  Or export {BOLD}TOKSTAT_CLAUDE_AI_SESSION{RESET}=<value>.

  Multiple accounts (e.g. perso + work) are supported. Each account
  shows up as a separate row under CONSUMPTION BY PROJECT.

{BOLD}MODES{RESET}
  claude-web-token-usage                          Aggregated overview
  claude-web-token-usage --prompts  [-p]          Per-exchange detail
  claude-web-token-usage --anomalies              Anomaly detection
  claude-web-token-usage --plan                   Cost breakdown + tips
  claude-web-token-usage --export   [file]        Export exchanges to JSON
  claude-web-token-usage --import <zip|json|dir>  Load the official export
                              [--account <name>]  (no scraping needed)
  claude-web-token-usage --set-cookie <v>         Store sessionKey cookie
                              [--account <name>]  ...for a named account
  claude-web-token-usage --clear-cookie           Forget all accounts
                              [--account <name>]  ...or just one
  claude-web-token-usage --list-accounts          Show configured accounts

{BOLD}OFFICIAL EXPORT (recommended){RESET}
  On claude.ai → Settings → Privacy → Export Data. You'll get an
  email with a ZIP. Run:
    claude-web-token-usage --import path/to/export.zip
  This populates the local cache without any HTTP traffic.

{BOLD}FILTERS{RESET}
  --period <p>    all, hour, "5 hours", today, "7 days", "30 days", year

{BOLD}DATA SOURCE{RESET}
  {MAGENTA}claude.ai{RESET}  private endpoints under https://claude.ai/api/
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

    if "--import" in args:
        idx = args.index("--import")
        if idx + 1 >= len(args) or args[idx + 1].startswith("-"):
            print(f"  {RED}--import requires a path "
                  f"(ZIP, JSON, or extracted folder).{RESET}")
            sys.exit(1)
        account = _parse_account(args)
        path = args[idx + 1]
        try:
            n = _import_from_path(path, account)
        except Exception as e:
            print(f"  {RED}Import failed: {e}{RESET}")
            sys.exit(1)
        print(f"  Imported {BOLD}{n}{RESET} conversation(s) into account "
              f"{BOLD}{account}{RESET} "
              f"({DIM}cache: ~/.cache/tokstat/web/{_SERVICE}/{RESET}).")
        print(f"  Run {BOLD}claude-web-token-usage --period all{RESET} "
              f"to view the data.")
        return

    if "--list-accounts" in args:
        accounts = get_accounts(_SERVICE)
        if not accounts:
            print(f"  {DIM}No claude.ai accounts configured.{RESET}")
        else:
            print(f"  {BOLD}claude.ai accounts:{RESET}")
            for name in sorted(accounts.keys()):
                print(f"    {MAGENTA}●{RESET} {name}")
        return

    if "--set-cookie" in args:
        idx = args.index("--set-cookie")
        if idx + 1 >= len(args):
            print(f"  {RED}--set-cookie requires a value.{RESET}")
            sys.exit(1)
        account = _parse_account(args)
        set_session(_SERVICE, args[idx + 1].strip(), account=account)
        print(f"  {BOLD}claude.ai{RESET} session cookie stored for account "
              f"{BOLD}{account}{RESET} "
              f"({DIM}~/.config/tokstat/web-auth.json{RESET}).")
        return
    if "--clear-cookie" in args:
        if "--account" in args:
            account = _parse_account(args)
            clear_session(_SERVICE, account=account)
            print(f"  claude.ai account {BOLD}{account}{RESET} cleared.")
        else:
            clear_session(_SERVICE)
            print(f"  claude.ai: all accounts cleared.")
        return

    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"\n  {RED}Unknown option(s): {', '.join(unknown)}{RESET}")
        print(f"  Run {BOLD}claude-web-token-usage --help{RESET} for usage.\n")
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
