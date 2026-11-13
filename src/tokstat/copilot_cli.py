#!/usr/bin/env python3
"""
copilot-token-usage — Activity view for GitHub Copilot Chat.

Scans the VS Code Copilot Chat session store
(`…/User/globalStorage/github.copilot-chat/session-store.db`) across VS Code and
its variants. Copilot does NOT record token counts, a model id, or a per-request
cost (it's a flat-rate subscription; "premium requests" are metered only on
GitHub's cloud billing). So tokstat reports **activity only** — prompts, turns
and the tool/file timeline — with token/cost left at 0 ([no tokens]). Inline
completions leave no local trace and are not covered.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from tokstat.cli import __version__

from tokstat._core import (
    BOLD, DIM, RESET, MAGENTA, RED, YELLOW,
    TOOL_COLORS, PRICING,
    load_pricing,
    resolve_period,
    _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact, show_tool_use, tool_target,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    print_retention_alerts, session_matches, project_matches,
)

TOOL_NAME = "GitHub Copilot"
TOOL_COLORS[TOOL_NAME] = MAGENTA

# Copilot does not record tokens/model/cost, so activity is tagged like this.
_MODEL = "GitHub Copilot [no tokens]"


def _copilot_dbs() -> list[Path]:
    """Every Copilot Chat session-store.db across VS Code and its variants
    (Code, Code - Insiders, VSCodium, Cursor, Windsurf, …) on macOS / Linux /
    Windows."""
    home = Path.home()
    roots = [
        home / "Library" / "Application Support",   # macOS
        home / ".config",                           # Linux
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))                 # Windows
    dbs: set[Path] = set()
    for root in roots:
        try:
            if not root.exists():
                continue
            dbs.update(root.glob(
                "*/User/globalStorage/github.copilot-chat/session-store.db"))
        except OSError:
            continue
    return sorted(dbs)


def _db_connect(db: Path) -> sqlite3.Connection:
    """Open a Copilot session store read-only (WAL-safe)."""
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _vscode_user_dirs() -> list[Path]:
    """Every VS Code (and variant) `User` directory on macOS / Linux / Windows."""
    home = Path.home()
    roots = [
        home / "Library" / "Application Support",   # macOS
        home / ".config",                           # Linux
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))                 # Windows
    dirs: list[Path] = []
    for root in roots:
        try:
            if root.exists():
                dirs.extend(root.glob("*/User"))
        except OSError:
            continue
    return dirs


def _vscode_chat_files() -> list[tuple[Path, Path | None]]:
    """Native VS Code chat session files (the built-in Chat / Copilot agent, a
    separate store from the Copilot extension's session-store.db):
    `…/User/workspaceStorage/<hash>/chatSessions/*.json` (with its workspace dir)
    and `…/User/globalStorage/emptyWindowChatSessions/*.json` (no workspace).
    Returns (json_path, workspace_dir_or_None)."""
    files: list[tuple[Path, Path | None]] = []
    for user in _vscode_user_dirs():
        try:
            for f in (user / "workspaceStorage").glob("*/chatSessions/*.json"):
                files.append((f, f.parent.parent))   # <hash>/ dir
        except OSError:
            pass
        try:
            for f in (user / "globalStorage" / "emptyWindowChatSessions").glob("*.json"):
                files.append((f, None))
        except OSError:
            pass
    return files


_WS_FOLDER_CACHE: dict[str, str] = {}


def _workspace_folder(ws_dir: Path | None) -> str:
    """Resolve a workspaceStorage/<hash> dir to its project path, via its
    workspace.json `folder` file-URI. Cached; 'unknown' when absent."""
    if ws_dir is None:
        return "unknown"
    key = str(ws_dir)
    if key in _WS_FOLDER_CACHE:
        return _WS_FOLDER_CACHE[key]
    folder = "unknown"
    try:
        meta = json.loads((ws_dir / "workspace.json").read_text(errors="replace"))
        uri = meta.get("folder") or meta.get("workspace") or ""
        if uri.startswith("file://"):
            folder = unquote(urlparse(uri).path) or "unknown"
        elif uri:
            folder = uri
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    _WS_FOLDER_CACHE[key] = folder
    return folder


def _ts_from_ms(ms) -> datetime | None:
    """VS Code native-chat timestamps are epoch milliseconds."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def _vscode_part_text(part) -> str:
    """Prose text from a response part (MarkdownString `.value`, or a
    markdownContent part's `.content.value`)."""
    if not isinstance(part, dict):
        return ""
    v = part.get("value")
    if isinstance(v, str):
        return v
    content = part.get("content")
    if isinstance(content, dict) and isinstance(content.get("value"), str):
        return content["value"]
    return ""


def _extract_exchanges_vscode_chat() -> list[dict]:
    """One exchange per request in a native VS Code chat session. Activity only
    (no token counts, like the Copilot extension). Captures the prompt, the
    assistant text, and any tool invocations in the response."""
    exchanges: list[dict] = []
    for path, ws_dir in _vscode_chat_files():
        try:
            data = json.loads(path.read_text(errors="replace"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        requests = data.get("requests") or []
        if not requests:
            continue
        project = _workspace_folder(ws_dir)
        session_id = data.get("sessionId") or path.stem
        session_fallback_ts = (_ts_from_ms(data.get("lastMessageDate"))
                               or _ts_from_ms(data.get("creationDate")))
        for req in requests:
            if not isinstance(req, dict):
                continue
            msg = req.get("message") or {}
            user_text = (msg.get("text") or "").strip()
            if not user_text and isinstance(msg.get("parts"), list):
                user_text = " ".join(
                    p.get("text", "") for p in msg["parts"]
                    if isinstance(p, dict) and p.get("text")).strip()
            ts = _ts_from_ms(req.get("timestamp")) or session_fallback_ts
            if ts is None:
                continue

            assistant_texts: list[str] = []
            tool_calls: list[dict] = []
            tools_used: dict[str, int] = defaultdict(int)
            for part in (req.get("response") or []):
                if not isinstance(part, dict):
                    continue
                kind = str(part.get("kind") or "")
                if "tool" in kind.lower():
                    name = (part.get("toolId") or part.get("toolName")
                            or part.get("toolSpecificData", {}).get("kind")
                            if isinstance(part.get("toolSpecificData"), dict)
                            else None) or part.get("toolId") or "tool"
                    inv = part.get("invocationMessage")
                    target = inv.get("value") if isinstance(inv, dict) else (inv or "")
                    call = {"name": str(name), "target": str(target or ""),
                            "error": bool(part.get("isError")), "ts": ts}
                    tool_calls.append(call)
                    tools_used[call["name"]] += 1
                else:
                    t = _vscode_part_text(part).strip()
                    if t:
                        assistant_texts.append(t)

            exchanges.append({
                "session_id":      session_id,
                "user_text":       user_text,
                "assistant_texts": assistant_texts,
                "tool_errors":     [],
                "tools_used":      tools_used,
                "tool_calls":      tool_calls,
                "num_turns":       1,
                "model":           _MODEL,
                "project":         project,
                "ts":              ts,
                "tokens":          {"input": 0, "output": 0,
                                    "cache_read": 0, "cache_write": 0},
                "cost":            0.0,
            })
    return exchanges


def _all_copilot_exchanges() -> list[dict]:
    """Copilot Chat activity from both stores: the Copilot extension's
    session-store.db and VS Code's native chat session files."""
    return _extract_exchanges_copilot() + _extract_exchanges_vscode_chat()


def _copilot_present() -> bool:
    """True when either Copilot store has data on disk."""
    return bool(_copilot_dbs()) or bool(_vscode_chat_files())


def _parse_ts(value) -> datetime | None:
    """Copilot timestamps are ISO-8601 with a trailing Z."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _extract_exchanges_copilot() -> list[dict]:
    """One exchange per Copilot Chat turn (user message + assistant reply),
    with its tool calls (from session_files) attached. Activity only —
    token/cost left at 0, tagged [no tokens]."""
    exchanges: list[dict] = []
    for db in _copilot_dbs():
        try:
            con = _db_connect(db)
        except sqlite3.Error:
            continue
        try:
            # session_id -> working directory (the project)
            cwd_by_session: dict[str, str] = {}
            try:
                for sid, cwd in con.execute("SELECT id, cwd FROM sessions"):
                    cwd_by_session[sid] = cwd or "unknown"
            except sqlite3.Error:
                pass

            # Tool calls: per-turn when turn_index is set, else session-level
            # (older Copilot leaves turn_index NULL — those attach to the
            # session's first turn, below, so they still surface once).
            tools_by_turn: dict[tuple, list[dict]] = defaultdict(list)
            tools_by_session: dict[str, list[dict]] = defaultdict(list)
            try:
                for sid, ti, tool_name, fpath in con.execute(
                        "SELECT session_id, turn_index, tool_name, file_path "
                        "FROM session_files ORDER BY turn_index, first_seen_at"):
                    call = {"name": tool_name or "tool", "target": fpath or "",
                            "error": False}
                    if ti is None:
                        tools_by_session[sid].append(call)
                    else:
                        tools_by_turn[(sid, ti)].append(call)
            except sqlite3.Error:
                pass

            try:
                rows = con.execute(
                    "SELECT session_id, turn_index, user_message, "
                    "assistant_response, timestamp FROM turns "
                    "ORDER BY timestamp").fetchall()
            except sqlite3.Error:
                rows = []

            seen_session: set = set()
            for sid, ti, user_msg, assistant, ts_str in rows:
                ts = _parse_ts(ts_str)
                if ts is None:
                    continue
                calls = list(tools_by_turn.get((sid, ti), []))
                if sid not in seen_session:
                    calls = tools_by_session.get(sid, []) + calls
                    seen_session.add(sid)
                for c in calls:
                    c["ts"] = ts
                tools_used: dict[str, int] = defaultdict(int)
                for c in calls:
                    tools_used[c["name"]] += 1
                exchanges.append({
                    "session_id":      sid,
                    "user_text":       (user_msg or "").strip(),
                    "assistant_texts": [assistant.strip()] if assistant else [],
                    "tool_errors":     [],
                    "tools_used":      tools_used,
                    "tool_calls":      calls,
                    "num_turns":       1,
                    "model":           _MODEL,
                    "project":         cwd_by_session.get(sid, "unknown"),
                    "ts":              ts,
                    "tokens":          {"input": 0, "output": 0,
                                        "cache_read": 0, "cache_write": 0},
                    "cost":            0.0,
                })
        finally:
            con.close()
    return exchanges


def scan_copilot() -> list[dict]:
    """One record per Copilot Chat turn. No token counts are available, so
    token/cost are left at 0 (activity only)."""
    records = []
    for ex in _all_copilot_exchanges():
        if ex["ts"] is None:
            continue
        records.append({
            "tool":        TOOL_NAME,
            "model":       ex["model"],
            "project":     ex["project"],
            "ts":          ex["ts"],
            "session_id":  ex["session_id"],
            "input":       0,
            "output":      0,
            "cache_read":  0,
            "cache_write": 0,
            "cost":        0.0,
        })
    return records


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Collect Copilot exchanges filtered by time."""
    all_exchanges: list[dict] = []
    tool_counts: dict[str, int] = {}

    def _add(tool_name, exchanges):
        if tool_filter and tool_name != tool_filter:
            return
        filtered = [ex for ex in exchanges
                    if ex["ts"] and ex["ts"] >= cutoff
                    and (cutoff_end is None or ex["ts"] < cutoff_end)]
        for ex in filtered:
            ex["tool"] = tool_name
        if filtered:
            all_exchanges.extend(filtered)
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + len(filtered)

    _add(TOOL_NAME, _all_copilot_exchanges())
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None,
         by_session: bool = False, by_project: bool = False,
         project_filter: str | None = None, session_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — {TOOL_NAME}{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    print(f"{DIM}  Scanning Copilot Chat sessions...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not _copilot_present():
        print(f"  {DIM}GitHub Copilot Chat session store not found.{RESET}\n")
        return

    records = scan_copilot()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]
    if project_filter:
        records = [r for r in records if project_matches(r.get("project"), project_filter)]
    if session_filter:
        records = [r for r in records if session_matches(r.get("session_id"), session_filter)]

    if records:
        print(f"  {MAGENTA}●{RESET} {TOOL_NAME:<14} {len(records):>6} records "
              f"{DIM}(activity only — no tokens){RESET}")
    print()
    print(f"  Period: {BOLD}{period_label}{RESET}")
    if project_filter:
        print(f"  Project: {BOLD}{project_filter}{RESET}")
    if session_filter:
        print(f"  Session: {BOLD}{session_filter}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No Copilot activity found.{RESET}\n")
        return

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, [], cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges,
                         by_session=by_session, by_project=by_project,
                         project_filter=project_filter, session_filter=session_filter)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--activity", "--total", "--impact", "--by-session", "--by-project",
    "--tool-use", "--session", "--project", "--export", "--period", "--since",
}


def _arg_value(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return default


def show_help():
    print(f"""
{BOLD}copilot-token-usage{RESET} — Activity view for GitHub Copilot Chat.

{BOLD}MODES{RESET}
  copilot-token-usage                      Activity overview (+ per-session table)
  copilot-token-usage --by-project         Only the project → sessions recap
  copilot-token-usage --prompts  [-p]      Per-exchange detail
  copilot-token-usage --tool-use           Timeline of tool calls (file + time)
  copilot-token-usage --export <file>      Export conversations to JSON

{BOLD}FILTERS{RESET}
  --period <period>    today, yesterday, "7 days", "30 days", all, …
  --session <id>       scope to one session (id or short handle)
  --project <name>     scope to one project (substring on the repo path)

{DIM}  Copilot records no token counts, model or cost (flat-rate subscription),
  so this is activity only ([no tokens]). Inline completions aren't covered.{RESET}
""")


def cli():
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print(f"copilot-token-usage {__version__}")
        return
    if "--help" in args or "-h" in args:
        show_help()
        return

    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"\n  {RED}Unknown option(s): {', '.join(unknown)}{RESET}")
        print(f"  Run {BOLD}copilot-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)
    project = _arg_value(args, "--project")
    session = _arg_value(args, "--session")

    if "--prompts" in args or "-p" in args:
        show_prompts(_collect_all_exchanges, period, TOOL_NAME,
                     session_filter=session, project_filter=project)
    elif "--anomalies" in args:
        show_anomalies(_collect_all_exchanges, period, TOOL_NAME)
    elif "--activity" in args:
        show_activity(_collect_all_exchanges, period, TOOL_NAME)
    elif "--total" in args:
        show_total(_collect_all_exchanges, period, TOOL_NAME)
    elif "--impact" in args:
        show_impact(_collect_all_exchanges, period, TOOL_NAME, _parse_region(args),
                    project_filter=project, session_filter=session)
    elif "--tool-use" in args:
        show_tool_use(_collect_all_exchanges, period, TOOL_NAME,
                      session_filter=session, project_filter=project)
    elif "--plan" in args:
        show_plan(_collect_all_exchanges, period, TOOL_NAME)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(_collect_all_exchanges, out, period, TOOL_NAME,
                             project_filter=project, session_filter=session)
    else:
        main(period, TOOL_NAME, by_session="--by-session" in args,
             by_project="--by-project" in args,
             project_filter=project, session_filter=session)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
