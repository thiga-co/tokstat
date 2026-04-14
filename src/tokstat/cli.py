#!/usr/bin/env python3
"""
claude-token-usage — Aggregate and display token consumption from Claude Code.

Scans ~/.claude/projects/ JSONL transcripts to extract token usage data and estimates costs.
"""

from __future__ import annotations

__version__ = "1.1.0"

import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Pricing (loaded dynamically from LiteLLM) ─────────────────────────────
# Source: https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
LITELLM_CACHE_PATH = Path.home() / ".cache" / "token-usage" / "litellm_prices.json"
LITELLM_CACHE_MAX_AGE = timedelta(hours=24)

# Populated at startup by load_pricing()
PRICING: dict[str, dict] = {}


def load_pricing():
    """Load model pricing from LiteLLM's model_prices JSON.

    Tries a local cache first (refreshed every 24h), then fetches from GitHub.
    Falls back to an empty dict if both fail (costs will show as $0).
    """
    global PRICING
    raw = None

    # Try cache
    if LITELLM_CACHE_PATH.exists():
        age = datetime.now() - datetime.fromtimestamp(LITELLM_CACHE_PATH.stat().st_mtime)
        if age < LITELLM_CACHE_MAX_AGE:
            try:
                raw = json.loads(LITELLM_CACHE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    # Fetch from GitHub
    if raw is None:
        try:
            req = urllib.request.Request(LITELLM_PRICING_URL, headers={"User-Agent": "claude-token-usage/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode())
            # Write cache
            LITELLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            LITELLM_CACHE_PATH.write_text(json.dumps(raw))
        except Exception:
            pass

    # Fallback: try stale cache
    if raw is None and LITELLM_CACHE_PATH.exists():
        try:
            raw = json.loads(LITELLM_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if raw is None:
        print(f"  {DIM}Warning: could not load LiteLLM pricing data, costs will show as $0{RESET}")
        return

    # Normalize into our internal format: key -> {input, output, cache_read, cache_write}
    # LiteLLM uses cost-per-token; we store cost-per-token too for direct multiplication.
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        inp = entry.get("input_cost_per_token")
        out = entry.get("output_cost_per_token")
        if inp is None and out is None:
            continue
        PRICING[key.lower()] = {
            "input":       float(inp or 0),
            "output":      float(out or 0),
            "cache_read":  float(entry.get("cache_read_input_token_cost") or 0),
            "cache_write": float(entry.get("cache_creation_input_token_cost") or 0),
        }


# ─── ANSI colors ─────────────────────────────────────────────────────────────
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
MAGENTA = "\033[35m"
WHITE  = "\033[97m"

BLUE = "\033[34m"
# Bright variants for better readability on dark backgrounds
BRED    = "\033[91m"
BYELLOW = "\033[93m"

TOOL_COLORS = {
    "Claude Code": CYAN,
}


# ─── Data structures ─────────────────────────────────────────────────────────

def empty_bucket():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def add_bucket(a, b):
    return {k: a[k] + b[k] for k in a}


# ─── Pricing helpers ─────────────────────────────────────────────────────────

ZERO_PRICE = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def match_model(model_name: str) -> dict:
    """Find the best pricing match for a model name in the LiteLLM data.

    LiteLLM keys use formats like "claude-opus-4-6", "openai/gpt-4o",
    "gemini/gemini-2.5-pro", etc. We try multiple matching strategies.
    """
    if not model_name or not PRICING:
        return ZERO_PRICE
    name = model_name.lower().split("[")[0].strip()  # strip [1m] suffixes

    # 1. Exact match
    if name in PRICING:
        return PRICING[name]

    # 2. Try with common provider prefixes that LiteLLM uses
    for prefix in ["", "openai/", "anthropic/", "gemini/", "vertex_ai/",
                    "deepseek/", "together_ai/", "fireworks_ai/"]:
        candidate = prefix + name
        if candidate in PRICING:
            return PRICING[candidate]

    # 3. Suffix match: find keys that end with our model name
    for key, val in PRICING.items():
        if key.endswith("/" + name) or key == name:
            return val

    # 4. Best substring match: find the longest PRICING key contained in name
    #    (or name contained in key), to handle version suffixes like -20250929
    best_key = None
    best_len = 0
    for key in PRICING:
        # Skip very short keys that would match too broadly
        if len(key) < 5:
            continue
        # Strip provider prefix from key for comparison
        bare_key = key.split("/")[-1] if "/" in key else key
        if bare_key in name and len(bare_key) > best_len:
            best_key = key
            best_len = len(bare_key)
        elif name in bare_key and len(name) > best_len:
            best_key = key
            best_len = len(name)

    if best_key:
        return PRICING[best_key]

    return ZERO_PRICE


def compute_cost(tokens: dict, model: str) -> float:
    """Compute cost in USD from token counts and model name.

    LiteLLM prices are per-token (not per-1M), so we multiply directly.
    """
    p = match_model(model)
    cost = 0.0
    cost += tokens.get("input", 0) * p["input"]
    cost += tokens.get("output", 0) * p["output"]
    cost += tokens.get("cache_read", 0) * p["cache_read"]
    cost += tokens.get("cache_write", 0) * p["cache_write"]
    return cost


# ─── Period helpers ──────────────────────────────────────────────────────────

def period_boundaries() -> dict:
    """Return named periods as {name: (start, end)} tuples.

    end is None for open-ended periods (up to now), or a datetime for
    bounded periods like "Yesterday".
    """
    now = datetime.now(timezone.utc)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "Last hour":    (now - timedelta(hours=1),           None),
        "Last 5 hours": (now - timedelta(hours=5),           None),
        "Today":        (today_midnight,                     None),
        "Yesterday":    (today_midnight - timedelta(days=1), today_midnight),
        "Last 7 days":  (now - timedelta(days=7),            None),
        "Last 30 days": (now - timedelta(days=30),           None),
        "Last year":    (now - timedelta(days=365),          None),
    }


def resolve_period(period_name: str | None, default: str = "today") -> tuple[datetime, datetime | None, str]:
    """Resolve a period name to a (cutoff_start, cutoff_end, display_name) tuple.

    cutoff_end is None for open-ended periods (up to now), or a datetime for
    bounded periods like "Yesterday".
    """
    if period_name is None and default == "all":
        return datetime.min.replace(tzinfo=timezone.utc), None, "All time"
    boundaries = period_boundaries()
    name = period_name or default
    if name.lower() in ("all", "tout"):
        return datetime.min.replace(tzinfo=timezone.utc), None, "All time"
    for bname, (start, end) in boundaries.items():
        if name.lower() in bname.lower():
            return start, end, bname
    valid = ", ".join(list(boundaries.keys()) + ["all"])
    raise ValueError(f"Unknown period '{name}'. Available: {valid}")


def classify_periods(ts: datetime, boundaries: dict) -> list[str]:
    """Return list of period names this timestamp falls into."""
    result = []
    for name, bounds in boundaries.items():
        if isinstance(bounds, tuple):
            start, end = bounds
        else:
            start, end = bounds, None
        if ts >= start and (end is None or ts < end):
            result.append(name)
    return result


# ─── Project normalization ────────────────────────────────────────────────────

import re


def normalize_project(path: str) -> str:
    """Return the project path as-is (no worktree resolution needed for Claude Code)."""
    return path


def _warm_worktree_cache(project_paths):
    """No-op: worktree resolution is not needed for Claude Code."""
    pass


# ─── Scanners ────────────────────────────────────────────────────────────────

def decode_project_dir(dirname: str) -> str:
    """Convert Claude Code project dir name back to a path.

    Encoding: '---' = literal dash, single '-' = path separator '/'.
    We use a placeholder to avoid collision during replacement.
    """
    PLACEHOLDER = "\x00DASH\x00"
    return dirname.replace("---", PLACEHOLDER).replace("-", "/").replace(PLACEHOLDER, "-")


def scan_claude_code() -> list[dict]:
    """Scan Claude Code JSONL transcripts for token usage.

    Claude Code writes one JSONL line per content block (thinking, text,
    tool_use), each carrying the cumulative usage for the API call so far.
    We deduplicate by message.id, keeping only the last record per API call
    (which has the final token counts).
    """
    records = []
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return records
    for proj_dir in base.iterdir():
        if not proj_dir.is_dir():
            continue
        project = decode_project_dir(proj_dir.name)
        for jsonl_file in proj_dir.glob("*.jsonl"):
            try:
                prev_msg_id = None
                pending = None
                with open(jsonl_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = rec.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        ts_str = rec.get("timestamp")
                        if not ts_str:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except (ValueError, AttributeError):
                            continue
                        msg_id = msg.get("id", "")
                        model = msg.get("model", "unknown")
                        speed_mode = usage.get("speed", "")
                        if speed_mode and speed_mode != "standard":
                            model = f"{model} [{speed_mode}]"
                        rec_project = rec.get("cwd") or project
                        entry = {
                            "tool":    "Claude Code",
                            "model":   model,
                            "project": rec_project,
                            "ts":      ts,
                            "input":       usage.get("input_tokens", 0),
                            "output":      usage.get("output_tokens", 0),
                            "cache_read":  usage.get("cache_read_input_tokens", 0),
                            "cache_write": usage.get("cache_creation_input_tokens", 0),
                        }
                        if msg_id and msg_id == prev_msg_id:
                            # Same API call: keep latest (highest output tokens)
                            pending = entry
                        else:
                            if pending:
                                pending["cost"] = compute_cost(pending, pending["model"])
                                records.append(pending)
                            pending = entry
                        prev_msg_id = msg_id
                if pending:
                    pending["cost"] = compute_cost(pending, pending["model"])
                    records.append(pending)
            except (OSError, IOError):
                continue
    return records


def scan_speed_claude_code() -> list[dict]:
    """Extract output speed (tokens/sec) from Claude Code JSONL transcripts.

    Approach: for each user message followed by assistant messages, compute
    output_tokens / duration. Multi-message turns use first→last assistant
    timestamps; single-message turns use user→assistant.
    """
    results = []
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return results
    for proj_dir in base.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            try:
                msgs = []
                with open(jsonl_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = rec.get("message", {})
                        if not isinstance(msg, dict) or not msg.get("role"):
                            continue
                        ts_str = rec.get("timestamp")
                        if not ts_str:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except (ValueError, AttributeError):
                            continue
                        usage = msg.get("usage") or {}
                        model = msg.get("model", "unknown")
                        speed_mode = usage.get("speed", "")
                        if speed_mode and speed_mode != "standard":
                            model = f"{model} [{speed_mode}]"
                        msgs.append({
                            "role":   msg["role"],
                            "ts":     ts,
                            "output": usage.get("output_tokens", 0),
                            "model":  model,
                        })

                # Walk through messages: find user → assistant(s) sequences
                for i, m in enumerate(msgs):
                    if m["role"] != "user":
                        continue
                    assistants = []
                    for j in range(i + 1, len(msgs)):
                        if msgs[j]["role"] == "assistant":
                            assistants.append(msgs[j])
                        else:
                            break
                    if not assistants:
                        continue
                    last = assistants[-1]
                    if last["output"] < 10:
                        continue  # skip trivial responses
                    if len(assistants) >= 2:
                        dt = (last["ts"] - assistants[0]["ts"]).total_seconds()
                        ttft = (assistants[0]["ts"] - m["ts"]).total_seconds()
                    else:
                        dt = (last["ts"] - m["ts"]).total_seconds()
                        ttft = dt  # can't separate TTFT from generation
                    if dt < 0.5 or dt > 300:
                        continue  # filter outliers
                    results.append({
                        "tool":    "Claude Code",
                        "model":   last["model"],
                        "ts":      last["ts"],
                        "tokens":  last["output"],
                        "duration": dt,
                        "speed":   last["output"] / dt,
                        "ttft":    ttft if len(assistants) >= 2 else None,
                    })
            except (OSError, IOError):
                continue
    return results


def fmt_tokens(n: int) -> str:
    """Format token count with K/M suffix."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(c: float) -> str:
    """Format cost in USD."""
    if c >= 1.0:
        return f"${c:.2f}"
    if c >= 0.01:
        return f"${c:.3f}"
    if c > 0:
        return f"${c:.4f}"
    return "$0.00"


def _strip_ansi(text: str) -> str:
    import re
    return re.sub(r'\033\[[0-9;]*m', '', text)


def calc_table_width(headers: list[str], rows: list[list[str]]) -> int:
    """Calculate the visible width of a table (including 2-char indent)."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(_strip_ansi(cell)))
    return 2 + sum(widths) + 2 * (len(widths) - 1)


def print_table(headers: list[str], rows: list[list[str]], col_aligns: list[str] | None = None) -> int:
    """Print a formatted ASCII table. Returns the visible width of the table."""
    if not rows:
        return 0
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(_strip_ansi(cell)))

    if col_aligns is None:
        col_aligns = ["<"] * len(headers)

    def pad(text, width, align):
        padding = width - len(_strip_ansi(text))
        if align == ">":
            return " " * padding + text
        return text + " " * padding

    table_width = 2 + sum(widths) + 2 * (len(widths) - 1)

    # Header
    header_line = "  ".join(pad(h, widths[i], col_aligns[i]) for i, h in enumerate(headers))
    print(f"  {BOLD}{header_line}{RESET}")
    sep = "  ".join("─" * w for w in widths)
    print(f"  {DIM}{sep}{RESET}")

    for row in rows:
        line = "  ".join(pad(row[i], widths[i], col_aligns[i]) for i in range(len(headers)))
        print(f"  {line}")

    return table_width


def shorten_path(path: str, max_len: int = 40) -> str:
    """Shorten a path for display."""
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


# ─── Main ────────────────────────────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage Aggregator{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning local AI coding tool data...{RESET}\n")

    # Resolve period filter
    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    # Scan Claude Code
    scanners = {
        "Claude Code": (scan_claude_code, "~/.claude/"),
    }

    all_records = []
    tools_found = []
    tools_missing = []

    for tool_name, (scanner, data_path) in scanners.items():
        if tool_filter and tool_name != tool_filter:
            continue
        expanded = Path(data_path.replace("~", str(Path.home())))
        if not expanded.exists():
            tools_missing.append(tool_name)
            continue
        records = scanner()
        # Apply period filter
        records = [r for r in records if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]
        color = TOOL_COLORS.get(tool_name, "")
        if records:
            tools_found.append((tool_name, len(records)))
            all_records.extend(records)
            print(f"  {color}●{RESET} {tool_name:<12} {len(records):>6} records from {data_path}")
        else:
            tools_found.append((tool_name, 0))

    if tools_missing:
        print(f"\n  {DIM}Not installed: {', '.join(tools_missing)}{RESET}")

    filter_info = [f"Period: {BOLD}{period_label}{RESET}"]
    if tool_filter:
        color = TOOL_COLORS.get(tool_filter, "")
        filter_info.append(f"Tool: {color}{BOLD}{tool_filter}{RESET}")
    print(f"\n  {'  '.join(filter_info)}")

    if not all_records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    # ─── 1. Consumption by period ────────────────────────────────────────
    boundaries = period_boundaries()
    if period_label == "All time":
        # Show all standard periods
        period_order = ["Last hour", "Last 5 hours", "Today", "Yesterday", "Last 7 days", "Last 30 days", "Last year"]
    else:
        # Single period mode
        period_order = [period_label]
        boundaries = {period_label: (cutoff, cutoff_end)}

    # Per-tool, per-period aggregation
    # tool -> period -> bucket
    tool_period = defaultdict(lambda: defaultdict(empty_bucket))
    period_totals = defaultdict(empty_bucket)

    for rec in all_records:
        periods = classify_periods(rec["ts"], boundaries)
        for period in periods:
            b = tool_period[rec["tool"]][period]
            b["input"]       += rec["input"]
            b["output"]      += rec["output"]
            b["cache_read"]  += rec["cache_read"]
            b["cache_write"] += rec["cache_write"]
            b["cost"]        += rec["cost"]
            t = period_totals[period]
            t["input"]       += rec["input"]
            t["output"]      += rec["output"]
            t["cache_read"]  += rec["cache_read"]
            t["cache_write"] += rec["cache_write"]
            t["cost"]        += rec["cost"]

    active_tools = sorted(set(r["tool"] for r in all_records))

    headers = ["Period", "Tool", "Input", "Output", "Cache R", "Cache W", "Cost"]
    aligns  = ["<",      "<",    ">",     ">",      ">",       ">",       ">"]
    rows = []

    for period in period_order:
        first = True
        for tool in active_tools:
            b = tool_period[tool].get(period)
            if not b or (b["input"] == 0 and b["output"] == 0):
                continue
            color = TOOL_COLORS.get(tool, "")
            rows.append([
                f"{BOLD}{period}{RESET}" if first else "",
                f"{color}{tool}{RESET}",
                fmt_tokens(b["input"]),
                fmt_tokens(b["output"]),
                fmt_tokens(b["cache_read"]),
                fmt_tokens(b["cache_write"]),
                fmt_cost(b["cost"]),
            ])
            first = False
        # Period total
        t = period_totals.get(period)
        if t and (t["input"] > 0 or t["output"] > 0):
            rows.append([
                f"{BOLD}{period}{RESET}" if first else "",
                f"{BOLD}TOTAL{RESET}",
                f"{BOLD}{fmt_tokens(t['input'])}{RESET}",
                f"{BOLD}{fmt_tokens(t['output'])}{RESET}",
                f"{BOLD}{fmt_tokens(t['cache_read'])}{RESET}",
                f"{BOLD}{fmt_tokens(t['cache_write'])}{RESET}",
                f"{BOLD}{fmt_cost(t['cost'])}{RESET}",
            ])
            rows.append([""] * 7)  # spacer

    w = calc_table_width(headers, rows)
    print(f"\n{'─' * w}")
    print(f"{BOLD} CONSUMPTION BY PERIOD{RESET}")
    print(f"{'─' * w}")
    print_table(headers, rows, aligns)

    # ─── 2. Consumption by project ───────────────────────────────────────
    # ─── 2. Consumption by project ───────────────────────────────────────
    _warm_worktree_cache(set(r["project"] for r in all_records))

    # project -> tool -> bucket
    proj_tool = defaultdict(lambda: defaultdict(empty_bucket))
    proj_totals = defaultdict(empty_bucket)

    for rec in all_records:
        p = normalize_project(rec["project"])
        b = proj_tool[p][rec["tool"]]
        b["input"]       += rec["input"]
        b["output"]      += rec["output"]
        b["cache_read"]  += rec["cache_read"]
        b["cache_write"] += rec["cache_write"]
        b["cost"]        += rec["cost"]
        t = proj_totals[p]
        t["input"]       += rec["input"]
        t["output"]      += rec["output"]
        t["cache_read"]  += rec["cache_read"]
        t["cache_write"] += rec["cache_write"]
        t["cost"]        += rec["cost"]

    # Sort projects by total cost descending
    sorted_projects = sorted(proj_totals.keys(), key=lambda p: proj_totals[p]["cost"], reverse=True)

    headers = ["Project", "Tool", "Input", "Output", "Cache R", "Cache W", "Cost"]
    aligns  = ["<",       "<",    ">",     ">",      ">",       ">",       ">"]
    rows = []

    for proj in sorted_projects:
        first = True
        short = shorten_path(proj, 38)
        for tool in active_tools:
            b = proj_tool[proj].get(tool)
            if not b or (b["input"] == 0 and b["output"] == 0):
                continue
            color = TOOL_COLORS.get(tool, "")
            rows.append([
                f"{BOLD}{short}{RESET}" if first else "",
                f"{color}{tool}{RESET}",
                fmt_tokens(b["input"]),
                fmt_tokens(b["output"]),
                fmt_tokens(b["cache_read"]),
                fmt_tokens(b["cache_write"]),
                fmt_cost(b["cost"]),
            ])
            first = False
        t = proj_totals[proj]
        rows.append([
            f"{BOLD}{short}{RESET}" if first else "",
            f"{BOLD}TOTAL{RESET}",
            f"{BOLD}{fmt_tokens(t['input'])}{RESET}",
            f"{BOLD}{fmt_tokens(t['output'])}{RESET}",
            f"{BOLD}{fmt_tokens(t['cache_read'])}{RESET}",
            f"{BOLD}{fmt_tokens(t['cache_write'])}{RESET}",
            f"{BOLD}{fmt_cost(t['cost'])}{RESET}",
        ])
        rows.append([""] * 7)

    w = calc_table_width(headers, rows)
    print(f"\n{'─' * w}")
    print(f"{BOLD} CONSUMPTION BY PROJECT{RESET}")
    print(f"{'─' * w}")
    print_table(headers, rows, aligns)

    # ─── 3. Model breakdown ──────────────────────────────────────────────
    model_data = defaultdict(lambda: {"input": 0, "output": 0, "cost": 0.0, "tool": ""})
    for rec in all_records:
        m = model_data[rec["model"]]
        m["input"]  += rec["input"]
        m["output"] += rec["output"]
        m["cost"]   += rec["cost"]
        m["tool"]    = rec["tool"]

    sorted_models = sorted(model_data.keys(), key=lambda m: model_data[m]["cost"], reverse=True)

    headers = ["Model", "Tool", "Input", "Output", "Cost"]
    aligns  = ["<",     "<",    ">",     ">",      ">"]
    rows = []
    for model in sorted_models:
        d = model_data[model]
        if d["input"] == 0 and d["output"] == 0:
            continue
        color = TOOL_COLORS.get(d["tool"], "")
        rows.append([
            model,
            f"{color}{d['tool']}{RESET}",
            fmt_tokens(d["input"]),
            fmt_tokens(d["output"]),
            fmt_cost(d["cost"]),
        ])

    total_cost = sum(d["cost"] for d in model_data.values())
    total_in   = sum(d["input"] for d in model_data.values())
    total_out  = sum(d["output"] for d in model_data.values())
    rows.append([
        f"{BOLD}ALL MODELS{RESET}",
        "",
        f"{BOLD}{fmt_tokens(total_in)}{RESET}",
        f"{BOLD}{fmt_tokens(total_out)}{RESET}",
        f"{BOLD}{fmt_cost(total_cost)}{RESET}",
    ])

    w = calc_table_width(headers, rows)
    print(f"\n{'─' * w}")
    print(f"{BOLD} COST BY MODEL{RESET}")
    print(f"{'─' * w}")
    print_table(headers, rows, aligns)

    # ─── 4. Speed analysis ──────────────────────────────────────────────
    speed_records = scan_speed_claude_code()
    speed_records = [sr for sr in speed_records if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]
    if tool_filter:
        speed_records = [sr for sr in speed_records if sr["tool"] == tool_filter]
    if speed_records:
        # Group by model
        speed_by_model = defaultdict(list)
        for sr in speed_records:
            speed_by_model[(sr["model"], sr["tool"])].append(sr)

        headers = ["Model", "Tool", "Samples", "Median", "Avg", "P10", "P90"]
        aligns  = ["<",     "<",    ">",       ">",      ">",   ">",   ">"]
        rows = []

        for (model, tool), samples in sorted(speed_by_model.items(), key=lambda x: -len(x[1])):
            speeds = sorted(s["speed"] for s in samples)
            n = len(speeds)
            median = speeds[n // 2]
            avg = sum(speeds) / n
            p10 = speeds[max(0, n // 10)]
            p90 = speeds[min(n - 1, n * 9 // 10)]
            color = TOOL_COLORS.get(tool, "")
            rows.append([
                model,
                f"{color}{tool}{RESET}",
                str(n),
                f"{median:.0f} t/s",
                f"{avg:.0f} t/s",
                f"{p10:.0f} t/s",
                f"{p90:.0f} t/s",
            ])

        w = calc_table_width(headers, rows)
        print(f"\n{'─' * w}")
        print(f"{BOLD} OUTPUT SPEED (tokens/sec){RESET}")
        print(f"{'─' * w}")
        print_table(headers, rows, aligns)

    # ─── Grand total ─────────────────────────────────────────────────────
    total_all_tokens = sum(r["input"] + r["output"] + r["cache_read"] + r["cache_write"] for r in all_records)
    print(f"\n  {BOLD}Grand total:{RESET} {fmt_tokens(total_all_tokens)} tokens across {len(all_records)} API calls")
    print(f"  {BOLD}Estimated cost:{RESET} {fmt_cost(total_cost)}")
    print(f"  {DIM}Period: {all_records[0]['ts'].strftime('%Y-%m-%d')} to {max(r['ts'] for r in all_records).strftime('%Y-%m-%d')}{RESET}")
    print()


def scan_claude_sessions() -> list[dict]:
    """Scan Claude Code JSONL transcripts and return per-prompt breakdowns.

    Returns a list of session dicts, each containing:
    - project, slug, session_id, prompts: list of prompt dicts
    Each prompt dict: text, ts, model, tokens (input/output/cache_read/cache_write),
                      cost, tools (Counter of tool names), num_turns
    """
    sessions = []
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return sessions

    for proj_dir in base.iterdir():
        if not proj_dir.is_dir():
            continue
        project = decode_project_dir(proj_dir.name)
        for jsonl_file in proj_dir.glob("*.jsonl"):
            # Skip subagent files
            if "/subagents/" in str(jsonl_file):
                continue
            try:
                with open(jsonl_file, "r", errors="replace") as f:
                    lines = []
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            lines.append(json.loads(raw))
                        except json.JSONDecodeError:
                            continue
            except (OSError, IOError):
                continue

            if not lines:
                continue

            # Extract session metadata
            session_id = None
            slug = None
            cwd = None
            for rec in lines:
                if not session_id:
                    session_id = rec.get("sessionId")
                if not slug:
                    slug = rec.get("slug")
                if not cwd:
                    cwd = rec.get("cwd")

            # Group messages by promptId to form prompt exchanges
            # A "prompt" = user message + all assistant turns until next user message
            prompts = []
            current_prompt = None

            for rec in lines:
                rec_type = rec.get("type")
                msg = rec.get("message", {})
                if not isinstance(msg, dict):
                    msg = {}
                content = msg.get("content", "")

                # Detect user prompt (not tool result)
                if rec_type == "user":
                    is_tool_result = False
                    if isinstance(content, list):
                        is_tool_result = any(
                            isinstance(c, dict) and c.get("type") == "tool_result"
                            for c in content
                        )
                    if not is_tool_result:
                        # Save previous prompt
                        if current_prompt:
                            prompts.append(current_prompt)
                        # Extract text
                        text = ""
                        if isinstance(content, str):
                            text = content.strip()
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c.get("text", "").strip()
                                    break
                        ts_str = rec.get("timestamp")
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else None
                        except (ValueError, AttributeError):
                            ts = None
                        current_prompt = {
                            "text": text,
                            "ts": ts,
                            "model": None,
                            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                            "cost": 0.0,
                            "tools": defaultdict(int),
                            "num_turns": 0,
                        }

                # Accumulate assistant data into current prompt
                elif rec_type == "assistant" and current_prompt is not None:
                    usage = msg.get("usage")
                    if usage:
                        current_prompt["num_turns"] += 1
                        model = msg.get("model", "unknown")
                        if not current_prompt["model"]:
                            current_prompt["model"] = model
                        tokens = {
                            "input":       usage.get("input_tokens", 0),
                            "output":      usage.get("output_tokens", 0),
                            "cache_read":  usage.get("cache_read_input_tokens", 0),
                            "cache_write": usage.get("cache_creation_input_tokens", 0),
                        }
                        for k in tokens:
                            current_prompt["tokens"][k] += tokens[k]
                        current_prompt["cost"] += compute_cost(tokens, model)

                    # Count tool uses
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "tool_use":
                                tool_name = c.get("name", "unknown")
                                current_prompt["tools"][tool_name] += 1

            # Don't forget last prompt
            if current_prompt:
                prompts.append(current_prompt)

            if prompts:
                sessions.append({
                    "project": cwd or project,
                    "slug": slug,
                    "session_id": session_id or jsonl_file.stem,
                    "file": str(jsonl_file),
                    "prompts": prompts,
                })

    return sessions


def show_prompts(period_name: str | None = None, tool_filter: str | None = None):
    """Show per-prompt/exchange token usage for Claude Code."""
    print(f"\n{BOLD} Exchanges — Prompt-level Usage{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning Claude Code exchanges...{RESET}\n")

    # Determine time filter
    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return
    print(f"  Period: {BOLD}{period_label}{RESET}\n")

    # Collect exchanges from Claude Code
    all_exchanges, tool_counts = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    if not all_exchanges:
        print(f"  {YELLOW}No exchanges found.{RESET}\n")
        return

    # Warm worktree cache for project normalization
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))

    # Group exchanges by (tool, project)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for ex in all_exchanges:
        key = (ex.get("tool", "Unknown"), ex.get("project", "unknown"))
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(ex)

    # Sort by total cost per group descending
    sorted_groups = sorted(grouped.items(),
                          key=lambda x: sum(e.get("cost", 0) for e in x[1]),
                          reverse=True)

    for (tool, project), exchanges in sorted_groups:
        proj_display = shorten_path(normalize_project(project), 50)
        tool_color = TOOL_COLORS.get(tool, "")
        total_cost = sum(e["cost"] for e in exchanges)
        total_turns = sum(e.get("num_turns", 0) for e in exchanges)

        print(f"  {tool_color}{BOLD}{tool}{RESET} {DIM}{proj_display}{RESET}  {CYAN}{len(exchanges)} exchanges{RESET}  {total_turns} turns  {BOLD}{fmt_cost(total_cost)}{RESET}")

        headers = ["#", "Time", "Input text", "Model", "Turns", "Input", "Output", "Cache R", "Cache W", "Tools", "Cost"]
        aligns  = [">", "<",    "<",          "<",     ">",     ">",     ">",      ">",       ">",       "<",     ">"]
        rows = []

        for i, ex in enumerate(sorted(exchanges, key=lambda e: e.get("ts") or datetime.min.replace(tzinfo=timezone.utc)), 1):
            # Truncate user text
            user_text = ex.get("user_text", "").replace("\n", " ")
            if len(user_text) > 50:
                user_text = user_text[:47] + "..."
            if not user_text:
                user_text = DIM + "(no text)" + RESET

            ts_str = ex["ts"].strftime("%H:%M") if ex.get("ts") else "?"
            model_short = (ex.get("model") or "?").split("/")[-1]
            if len(model_short) > 20:
                model_short = model_short[:17] + "..."

            # Format tools summary
            tools = ex.get("tools_used", {})
            if tools:
                tool_parts = []
                for tname in sorted(tools, key=lambda t: -tools[t]):
                    tool_parts.append(f"{tname}:{tools[tname]}" if tools[tname] > 1 else tname)
                tools_str = " ".join(tool_parts[:4])
                if len(tools) > 4:
                    tools_str += f" +{len(tools)-4}"
            else:
                tools_str = DIM + "-" + RESET

            tok = ex.get("tokens", {})
            rows.append([
                str(i),
                ts_str,
                user_text,
                DIM + model_short + RESET,
                str(ex.get("num_turns", 0)),
                fmt_tokens(tok.get("input", 0)),
                fmt_tokens(tok.get("output", 0)),
                fmt_tokens(tok.get("cache_read", 0)),
                fmt_tokens(tok.get("cache_write", 0)),
                tools_str,
                fmt_cost(ex.get("cost", 0)),
            ])

        print_table(headers, rows, aligns)
        print()




def _extract_exchanges(jsonl_path: str) -> list[dict]:
    """Parse a JSONL transcript into a list of exchanges.

    Each exchange: {user_text, assistant_texts: [str], tool_errors: [str],
                    assistant_contradicts_self: bool, ts}
    """
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            lines = []
            for raw in f:
                raw = raw.strip()
                if raw:
                    try:
                        lines.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
    except (OSError, IOError):
        return []

    exchanges = []
    current = None

    for rec in lines:
        rec_type = rec.get("type")
        msg = rec.get("message", {})
        if not isinstance(msg, dict):
            msg = {}
        content = msg.get("content", "")

        if rec_type == "user":
            is_tool_result = False
            if isinstance(content, list):
                is_tool_result = any(
                    isinstance(c, dict) and c.get("type") == "tool_result"
                    for c in content
                )
            if not is_tool_result:
                if current:
                    exchanges.append(current)
                text = ""
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "").strip()
                            break
                ts_str = rec.get("timestamp")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else None
                except (ValueError, AttributeError):
                    ts = None
                current = {
                    "user_text": text,
                    "assistant_texts": [],
                    "tool_errors": [],
                    "tools_used": defaultdict(int),
                    "num_turns": 0,
                    "model": None,
                    "project": rec.get("cwd"),
                    "ts": ts,
                    "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                    "cost": 0.0,
                }
            elif current and isinstance(content, list):
                # Collect tool errors
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                        err = c.get("content", "")
                        if isinstance(err, list):
                            err = " ".join(str(e) for e in err)
                        current["tool_errors"].append(str(err)[:200])

        elif rec_type == "assistant" and current is not None:
            if not current["model"]:
                current["model"] = msg.get("model")
            usage = msg.get("usage")
            if usage:
                # Deduplicate by message.id: Claude Code writes one JSONL line
                # per content block, each with cumulative usage for the API call.
                # Keep only the last record per message.id (final token counts).
                msg_id = msg.get("id", "")
                prev_id = current.get("_prev_msg_id")
                if msg_id and msg_id == prev_id:
                    # Same API call: update tokens to latest values (not additive)
                    current["tokens"]["input"]       = current["_prev_tokens"]["input"]      + usage.get("input_tokens", 0)
                    current["tokens"]["output"]      = current["_prev_tokens"]["output"]     + usage.get("output_tokens", 0)
                    current["tokens"]["cache_read"]  = current["_prev_tokens"]["cache_read"] + usage.get("cache_read_input_tokens", 0)
                    current["tokens"]["cache_write"] = current["_prev_tokens"]["cache_write"]+ usage.get("cache_creation_input_tokens", 0)
                    current["cost"] = current["_prev_cost"] + compute_cost({
                        "input": usage.get("input_tokens", 0),
                        "output": usage.get("output_tokens", 0),
                        "cache_read": usage.get("cache_read_input_tokens", 0),
                        "cache_write": usage.get("cache_creation_input_tokens", 0),
                    }, msg.get("model", ""))
                else:
                    # New API call: save checkpoint and add tokens
                    current["_prev_msg_id"] = msg_id
                    current["_prev_tokens"] = dict(current["tokens"])
                    current["_prev_cost"] = current["cost"]
                    current["num_turns"] += 1
                    current["tokens"]["input"]      += usage.get("input_tokens", 0)
                    current["tokens"]["output"]     += usage.get("output_tokens", 0)
                    current["tokens"]["cache_read"] += usage.get("cache_read_input_tokens", 0)
                    current["tokens"]["cache_write"]+= usage.get("cache_creation_input_tokens", 0)
                    current["cost"] += compute_cost({
                        "input": usage.get("input_tokens", 0),
                        "output": usage.get("output_tokens", 0),
                        "cache_read": usage.get("cache_read_input_tokens", 0),
                        "cache_write": usage.get("cache_creation_input_tokens", 0),
                    }, msg.get("model", ""))
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            t = c.get("text", "").strip()
                            if t:
                                current["assistant_texts"].append(t)
                        elif c.get("type") == "tool_use":
                            current["tools_used"][c.get("name", "unknown")] += 1

    if current:
        exchanges.append(current)
    for ex in exchanges:
        ex.pop("_prev_msg_id", None)
        ex.pop("_prev_tokens", None)
        ex.pop("_prev_cost", None)
    return exchanges


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None, cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Collect exchanges from all supported tools, filtered by cutoff time and tool.

    Tags each exchange with a 'tool' key.
    Returns (all_exchanges, {tool_name: count}).
    """
    all_exchanges = []
    tool_counts = {}

    def _add(tool_name, exchanges):
        if tool_filter and tool_name != tool_filter:
            return
        filtered = [ex for ex in exchanges if ex["ts"] and ex["ts"] >= cutoff and (cutoff_end is None or ex["ts"] < cutoff_end)]
        for ex in filtered:
            ex["tool"] = tool_name
        if filtered:
            all_exchanges.extend(filtered)
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + len(filtered)

    # ── Claude Code ──
    base = Path.home() / ".claude" / "projects"
    if base.exists():
        for proj_dir in base.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl_file in proj_dir.glob("*.jsonl"):
                if "/subagents/" in str(jsonl_file):
                    continue
                _add("Claude Code", _extract_exchanges(str(jsonl_file)))

    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))

    return all_exchanges, tool_counts


def show_anomalies(period_name: str | None = None, tool_filter: str | None = None):
    """Detect technical anomalies in Claude Code sessions."""
    print(f"\n{BOLD} Technical Anomaly Detection{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning Claude Code transcripts...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return
    label = f"  Period: {BOLD}{period_label}{RESET}"
    if tool_filter:
        color = TOOL_COLORS.get(tool_filter, "")
        label += f"  Tool: {color}{BOLD}{tool_filter}{RESET}"
    print(label + "\n")
    all_prompts, tool_counts = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)

    # Filter to exchanges that have token data
    all_prompts = [p for p in all_prompts if p.get("tokens") and p["ts"]]

    if not all_prompts:
        print(f"  {YELLOW}No usage data found.{RESET}\n")
        return

    # Print scan summary
    tools_with_tokens = defaultdict(int)
    for p in all_prompts:
        if p["tokens"]["input"] > 0 or p["tokens"]["output"] > 0 or p["cost"] > 0:
            tools_with_tokens[p.get("tool", "?")] += 1
    for tool_name, count in sorted(tools_with_tokens.items(), key=lambda x: -x[1]):
        color = TOOL_COLORS.get(tool_name, "")
        print(f"  {color}●{RESET} {tool_name:<12} {count:>5} exchanges with token data")
    print()

    # Only keep exchanges with actual token data
    all_prompts = [p for p in all_prompts if p["tokens"]["input"] > 0 or p["tokens"]["output"] > 0 or p["cost"] > 0]

    if not all_prompts:
        print(f"  {YELLOW}No token data found in exchanges.{RESET}\n")
        return

    # Compute stats for thresholds
    costs = [p["cost"] for p in all_prompts if p["cost"] > 0]
    turns = [p["num_turns"] for p in all_prompts if p["num_turns"] > 0]
    tool_call_counts = [sum(p.get("tools_used", {}).values()) for p in all_prompts]

    median_cost = sorted(costs)[len(costs) // 2] if costs else 0
    median_turns = sorted(turns)[len(turns) // 2] if turns else 1
    p90_cost = sorted(costs)[min(len(costs) - 1, len(costs) * 9 // 10)] if costs else 0
    p90_turns = sorted(turns)[min(len(turns) - 1, len(turns) * 9 // 10)] if turns else 1

    _warm_worktree_cache(set(p.get("project") or "unknown" for p in all_prompts))

    # anomaly tuple: (severity, type, detail, project, tool, model, ts, prompt_preview)
    anomalies = []

    for p in all_prompts:
        tool_name = p.get("tool", "?")
        model = p.get("model") or "?"
        project = normalize_project(p.get("project") or "unknown")
        ts = p["ts"]
        prompt_short = p["user_text"].replace("\n", " ")[:50]
        if len(p["user_text"]) > 50:
            prompt_short += "..."

        tok = p["tokens"]
        total_tools = sum(p.get("tools_used", {}).values())

        def _add(sev, atype, detail):
            anomalies.append((sev, atype, detail, project, tool_name, model, ts, prompt_short))

        # 1. Runaway cost — prompt costs 10x+ the P90
        if p["cost"] > 0 and p90_cost > 0 and p["cost"] > p90_cost * 10:
            _add("high", "Runaway cost", f"{fmt_cost(p['cost'])} ({p['cost']/median_cost:.0f}x median)")

        # 2. High cost — prompt costs 5x+ the P90
        elif p["cost"] > 0 and p90_cost > 0 and p["cost"] > p90_cost * 5:
            _add("medium", "High cost", f"{fmt_cost(p['cost'])} ({p['cost']/median_cost:.0f}x median)")

        # 3. Tool call storm
        if total_tools > 30:
            _add("high" if total_tools > 60 else "medium", "Tool storm", f"{total_tools} tool calls")

        # 4. Turn spiral
        if p["num_turns"] > 0 and p90_turns > 0 and p["num_turns"] > p90_turns * 5:
            _add("high" if p["num_turns"] > p90_turns * 10 else "medium", "Turn spiral",
                 f"{p['num_turns']} turns ({p['num_turns']/median_turns:.0f}x median)")

        # 5. Cache thrashing
        if tok["cache_write"] > 50_000 and tok["cache_read"] < tok["cache_write"] * 0.5:
            ratio = tok["cache_read"] / tok["cache_write"] if tok["cache_write"] > 0 else 0
            _add("medium", "Cache thrashing", f"{fmt_tokens(tok['cache_write'])} written, only {ratio:.0%} read back")

        # 6. Context bloat
        if tok["input"] > 10_000 and tok["output"] > 0 and tok["input"] / tok["output"] > 50:
            _add("low", "Context bloat",
                 f"{fmt_tokens(tok['input'])} in / {fmt_tokens(tok['output'])} out (ratio {tok['input']/tok['output']:.0f}:1)")

        # 7. Empty exchange
        if p["num_turns"] > 5 and tok["output"] < 100:
            _add("medium", "Empty exchange", f"{p['num_turns']} turns but only {tok['output']} output tokens")

    if not anomalies:
        print(f"  {DIM}No anomalies detected.{RESET}")
        print(f"  {DIM}Stats: {len(all_prompts)} exchanges, median cost {fmt_cost(median_cost)}, P90 cost {fmt_cost(p90_cost)}{RESET}\n")
        return

    # Print stats
    print(f"  {DIM}{len(all_prompts)} exchanges analyzed — median cost {fmt_cost(median_cost)}, P90 {fmt_cost(p90_cost)}, median turns {median_turns}, P90 turns {p90_turns}{RESET}\n")

    # Group by project, then by anomaly type within each project
    by_project = defaultdict(list)
    for a in anomalies:
        by_project[a[3]].append(a)  # a[3] = project

    # Sort projects by worst severity then count
    def _proj_sort_key(proj_items):
        proj, items = proj_items
        worst = min(_SEVERITY_ORDER.get(a[0], 9) for a in items)
        return (worst, -len(items))

    for proj, items in sorted(by_project.items(), key=_proj_sort_key):
        proj_short = shorten_path(proj, 45)
        high = sum(1 for a in items if a[0] == "high")
        med  = sum(1 for a in items if a[0] == "medium")
        low  = sum(1 for a in items if a[0] == "low")
        parts = []
        if high: parts.append(f"{BRED}{high} high{RESET}")
        if med:  parts.append(f"{BYELLOW}{med} med{RESET}")
        if low:  parts.append(f"{DIM}{low} low{RESET}")

        print(f"  {BOLD}{proj_short}{RESET}  ({', '.join(parts)})")

        # Sub-group by anomaly type
        by_type = defaultdict(list)
        for a in items:
            by_type[a[1]].append(a)

        for atype in sorted(by_type, key=lambda t: min(_SEVERITY_ORDER.get(a[0], 9) for a in by_type[t])):
            type_items = by_type[atype]
            type_items.sort(key=lambda a: _SEVERITY_ORDER.get(a[0], 9))
            print(f"    {DIM}{atype} ({len(type_items)}){RESET}")
            for sev, _, detail, _, tool_name, model, ts, prompt in type_items:
                sev_color = _SEVERITY_COLORS.get(sev, "")
                tool_color = TOOL_COLORS.get(tool_name, "")
                model_short = model.split("/")[-1]
                if len(model_short) > 20:
                    model_short = model_short[:17] + "..."
                ts_str = ts.strftime("%m-%d %H:%M") if ts else "?"
                print(f"      {sev_color}[{sev.upper():6s}]{RESET} {tool_color}{tool_name}{RESET} {DIM}{model_short}{RESET}  {ts_str}  {detail}")
                if prompt:
                    print(f"      {DIM}         {prompt}{RESET}")
        print()

    # Summary
    print(f"  {'─' * 60}")
    total = len(anomalies)
    high_t = sum(1 for a in anomalies if a[0] == "high")
    med_t  = sum(1 for a in anomalies if a[0] == "medium")
    low_t  = sum(1 for a in anomalies if a[0] == "low")
    print(f"  {BOLD}{total} anomalies{RESET} across {BOLD}{len(by_project)} projects{RESET}: {BRED}{high_t} high{RESET}, {BYELLOW}{med_t} med{RESET}, {DIM}{low_t} low{RESET}")
    print()


# ─── Plan recommendation ────────────────────────────────────────────────────

# Known plan tiers (monthly cost, included API credit equivalent)
_PLAN_TIERS = [
    ("Free",             0,     5),
    ("Pro",             20,    18),
    ("Max (5x)",       100,   100),
    ("Max (20x)",      200,   200),
    ("Team",            30,    30),
    ("Team + Max (5x)", 130,  130),
    ("Enterprise",     None,  None),  # custom
]


def show_plan(period_name: str | None = None, tool_filter: str | None = None):
    """Recommend plan and optimization strategies based on usage patterns."""
    print(f"\n{BOLD} Plan & Optimization Recommendations{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning usage data...{RESET}\n")

    label_parts = []
    if tool_filter:
        color = TOOL_COLORS.get(tool_filter, "")
        label_parts.append(f"Tool: {color}{BOLD}{tool_filter}{RESET}")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return
    label_parts.append(f"Period: {BOLD}{period_label}{RESET}")
    boundaries = {period_label: (cutoff, cutoff_end)}
    now = datetime.now(timezone.utc)

    if label_parts:
        print(f"  {'  '.join(label_parts)}\n")

    # Collect analysis data across periods
    analysis = {}

    for pname, (p_cutoff, p_cutoff_end) in boundaries.items():
        # Use exchanges for the filtered tool (or all)
        all_exs, _ = _collect_all_exchanges(p_cutoff, tool_filter, p_cutoff_end)
        # Only keep exchanges with token data
        period_exs = [e for e in all_exs if e.get("tokens") and e["ts"]
                      and (e["tokens"]["input"] > 0 or e["tokens"]["output"] > 0 or e.get("cost", 0) > 0)]
        if not period_exs:
            continue

        total_cost = sum(e.get("cost", 0) for e in period_exs)
        total_input = sum(e["tokens"]["input"] for e in period_exs)
        total_output = sum(e["tokens"]["output"] for e in period_exs)
        total_cache_r = sum(e["tokens"]["cache_read"] for e in period_exs)
        total_cache_w = sum(e["tokens"]["cache_write"] for e in period_exs)
        # Use actual data span (first→last exchange) for "all time", otherwise cutoff→now
        first_ts = min(e["ts"] for e in period_exs)
        last_ts = max(e["ts"] for e in period_exs)
        data_span = (last_ts - first_ts).days
        period_span = ((p_cutoff_end or now) - p_cutoff).days
        days_span = max(1, min(data_span, period_span) if data_span > 0 else period_span)
        daily_cost = total_cost / days_span
        monthly_projected = daily_cost * 30
        daily_output = total_output / days_span
        api_calls = len(period_exs)
        daily_calls = api_calls / days_span
        active_days = len(set(e["ts"].strftime("%Y-%m-%d") for e in period_exs))
        models = set(e.get("model") or "?" for e in period_exs)
        cache_ratio = total_cache_r / (total_cache_r + total_cache_w) if (total_cache_r + total_cache_w) > 0 else 0

        # Per-model cost breakdown
        model_costs = defaultdict(float)
        model_calls = defaultdict(int)
        for e in period_exs:
            m = e.get("model") or "?"
            model_costs[m] += e.get("cost", 0)
            model_calls[m] += 1

        # Cost distribution
        daily_costs_map = defaultdict(float)
        for e in period_exs:
            daily_costs_map[e["ts"].strftime("%Y-%m-%d")] += e.get("cost", 0)
        sorted_daily = sorted(daily_costs_map.values()) if daily_costs_map else [0]
        max_daily = sorted_daily[-1]

        # Prompt-level analysis
        high_cost_prompts = [e for e in period_exs if e.get("cost", 0) > daily_cost * 0.5] if daily_cost > 0 else []
        heavy_tool_prompts = [e for e in period_exs if sum(e.get("tools_used", {}).values()) > 30]

        # Session approximation: group by date for one-shot detection
        from itertools import groupby
        date_groups = {}
        for e in sorted(period_exs, key=lambda x: x["ts"]):
            d = e["ts"].strftime("%Y-%m-%d")
            date_groups.setdefault(d, []).append(e)
        one_shot_sessions = sum(1 for exs in date_groups.values() if len(exs) == 1)
        total_sessions = len(date_groups)

        analysis[pname] = {
            "total_cost": total_cost, "daily_cost": daily_cost, "monthly_projected": monthly_projected,
            "api_calls": api_calls, "daily_calls": daily_calls, "active_days": active_days,
            "days_span": days_span, "models": models, "cache_ratio": cache_ratio,
            "total_output": total_output, "daily_output": daily_output,
            "total_cache_r": total_cache_r, "total_cache_w": total_cache_w,
            "model_costs": model_costs, "model_calls": model_calls,
            "max_daily": max_daily, "high_cost_prompts": len(high_cost_prompts),
            "heavy_tool_prompts": len(heavy_tool_prompts),
            "one_shot_sessions": one_shot_sessions,
            "total_sessions": total_sessions,
            "opus_pct": sum(c for m, c in model_costs.items() if "opus" in m.lower()) / total_cost * 100 if total_cost else 0,
        }

    if not analysis:
        tool_label = tool_filter or "any tool"
        period_label = period_name or "last 30 days"
        print(f"  {YELLOW}No token data found for {tool_label} in {period_label}.{RESET}\n")
        return

    # Use the longest period for display + plan + recommendations
    a = list(analysis.values())[-1]
    pname = list(analysis.keys())[-1]

    # ── Cost table by model ──
    print(f"  {DIM}{pname} — {a['active_days']} active days / {a['days_span']}{RESET}\n")
    headers = ["Model", "Calls", "Cost", "Avg/day", "Projected/mo", "Cache", "Share"]
    aligns  = ["<", ">", ">", ">", ">", ">", ">"]
    rows = []
    for model in sorted(a["model_costs"], key=lambda m: -a["model_costs"][m]):
        mc = a["model_costs"][model]
        calls = a["model_calls"][model]
        share = mc / a["total_cost"] * 100 if a["total_cost"] else 0
        daily = mc / a["days_span"]
        projected = daily * 30
        rows.append([
            model,
            str(calls),
            fmt_cost(mc),
            f"{fmt_cost(daily)}/d",
            f"{fmt_cost(projected)}/mo",
            f"{a['cache_ratio'] * 100:.0f}%",
            f"{share:.0f}%",
        ])
    rows.append([
        f"{BOLD}TOTAL{RESET}",
        f"{BOLD}{a['api_calls']}{RESET}",
        f"{BOLD}{fmt_cost(a['total_cost'])}{RESET}",
        f"{BOLD}{fmt_cost(a['daily_cost'])}/d{RESET}",
        f"{BOLD}{fmt_cost(a['monthly_projected'])}/mo{RESET}",
        f"{a['cache_ratio'] * 100:.0f}%",
        "",
    ])
    print_table(headers, rows, aligns)
    print()

    # ── Plan recommendation ──
    mp = a["monthly_projected"]
    print(f"  {BOLD}Plan{RESET} {DIM}(based on {pname}){RESET}")
    print(f"  {'─' * 60}")

    if mp <= 5:
        print(f"    {GREEN}Free tier{RESET} covers your usage.")
        print(f"    {DIM}Projected: {fmt_cost(mp)}/mo vs $5 included{RESET}")
    elif mp <= 18:
        print(f"    {GREEN}Pro ($20/mo){RESET} covers your usage.")
        print(f"    {DIM}Projected: {fmt_cost(mp)}/mo vs ~$18 included{RESET}")
    elif mp <= 100:
        if mp > 30:
            print(f"    {BYELLOW}Max 5x ($100/mo){RESET} recommended.")
            print(f"    {DIM}Projected API cost: {fmt_cost(mp)}/mo — Pro ($20) would be exceeded{RESET}")
        else:
            print(f"    {GREEN}Pro ($20/mo){RESET} still reasonable, approaching Max territory.")
            print(f"    {DIM}Projected: {fmt_cost(mp)}/mo{RESET}")
    elif mp <= 200:
        print(f"    {BYELLOW}Max 5x ($100/mo){RESET} or {BOLD}Max 20x ($200/mo){RESET} recommended.")
        print(f"    {DIM}Projected API cost: {fmt_cost(mp)}/mo{RESET}")
        print(f"    {DIM}Max 5x saves ~{fmt_cost(mp - 100)}/mo vs API pricing{RESET}")
    else:
        print(f"    {BRED}Max 20x ($200/mo){RESET} strongly recommended.")
        print(f"    {DIM}Projected API cost: {fmt_cost(mp)}/mo — you'd save ~{fmt_cost(mp - 200)}/mo{RESET}")
        if mp > 500:
            print(f"    {BRED}Consider Enterprise or Team + Max for volume discount{RESET}")
    print()

    # ── Alerts ──
    alerts = []
    if a["daily_calls"] > 200:
        alerts.append(f"{BYELLOW}!{RESET}  High API call volume ({a['daily_calls']:.0f}/day) — check for runaway agents")
    if a["cache_ratio"] < 0.5 and (a["total_cache_r"] + a["total_cache_w"]) > 0:
        alerts.append(f"{BYELLOW}!{RESET}  Low cache hit rate ({a['cache_ratio']:.0%}) — short sessions waste cache investment")
    elif a["cache_ratio"] > 0.9:
        alerts.append(f"{GREEN}+{RESET}  Excellent cache hit rate ({a['cache_ratio']:.0%})")
    if a["active_days"] < a["days_span"] * 0.3:
        alerts.append(f"{DIM}i{RESET}  Sporadic usage ({a['active_days']}/{a['days_span']} days) — daily averages may overestimate")
    if a["max_daily"] > a["daily_cost"] * 3 and a["daily_cost"] > 0:
        alerts.append(f"{BYELLOW}!{RESET}  Spiky usage: peak day {fmt_cost(a['max_daily'])} vs avg {fmt_cost(a['daily_cost'])}/day")
    if alerts:
        for al in alerts:
            print(f"    {al}")
        print()

    print(f"  {'━' * 60}")
    print(f"  {BOLD}Optimization Recommendations{RESET}")
    print(f"  {'━' * 60}\n")

    recommendations = []

    # 1. Model selection — if top model takes >80% of spend, find a cheaper alternative
    if a["total_cost"] > 0:
        sorted_models = sorted(a["model_costs"].items(), key=lambda x: -x[1])
        top_model, top_cost = sorted_models[0]
        top_pct = top_cost / a["total_cost"] * 100
        if top_pct > 80 and top_cost > 5:
            top_price = match_model(top_model)
            if top_price["output"] > 0:
                # Search LiteLLM for a cheaper model from the same provider
                # Detect provider from model name
                top_lower = top_model.lower()
                # Detect provider family keywords to match in LiteLLM keys
                family_keywords = {
                    "claude": "claude", "gpt": "gpt", "gemini": "gemini",
                    "qwen": "qwen", "glm": "glm", "llama": "llama",
                    "mistral": "mistral", "codex": "codex",
                }
                family = ""
                for key, kw in family_keywords.items():
                    if key in top_lower:
                        family = kw
                        break

                # Find the best alternative: one tier down (1.5x-8x cheaper, not the absolute cheapest)
                best_alt_name = None
                best_alt_ratio = 999.0  # start high, find closest to 1.5x
                for pkey, pval in PRICING.items():
                    if not family or family not in pkey.lower():
                        continue
                    alt_out = pval.get("output", 0)
                    if alt_out <= 0 or alt_out >= top_price["output"]:
                        continue
                    ratio = top_price["output"] / alt_out
                    if 1.5 <= ratio <= 8 and ratio < best_alt_ratio:
                        best_alt_ratio = ratio
                        best_alt_name = pkey
                        for sep in ("/", "."):
                            if sep in best_alt_name:
                                best_alt_name = best_alt_name.split(sep)[-1]

                if best_alt_name:
                    potential_savings = top_cost * 0.3 * (1 - 1/best_alt_ratio) / a["days_span"] * 30
                    if potential_savings > 3:
                        recommendations.append((
                            "Model selection",
                            f"{top_pct:.0f}% of spend is on {top_model}. {best_alt_name} is {best_alt_ratio:.0f}x cheaper.",
                            [
                                f"Use {best_alt_name} for simple tasks: file reads, search, refactoring, Q&A",
                                f"Reserve {top_model} for complex multi-step tasks, architecture, debugging",
                                f"Switching 30% to {best_alt_name} would save ~{fmt_cost(potential_savings)}/mo",
                            ],
                        ))

    # 2. Cache optimization — only if cache hit rate is actually low
    if a["cache_ratio"] < 0.7 and (a["total_cache_r"] + a["total_cache_w"]) > 100_000:
        one_shot_pct = a["one_shot_sessions"] / a["total_sessions"] * 100 if a["total_sessions"] > 0 else 0
        items = [
            "Prefer longer sessions over many short ones (cache builds up over turns)",
            f"{a['one_shot_sessions']}/{a['total_sessions']} sessions are single-prompt ({one_shot_pct:.0f}%) — each wastes cache warm-up" if one_shot_pct > 30 else None,
            "Use /compact instead of starting new sessions when context gets large",
        ]
        recommendations.append((
            "Cache optimization",
            f"Cache hit rate is {a['cache_ratio']:.0%} — {fmt_cost(a['total_cache_w'] * match_model('claude-opus-4-6').get('cache_write', 0))} spent on cache that wasn't reused.",
            items,
        ))

    # 3. Guardrails — only if runaways actually happened
    if a["heavy_tool_prompts"] > 2 or a["high_cost_prompts"] > 3:
        items = []
        if a["heavy_tool_prompts"] > 0:
            items.append(f"{a['heavy_tool_prompts']} prompts had 30+ tool calls — set max_turns in settings (e.g. 25-30)")
        if a["high_cost_prompts"] > 0:
            items.append(f"{a['high_cost_prompts']} prompts cost more than half a day's average — use hooks to alert on high spend")
        items.append("Break large tasks into smaller prompts with explicit checkpoints")
        recommendations.append(("Guardrails", "Runaway agents detected in your data.", items))

    # 4. Context management — only if cache writes are large
    if a["total_cache_w"] > 5_000_000:
        recommendations.append((
            "Context reduction",
            f"{fmt_tokens(a['total_cache_w'])} cache tokens written — large context footprint.",
            [
                "Write a CLAUDE.md at project root — reduces discovery turns and token waste",
                "RTK (Rust Token Killer) — strips comments and whitespace before sending to LLM",
                "Repomix (github.com/yamadashy/repomix) — pack repo into a single optimized context file",
                ".claudeignore / .gitattributes — exclude generated files, binaries, node_modules/",
                "Use /compact to compress context mid-session instead of starting fresh",
            ],
        ))

    # 5. Spending hygiene — only if spikes are real
    if a["max_daily"] > a["daily_cost"] * 5 and a["daily_cost"] > 1:
        recommendations.append((
            "Spending hygiene",
            f"Peak day ({fmt_cost(a['max_daily'])}) is {a['max_daily']/a['daily_cost']:.0f}x the daily average.",
            [
                "Set a daily budget alert (Claude Max shows usage in account settings)",
                "Avoid launching many parallel agents on the same repo (worktree storms)",
                "Run --anomalies to identify the specific runaway prompts",
            ],
        ))


    for title, summary, items in recommendations:
        print(f"  {BOLD}{title}{RESET}")
        print(f"    {DIM}{summary}{RESET}")
        for item in items:
            if item:
                print(f"      - {item}")
        print()

    print()


def export_conversations(output_path: str, period_name: str | None = None, tool_filter: str | None = None):
    """Export all Claude Code conversations to a JSON file."""
    print(f"\n{BOLD} Exporting conversations{RESET}")
    print(f"{DIM}  Scanning Claude Code transcripts...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return
    label = f"  Period: {BOLD}{period_label}{RESET}"
    if tool_filter:
        color = TOOL_COLORS.get(tool_filter, "")
        label += f"  Tool: {color}{BOLD}{tool_filter}{RESET}"
    print(label + "\n")
    all_exchanges, tool_counts = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)

    if not all_exchanges:
        print(f"  {YELLOW}No conversation data found.{RESET}\n")
        return

    for tool_name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        color = TOOL_COLORS.get(tool_name, "")
        print(f"  {color}●{RESET} {tool_name:<12} {count:>5} exchanges")

    # Sort chronologically
    all_exchanges.sort(key=lambda e: e["ts"] or datetime.min.replace(tzinfo=timezone.utc))

    # Serialize
    export = []
    for ex in all_exchanges:
        entry = {
            "tool": ex.get("tool", "?"),
            "model": ex.get("model"),
            "timestamp": ex["ts"].isoformat() if ex["ts"] else None,
            "user": ex["user_text"],
            "assistant": ex["assistant_texts"],
            "turns": ex.get("num_turns", 0),
        }
        tools_used = ex.get("tools_used")
        if tools_used:
            entry["tools_used"] = dict(tools_used)
        if ex.get("tool_errors"):
            entry["tool_errors"] = ex["tool_errors"]
        export.append(entry)

    out = Path(output_path)
    out.write_text(json.dumps(export, ensure_ascii=False, indent=2))

    total = len(export)
    size_kb = out.stat().st_size / 1024
    first_ts = next((e["timestamp"] for e in export if e["timestamp"]), "?")
    last_ts = next((e["timestamp"] for e in reversed(export) if e["timestamp"]), "?")

    print(f"\n  {BOLD}{total}{RESET} exchanges exported to {BOLD}{output_path}{RESET}")
    print(f"  {DIM}{size_kb:.0f} KB — {first_ts[:10]} to {last_ts[:10]}{RESET}\n")


def _parse_period(args: list[str]) -> str | None:
    """Extract --period/--since value from args."""
    for flag in ("--period", "--since"):
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
    return None


_TOOL_ALIASES = {
    "claude": "Claude Code", "claude-code": "Claude Code", "claudecode": "Claude Code",
}


def _parse_tool(args: list[str]) -> str | None:
    """Extract --tool value from args. Returns canonical tool name or None (all)."""
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
    # Partial match
    for alias, name in _TOOL_ALIASES.items():
        if raw in alias or raw in name.lower():
            return name
    valid = ", ".join(sorted(set(_TOOL_ALIASES.values())))
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: {valid}")


def show_help():
    print(f"""
{BOLD}claude-token-usage{RESET} — Aggregate and analyze Claude Code token consumption.

{BOLD}MODES{RESET}
  claude-token-usage                            Aggregated overview (period, project, model, speed)
  claude-token-usage --prompts  [-p]            Per-exchange detail (text, model, turns, tokens, tools)
  claude-token-usage --anomalies                Technical anomaly detection (cost, cache, tool storms)
  claude-token-usage --plan                     Cost breakdown + plan recommendation + optimization tips
  claude-token-usage --export   [file.json]     Export all exchanges to JSON
  claude-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}  {DIM}(apply to all modes){RESET}
  --period <period>      Time filter — all, hour, "5 hours", today, yesterday, "7 days", "30 days", year

  Default period: today. Partial match works ("7" → "Last 7 days").

{BOLD}DATA SOURCE{RESET}
  {CYAN}Claude Code{RESET}    {DIM}~/.claude/projects/{RESET}    ✓ Tokens ✓ Text ✓ Tools ✓ Speed

{BOLD}QUICK START{RESET}
  claude-token-usage                              # Full overview, today
  claude-token-usage --period all                 # All time

{BOLD}PER-EXCHANGE DETAIL{RESET}
  claude-token-usage --prompts                    # Today's exchanges
  claude-token-usage -p --period "7 days"         # Last 7 days

{BOLD}ANALYSIS MODES{RESET}
  claude-token-usage --anomalies                  # Technical anomalies (high cost, tool storms)
  claude-token-usage --anomalies --period "30 days"
  claude-token-usage --plan                       # Cost breakdown + plan recommendation
  claude-token-usage --plan --period all          # Projection based on all-time usage

{BOLD}EXPORT{RESET}
  claude-token-usage --export                     # Save to conversations.json
  claude-token-usage --export out.json --period "7 days"
""")


_KNOWN_FLAGS = {
    "--help", "-h",
    "--prompts", "-p",
    "--anomalies",
    "--plan",
    "--export",
    "--period", "--since",
    "--tool",
}


def cli():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        show_help()
        return

    # Detect unknown flags (anything starting with - not in the known set)
    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"\n  {RED}Unknown option(s): {', '.join(unknown)}{RESET}")
        print(f"  Run {BOLD}claude-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)
    try:
        tool = _parse_tool(args)
    except ValueError as e:
        print(f"\n  {RED}{e}{RESET}\n")
        sys.exit(1)

    if "--prompts" in args or "-p" in args:
        show_prompts(period, tool)
    elif "--anomalies" in args:
        show_anomalies(period, tool)
    elif "--plan" in args:
        show_plan(period, tool)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(out, period, tool)
    else:
        main(period, tool)


if __name__ == "__main__":
    cli()
