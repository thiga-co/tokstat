#!/usr/bin/env python3
"""
kiro-token-usage — Aggregate and display activity from Kiro.

Data sources:
  • per-session JSON (older Kiro IDE) under
      ~/Library/.../kiro.kiroagent/workspace-sessions/
        <base64(project path)>/<sessionId>.json   (history)
        <base64(project path)>/sessions.json       (dateCreated, project)
  • recent Kiro IDE no longer writes that store — it keeps session references
      (id + title) in each workspace's state.vscdb under
      `kiro.kiroAgent → sessionPanels.entries`; read as activity-only.
  • Kiro CLI (`kiro-cli`, the terminal agent) uses a SEPARATE store the IDE
      never touches:
      ~/.kiro/sessions/<hash>/sess_<uuid>/
        session.json    (id, title, workspacePaths, createdAt, modelId)
        messages.jsonl  (one {id, timestamp, payload} per line)

Kiro records no usable token counts — neither store has per-message tokens
(the IDE's tokens_generated log is always zero; the CLI meters in opaque
"credits" via usage_summary). So tokstat reports activity only —
prompts/turns — with token/cost left at 0 ([no tokens]). It does NOT
estimate, to avoid misleading figures.

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
    BOLD, DIM, RESET, YELLOW, RED, GREEN,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    print_retention_alerts,
)

TOOL_COLORS["Kiro"] = YELLOW

_KIRO_BASE = (Path.home() / "Library" / "Application Support" / "Kiro"
              / "User" / "globalStorage" / "kiro.kiroagent")
_SESSIONS_DIR = _KIRO_BASE / "workspace-sessions"
# Kiro CLI (kiro-cli) store — separate from the IDE's globalStorage above.
_KIRO_CLI_SESSIONS = Path.home() / ".kiro" / "sessions"


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_kiro() -> list[dict]:
    """One record per assistant turn. Kiro does not store real token counts
    (its tokens_generated log is always zero), so token/cost are left at 0
    rather than estimated — see _extract_exchanges_kiro."""
    records = []
    for ex in _extract_exchanges_kiro():
        if ex["ts"] is None:
            continue
        records.append({
            "tool":        "Kiro",
            "model":       ex["model"],
            "project":     ex["project"],
            "ts":          ex["ts"],
            "input":       ex["tokens"]["input"],
            "output":      ex["tokens"]["output"],
            "cache_read":  0,
            "cache_write": 0,
            "cost":        ex["cost"],
        })
    return records


def _session_text(content) -> str:
    """Flatten a Kiro history message content (str or list of parts)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text").strip()
    return ""


def _extract_exchanges_kiro() -> list[dict]:
    """Extract exchanges from Kiro's per-session JSON files.

    Modern Kiro stores each conversation at
      workspace-sessions/<base64(path)>/<sessionId>.json
    with a `history` array, `workspaceDirectory`, and a model title. The old
    {hash}/*.chat layout is gone. Kiro records no usable token counts
    (tokens_generated.jsonl has generatedTokens=0 and no per-message data),
    so we do NOT estimate — exchanges are counted as activity (prompts/turns)
    with token/cost left at 0, tagged [no tokens].
    """
    exchanges: list[dict] = []
    sessions_dir = _KIRO_BASE / "workspace-sessions"
    for ws_dir in (sorted(sessions_dir.iterdir()) if sessions_dir.exists() else []):
        if not ws_dir.is_dir():
            continue
        # Map sessionId -> (dateCreated_ms, workspaceDirectory, title)
        meta: dict[str, tuple] = {}
        sj = ws_dir / "sessions.json"
        if sj.exists():
            try:
                for s in json.loads(sj.read_text(errors="replace")):
                    sid = s.get("sessionId")
                    if sid:
                        meta[sid] = (int(s.get("dateCreated", 0) or 0),
                                     s.get("workspaceDirectory", "") or "unknown",
                                     s.get("title", ""))
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        for sess_file in ws_dir.glob("*.json"):
            if sess_file.name == "sessions.json":
                continue
            try:
                data = json.loads(sess_file.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            sid = data.get("sessionId") or sess_file.stem
            ms, wd, _title = meta.get(sid, (0, "", ""))
            project = data.get("workspaceDirectory") or wd or "unknown"
            ts = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms
                  else datetime.fromtimestamp(sess_file.stat().st_mtime, tz=timezone.utc))

            history = data.get("history") or []
            current = None
            for item in history:
                msg = item.get("message") or {}
                role = msg.get("role", "")
                text = _session_text(msg.get("content", ""))
                if role == "user":
                    if current:
                        exchanges.append(current)
                    current = {
                        "session_id":      sid,
                        "user_text":       text[:500],
                        "assistant_texts": [],
                        "tool_errors":     [],
                        "tools_used":      defaultdict(int),
                        "num_turns":       0,
                        "model":           "Kiro Agent [no tokens]",
                        "project":         project,
                        "ts":              ts,
                        "tokens":          {"input": 0, "output": 0,
                                            "cache_read": 0, "cache_write": 0},
                        "cost":            0.0,
                    }
                elif role == "assistant" and current is not None:
                    current["num_turns"] += 1
                    if text:
                        current["assistant_texts"].append(text[:500])
            if current:
                exchanges.append(current)

    # Kiro CLI (kiro-cli) keeps its own store under ~/.kiro/sessions — a store
    # the IDE never touches. Merge it in (activity-only, like everything here).
    exchanges += _extract_exchanges_kiro_cli()

    # Recent Kiro no longer writes the JSON store; merge in state.vscdb sessions.
    known_ids = {e["session_id"] for e in exchanges}
    exchanges += _extract_vscdb_sessions(known_ids)

    return [e for e in exchanges if e.get("user_text") or e["num_turns"] > 0]


def _parse_iso(s) -> datetime | None:
    """Parse an ISO-8601 timestamp (Kiro CLI uses a trailing Z)."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_exchanges_kiro_cli() -> list[dict]:
    """Kiro CLI (`kiro-cli`, the Amazon-Q-derived terminal agent) stores each
    conversation under ~/.kiro/sessions/<hash>/sess_<uuid>/ as:
        session.json    — id, title, workspacePaths/rootPaths, createdAt, modelId
        messages.jsonl  — one {id, timestamp, payload} per line; payload.type is
                          user / assistant / tool_call / tool_result / turn_* / …

    Like the IDE store, the CLI records NO per-message token counts (it meters
    in opaque "credits" via usage_summary, with contextUsage percentages only),
    so these are activity-only exchanges — token/cost left at 0, tagged
    [no tokens]. One exchange per user turn; assistant "Say" messages count as
    turns ("Reasoning" entries are skipped to avoid double-counting)."""
    root = _KIRO_CLI_SESSIONS
    if not root.exists():
        return []

    exchanges: list[dict] = []
    for ws_dir in sorted(root.iterdir()):
        if not ws_dir.is_dir():
            continue
        for sess_dir in sorted(ws_dir.iterdir()):
            if not sess_dir.is_dir():
                continue
            sj = sess_dir / "session.json"
            if not sj.exists():
                continue
            try:
                meta = json.loads(sj.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            sid = meta.get("id") or sess_dir.name
            paths = meta.get("workspacePaths") or meta.get("rootPaths") or []
            project = (paths[0] if paths else "") or "unknown"
            created = _parse_iso(meta.get("createdAt"))

            mfile = sess_dir / "messages.jsonl"
            if not mfile.exists():
                continue
            try:
                lines = mfile.read_text(errors="replace").splitlines()
            except OSError:
                continue

            current = None
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = rec.get("payload") or {}
                ptype = p.get("type")
                ts = _parse_iso(rec.get("timestamp")) or created
                if ptype == "user":
                    if current:
                        exchanges.append(current)
                    current = {
                        "session_id":      sid,
                        "user_text":       (p.get("content") or "")[:500],
                        "assistant_texts": [],
                        "tool_errors":     [],
                        "tools_used":      defaultdict(int),
                        "num_turns":       0,
                        "model":           "Kiro Agent [no tokens]",
                        "project":         project,
                        "ts":              ts or created,
                        "tokens":          {"input": 0, "output": 0,
                                            "cache_read": 0, "cache_write": 0},
                        "cost":            0.0,
                    }
                elif ptype == "assistant" and current is not None:
                    if p.get("operationType") == "Reasoning":
                        continue
                    current["num_turns"] += 1
                    txt = (p.get("content") or "").strip()
                    if txt:
                        current["assistant_texts"].append(txt[:500])
                elif ptype == "tool_call" and current is not None:
                    current["tools_used"][p.get("toolName") or "tool"] += 1
            if current:
                exchanges.append(current)

    # Non-interactive mode uses a flatter layout under sessions/cli/.
    exchanges += _extract_cli_flat()
    return exchanges


def _cli_flat_text(content) -> str:
    """Flatten a Kiro CLI flat-layout message `content` list to plain text
    (keep `text` parts, drop `thinking`/tool parts)."""
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("kind") == "text":
            d = c.get("data")
            if isinstance(d, str):
                parts.append(d)
    return " ".join(parts).strip()


def _extract_cli_flat() -> list[dict]:
    """Kiro CLI non-interactive mode (`kiro-cli chat --no-interactive`, as a
    script or subagent) writes a flatter layout than the TUI:
      ~/.kiro/sessions/cli/<uuid>.jsonl   — messages, one {version, kind, data}
                                            per line (kind Prompt / AssistantMessage)
      ~/.kiro/sessions/cli/<uuid>.json    — session_id, cwd, created_at, title,
                                            session_state.conversation_metadata
                                              .user_turn_metadatas[] (per-turn stats)

    The turn metadata DOES carry input_/output_token_count fields, but Kiro
    leaves them 0 and meters only in credits (metering_usage) — verified across
    models — so this stays activity-only, token/cost left at 0, [no tokens]."""
    flat_dir = _KIRO_CLI_SESSIONS / "cli"
    if not flat_dir.exists():
        return []

    exchanges: list[dict] = []
    for mfile in sorted(flat_dir.glob("*.jsonl")):
        sid = mfile.stem
        project = "unknown"
        created = None
        model = "Kiro Agent"
        meta_file = mfile.with_suffix(".json")
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(errors="replace"))
                sid = meta.get("session_id") or sid
                project = meta.get("cwd") or "unknown"
                created = _parse_iso(meta.get("created_at"))
                turns = (((meta.get("session_state") or {})
                          .get("conversation_metadata") or {})
                         .get("user_turn_metadatas") or [])
                for t in turns:
                    if t.get("model"):
                        model = t["model"]
                        break
            except (json.JSONDecodeError, OSError):
                pass
        model_label = f"{model} [no tokens]"

        try:
            lines = mfile.read_text(errors="replace").splitlines()
        except OSError:
            continue

        current = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("kind")
            data = rec.get("data") or {}
            ts = created
            m = data.get("meta") or {}
            if isinstance(m.get("timestamp"), (int, float)):
                try:
                    ts = datetime.fromtimestamp(m["timestamp"], tz=timezone.utc)
                except (OSError, ValueError):
                    ts = created
            if kind == "Prompt":
                if current:
                    exchanges.append(current)
                current = {
                    "session_id":      sid,
                    "user_text":       _cli_flat_text(data.get("content"))[:500],
                    "assistant_texts": [],
                    "tool_errors":     [],
                    "tools_used":      defaultdict(int),
                    "num_turns":       0,
                    "model":           model_label,
                    "project":         project,
                    "ts":              ts or created,
                    "tokens":          {"input": 0, "output": 0,
                                        "cache_read": 0, "cache_write": 0},
                    "cost":            0.0,
                }
            elif kind == "AssistantMessage" and current is not None:
                current["num_turns"] += 1
                txt = _cli_flat_text(data.get("content"))
                if txt:
                    current["assistant_texts"].append(txt[:500])
        if current:
            exchanges.append(current)
    return exchanges


def _vscdb_workspace_project(ws_dir: Path) -> str:
    """Project path for a workspaceStorage dir, from its workspace.json folder URI."""
    try:
        d = json.loads((ws_dir / "workspace.json").read_text(errors="replace"))
        folder = d.get("folder") or ""
        if folder.startswith("file://"):
            from urllib.parse import unquote
            return unquote(folder[len("file://"):])
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return "unknown"


def _extract_vscdb_sessions(known_ids: set) -> list[dict]:
    """Recent Kiro versions stopped writing the workspace-sessions JSON store;
    they keep only lightweight session references (id + title) in each
    workspace's state.vscdb, under `kiro.kiroAgent` → `sessionPanels.entries`.

    Surface those as **activity-only** exchanges — no turn detail, no tokens
    (Kiro exposes no usable token counts). The message content lives in a
    chromium leveldb we don't parse, so this is the honest maximum. Timestamp =
    the state.vscdb mtime (best available signal of last activity)."""
    ws_root = _KIRO_BASE.parent.parent / "workspaceStorage"
    if not ws_root.exists():
        return []

    exchanges: list[dict] = []
    seen: set[str] = set()
    for ws_dir in ws_root.iterdir():
        db = ws_dir / "state.vscdb"
        if not db.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT value FROM ItemTable WHERE key='kiro.kiroAgent'").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if not row:
            continue
        try:
            entries = (json.loads(row[0]) or {}).get("sessionPanels.entries") or []
        except (TypeError, json.JSONDecodeError):
            continue
        if not entries:
            continue
        try:
            ts = datetime.fromtimestamp(db.stat().st_mtime, tz=timezone.utc)
        except OSError:
            ts = None
        project = _vscdb_workspace_project(ws_dir)
        for e in entries:
            sid = e.get("id")
            if not sid or sid in seen:
                continue
            bare = sid[len("sess_"):] if sid.startswith("sess_") else sid
            if sid in known_ids or bare in known_ids:
                continue          # already read (richer) from the JSON store
            seen.add(sid)
            exchanges.append({
                "session_id":      sid,
                "user_text":       (e.get("title") or "")[:500],
                "assistant_texts": [],
                "tool_errors":     [],
                "tools_used":      defaultdict(int),
                "num_turns":       1,      # existence only; real count unknown
                "model":           "Kiro Agent [no tokens]",
                "project":         project,
                "ts":              ts,
                "tokens":          {"input": 0, "output": 0,
                                    "cache_read": 0, "cache_write": 0},
                "cost":            0.0,
            })
    return exchanges


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Collect Kiro exchanges filtered by time."""
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

    _add("Kiro", _extract_exchanges_kiro())
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None,
         by_session: bool = False):
    print(f"\n{BOLD} Token Usage — Kiro{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning Kiro data...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    _ws_storage = _KIRO_BASE.parent.parent / "workspaceStorage"
    if (not (_KIRO_BASE / "workspace-sessions").exists()
            and not _ws_storage.exists()
            and not _KIRO_CLI_SESSIONS.exists()):
        print(f"  {DIM}Kiro not found at {_KIRO_BASE} or {_KIRO_CLI_SESSIONS}{RESET}\n")
        return

    records = scan_kiro()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {YELLOW}●{RESET} {'Kiro':<12} {len(records):>6} records from {_KIRO_BASE.parent.parent}")
    print()
    print_retention_alerts(["Kiro"])
    print(f"  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, [], cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges,
                         by_session=by_session)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"kiro": "Kiro"}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--activity", "--total", "--impact", "--by-session", "--export", "--period", "--since", "--tool",
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
{BOLD}kiro-token-usage{RESET} — Aggregate and analyze Kiro token consumption.

{BOLD}MODES{RESET}
  kiro-token-usage                            Aggregated overview (period, project, model)
  kiro-token-usage --prompts  [-p]            Per-exchange detail (text, turns, tokens, cost)
  kiro-token-usage --anomalies                Technical anomaly detection
  kiro-token-usage --activity                 Activity calendar (GitHub-style, by day)
  kiro-token-usage --total                    Compact totals (tokens + cost + data span)
  kiro-token-usage --impact                   Energy & CO₂ estimate (EcoLogits)
  kiro-token-usage --by-session               Overview + per-session table (all sessions)
  kiro-token-usage --plan                     Cost breakdown + optimization tips
  kiro-token-usage --export   [file.json]     Export all exchanges to JSON
  kiro-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}
  --period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days", "3 months", "6 months", year

{BOLD}DATA SOURCE{RESET}
  {YELLOW}Kiro IDE{RESET}  {DIM}{_SESSIONS_DIR}/<project>/<sessionId>.json{RESET}
  {YELLOW}Kiro CLI{RESET}  {DIM}{_KIRO_CLI_SESSIONS}/<hash>/sess_<uuid>/messages.jsonl{RESET}
          {DIM}Kiro stores no token counts → activity only ([no tokens]){RESET}
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
        print(f"  Run {BOLD}kiro-token-usage --help{RESET} for usage.\n")
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
