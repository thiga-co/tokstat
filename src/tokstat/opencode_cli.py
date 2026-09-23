#!/usr/bin/env python3
"""
opencode-token-usage — Aggregate and display token consumption from opencode.

Data sources (auto-detected):
  • SQLite (OpenCode ≥ 0.6 / 2026): ~/.local/share/opencode/opencode.db
      - `message` table: JSON blob per message (role, tokens, time, modelID…)
      - `part` table:    JSON blob per part (text, tool calls…)
      - `session`/`session_v2`: session metadata incl. `directory` (cwd)
  • Legacy JSON: ~/.local/share/opencode/storage/message/{session}/{msg}.json
    Each assistant message embeds `tokens` (input/output/cache) and `path.cwd`.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tokstat.cli import __version__
from tokstat._core import (
    BOLD, DIM, RESET, MAGENTA, YELLOW, RED,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact, show_audit,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    print_retention_alerts, tool_target,
)

TOOL_COLORS["opencode"] = MAGENTA

_BASE       = Path.home() / ".local" / "share" / "opencode"
_DB         = _BASE / "opencode.db"
_MSG_BASE   = _BASE / "storage" / "message"
_SESS_BASE  = _BASE / "storage" / "session"


# ─── SQLite backend (opencode.db) ─────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    """Open opencode.db read-only (WAL-safe: reads committed data only)."""
    return sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)


def _db_session_cwd_map(con: sqlite3.Connection) -> dict[str, str]:
    """{session_id: directory} from session/session_v2 tables."""
    out: dict[str, str] = {}
    for table in ("session", "session_v2"):
        try:
            rows = con.execute(f"SELECT id, directory FROM {table}")  # noqa: S608
            for sid, directory in rows:
                if sid and directory and sid not in out:
                    out[sid] = directory
        except sqlite3.Error:
            continue
    return out


_DB_MSG_CACHE: dict = {}   # {mtime: parsed messages} — one entry, mtime-keyed


def _load_db_messages() -> list[tuple[str, dict, str | None, list[dict]]]:
    """Cached wrapper: parse opencode.db once per (path, mtime).

    The DB is read by scan / scan_speed / exchange extraction in the same run;
    keying on mtime avoids re-parsing it each time while staying correct under
    ``--watch`` (a write bumps mtime and invalidates the cache)."""
    try:
        mtime = _DB.stat().st_mtime
    except OSError:
        return _load_db_messages_uncached()
    hit = _DB_MSG_CACHE.get(mtime)
    if hit is None:
        _DB_MSG_CACHE.clear()
        hit = _DB_MSG_CACHE[mtime] = _load_db_messages_uncached()
    return hit


def _load_db_messages_uncached() -> list[tuple[str, dict, str | None, list[dict]]]:
    """[(session_id, msg, session_directory, parts)] ordered by time.

    Supports both SQLite schemas:
      • New (``session_message``): ``type`` column (user/assistant/…), user
        text in ``data.text``, parts inlined in ``data.content``, model in
        ``data.model.id``. Rows are normalized to the legacy shape.
      • Legacy (``message`` + ``part``): blobs as in the old JSON storage.
    """
    con = _db_connect()
    try:
        cwd_map = _db_session_cwd_map(con)

        has_new = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='session_message'").fetchone() is not None
        if has_new:
            rows = con.execute(
                "SELECT type, session_id, data FROM session_message "
                "ORDER BY time_created, seq").fetchall()
            if rows:
                out = []
                for typ, sid, data in rows:
                    if typ not in ("user", "assistant"):
                        continue        # synthetic, model-switched, …
                    try:
                        m = json.loads(data)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    model = m.get("model")
                    if isinstance(model, dict) and model.get("id"):
                        m["modelID"] = model["id"]
                    parts = []
                    if typ == "user":
                        if m.get("text"):
                            parts.append({"type": "text", "text": m["text"]})
                    else:
                        for p in m.get("content") or []:
                            if not isinstance(p, dict):
                                continue
                            if p.get("type") == "tool":
                                st = p.get("state") or {}
                                t  = p.get("time") or {}
                                parts.append({
                                    "type": "tool",
                                    "tool": p.get("name") or "?",
                                    "state": {
                                        "status": st.get("status"),
                                        "input":  st.get("input"),
                                        "error":  st.get("error"),
                                        "time":   {"start": t.get("created"),
                                                   "end":   t.get("completed")
                                                            or t.get("ran")},
                                    },
                                })
                            else:
                                parts.append(p)
                    m["role"] = typ
                    out.append((sid, m, cwd_map.get(sid), parts))
                return out

        # Legacy in-DB schema (message + part tables)
        parts_by_msg: dict[str, list[dict]] = defaultdict(list)
        try:
            for mid, pdata in con.execute(
                    "SELECT message_id, data FROM part ORDER BY time_created, id"):
                try:
                    parts_by_msg[mid].append(json.loads(pdata))
                except (TypeError, json.JSONDecodeError):
                    continue
        except sqlite3.Error:
            pass
        out = []
        try:
            msg_rows = con.execute(
                "SELECT id, session_id, data FROM message "
                "ORDER BY time_created, id").fetchall()
        except sqlite3.Error:
            msg_rows = []          # neither session_message nor message present
        for mid, sid, data in msg_rows:
            try:
                msg = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                continue
            out.append((sid, msg, cwd_map.get(sid), parts_by_msg.get(mid, [])))
        return out
    finally:
        con.close()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ms_to_dt(ms) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _load_session_cwd_map() -> dict[str, str]:
    """Return {session_id: directory} from SQLite or legacy session/*.json."""
    if _DB.exists():
        con = _db_connect()
        try:
            out = _db_session_cwd_map(con)
            if out:
                return out
        finally:
            con.close()
    if not _SESS_BASE.exists():
        return {}
    out: dict[str, str] = {}
    for f in _SESS_BASE.rglob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = d.get("id")
        cwd = d.get("directory")
        if sid and cwd:
            out[sid] = cwd
    return out


def _iter_assistant_messages():
    """Yield (msg_dict, fallback_cwd) for every assistant message."""
    if _DB.exists():
        try:
            messages = _load_db_messages()
        except sqlite3.Error:
            messages = []
        for _sid, msg, directory, _parts in messages:
            if msg.get("role") == "assistant":
                yield msg, directory or "unknown"
        return
    if not _MSG_BASE.exists():
        return
    session_cwd = _load_session_cwd_map()
    for ses_dir in _MSG_BASE.iterdir():
        if not ses_dir.is_dir():
            continue
        fallback_cwd = session_cwd.get(ses_dir.name, "unknown")
        for f in ses_dir.iterdir():
            if not f.name.endswith(".json"):
                continue
            try:
                msg = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            yield msg, fallback_cwd


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_opencode() -> list[dict]:
    """Scan opencode assistant messages for token usage."""
    records = []
    for msg, fallback_cwd in _iter_assistant_messages():
        if msg.get("role") != "assistant":
            continue
        tok = msg.get("tokens") or {}
        if not isinstance(tok, dict):
            continue
        ts = _ms_to_dt((msg.get("time") or {}).get("created"))
        if ts is None:
            continue
        cache = tok.get("cache") or {}
        tokens = {
            "input":       int(tok.get("input", 0) or 0),
            "output":      int(tok.get("output", 0) or 0) + int(tok.get("reasoning", 0) or 0),
            "cache_read":  int(cache.get("read", 0) or 0),
            "cache_write": int(cache.get("write", 0) or 0),
        }
        if tokens["input"] == 0 and tokens["output"] == 0:
            continue
        model = msg.get("modelID") or "opencode-unknown"
        cwd = (msg.get("path") or {}).get("cwd") or fallback_cwd
        records.append({
            "tool":    "opencode",
            "model":   model,
            "project": cwd,
            "ts":      ts,
            **tokens,
            "cost":    compute_cost(tokens, model),
        })
    return records


def scan_speed_opencode() -> list[dict]:
    """Output speed (tokens/sec) from completed-created timestamps."""
    results = []
    for msg, _fallback in _iter_assistant_messages():
        if msg.get("role") != "assistant":
            continue
        t = msg.get("time") or {}
        start = _ms_to_dt(t.get("created"))
        end   = _ms_to_dt(t.get("completed"))
        if start is None or end is None:
            continue
        dt = (end - start).total_seconds()
        if dt < 0.5 or dt > 300:
            continue
        tok = msg.get("tokens") or {}
        if not isinstance(tok, dict):
            continue
        out = int(tok.get("output", 0) or 0) + int(tok.get("reasoning", 0) or 0)
        if out < 10:
            continue
        results.append({
            "tool":     "opencode",
            "model":    msg.get("modelID") or "opencode-unknown",
            "ts":       end,
            "tokens":   out,
            "duration": dt,
            "speed":    out / dt,
            "ttft":     None,
        })
    return results


# ─── Exchanges ────────────────────────────────────────────────────────────────

def _extract_exchanges_opencode() -> list[dict]:
    """Group user → assistant messages per session into exchanges."""
    if _DB.exists():
        return _extract_exchanges_db()
    if not _MSG_BASE.exists():
        return []

    session_cwd = _load_session_cwd_map()
    exchanges: list[dict] = []

    for ses_dir in sorted(_MSG_BASE.iterdir()):
        if not ses_dir.is_dir():
            continue
        msgs = []
        for f in ses_dir.iterdir():
            if not f.name.endswith(".json"):
                continue
            try:
                m = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            ts = _ms_to_dt((m.get("time") or {}).get("created"))
            if ts is None:
                continue
            m["_ts"] = ts
            msgs.append(m)
        msgs.sort(key=lambda m: m["_ts"])
        fallback_cwd = session_cwd.get(ses_dir.name, "unknown")

        current = None
        for m in msgs:
            role = m.get("role")
            cwd  = (m.get("path") or {}).get("cwd") or fallback_cwd
            if role == "user":
                if current:
                    exchanges.append(current)
                summary = (m.get("summary") or {}).get("title", "") or ""
                current = {
                    "session_id":      ses_dir.name,
                    "user_text":       summary.strip(),
                    "assistant_texts": [],
                    "tool_errors":     [],
                    "tools_used":      defaultdict(int),
                    "num_turns":       0,
                    "model":           m.get("model", {}).get("modelID", "") if isinstance(m.get("model"), dict) else "",
                    "project":         cwd,
                    "ts":              m["_ts"],
                    "tokens":          {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                    "cost":            0.0,
                }
            elif role == "assistant" and current is not None:
                current["num_turns"] += 1
                model = m.get("modelID") or ""
                if model:
                    current["model"] = model
                tok = m.get("tokens") or {}
                if isinstance(tok, dict):
                    cache = tok.get("cache") or {}
                    inp = int(tok.get("input", 0) or 0)
                    out = int(tok.get("output", 0) or 0) + int(tok.get("reasoning", 0) or 0)
                    cr  = int(cache.get("read", 0) or 0)
                    cw  = int(cache.get("write", 0) or 0)
                    current["tokens"]["input"]       += inp
                    current["tokens"]["output"]      += out
                    current["tokens"]["cache_read"]  += cr
                    current["tokens"]["cache_write"] += cw
                    current["cost"] += compute_cost(
                        {"input": inp, "output": out, "cache_read": cr, "cache_write": cw},
                        current["model"] or model,
                    )
        if current:
            exchanges.append(current)
    return exchanges


def _arg_value(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return default


def _extract_exchanges_db() -> list[dict]:
    """Exchange extraction from the SQLite backend (opencode.db).

    User prompt text and tool calls live in the `part` table; token usage
    and model info live in the `message` JSON blob."""
    exchanges: list[dict] = []
    current: dict | None = None
    last_sid: str | None = None

    for sid, m, directory, parts in _load_db_messages():
        if sid != last_sid:
            if current:
                exchanges.append(current)
            current = None
            last_sid = sid
        ts = _ms_to_dt((m.get("time") or {}).get("created"))
        if ts is None:
            continue
        role = m.get("role")
        cwd  = (m.get("path") or {}).get("cwd") or directory or "unknown"

        if role == "user":
            if current:
                exchanges.append(current)
            text = ""
            for p in parts:
                if p.get("type") == "text" and p.get("text"):
                    text = str(p["text"]).strip()
                    break
            current = {
                "session_id":      sid,
                "user_text":       text,
                "assistant_texts": [],
                "tool_errors":     [],
                "tools_used":      defaultdict(int),
                "num_turns":       0,
                "model":           (m.get("model") or {}).get("modelID", "")
                                   if isinstance(m.get("model"), dict) else "",
                "project":         cwd,
                "ts":              ts,
                "tokens":          {"input": 0, "output": 0,
                                    "cache_read": 0, "cache_write": 0},
                "cost":            0.0,
                "tool_calls":      [],
            }
        elif role == "assistant" and current is not None:
            current["num_turns"] += 1
            model = m.get("modelID") or ""
            if model:
                current["model"] = model
            tok = m.get("tokens") or {}
            if isinstance(tok, dict):
                cache = tok.get("cache") or {}
                inp = int(tok.get("input", 0) or 0)
                out = int(tok.get("output", 0) or 0) + int(tok.get("reasoning", 0) or 0)
                cr  = int(cache.get("read", 0) or 0)
                cw  = int(cache.get("write", 0) or 0)
                current["tokens"]["input"]       += inp
                current["tokens"]["output"]      += out
                current["tokens"]["cache_read"]  += cr
                current["tokens"]["cache_write"] += cw
                current["cost"] += compute_cost(
                    {"input": inp, "output": out,
                     "cache_read": cr, "cache_write": cw},
                    current["model"] or model,
                )
            for p in parts:
                ptype = p.get("type")
                if ptype == "text" and p.get("text"):
                    t = str(p["text"]).strip()
                    if t:
                        current["assistant_texts"].append(t)
                elif ptype == "tool":
                    name  = p.get("tool") or "?"
                    state = p.get("state") or {}
                    target = tool_target(name, state.get("input") or {})
                    if not target:
                        target = str(state.get("title") or "")
                    is_err = state.get("status") == "error"
                    call_ts = _ms_to_dt((state.get("time") or {}).get("start")) or ts
                    current["tool_calls"].append({
                        "ts": call_ts, "name": name,
                        "target": target, "error": is_err,
                    })
                    current["tools_used"][name] += 1
                    if is_err:
                        err = state.get("error")
                        msg = err.get("message", "") if isinstance(err, dict) else str(err or "")
                        current["tool_errors"].append(f"{name}: {msg}".strip())
    if current:
        exchanges.append(current)
    return exchanges


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    all_exchanges = []
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

    _add("opencode", _extract_exchanges_opencode())
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None,
         by_session: bool = False):
    print(f"\n{BOLD} Token Usage — opencode{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.local/share/opencode/...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not _DB.exists() and not _MSG_BASE.exists():
        print(f"  {DIM}opencode not found at {_BASE}{RESET}\n")
        return

    records = scan_opencode()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {MAGENTA}●{RESET} {'opencode':<12} {len(records):>6} records from ~/.local/share/opencode/")
    print()
    print_retention_alerts(["opencode"])
    print(f"  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    speed_records = scan_speed_opencode()
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges,
                         by_session=by_session)


# ─── CLI ──────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"opencode": "opencode", "open-code": "opencode"}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--activity", "--total", "--impact", "--by-session", "--audit", "--judge", "--model", "--judge-max", "--verify", "--ollama-judge", "--claude-judge", "--claude-model", "--codex-judge", "--codex-model", "--frontier-consensus", "--consensus-log", "--export", "--period", "--since", "--tool",
}


def _parse_tool(args: list[str]) -> str | None:
    if "--tool" not in args:
        return None
    idx = args.index("--tool")
    if idx + 1 >= len(args):
        return None
    raw = args[idx + 1].lower().strip()
    if raw in ("all", "tous", "*"):
        return None
    canonical = _TOOL_ALIASES.get(raw)
    if canonical:
        return canonical
    valid = ", ".join(sorted(set(_TOOL_ALIASES.values())))
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: {valid}")


def show_help():
    print(f"""
{BOLD}opencode-token-usage{RESET} — Aggregate and analyze opencode token consumption.

{BOLD}MODES{RESET}
  opencode-token-usage                            Aggregated overview
  opencode-token-usage --prompts  [-p]            Per-exchange detail
  opencode-token-usage --anomalies                Technical anomaly detection
  opencode-token-usage --activity                 Activity calendar (GitHub-style, by day)
  opencode-token-usage --total                    Compact totals (tokens + cost + data span)
  opencode-token-usage --impact                   Energy & CO₂ estimate (EcoLogits)
  opencode-token-usage --by-session               Overview + per-session table (all sessions)
  opencode-token-usage --plan                     Cost breakdown + optimization tips
  opencode-token-usage --export   [file.json]     Export all exchanges to JSON
  opencode-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}
  --period <period>    all, today, yesterday, year, or any "N unit" (e.g. "5 days", "31 days", "2 weeks", "3 months"); default: today

{BOLD}DATA SOURCE{RESET}
  {MAGENTA}opencode{RESET}    {DIM}~/.local/share/opencode/opencode.db (SQLite){RESET}
              {DIM}or legacy storage/message/{{session}}/{{msg}}.json{RESET}
              ✓ Tokens ✓ Speed ✓ Project (cwd) ✓ Tool calls
""")


def cli():
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print(f"tokstat {__version__}")
        return
    if "--help" in args or "-h" in args:
        show_help()
        return

    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"\n  {RED}Unknown option(s): {', '.join(unknown)}{RESET}")
        print(f"  Run {BOLD}opencode-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)
    try:
        tool = _parse_tool(args)
    except ValueError as e:
        print(f"\n  {RED}{e}{RESET}\n")
        sys.exit(1)

    if "--prompts" in args or "-p" in args:
        show_prompts(_collect_all_exchanges, period, tool)
    elif "--anomalies" in args:
        show_anomalies(_collect_all_exchanges, period, tool)
    elif "--activity" in args:
        show_activity(_collect_all_exchanges, period, tool)
    elif "--total" in args:
        show_total(_collect_all_exchanges, period, tool)
    elif "--impact" in args:
        show_impact(_collect_all_exchanges, period, tool, _parse_region(args))
    elif "--audit" in args:
        jmax_raw = _arg_value(args, "--judge-max")
        try:
            jmax = int(jmax_raw) if jmax_raw is not None else None
        except ValueError:
            jmax = None
        show_audit(_collect_all_exchanges, period, tool,
                   judge_model=_arg_value(args, "--model"), judge_max=jmax,
                   verify="--verify" in args,
                   ollama_judge="--ollama-judge" in args,
                   claude_judge="--claude-judge" in args,
                   claude_model=_arg_value(args, "--claude-model"),
                   codex_judge="--codex-judge" in args,
                   codex_model=_arg_value(args, "--codex-model"),
                   frontier_consensus=("--frontier-consensus" in args
                                       or "--consensus-log" in args),
                   consensus_log="--consensus-log" in args)
    elif "--plan" in args:
        show_plan(_collect_all_exchanges, period, tool)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(_collect_all_exchanges, out, period, tool)
    else:
        main(period, tool, by_session="--by-session" in args)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
