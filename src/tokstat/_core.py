"""
tokstat._core — Shared utilities for all token-usage CLI tools.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Pricing (loaded dynamically from LiteLLM) ────────────────────────────
LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
LITELLM_CACHE_PATH = Path.home() / ".cache" / "token-usage" / "litellm_prices.json"
LITELLM_CACHE_MAX_AGE = timedelta(hours=24)

PRICING: dict[str, dict] = {}


def load_pricing():
    global PRICING
    raw = None
    if LITELLM_CACHE_PATH.exists():
        age = datetime.now() - datetime.fromtimestamp(LITELLM_CACHE_PATH.stat().st_mtime)
        if age < LITELLM_CACHE_MAX_AGE:
            try:
                raw = json.loads(LITELLM_CACHE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    if raw is None:
        try:
            req = urllib.request.Request(LITELLM_PRICING_URL, headers={"User-Agent": "tokstat/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode())
            LITELLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            LITELLM_CACHE_PATH.write_text(json.dumps(raw))
        except Exception:
            pass
    if raw is None and LITELLM_CACHE_PATH.exists():
        try:
            raw = json.loads(LITELLM_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    if raw is None:
        print(f"  {DIM}Warning: could not load LiteLLM pricing data, costs will show as $0{RESET}")
        return
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


# ─── ANSI colors ──────────────────────────────────────────────────────────
BOLD    = "\033[1m"
DIM     = "\033[2m"
RESET   = "\033[0m"
CYAN    = "\033[36m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
RED     = "\033[31m"
MAGENTA = "\033[35m"
WHITE   = "\033[97m"
BLUE    = "\033[34m"
BRED    = "\033[91m"
BYELLOW = "\033[93m"

# Populated by each tool module: {"Claude Code": CYAN, "Codex": GREEN, ...}
TOOL_COLORS: dict[str, str] = {}


# ─── Data structures ──────────────────────────────────────────────────────

def empty_bucket():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def add_bucket(a, b):
    return {k: a[k] + b[k] for k in a}


# ─── Pricing helpers ──────────────────────────────────────────────────────

ZERO_PRICE = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def match_model(model_name: str) -> dict:
    if not model_name or not PRICING:
        return ZERO_PRICE
    name = model_name.lower().split("[")[0].strip()
    if name in PRICING:
        return PRICING[name]
    for prefix in ["", "openai/", "anthropic/", "gemini/", "vertex_ai/",
                   "deepseek/", "together_ai/", "fireworks_ai/"]:
        candidate = prefix + name
        if candidate in PRICING:
            return PRICING[candidate]
    for key, val in PRICING.items():
        if key.endswith("/" + name) or key == name:
            return val
    best_key = None
    best_len = 0
    for key in PRICING:
        if len(key) < 5:
            continue
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
    p = match_model(model)
    cost = 0.0
    cost += tokens.get("input", 0) * p["input"]
    cost += tokens.get("output", 0) * p["output"]
    cost += tokens.get("cache_read", 0) * p["cache_read"]
    cost += tokens.get("cache_write", 0) * p["cache_write"]
    return cost


# ─── Period helpers ───────────────────────────────────────────────────────

def period_boundaries() -> dict:
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
    result = []
    for name, bounds in boundaries.items():
        if isinstance(bounds, tuple):
            start, end = bounds
        else:
            start, end = bounds, None
        if ts >= start and (end is None or ts < end):
            result.append(name)
    return result


# ─── Project normalization ─────────────────────────────────────────────────

import re
import subprocess
from pathlib import Path as _Path

_worktree_cache: dict = {}
_all_known_paths: set = set()

_WORKTREE_PATH_RE = re.compile(r"^(.+)/[0-9a-f]{4,8}/([^/]+)$")


def normalize_project(path: str) -> str:
    if not path or path == "unknown":
        return "unknown"
    if path in _worktree_cache:
        return _worktree_cache[path]

    if _Path(path).exists():
        try:
            result = subprocess.run(
                ["git", "-C", path, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("worktree "):
                        main = line[len("worktree "):]
                        _worktree_cache[path] = main
                        return main
        except Exception:
            pass

    m = _WORKTREE_PATH_RE.match(path)
    if m:
        name = m.group(2)
        for known in _all_known_paths:
            if known != path and not _WORKTREE_PATH_RE.match(known):
                if _Path(known).name == name or known.endswith("/" + name):
                    _worktree_cache[path] = known
                    return known
        synthetic = str(_Path.home() / "Code" / name)
        _worktree_cache[path] = synthetic
        return synthetic

    _worktree_cache[path] = path
    return path


def _warm_worktree_cache(project_paths):
    _all_known_paths.update(project_paths)
    for p in sorted(project_paths, key=lambda x: bool(_WORKTREE_PATH_RE.match(x))):
        normalize_project(p)


# ─── Formatting helpers ───────────────────────────────────────────────────

def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(c: float) -> str:
    if c >= 1.0:
        return f"${c:.2f}"
    if c >= 0.01:
        return f"${c:.3f}"
    if c > 0:
        return f"${c:.4f}"
    return "$0.00"


def _strip_ansi(text: str) -> str:
    return re.sub(r'\033\[[0-9;]*m', '', text)


def calc_table_width(headers: list[str], rows: list[list[str]]) -> int:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(_strip_ansi(cell)))
    return 2 + sum(widths) + 2 * (len(widths) - 1)


def print_table(headers: list[str], rows: list[list[str]], col_aligns: list[str] | None = None) -> int:
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
    header_line = "  ".join(pad(h, widths[i], col_aligns[i]) for i, h in enumerate(headers))
    print(f"  {BOLD}{header_line}{RESET}")
    sep = "  ".join("─" * w for w in widths)
    print(f"  {DIM}{sep}{RESET}")
    for row in rows:
        line = "  ".join(pad(row[i], widths[i], col_aligns[i]) for i in range(len(headers)))
        print(f"  {line}")
    return table_width


def shorten_path(path: str | None, max_len: int = 40) -> str:
    if not path:
        return "unknown"
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


# ─── Severity helpers ─────────────────────────────────────────────────────

_SEVERITY_COLORS = {"high": BRED, "medium": BYELLOW, "low": DIM}
_SEVERITY_ORDER  = {"high": 0, "medium": 1, "low": 2}


# ─── Shared display: overview tables ─────────────────────────────────────

def show_overview_tables(all_records: list[dict], speed_records: list[dict],
                         cutoff: datetime, cutoff_end: datetime | None,
                         period_label: str, tool_filter: str | None = None):
    """Print period, project, model, and speed tables from a list of records."""

    # ─── 1. Consumption by period ──────────────────────────────────────
    boundaries = period_boundaries()
    if period_label == "All time":
        period_order = ["Last hour", "Last 5 hours", "Today", "Yesterday",
                        "Last 7 days", "Last 30 days", "Last year"]
    else:
        period_order = [period_label]
        boundaries = {period_label: (cutoff, cutoff_end)}

    tool_period = defaultdict(lambda: defaultdict(empty_bucket))
    period_totals = defaultdict(empty_bucket)

    for rec in all_records:
        for period in classify_periods(rec["ts"], boundaries):
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
                fmt_tokens(b["input"]), fmt_tokens(b["output"]),
                fmt_tokens(b["cache_read"]), fmt_tokens(b["cache_write"]),
                fmt_cost(b["cost"]),
            ])
            first = False
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
            rows.append([""] * 7)

    w = calc_table_width(headers, rows)
    print(f"\n{'─' * w}")
    print(f"{BOLD} CONSUMPTION BY PERIOD{RESET}")
    print(f"{'─' * w}")
    print_table(headers, rows, aligns)

    # ─── 2. Consumption by project ─────────────────────────────────────
    _warm_worktree_cache(set(r["project"] for r in all_records))

    proj_tool   = defaultdict(lambda: defaultdict(empty_bucket))
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
                fmt_tokens(b["input"]), fmt_tokens(b["output"]),
                fmt_tokens(b["cache_read"]), fmt_tokens(b["cache_write"]),
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

    # ─── 3. Model breakdown ────────────────────────────────────────────
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
        rows.append([model, f"{color}{d['tool']}{RESET}",
                     fmt_tokens(d["input"]), fmt_tokens(d["output"]), fmt_cost(d["cost"])])

    total_cost = sum(d["cost"] for d in model_data.values())
    total_in   = sum(d["input"] for d in model_data.values())
    total_out  = sum(d["output"] for d in model_data.values())
    rows.append([f"{BOLD}ALL MODELS{RESET}", "",
                 f"{BOLD}{fmt_tokens(total_in)}{RESET}",
                 f"{BOLD}{fmt_tokens(total_out)}{RESET}",
                 f"{BOLD}{fmt_cost(total_cost)}{RESET}"])

    w = calc_table_width(headers, rows)
    print(f"\n{'─' * w}")
    print(f"{BOLD} COST BY MODEL{RESET}")
    print(f"{'─' * w}")
    print_table(headers, rows, aligns)

    # ─── 4. Speed analysis ────────────────────────────────────────────
    if speed_records:
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
            rows.append([model, f"{color}{tool}{RESET}", str(n),
                         f"{median:.0f} t/s", f"{avg:.0f} t/s",
                         f"{p10:.0f} t/s", f"{p90:.0f} t/s"])

        w = calc_table_width(headers, rows)
        print(f"\n{'─' * w}")
        print(f"{BOLD} OUTPUT SPEED (tokens/sec){RESET}")
        print(f"{'─' * w}")
        print_table(headers, rows, aligns)

    # ─── Grand total ──────────────────────────────────────────────────
    total_all_tokens = sum(r["input"] + r["output"] + r["cache_read"] + r["cache_write"]
                           for r in all_records)
    print(f"\n  {BOLD}Grand total:{RESET} {fmt_tokens(total_all_tokens)} tokens across {len(all_records)} API calls")
    print(f"  {BOLD}Estimated cost:{RESET} {fmt_cost(total_cost)}")
    print(f"  {DIM}Period: {all_records[0]['ts'].strftime('%Y-%m-%d')} to "
          f"{max(r['ts'] for r in all_records).strftime('%Y-%m-%d')}{RESET}")
    print()


# ─── Shared display: prompts ──────────────────────────────────────────────

def show_prompts(collect_fn, period_name: str | None = None, tool_filter: str | None = None):
    """Show per-prompt/exchange token usage."""
    print(f"\n{BOLD} Exchanges — Prompt-level Usage{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning exchanges...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return
    print(f"  Period: {BOLD}{period_label}{RESET}\n")

    all_exchanges, tool_counts = collect_fn(cutoff, tool_filter, cutoff_end)
    if not all_exchanges:
        print(f"  {YELLOW}No exchanges found.{RESET}\n")
        return

    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))

    grouped: dict[tuple[str, str], list[dict]] = {}
    for ex in all_exchanges:
        key = (ex.get("tool", "Unknown"), ex.get("project", "unknown"))
        grouped.setdefault(key, []).append(ex)

    sorted_groups = sorted(grouped.items(),
                           key=lambda x: sum(e.get("cost", 0) for e in x[1]),
                           reverse=True)

    for (tool, project), exchanges in sorted_groups:
        proj_display = shorten_path(normalize_project(project), 50)
        tool_color = TOOL_COLORS.get(tool, "")
        total_cost = sum(e["cost"] for e in exchanges)
        total_turns = sum(e.get("num_turns", 0) for e in exchanges)

        print(f"  {tool_color}{BOLD}{tool}{RESET} {DIM}{proj_display}{RESET}  "
              f"{CYAN}{len(exchanges)} exchanges{RESET}  {total_turns} turns  "
              f"{BOLD}{fmt_cost(total_cost)}{RESET}")

        headers = ["#", "Time", "Input text", "Model", "Turns",
                   "Input", "Output", "Cache R", "Cache W", "Tools", "Cost"]
        aligns  = [">", "<",    "<",          "<",     ">",
                   ">",     ">",      ">",       ">",       "<",     ">"]
        rows = []

        for i, ex in enumerate(sorted(exchanges,
                                      key=lambda e: e.get("ts") or datetime.min.replace(tzinfo=timezone.utc)), 1):
            user_text = ex.get("user_text", "").replace("\n", " ")
            if len(user_text) > 50:
                user_text = user_text[:47] + "..."
            if not user_text:
                user_text = DIM + "(no text)" + RESET

            ts_str = ex["ts"].strftime("%H:%M") if ex.get("ts") else "?"
            model_short = (ex.get("model") or "?").split("/")[-1]
            if len(model_short) > 20:
                model_short = model_short[:17] + "..."

            tools = ex.get("tools_used", {})
            if tools:
                tool_parts = [f"{t}:{c}" if c > 1 else t
                              for t, c in sorted(tools.items(), key=lambda x: -x[1])[:4]]
                tools_str = " ".join(tool_parts)
                if len(tools) > 4:
                    tools_str += f" +{len(tools)-4}"
            else:
                tools_str = DIM + "-" + RESET

            tok = ex.get("tokens", {})
            rows.append([
                str(i), ts_str, user_text, DIM + model_short + RESET,
                str(ex.get("num_turns", 0)),
                fmt_tokens(tok.get("input", 0)), fmt_tokens(tok.get("output", 0)),
                fmt_tokens(tok.get("cache_read", 0)), fmt_tokens(tok.get("cache_write", 0)),
                tools_str, fmt_cost(ex.get("cost", 0)),
            ])

        print_table(headers, rows, aligns)
        print()


# ─── Shared display: anomalies ────────────────────────────────────────────

def show_anomalies(collect_fn, period_name: str | None = None, tool_filter: str | None = None):
    """Detect technical anomalies."""
    print(f"\n{BOLD} Technical Anomaly Detection{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning transcripts...{RESET}\n")

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

    all_prompts, tool_counts = collect_fn(cutoff, tool_filter, cutoff_end)
    all_prompts = [p for p in all_prompts if p.get("tokens") and p["ts"]]

    if not all_prompts:
        print(f"  {YELLOW}No usage data found.{RESET}\n")
        return

    tools_with_tokens = defaultdict(int)
    for p in all_prompts:
        if p["tokens"]["input"] > 0 or p["tokens"]["output"] > 0 or p["cost"] > 0:
            tools_with_tokens[p.get("tool", "?")] += 1
    for tool_name, count in sorted(tools_with_tokens.items(), key=lambda x: -x[1]):
        color = TOOL_COLORS.get(tool_name, "")
        print(f"  {color}●{RESET} {tool_name:<12} {count:>5} exchanges with token data")
    print()

    all_prompts = [p for p in all_prompts if
                   p["tokens"]["input"] > 0 or p["tokens"]["output"] > 0 or p["cost"] > 0]
    if not all_prompts:
        print(f"  {YELLOW}No token data found in exchanges.{RESET}\n")
        return

    costs  = [p["cost"] for p in all_prompts if p["cost"] > 0]
    turns  = [p["num_turns"] for p in all_prompts if p["num_turns"] > 0]
    median_cost  = sorted(costs)[len(costs) // 2] if costs else 0
    median_turns = sorted(turns)[len(turns) // 2] if turns else 1
    p90_cost  = sorted(costs)[min(len(costs) - 1, len(costs) * 9 // 10)] if costs else 0
    p90_turns = sorted(turns)[min(len(turns) - 1, len(turns) * 9 // 10)] if turns else 1

    _warm_worktree_cache(set(p.get("project") or "unknown" for p in all_prompts))

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

        if p["cost"] > 0 and p90_cost > 0 and p["cost"] > p90_cost * 10:
            _add("high", "Runaway cost", f"{fmt_cost(p['cost'])} ({p['cost']/median_cost:.0f}x median)")
        elif p["cost"] > 0 and p90_cost > 0 and p["cost"] > p90_cost * 5:
            _add("medium", "High cost", f"{fmt_cost(p['cost'])} ({p['cost']/median_cost:.0f}x median)")
        if total_tools > 30:
            _add("high" if total_tools > 60 else "medium", "Tool storm", f"{total_tools} tool calls")
        if p["num_turns"] > 0 and p90_turns > 0 and p["num_turns"] > p90_turns * 5:
            _add("high" if p["num_turns"] > p90_turns * 10 else "medium", "Turn spiral",
                 f"{p['num_turns']} turns ({p['num_turns']/median_turns:.0f}x median)")
        if tok["cache_write"] > 50_000 and tok["cache_read"] < tok["cache_write"] * 0.5:
            ratio = tok["cache_read"] / tok["cache_write"] if tok["cache_write"] > 0 else 0
            _add("medium", "Cache thrashing",
                 f"{fmt_tokens(tok['cache_write'])} written, only {ratio:.0%} read back")
        if tok["input"] > 10_000 and tok["output"] > 0 and tok["input"] / tok["output"] > 50:
            _add("low", "Context bloat",
                 f"{fmt_tokens(tok['input'])} in / {fmt_tokens(tok['output'])} out "
                 f"(ratio {tok['input']/tok['output']:.0f}:1)")
        if p["num_turns"] > 5 and tok["output"] < 100:
            _add("medium", "Empty exchange",
                 f"{p['num_turns']} turns but only {tok['output']} output tokens")

    if not anomalies:
        print(f"  {DIM}No anomalies detected.{RESET}")
        print(f"  {DIM}Stats: {len(all_prompts)} exchanges, median cost {fmt_cost(median_cost)}, "
              f"P90 cost {fmt_cost(p90_cost)}{RESET}\n")
        return

    print(f"  {DIM}{len(all_prompts)} exchanges analyzed — median cost {fmt_cost(median_cost)}, "
          f"P90 {fmt_cost(p90_cost)}, median turns {median_turns}, P90 turns {p90_turns}{RESET}\n")

    by_project = defaultdict(list)
    for a in anomalies:
        by_project[a[3]].append(a)

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

        by_type = defaultdict(list)
        for a in items:
            by_type[a[1]].append(a)
        for atype in sorted(by_type, key=lambda t: min(_SEVERITY_ORDER.get(a[0], 9)
                                                       for a in by_type[t])):
            type_items = by_type[atype]
            type_items.sort(key=lambda a: _SEVERITY_ORDER.get(a[0], 9))
            print(f"    {DIM}{atype} ({len(type_items)}){RESET}")
            for sev, _, detail, _, tname, model, ts, prompt in type_items:
                sev_color = _SEVERITY_COLORS.get(sev, "")
                tool_color = TOOL_COLORS.get(tname, "")
                model_short = model.split("/")[-1][:20]
                ts_str = ts.strftime("%m-%d %H:%M") if ts else "?"
                print(f"      {sev_color}[{sev.upper():6s}]{RESET} "
                      f"{tool_color}{tname}{RESET} {DIM}{model_short}{RESET}  {ts_str}  {detail}")
                if prompt:
                    print(f"      {DIM}         {prompt}{RESET}")
        print()

    total = len(anomalies)
    high_t = sum(1 for a in anomalies if a[0] == "high")
    med_t  = sum(1 for a in anomalies if a[0] == "medium")
    low_t  = sum(1 for a in anomalies if a[0] == "low")
    print(f"  {'─' * 60}")
    print(f"  {BOLD}{total} anomalies{RESET} across {BOLD}{len(by_project)} projects{RESET}: "
          f"{BRED}{high_t} high{RESET}, {BYELLOW}{med_t} med{RESET}, {DIM}{low_t} low{RESET}")
    print()


# ─── Shared display: plan ─────────────────────────────────────────────────

def show_plan(collect_fn, period_name: str | None = None, tool_filter: str | None = None):
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

    analysis = {}
    for pname, (p_cutoff, p_cutoff_end) in boundaries.items():
        all_exs, _ = collect_fn(p_cutoff, tool_filter, p_cutoff_end)
        period_exs = [e for e in all_exs if e.get("tokens") and e["ts"]
                      and (e["tokens"]["input"] > 0 or e["tokens"]["output"] > 0
                           or e.get("cost", 0) > 0)]
        if not period_exs:
            continue

        total_cost   = sum(e.get("cost", 0) for e in period_exs)
        total_input  = sum(e["tokens"]["input"] for e in period_exs)
        total_output = sum(e["tokens"]["output"] for e in period_exs)
        total_cache_r = sum(e["tokens"]["cache_read"] for e in period_exs)
        total_cache_w = sum(e["tokens"]["cache_write"] for e in period_exs)

        first_ts = min(e["ts"] for e in period_exs)
        last_ts  = max(e["ts"] for e in period_exs)
        data_span   = (last_ts - first_ts).days
        period_span = ((p_cutoff_end or now) - p_cutoff).days
        days_span = max(1, min(data_span, period_span) if data_span > 0 else period_span)

        daily_cost = total_cost / days_span
        monthly_projected = daily_cost * 30
        api_calls   = len(period_exs)
        daily_calls = api_calls / days_span
        active_days = len(set(e["ts"].strftime("%Y-%m-%d") for e in period_exs))
        models = set(e.get("model") or "?" for e in period_exs)
        cache_ratio = (total_cache_r / (total_cache_r + total_cache_w)
                       if (total_cache_r + total_cache_w) > 0 else 0)

        model_costs = defaultdict(float)
        model_calls = defaultdict(int)
        for e in period_exs:
            m = e.get("model") or "?"
            model_costs[m] += e.get("cost", 0)
            model_calls[m] += 1

        daily_costs_map = defaultdict(float)
        for e in period_exs:
            daily_costs_map[e["ts"].strftime("%Y-%m-%d")] += e.get("cost", 0)
        max_daily = sorted(daily_costs_map.values())[-1] if daily_costs_map else 0

        high_cost_prompts  = ([e for e in period_exs if e.get("cost", 0) > daily_cost * 0.5]
                               if daily_cost > 0 else [])
        heavy_tool_prompts = [e for e in period_exs
                               if sum(e.get("tools_used", {}).values()) > 30]

        from itertools import groupby
        date_groups = {}
        for e in sorted(period_exs, key=lambda x: x["ts"]):
            date_groups.setdefault(e["ts"].strftime("%Y-%m-%d"), []).append(e)
        one_shot_sessions = sum(1 for exs in date_groups.values() if len(exs) == 1)
        total_sessions = len(date_groups)

        analysis[pname] = {
            "total_cost": total_cost, "daily_cost": daily_cost,
            "monthly_projected": monthly_projected,
            "api_calls": api_calls, "daily_calls": daily_calls,
            "active_days": active_days, "days_span": days_span,
            "models": models, "cache_ratio": cache_ratio,
            "total_output": total_output,
            "total_cache_r": total_cache_r, "total_cache_w": total_cache_w,
            "model_costs": model_costs, "model_calls": model_calls,
            "max_daily": max_daily,
            "high_cost_prompts": len(high_cost_prompts),
            "heavy_tool_prompts": len(heavy_tool_prompts),
            "one_shot_sessions": one_shot_sessions,
            "total_sessions": total_sessions,
        }

    if not analysis:
        print(f"  {YELLOW}No token data found.{RESET}\n")
        return

    a = list(analysis.values())[-1]
    pname = list(analysis.keys())[-1]

    print(f"  {DIM}{pname} — {a['active_days']} active days / {a['days_span']}{RESET}\n")
    headers = ["Model", "Calls", "Cost", "Avg/day", "Projected/mo", "Cache", "Share"]
    aligns  = ["<", ">", ">", ">", ">", ">", ">"]
    rows = []
    for model in sorted(a["model_costs"], key=lambda m: -a["model_costs"][m]):
        mc = a["model_costs"][model]
        calls = a["model_calls"][model]
        share = mc / a["total_cost"] * 100 if a["total_cost"] else 0
        daily = mc / a["days_span"]
        rows.append([model, str(calls), fmt_cost(mc),
                     f"{fmt_cost(daily)}/d", f"{fmt_cost(daily*30)}/mo",
                     f"{a['cache_ratio']*100:.0f}%", f"{share:.0f}%"])
    rows.append([f"{BOLD}TOTAL{RESET}", f"{BOLD}{a['api_calls']}{RESET}",
                 f"{BOLD}{fmt_cost(a['total_cost'])}{RESET}",
                 f"{BOLD}{fmt_cost(a['daily_cost'])}/d{RESET}",
                 f"{BOLD}{fmt_cost(a['monthly_projected'])}/mo{RESET}",
                 f"{a['cache_ratio']*100:.0f}%", ""])
    print_table(headers, rows, aligns)
    print()

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

    if a["total_cost"] > 0:
        sorted_models = sorted(a["model_costs"].items(), key=lambda x: -x[1])
        top_model, top_cost = sorted_models[0]
        top_pct = top_cost / a["total_cost"] * 100
        if top_pct > 80 and top_cost > 5:
            top_price = match_model(top_model)
            if top_price["output"] > 0:
                family_keywords = {
                    "claude": "claude", "gpt": "gpt", "gemini": "gemini",
                    "qwen": "qwen", "llama": "llama", "mistral": "mistral",
                }
                family = next((kw for key, kw in family_keywords.items()
                               if key in top_model.lower()), "")
                best_alt_name = None
                best_alt_ratio = 999.0
                for pkey, pval in PRICING.items():
                    if not family or family not in pkey.lower():
                        continue
                    alt_out = pval.get("output", 0)
                    if alt_out <= 0 or alt_out >= top_price["output"]:
                        continue
                    ratio = top_price["output"] / alt_out
                    if 1.5 <= ratio <= 8 and ratio < best_alt_ratio:
                        best_alt_ratio = ratio
                        best_alt_name = pkey.split("/")[-1] if "/" in pkey else pkey
                if best_alt_name:
                    savings = top_cost * 0.3 * (1 - 1/best_alt_ratio) / a["days_span"] * 30
                    if savings > 3:
                        recommendations.append((
                            "Model selection",
                            f"{top_pct:.0f}% of spend is on {top_model}. {best_alt_name} is {best_alt_ratio:.0f}x cheaper.",
                            [f"Use {best_alt_name} for simple tasks",
                             f"Reserve {top_model} for complex tasks",
                             f"Switching 30% would save ~{fmt_cost(savings)}/mo"],
                        ))

    if a["cache_ratio"] < 0.7 and (a["total_cache_r"] + a["total_cache_w"]) > 100_000:
        one_shot_pct = (a["one_shot_sessions"] / a["total_sessions"] * 100
                        if a["total_sessions"] > 0 else 0)
        items = ["Prefer longer sessions over many short ones (cache builds up over turns)"]
        if one_shot_pct > 30:
            items.append(f"{a['one_shot_sessions']}/{a['total_sessions']} sessions are "
                         f"single-prompt ({one_shot_pct:.0f}%) — each wastes cache warm-up")
        recommendations.append((
            "Cache optimization",
            f"Cache hit rate is {a['cache_ratio']:.0%}.",
            items,
        ))

    if a["heavy_tool_prompts"] > 2 or a["high_cost_prompts"] > 3:
        items = []
        if a["heavy_tool_prompts"] > 0:
            items.append(f"{a['heavy_tool_prompts']} prompts had 30+ tool calls — consider limiting agent turns")
        if a["high_cost_prompts"] > 0:
            items.append(f"{a['high_cost_prompts']} prompts cost more than half a day's average")
        items.append("Break large tasks into smaller prompts with explicit checkpoints")
        recommendations.append(("Guardrails", "Runaway agents detected in your data.", items))

    if a["total_cache_w"] > 5_000_000:
        recommendations.append((
            "Context reduction",
            f"{fmt_tokens(a['total_cache_w'])} cache tokens written — large context footprint.",
            ["Add a project-level instructions file to reduce discovery turns",
             "Exclude generated files, binaries, and dependencies from context",
             "Compress context mid-session instead of starting fresh"],
        ))

    if a["max_daily"] > a["daily_cost"] * 5 and a["daily_cost"] > 1:
        recommendations.append((
            "Spending hygiene",
            f"Peak day ({fmt_cost(a['max_daily'])}) is {a['max_daily']/a['daily_cost']:.0f}x the daily average.",
            ["Set a daily budget alert in your account settings",
             "Avoid launching many parallel agents on the same repo",
             "Run --anomalies to identify the specific runaway prompts"],
        ))

    for title, summary, items in recommendations:
        print(f"  {BOLD}{title}{RESET}")
        print(f"    {DIM}{summary}{RESET}")
        for item in items:
            if item:
                print(f"      - {item}")
        print()

    print()


# ─── Shared display: export ───────────────────────────────────────────────

def export_conversations(collect_fn, output_path: str,
                         period_name: str | None = None,
                         tool_filter: str | None = None):
    """Export all conversations to a JSON file."""
    print(f"\n{BOLD} Exporting conversations{RESET}")
    print(f"{DIM}  Scanning transcripts...{RESET}\n")

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

    all_exchanges, tool_counts = collect_fn(cutoff, tool_filter, cutoff_end)
    if not all_exchanges:
        print(f"  {YELLOW}No conversation data found.{RESET}\n")
        return

    for tool_name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        color = TOOL_COLORS.get(tool_name, "")
        print(f"  {color}●{RESET} {tool_name:<12} {count:>5} exchanges")

    all_exchanges.sort(key=lambda e: e["ts"] or datetime.min.replace(tzinfo=timezone.utc))

    export = []
    for ex in all_exchanges:
        entry = {
            "tool":      ex.get("tool", "?"),
            "model":     ex.get("model"),
            "timestamp": ex["ts"].isoformat() if ex["ts"] else None,
            "user":      ex["user_text"],
            "assistant": ex["assistant_texts"],
            "turns":     ex.get("num_turns", 0),
        }
        if ex.get("tools_used"):
            entry["tools_used"] = dict(ex["tools_used"])
        if ex.get("tool_errors"):
            entry["tool_errors"] = ex["tool_errors"]
        export.append(entry)

    out = Path(output_path)
    out.write_text(json.dumps(export, ensure_ascii=False, indent=2))
    size_kb = out.stat().st_size / 1024
    first_ts = next((e["timestamp"] for e in export if e["timestamp"]), "?")
    last_ts  = next((e["timestamp"] for e in reversed(export) if e["timestamp"]), "?")

    print(f"\n  {BOLD}{len(export)}{RESET} exchanges exported to {BOLD}{output_path}{RESET}")
    print(f"  {DIM}{size_kb:.0f} KB — {first_ts[:10]} to {last_ts[:10]}{RESET}\n")


# ─── Update checker ──────────────────────────────────────────────────────

_UPDATE_CACHE = Path.home() / ".cache" / "token-usage" / "update_check.json"


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def check_for_update(current_version: str) -> str | None:
    """Check PyPI for a newer version of tokstat. Returns the latest version
    string if an update is available, or None. Cached for 24 hours."""
    try:
        # Try cache first
        if _UPDATE_CACHE.exists():
            age = datetime.now() - datetime.fromtimestamp(_UPDATE_CACHE.stat().st_mtime)
            if age < timedelta(hours=24):
                data = json.loads(_UPDATE_CACHE.read_text())
                latest = data.get("latest", current_version)
                return latest if _version_tuple(latest) > _version_tuple(current_version) else None

        # Query PyPI
        req = urllib.request.Request(
            "https://pypi.org/pypi/tokstat/json",
            headers={"User-Agent": f"tokstat/{current_version}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            latest = json.loads(resp.read().decode())["info"]["version"]
        _UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _UPDATE_CACHE.write_text(json.dumps({"latest": latest}))
        return latest if _version_tuple(latest) > _version_tuple(current_version) else None
    except Exception:
        return None


def print_update_notice(current_version: str) -> None:
    """Print an update notice if a newer version is available on PyPI."""
    latest = check_for_update(current_version)
    if latest:
        print(f"\n  {BYELLOW}┌─ Update available: {current_version} → {latest}{RESET}")
        print(f"  {BYELLOW}└─ Run: pip install --upgrade tokstat{RESET}\n")


# ─── Arg parsing helpers ──────────────────────────────────────────────────

def _parse_period(args: list[str]) -> str | None:
    for flag in ("--period", "--since"):
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
    return None
