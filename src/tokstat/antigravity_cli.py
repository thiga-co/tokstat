#!/usr/bin/env python3
"""
antigravity-token-usage — Aggregate and display token consumption from Google Antigravity.

Data sources:
- ~/.gemini/antigravity-cli/conversations/<uuid>.db : SQLite stores steps, exact token counts, and models.
- ~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript.jsonl : user prompts, assistant thoughts, tool calls.
- ~/.gemini/antigravity-cli/conversation_summaries.db : session titles and project workspace URIs.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tokstat.cli import __version__
from tokstat._core import (
    BOLD, DIM, RESET, BLUE, YELLOW, RED, CYAN,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact, show_tool_use, tool_target,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    print_retention_alerts,
)

TOOL_NAME = "Antigravity"
TOOL_COLORS[TOOL_NAME] = BLUE

_BASE = Path.home() / ".gemini" / "antigravity-cli"
_CONV_DIR = _BASE / "conversations"
_BRAIN_DIR = _BASE / "brain"
_SUMMARIES_DB = _BASE / "conversation_summaries.db"

_KNOWN_FLAGS = {
    "--prompts", "-p", "--anomalies", "--activity", "--total", "--impact",
    "--plan", "--by-session", "--tool-use", "--session", "--export",
    "--period", "--since", "--region", "--help", "-h", "--version", "-V",
}


# ─── Protobuf Decoder ────────────────────────────────────────────────────────

def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    res = 0
    shift = 0
    while True:
        b = data[offset]
        offset += 1
        res |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return res, offset


def _parse_proto(data: bytes) -> list[tuple[int, int, Any]]:
    """Lightweight pure-Python protobuf parser returning (field_number, wire_type, value)."""
    offset = 0
    fields = []
    length = len(data)
    while offset < length:
        try:
            tag, offset = _decode_varint(data, offset)
        except Exception:
            break
        wire_type = tag & 0x7
        field_num = tag >> 3
        if wire_type == 0:  # varint
            try:
                val, offset = _decode_varint(data, offset)
                fields.append((field_num, 0, val))
            except Exception:
                break
        elif wire_type == 2:  # length-delimited bytes
            try:
                l, offset = _decode_varint(data, offset)
                val = data[offset:offset + l]
                offset += l
                fields.append((field_num, 2, val))
            except Exception:
                break
        elif wire_type == 1:  # 64-bit
            offset += 8
        elif wire_type == 5:  # 32-bit
            offset += 4
        else:
            break
    return fields


def _get_time_from_proto(val: bytes) -> float | None:
    """Extract epoch timestamp in seconds from proto field [(1, sec), (2, nsec)]."""
    sec = 0
    nsec = 0
    for fn, wt, v in _parse_proto(val):
        if fn == 1 and wt == 0:
            sec = v
        elif fn == 2 and wt == 0:
            nsec = v
    if sec == 0:
        return None
    return sec + nsec / 1e9


# ─── Metadata & Project Resolution ───────────────────────────────────────────

def _load_summaries_meta() -> dict[str, dict]:
    """Load conversation metadata from conversation_summaries.db."""
    meta: dict[str, dict] = {}
    if not _SUMMARIES_DB.exists():
        return meta
    try:
        con = sqlite3.connect(f"file:{_SUMMARIES_DB}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT conversation_id, title, workspace_uris FROM conversation_summaries")
        for cid, title, uris in cur.fetchall():
            proj = "unknown"
            if uris:
                try:
                    u = json.loads(uris)
                    if isinstance(u, list) and u:
                        p = u[0]
                        if p.startswith("file://"):
                            p = p[7:]
                        proj = p
                except Exception:
                    pass
            meta[cid] = {"title": title, "project": proj}
        con.close()
    except Exception:
        pass
    return meta


def _extract_fallback_project(con: sqlite3.Connection) -> str:
    """Extract fallback project path from trajectory_metadata_blob if available."""
    try:
        cur = con.cursor()
        cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
        row = cur.fetchone()
        if row and row[0]:
            matches = re.findall(rb'file://([^\x00-\x1f\x7f-\xff\s"]+)', row[0])
            if matches:
                p = matches[0].decode("utf-8", "ignore")
                if p.endswith("z"):
                    p = p[:-1]
                return p
    except Exception:
        pass
    return "unknown"


def _normalize_model_name(name: str | None) -> str:
    """Normalize Antigravity model names/placeholders to canonical model identifiers."""
    if not name:
        return "gemini-unknown"
    n = name.strip()
    nl = n.lower()
    if "3.8" in nl and "flash" in nl:
        return "gemini-3.8-flash"
    if "3.7" in nl and "flash" in nl:
        return "gemini-3.7-flash"
    if "3.6" in nl and "flash" in nl:
        return "gemini-3.6-flash"
    if "3.5" in nl and "flash" in nl:
        return "gemini-3.5-flash"
    if "3-flash-a" in nl:
        return "gemini-2.5-flash"
    if "gemini-default" in nl:
        return "gemini-2.5-flash"
    if "sonnet-4-6" in nl or "sonnet 4.6" in nl:
        return "claude-sonnet-4-6"
    if "opus-4-6" in nl or "opus 4.6" in nl:
        return "claude-opus-4-6-thinking"
    return n


def _extract_gen_models(cur: sqlite3.Cursor) -> tuple[dict[int, str], dict[int, str]]:
    """Parse gen_metadata table to get {step_idx: model_name} and {model_enum: model_name}."""
    step_model: dict[int, str] = {}
    enum_model: dict[int, str] = {}
    try:
        cur.execute("SELECT data FROM gen_metadata")
        for (data,) in cur.fetchall():
            p = _parse_proto(data)
            model = None
            enum = None
            for fn, wt, val in p:
                if fn == 1 and wt == 2:
                    for sfn, swt, sval in _parse_proto(val):
                        if sfn == 19 and swt == 2:
                            try:
                                model = sval.decode("utf-8", "ignore")
                            except Exception:
                                pass
                        elif sfn == 21 and swt == 2 and not model:
                            try:
                                model = sval.decode("utf-8", "ignore")
                            except Exception:
                                pass
                        elif sfn == 3 and swt == 0:
                            enum = sval
            if enum is not None and model:
                enum_model[enum] = _normalize_model_name(model)
            for fn, wt, val in p:
                if fn == 2 and wt == 2:
                    off = 0
                    l = len(val)
                    while off < l:
                        sidx, off = _decode_varint(val, off)
                        if model:
                            step_model[sidx] = _normalize_model_name(model)
    except Exception:
        pass
    return step_model, enum_model


# ─── Scanners ────────────────────────────────────────────────────────────────

def _report_scan_health(total15: int, unparsed15: int, unpriced: set) -> None:
    """Fail loudly, not silently. The token metrics are read from protobuf
    blobs with no public schema, so a format change would otherwise just make
    tokens vanish. Warn (on stderr, so tables stay clean) when a large share of
    steps stop parsing, or when models resolve to names LiteLLM can't price."""
    if total15 >= 20 and unparsed15 / total15 > 0.5:
        print(f"  {YELLOW}⚠ Antigravity: {unparsed15}/{total15} steps had no "
              f"recognizable token structure — the on-disk format may have "
              f"changed; update tokstat or open an issue.{RESET}", file=sys.stderr)
    if unpriced:
        shown = ", ".join(sorted(unpriced)[:6])
        more = f" (+{len(unpriced) - 6} more)" if len(unpriced) > 6 else ""
        print(f"  {YELLOW}⚠ Antigravity: no LiteLLM price for {len(unpriced)} "
              f"model(s) — cost shown as $0: {shown}{more}.{RESET}", file=sys.stderr)


def scan_antigravity() -> list[dict]:
    """Scan Antigravity conversation SQLite databases for token usage."""
    if not _CONV_DIR.exists():
        return []

    summaries = _load_summaries_meta()
    records = []
    total15 = 0                 # step_type=15 rows carrying metadata
    unparsed15 = 0             # …where the token container (field 9) is absent
    unpriced: set[str] = set()  # models with output tokens but no LiteLLM price

    for db_path in sorted(_CONV_DIR.glob("*.db")):
        cid = db_path.stem
        meta_info = summaries.get(cid, {})
        project = meta_info.get("project", "unknown")

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = con.cursor()

            if project == "unknown":
                project = _extract_fallback_project(con)

            step_model, enum_model = _extract_gen_models(cur)

            cur.execute("SELECT idx, metadata FROM steps WHERE step_type = 15 AND metadata IS NOT NULL")
            for idx, meta in cur.fetchall():
                total15 += 1
                p = _parse_proto(meta)
                f9 = None
                t_sec = None
                for fn, wt, val in p:
                    if fn == 9 and wt == 2:
                        f9 = val
                    elif fn == 1 and wt == 2:
                        t_sec = _get_time_from_proto(val)

                if not f9:
                    unparsed15 += 1     # token container gone → likely drift
                    continue
                if t_sec is None:
                    continue

                tok_map = {fn: val for fn, wt, val in _parse_proto(f9) if wt == 0}
                inp = tok_map.get(2, 0)
                out = tok_map.get(3, 0)
                cached = tok_map.get(5, 0)
                menum = tok_map.get(1)

                if inp == 0 and out == 0:
                    continue

                raw_model = step_model.get(idx) or enum_model.get(menum) or "gemini-unknown"
                model = _normalize_model_name(raw_model)
                ts = datetime.fromtimestamp(t_sec, tz=timezone.utc)
                tokens = {
                    "input":       inp,
                    "output":      out,
                    "cache_read":  cached,
                    "cache_write": 0,
                }
                cost = compute_cost(tokens, model)
                if cost == 0 and out > 0:
                    unpriced.add(model)
                records.append({
                    "tool":    TOOL_NAME,
                    "model":   model,
                    "project": project,
                    "ts":      ts,
                    **tokens,
                    "cost":    cost,
                })

            con.close()
        except (sqlite3.Error, OSError):
            continue

    _report_scan_health(total15, unparsed15, unpriced)
    return records


def scan_speed_antigravity() -> list[dict]:
    """Extract generation speed (tokens/sec) from Antigravity steps."""
    if not _CONV_DIR.exists():
        return []

    results = []

    for db_path in sorted(_CONV_DIR.glob("*.db")):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = con.cursor()

            step_model, enum_model = _extract_gen_models(cur)

            cur.execute("SELECT idx, step_type, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx")
            prev_ts = None

            for idx, stype, meta in cur.fetchall():
                p = _parse_proto(meta)
                m = {fn: val for fn, wt, val in p}
                t = _get_time_from_proto(m[1]) if 1 in m else None
                if not t:
                    continue

                if stype == 15 and 9 in m and prev_ts is not None:
                    p9 = _parse_proto(m[9])
                    tok_map = {fn: val for fn, wt, val in p9 if wt == 0}
                    out = tok_map.get(3, 0)
                    menum = tok_map.get(1)
                    model = step_model.get(idx) or enum_model.get(menum) or "gemini-unknown"

                    dt = t - prev_ts
                    if 0.5 < dt < 300 and out >= 10:
                        ts = datetime.fromtimestamp(t, tz=timezone.utc)
                        results.append({
                            "tool":     TOOL_NAME,
                            "model":    model,
                            "ts":       ts,
                            "tokens":   out,
                            "duration": dt,
                            "speed":    out / dt,
                            "ttft":     None,
                        })

                prev_ts = t

            con.close()
        except (sqlite3.Error, OSError):
            continue

    return results


# ─── Exchanges ────────────────────────────────────────────────────────────────

def _extract_exchanges_antigravity() -> list[dict]:
    """Extract conversation exchanges, tool calls, and per-turn token usage."""
    if not _CONV_DIR.exists():
        return []

    summaries = _load_summaries_meta()
    exchanges: list[dict] = []

    for db_path in sorted(_CONV_DIR.glob("*.db")):
        cid = db_path.stem
        meta_info = summaries.get(cid, {})
        project = meta_info.get("project", "unknown")

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = con.cursor()

            if project == "unknown":
                project = _extract_fallback_project(con)

            step_model, enum_model = _extract_gen_models(cur)

            # Map step_idx -> token breakdown
            step_tokens: dict[int, dict] = {}
            cur.execute("SELECT idx, metadata FROM steps WHERE step_type = 15 AND metadata IS NOT NULL")
            for idx, meta in cur.fetchall():
                p = _parse_proto(meta)
                m = {fn: val for fn, wt, val in p}
                if 9 not in m:
                    continue
                tok_map = {fn: val for fn, wt, val in _parse_proto(m[9]) if wt == 0}
                inp = tok_map.get(2, 0)
                out = tok_map.get(3, 0)
                cached = tok_map.get(5, 0)
                menum = tok_map.get(1)
                model = step_model.get(idx) or enum_model.get(menum) or "gemini-unknown"
                step_tokens[idx] = {
                    "input": inp, "output": out, "cache_read": cached, "cache_write": 0,
                    "model": model,
                }

            con.close()
        except (sqlite3.Error, OSError):
            continue

        transcript_path = _BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if not transcript_path.exists():
            transcript_path = _BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript_full.jsonl"

        if not transcript_path.exists():
            continue

        current = None
        try:
            with open(transcript_path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    stype = d.get("type")
                    sidx = d.get("step_index")
                    ts_str = d.get("created_at")
                    ts = None
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except (ValueError, AttributeError):
                            pass

                    if stype == "USER_INPUT":
                        if current:
                            if current.get("ts") and current.get("last_ts"):
                                dur = (current["last_ts"] - current["ts"]).total_seconds()
                                if dur >= 0:
                                    current["duration_s"] = dur
                            exchanges.append(current)
                        content = d.get("content", "")
                        m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
                        user_text = m.group(1).strip() if m else content.strip()

                        current = {
                            "session_id":      cid,
                            "user_text":       user_text,
                            "assistant_texts": [],
                            "tool_errors":     [],
                            "tools_used":      defaultdict(int),
                            "tool_calls":      [],
                            "num_turns":       0,
                            "model":           None,
                            "project":         project,
                            "ts":              ts,
                            "last_ts":         ts,
                            "context_peak":    0,
                            "tokens":          {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                            "cost":            0.0,
                        }

                    elif stype == "PLANNER_RESPONSE" and current is not None:
                        current["num_turns"] += 1
                        if ts:
                            current["last_ts"] = ts
                        tok = step_tokens.get(sidx)
                        if tok:
                            if not current["model"] or current["model"] == "gemini-unknown":
                                current["model"] = tok["model"]
                            current["tokens"]["input"]      += tok["input"]
                            current["tokens"]["output"]     += tok["output"]
                            current["tokens"]["cache_read"] += tok["cache_read"]
                            current["cost"] += compute_cost(tok, tok["model"])
                            ctx = tok.get("input", 0) + tok.get("cache_read", 0)
                            if ctx > current.get("context_peak", 0):
                                current["context_peak"] = ctx

                        content = d.get("content")
                        if content:
                            current["assistant_texts"].append(content)

                        # Tool calls
                        for tc in d.get("tool_calls") or []:
                            if not isinstance(tc, dict):
                                continue
                            name = tc.get("name", "tool")
                            current["tools_used"][name] += 1
                            args = tc.get("args") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {}

                            target = (
                                args.get("TargetFile") or args.get("AbsolutePath") or
                                args.get("CommandLine") or args.get("SearchPath") or
                                args.get("SearchDirectory") or args.get("Pattern") or
                                args.get("Query") or args.get("Url") or
                                tool_target(name, args)
                            )
                            if isinstance(target, str):
                                target = target.strip('"\'')

                            current["tool_calls"].append({
                                "name":   name,
                                "target": target,
                                "ts":     ts,
                                "error":  False,
                            })

                    elif current is not None and (d.get("status") == "ERROR" or d.get("error")):
                        err = d.get("content") or d.get("error") or ""
                        current["tool_errors"].append(str(err)[:200])
                        if current["tool_calls"]:
                            current["tool_calls"][-1]["error"] = True

            if current:
                if current.get("ts") and current.get("last_ts"):
                    dur = (current["last_ts"] - current["ts"]).total_seconds()
                    if dur >= 0:
                        current["duration_s"] = dur
                exchanges.append(current)

        except (OSError, IOError):
            continue

    return exchanges


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    all_exchanges = []
    tool_counts: dict[str, int] = {}

    def _add(tname: str, exs: list[dict]):
        if tool_filter and tname != tool_filter:
            return
        filtered = [ex for ex in exs
                    if ex.get("ts") and ex["ts"] >= cutoff
                    and (cutoff_end is None or ex["ts"] < cutoff_end)]
        for ex in filtered:
            ex["tool"] = tname
        if filtered:
            all_exchanges.extend(filtered)
            tool_counts[tname] = tool_counts.get(tname, 0) + len(filtered)

    _add(TOOL_NAME, _extract_exchanges_antigravity())
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── CLI Entrypoint ──────────────────────────────────────────────────────────

def _arg_value(args: list[str], flag: str, default=None) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return default


def main(period_name: str | None = None, tool_filter: str | None = None,
         by_session: bool = False):
    print(f"\n{BOLD} Token Usage — {TOOL_NAME}{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.gemini/antigravity-cli/...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not _BASE.exists():
        print(f"  {DIM}Antigravity not found at {_BASE}{RESET}\n")
        return

    records = scan_antigravity()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {BLUE}●{RESET} {TOOL_NAME:<12} {len(records):>6} records from ~/.gemini/antigravity-cli/")
    print()
    print_retention_alerts([TOOL_NAME])
    print(f"  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    speed_records = scan_speed_antigravity()
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)

    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges,
                         by_session=by_session)


def show_help():
    print(f"""
{BOLD}antigravity-token-usage{RESET} — Aggregate and analyze Google Antigravity token consumption.

{BOLD}MODES{RESET}
  antigravity-token-usage                       Aggregated overview (period, project, model, speed)
  antigravity-token-usage --prompts  [-p]       Per-exchange detail (text, turns, tokens, tools, cost)
  antigravity-token-usage --anomalies           Technical anomaly detection
  antigravity-token-usage --activity            Activity calendar (GitHub-style, by day)
  antigravity-token-usage --total               Compact totals (tokens + cost + data span)
  antigravity-token-usage --impact              Energy & CO₂ estimate (EcoLogits)
  antigravity-token-usage --by-session          Overview + per-session table (all sessions)
  antigravity-token-usage --tool-use            Timeline of tool calls (file/command + time)
  antigravity-token-usage --plan                Cost breakdown + optimization tips
  antigravity-token-usage --export   [file.json] Export all exchanges to JSON
  antigravity-token-usage --help     [-h]       This help

{BOLD}FILTERS{RESET}
  --period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days", "3 months", "6 months", year
  --session <id>       Filter by conversation UUID or prefix (for --tool-use / --prompts)

{BOLD}DATA SOURCES{RESET}
  {BLUE}Antigravity{RESET}   {DIM}~/.gemini/antigravity-cli/conversations/<uuid>.db{RESET}
                {DIM}~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript.jsonl{RESET}
                {DIM}~/.gemini/antigravity-cli/conversation_summaries.db{RESET}
                ✓ Tokens ✓ Text ✓ Tools ✓ Speed
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
        print(f"  Run {BOLD}antigravity-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)

    if "--prompts" in args or "-p" in args:
        show_prompts(_collect_all_exchanges, period, TOOL_NAME)
    elif "--anomalies" in args:
        show_anomalies(_collect_all_exchanges, period, TOOL_NAME)
    elif "--activity" in args:
        show_activity(_collect_all_exchanges, period, TOOL_NAME)
    elif "--total" in args:
        show_total(_collect_all_exchanges, period, TOOL_NAME)
    elif "--impact" in args:
        show_impact(_collect_all_exchanges, period, TOOL_NAME, _parse_region(args))
    elif "--tool-use" in args:
        show_tool_use(_collect_all_exchanges, period, TOOL_NAME,
                      session_filter=_arg_value(args, "--session"))
    elif "--plan" in args:
        show_plan(_collect_all_exchanges, period, TOOL_NAME)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(_collect_all_exchanges, out, period, TOOL_NAME)
    else:
        main(period, TOOL_NAME, by_session="--by-session" in args)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
