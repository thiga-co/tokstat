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
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0,
            "prompts": 0, "turns": 0, "api_calls": 0}


def add_bucket(a, b):
    return {k: a[k] + b[k] for k in a}


# ─── Pricing helpers ──────────────────────────────────────────────────────

ZERO_PRICE = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


# Provider identification by model string — used by show_plan to recommend
# the right subscription per provider rather than aggregating across all
# vendors. Keep the keyword lists narrow to avoid false positives (e.g.
# "claude-via-openrouter" should still be Anthropic).
_PROVIDER_HINTS = (
    ("Anthropic", ("claude",)),
    ("OpenAI",    ("gpt", "o1-", "o3-", "o4-", "chatgpt", "davinci", "codex")),
    ("Google",    ("gemini", "gemma")),
    ("Local",     ("llama", "qwen", "mistral", "ministral", "glm", "kimi",
                   "minimax", "nemotron", "grok", "ollama", "deepseek")),
)


def _model_provider(model_name: str) -> str:
    """Return the upstream provider for a given model string.

    Used to scope plan recommendations: spend on Anthropic models drives
    the Anthropic plan reco, spend on OpenAI models the ChatGPT one, etc.
    Returns "Other" when nothing matches.
    """
    if not model_name:
        return "Other"
    n = model_name.lower()
    for provider, keywords in _PROVIDER_HINTS:
        if any(k in n for k in keywords):
            return provider
    return "Other"


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
        "Last 1 month":  (now - timedelta(days=30),          None),
        "Last 2 months": (now - timedelta(days=60),          None),
        "Last 3 months": (now - timedelta(days=90),          None),
        "Last 6 months": (now - timedelta(days=180),         None),
        "Last year":    (now - timedelta(days=365),          None),
        "Forever":      (datetime.min.replace(tzinfo=timezone.utc), None),
    }


def resolve_period(period_name: str | None, default: str = "today") -> tuple[datetime, datetime | None, str]:
    if period_name is None and default == "all":
        return datetime.min.replace(tzinfo=timezone.utc), None, "All time"
    boundaries = period_boundaries()
    name = period_name or default
    if name.lower() in ("all", "tout"):
        return datetime.min.replace(tzinfo=timezone.utc), None, "All time"
    # Dynamic "N unit" (e.g. "5 days", "31 days", "12 hours", "2 weeks").
    import re as _re
    m = _re.fullmatch(r"\s*(\d+)\s*(hour|day|week|month|year)s?\s*",
                      name, _re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if n >= 1:
            per_unit = {"hour": timedelta(hours=1), "day": timedelta(days=1),
                        "week": timedelta(weeks=1), "month": timedelta(days=30),
                        "year": timedelta(days=365)}[unit]
            now = datetime.now(timezone.utc)
            plural = "" if n == 1 else "s"
            return now - per_unit * n, None, f"Last {n} {unit}{plural}"
    for bname, (start, end) in boundaries.items():
        if name.lower() in bname.lower():
            return start, end, bname
    valid = ", ".join(list(boundaries.keys()) + ["all", "N days/hours/weeks/..."])
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

    # Collapse any deeper subdirectory whose ancestor is itself a known
    # project. Catches patterns like
    #   ~/Code/benchmark/results/20260423_212607/sonnet-4-6
    # being attributed to ~/Code/benchmark when claude is launched from
    # an output subdirectory.
    p_obj = _Path(path)
    best_ancestor: str | None = None
    for ancestor in p_obj.parents:
        a_str = str(ancestor)
        if a_str in _all_known_paths and a_str != path:
            # Prefer the deepest matching ancestor.
            if best_ancestor is None or len(a_str) > len(best_ancestor):
                best_ancestor = a_str
    if best_ancestor:
        _worktree_cache[path] = best_ancestor
        return best_ancestor

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

def compute_overview_state(records: list[dict], exchanges: list[dict],
                           cutoff: datetime, cutoff_end: datetime | None,
                           period_label: str) -> dict:
    """Return a per-row stable signature of the overview, for diffing across
    watch refreshes. Keys mirror those used in `show_overview_tables`:
        ("period",  period_name, tool_name) → (tokens_in, tokens_out, cost, prompts, turns, api)
        ("project", normalized_proj, tool_name) → same tuple
        ("model",   model_name) → same tuple
    """
    boundaries = period_boundaries()
    if period_label == "All time":
        pass  # use full boundaries
    else:
        boundaries = {period_label: (cutoff, cutoff_end)}

    state: dict = {}

    def _bump(key, *, inp=0, out=0, cost=0.0, prompts=0, turns=0, api=0):
        cur = state.get(key, (0, 0, 0.0, 0, 0, 0))
        state[key] = (cur[0] + inp, cur[1] + out, cur[2] + cost,
                      cur[3] + prompts, cur[4] + turns, cur[5] + api)

    for rec in records:
        for period in classify_periods(rec["ts"], boundaries):
            _bump(("period", period, rec["tool"]),
                  inp=rec["input"], out=rec["output"], cost=rec["cost"], api=1)
        _bump(("project", normalize_project(rec["project"]), rec["tool"]),
              inp=rec["input"], out=rec["output"], cost=rec["cost"], api=1)
        _bump(("model", rec["model"]),
              inp=rec["input"], out=rec["output"], cost=rec["cost"], api=1)

    for ex in exchanges:
        ts = ex.get("ts")
        if ts is None:
            continue
        nturns = ex.get("num_turns", 0) or 0
        for period in classify_periods(ts, boundaries):
            _bump(("period", period, ex["tool"]), prompts=1, turns=nturns)
        _bump(("project", normalize_project(ex.get("project") or "unknown"), ex["tool"]),
              prompts=1, turns=nturns)
        m = ex.get("model") or ""
        if m:
            _bump(("model", m), prompts=1, turns=nturns)

    return state


def show_overview_tables(all_records: list[dict], speed_records: list[dict],
                         cutoff: datetime, cutoff_end: datetime | None,
                         period_label: str, tool_filter: str | None = None,
                         all_exchanges: list[dict] | None = None,
                         changed_keys: set | None = None):
    """Print period, project, model, and speed tables from a list of records.

    If `all_exchanges` is provided, three extra activity columns are added to
    each table — Prompts (user inputs), Turns (assistant turns within each
    exchange), and API (raw API calls). Otherwise these columns are omitted.

    If `changed_keys` is provided (a set produced by `compute_overview_state`
    diffed across two refreshes), rows whose compound key is in the set get a
    ◆ marker in their leftmost data column. Used by `tokstat --watch`.
    """
    show_activity = all_exchanges is not None
    exchanges = all_exchanges or []
    watching = changed_keys is not None
    marked_keys = changed_keys or set()

    def _mark(key) -> str:
        if not watching:
            return ""
        return f"{BYELLOW}◆{RESET} " if key in marked_keys else "  "

    # ─── 1. Consumption by period ──────────────────────────────────────
    boundaries = period_boundaries()
    if period_label == "All time":
        period_order = ["Last hour", "Last 5 hours", "Today", "Yesterday",
                        "Last 7 days", "Last 30 days", "Last 2 months",
                        "Last 3 months", "Last 6 months", "Last year", "Forever"]
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
            b["api_calls"]   += 1
            t = period_totals[period]
            t["input"]       += rec["input"]
            t["output"]      += rec["output"]
            t["cache_read"]  += rec["cache_read"]
            t["cache_write"] += rec["cache_write"]
            t["cost"]        += rec["cost"]
            t["api_calls"]   += 1

    for ex in exchanges:
        ts = ex.get("ts")
        if ts is None:
            continue
        nturns = ex.get("num_turns", 0) or 0
        for period in classify_periods(ts, boundaries):
            b = tool_period[ex["tool"]][period]
            b["prompts"] += 1
            b["turns"]   += nturns
            t = period_totals[period]
            t["prompts"] += 1
            t["turns"]   += nturns

    active_tools = sorted(set(r["tool"] for r in all_records))
    if show_activity:
        headers = ["Period", "Tool", "Prompts", "Turns", "API",
                   "Input", "Output", "Cache R", "Cache W", "Cost"]
        aligns  = ["<",      "<",    ">",       ">",     ">",
                   ">",     ">",      ">",       ">",       ">"]
    else:
        headers = ["Period", "Tool", "Input", "Output", "Cache R", "Cache W", "Cost"]
        aligns  = ["<",      "<",    ">",     ">",      ">",       ">",       ">"]
    rows = []
    blank_cols = len(headers)

    def _row(label, tool_disp, b, *, bold=False):
        def F(s):
            return f"{BOLD}{s}{RESET}" if bold else s
        no_tok = (b["input"] == 0 and b["output"] == 0
                  and b["cache_read"] == 0 and b["cache_write"] == 0)
        # No local token data (Kiro, recent Cursor) → flag it explicitly in
        # the cost column instead of a bare dash.
        cost_cell = f"{BYELLOW}⚠ no data{RESET}" if no_tok else fmt_cost(b["cost"])
        cells = [label, tool_disp]
        if show_activity:
            cells += [F(str(b["prompts"])), F(str(b["turns"])), F(str(b["api_calls"]))]
        cells += [F(fmt_tokens(b["input"])), F(fmt_tokens(b["output"])),
                  F(fmt_tokens(b["cache_read"])), F(fmt_tokens(b["cache_write"])),
                  F(cost_cell)]
        return cells

    for period in period_order:
        first = True
        any_changed_here = False
        for tool in active_tools:
            b = tool_period[tool].get(period)
            if not b:
                continue
            if b["input"] == 0 and b["output"] == 0:
                has_activity = show_activity and (b["prompts"] or b["turns"] or b["api_calls"])
                if not has_activity:
                    continue
            color = TOOL_COLORS.get(tool, "")
            key = ("period", period, tool)
            mark = _mark(key)
            if key in marked_keys:
                any_changed_here = True
            rows.append(_row(
                f"{BOLD}{period}{RESET}" if first else "",
                f"{mark}{color}{tool}{RESET}",
                b,
            ))
            first = False
        t = period_totals.get(period)
        if t and (t["input"] > 0 or t["output"] > 0):
            mark = (f"{BYELLOW}◆{RESET} " if watching and any_changed_here
                    else ("  " if watching else ""))
            rows.append(_row(
                f"{BOLD}{period}{RESET}" if first else "",
                f"{mark}{BOLD}TOTAL{RESET}",
                t,
                bold=True,
            ))
            rows.append([""] * blank_cols)

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
        b["api_calls"]   += 1
        t = proj_totals[p]
        t["input"]       += rec["input"]
        t["output"]      += rec["output"]
        t["cache_read"]  += rec["cache_read"]
        t["cache_write"] += rec["cache_write"]
        t["cost"]        += rec["cost"]
        t["api_calls"]   += 1

    for ex in exchanges:
        p = normalize_project(ex.get("project") or "unknown")
        nturns = ex.get("num_turns", 0) or 0
        b = proj_tool[p][ex["tool"]]
        b["prompts"] += 1
        b["turns"]   += nturns
        t = proj_totals[p]
        t["prompts"] += 1
        t["turns"]   += nturns

    sorted_projects = sorted(proj_totals.keys(), key=lambda p: proj_totals[p]["cost"], reverse=True)
    if show_activity:
        headers = ["Project", "Tool", "Prompts", "Turns", "API",
                   "Input", "Output", "Cache R", "Cache W", "Cost"]
        aligns  = ["<",       "<",    ">",       ">",     ">",
                   ">",     ">",      ">",       ">",       ">"]
    else:
        headers = ["Project", "Tool", "Input", "Output", "Cache R", "Cache W", "Cost"]
        aligns  = ["<",       "<",    ">",     ">",      ">",       ">",       ">"]
    rows = []
    blank_cols = len(headers)

    for proj in sorted_projects:
        first = True
        any_changed_here = False
        short = shorten_path(proj, 38)
        for tool in active_tools:
            b = proj_tool[proj].get(tool)
            if not b:
                continue
            if b["input"] == 0 and b["output"] == 0:
                # No tokens: still show the tool row if it has activity
                # (prompts/turns/calls), e.g. Kiro / Cursor [no tokens].
                has_activity = show_activity and (b["prompts"] or b["turns"] or b["api_calls"])
                if not has_activity:
                    continue
            color = TOOL_COLORS.get(tool, "")
            key = ("project", proj, tool)
            mark = _mark(key)
            if key in marked_keys:
                any_changed_here = True
            rows.append(_row(
                f"{BOLD}{short}{RESET}" if first else "",
                f"{mark}{color}{tool}{RESET}",
                b,
            ))
            first = False
        t = proj_totals[proj]
        mark = (f"{BYELLOW}◆{RESET} " if watching and any_changed_here
                else ("  " if watching else ""))
        rows.append(_row(
            f"{BOLD}{short}{RESET}" if first else "",
            f"{mark}{BOLD}TOTAL{RESET}",
            t,
            bold=True,
        ))
        rows.append([""] * blank_cols)

    w = calc_table_width(headers, rows)
    print(f"\n{'─' * w}")
    print(f"{BOLD} CONSUMPTION BY PROJECT{RESET}")
    print(f"{'─' * w}")
    print_table(headers, rows, aligns)

    # ─── 3. Model breakdown ────────────────────────────────────────────
    model_data = defaultdict(lambda: {"input": 0, "output": 0, "cost": 0.0, "tool": "",
                                      "prompts": 0, "turns": 0, "api_calls": 0})
    for rec in all_records:
        m = model_data[rec["model"]]
        m["input"]     += rec["input"]
        m["output"]    += rec["output"]
        m["cost"]      += rec["cost"]
        m["api_calls"] += 1
        m["tool"]       = rec["tool"]
    for ex in exchanges:
        model = ex.get("model") or ""
        if not model:
            continue
        m = model_data[model]
        m["prompts"] += 1
        m["turns"]   += ex.get("num_turns", 0) or 0
        if not m["tool"]:
            m["tool"] = ex.get("tool", "")

    sorted_models = sorted(model_data.keys(), key=lambda m: model_data[m]["cost"], reverse=True)
    if show_activity:
        headers = ["Model", "Tool", "Prompts", "Turns", "API", "Input", "Output", "Cost"]
        aligns  = ["<",     "<",    ">",       ">",     ">",   ">",     ">",      ">"]
    else:
        headers = ["Model", "Tool", "Input", "Output", "Cost"]
        aligns  = ["<",     "<",    ">",     ">",      ">"]
    rows = []
    any_model_changed = False
    for model in sorted_models:
        d = model_data[model]
        # Skip models with no tokens — unless we're showing activity and the
        # model still has prompts/turns/calls (e.g. Cursor [no tokens] rows).
        if d["input"] == 0 and d["output"] == 0:
            has_activity = show_activity and (d["prompts"] or d["turns"] or d["api_calls"])
            if not has_activity:
                continue
        color = TOOL_COLORS.get(d["tool"], "")
        key = ("model", model)
        mark = _mark(key)
        if key in marked_keys:
            any_model_changed = True
        no_tok = d["input"] == 0 and d["output"] == 0
        cost_cell = f"{BYELLOW}⚠ no data{RESET}" if no_tok else fmt_cost(d["cost"])
        row = [f"{mark}{model}", f"{color}{d['tool']}{RESET}"]
        if show_activity:
            row += [str(d["prompts"]), str(d["turns"]), str(d["api_calls"])]
        row += [fmt_tokens(d["input"]), fmt_tokens(d["output"]), cost_cell]
        rows.append(row)

    total_cost = sum(d["cost"] for d in model_data.values())
    total_in   = sum(d["input"] for d in model_data.values())
    total_out  = sum(d["output"] for d in model_data.values())
    total_mark = (f"{BYELLOW}◆{RESET} " if watching and any_model_changed
                  else ("  " if watching else ""))
    total_row = [f"{total_mark}{BOLD}ALL MODELS{RESET}", ""]
    if show_activity:
        total_prompts = sum(d["prompts"] for d in model_data.values())
        total_turns   = sum(d["turns"] for d in model_data.values())
        total_api     = sum(d["api_calls"] for d in model_data.values())
        total_row += [f"{BOLD}{total_prompts}{RESET}",
                      f"{BOLD}{total_turns}{RESET}",
                      f"{BOLD}{total_api}{RESET}"]
    total_row += [f"{BOLD}{fmt_tokens(total_in)}{RESET}",
                  f"{BOLD}{fmt_tokens(total_out)}{RESET}",
                  f"{BOLD}{fmt_cost(total_cost)}{RESET}"]
    rows.append(total_row)

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

    # ─── Grand total (as its own section) ─────────────────────────────
    total_all_tokens = sum(r["input"] + r["output"] + r["cache_read"] + r["cache_write"]
                           for r in all_records)
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    last_hour_records = [r for r in all_records if r["ts"] >= one_hour_ago]
    last_hour_tokens = sum(r["input"] + r["output"] + r["cache_read"] + r["cache_write"]
                           for r in last_hour_records)
    last_hour_cost = sum(r["cost"] for r in last_hour_records)
    active_tools_last_hour = sorted({r["tool"] for r in last_hour_records})

    if show_activity:
        n_prompts = sum(1 for ex in exchanges if ex.get("ts") is not None)
        n_turns   = sum((ex.get("num_turns", 0) or 0) for ex in exchanges)
        summary = (f"{fmt_tokens(total_all_tokens)} tokens · "
                   f"{n_prompts} prompts · {n_turns} turns · "
                   f"{len(all_records)} API calls")
    else:
        summary = f"{fmt_tokens(total_all_tokens)} tokens across {len(all_records)} API calls"

    period_line = (f"Period: {all_records[0]['ts'].strftime('%Y-%m-%d')} to "
                   f"{max(r['ts'] for r in all_records).strftime('%Y-%m-%d')}")
    rate_line   = (f"Current rate (last 60 min): "
                   f"{fmt_tokens(last_hour_tokens)} t/h · "
                   f"{fmt_cost(last_hour_cost)}/h")

    # Compute box width from the longest plain-text content line.
    inner_lines = [summary, f"Estimated cost: {fmt_cost(total_cost)}",
                   rate_line, period_line]
    plain = [_strip_ansi(s) for s in inner_lines]
    w = max(len(p) for p in plain) + 4  # padding
    print(f"\n{'─' * w}")
    print(f"{BOLD} GRAND TOTAL{RESET}")
    print(f"{'─' * w}")
    print(f"  {BOLD}Total:{RESET}        {summary}")
    print(f"  {BOLD}Estimated:{RESET}    {fmt_cost(total_cost)}")
    if active_tools_last_hour:
        agents_str = ", ".join(f"{TOOL_COLORS.get(t, '')}{t}{RESET}"
                               for t in active_tools_last_hour)
        agents_suffix = f"  {DIM}({agents_str}{DIM}){RESET}"
    else:
        agents_suffix = ""
    print(f"  {BOLD}Last 60 min:{RESET}  {fmt_tokens(last_hour_tokens)} t/h · "
          f"{fmt_cost(last_hour_cost)}/h{agents_suffix}")
    print(f"  {DIM}{period_line}{RESET}")
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

    # Thresholds (cost / turns medians and P90) are computed PER TOOL.
    # Mixing e.g. Claude Code (cache-heavy, costly) with web-export ChatGPT
    # (output-only, near-zero estimated cost) into one global median makes
    # every tool's anomaly bar wrong. A "10x median Codex prompt" should be
    # judged against Codex, not against the whole fleet.
    def _stats(values, default=0):
        if not values:
            return default, default
        s = sorted(values)
        med = s[len(s) // 2]
        p90 = s[min(len(s) - 1, len(s) * 9 // 10)]
        return med, p90

    per_tool_stats: dict[str, dict] = {}
    by_tool = defaultdict(list)
    for p in all_prompts:
        by_tool[p.get("tool", "?")].append(p)
    for tname, prompts in by_tool.items():
        c_med, c_p90 = _stats([p["cost"] for p in prompts if p["cost"] > 0])
        t_med, t_p90 = _stats([p["num_turns"] for p in prompts if p["num_turns"] > 0], 1)
        # Input/output ratio is structural per tool — Codex routinely sends
        # huge inputs for tiny outputs. Flag "context bloat" only when a
        # prompt is an outlier *for its own tool*, not against a fixed 50:1.
        ratios = [p["tokens"]["input"] / p["tokens"]["output"]
                  for p in prompts
                  if p["tokens"]["input"] > 10_000 and p["tokens"]["output"] > 0]
        _, r_p90 = _stats(ratios)
        per_tool_stats[tname] = {
            "median_cost": c_med or 0, "p90_cost": c_p90 or 0,
            "median_turns": t_med or 1, "p90_turns": t_p90 or 1,
            "p90_ratio": r_p90 or 0,
        }

    _warm_worktree_cache(set(p.get("project") or "unknown" for p in all_prompts))

    anomalies = []

    for p in all_prompts:
        tool_name = p.get("tool", "?")
        st = per_tool_stats.get(tool_name, {})
        median_cost  = st.get("median_cost", 0)
        p90_cost     = st.get("p90_cost", 0)
        median_turns = st.get("median_turns", 1)
        p90_turns    = st.get("p90_turns", 1)
        p90_ratio    = st.get("p90_ratio", 0)
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

        def _x_median(value, med):
            return f"{value/med:.0f}x median" if med > 0 else "no baseline"

        if p["cost"] > 0 and p90_cost > 0 and p["cost"] > p90_cost * 10:
            _add("high", "Runaway cost", f"{fmt_cost(p['cost'])} ({_x_median(p['cost'], median_cost)})")
        elif p["cost"] > 0 and p90_cost > 0 and p["cost"] > p90_cost * 5:
            _add("medium", "High cost", f"{fmt_cost(p['cost'])} ({_x_median(p['cost'], median_cost)})")
        if total_tools > 30:
            _add("high" if total_tools > 60 else "medium", "Tool storm", f"{total_tools} tool calls")
        if p["num_turns"] > 0 and p90_turns > 0 and p["num_turns"] > p90_turns * 5:
            _add("high" if p["num_turns"] > p90_turns * 10 else "medium", "Turn spiral",
                 f"{p['num_turns']} turns ({_x_median(p['num_turns'], median_turns)})")
        if tok["cache_write"] > 50_000 and tok["cache_read"] < tok["cache_write"] * 0.5:
            ratio = tok["cache_read"] / tok["cache_write"] if tok["cache_write"] > 0 else 0
            _add("medium", "Cache thrashing",
                 f"{fmt_tokens(tok['cache_write'])} written, only {ratio:.0%} read back")
        # Context bloat: outlier vs this tool's own P90 ratio (and >2x it),
        # so a tool that's always input-heavy doesn't flag every prompt.
        if (tok["input"] > 10_000 and tok["output"] > 0 and p90_ratio > 0
                and tok["input"] / tok["output"] > max(p90_ratio * 2, 50)):
            _add("low", "Context bloat",
                 f"{fmt_tokens(tok['input'])} in / {fmt_tokens(tok['output'])} out "
                 f"(ratio {tok['input']/tok['output']:.0f}:1, tool P90 {p90_ratio:.0f}:1)")
        if p["num_turns"] > 5 and tok["output"] < 100:
            _add("medium", "Empty exchange",
                 f"{p['num_turns']} turns but only {tok['output']} output tokens")

    def _stats_summary() -> str:
        parts = []
        for tname in sorted(per_tool_stats, key=lambda t: -len(by_tool[t])):
            st = per_tool_stats[tname]
            parts.append(f"{tname}: med {fmt_cost(st['median_cost'])} / "
                         f"P90 {fmt_cost(st['p90_cost'])}")
        return "  ·  ".join(parts)

    if not anomalies:
        print(f"  {DIM}No anomalies detected.{RESET}")
        print(f"  {DIM}{len(all_prompts)} exchanges — per-tool cost baseline: "
              f"{_stats_summary()}{RESET}\n")
        return

    print(f"  {DIM}{len(all_prompts)} exchanges analyzed (thresholds per tool) — "
          f"{_stats_summary()}{RESET}\n")

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

def _reco_anthropic(mp: float) -> None:
    """Print the Anthropic plan recommendation for `mp` $/month projected."""
    head = f"    {BOLD}Anthropic (Claude){RESET} — "
    proj = f"{DIM}{fmt_cost(mp)}/mo projected{RESET}"
    if mp <= 5:
        print(head + f"{GREEN}Free tier{RESET} covers it. {proj}")
    elif mp <= 18:
        print(head + f"{GREEN}Pro ($20/mo){RESET} covers it. {proj}")
    elif mp <= 100:
        if mp > 30:
            print(head + f"{BYELLOW}Max 5x ($100/mo){RESET} recommended. {proj}")
        else:
            print(head + f"{GREEN}Pro ($20/mo){RESET} ok, approaching Max. {proj}")
    elif mp <= 200:
        print(head + f"{BYELLOW}Max 5x ($100/mo){RESET} or "
              f"{BOLD}Max 20x ($200/mo){RESET} recommended. {proj}")
        print(f"      {DIM}Max 5x saves ~{fmt_cost(mp - 100)}/mo vs API{RESET}")
    else:
        print(head + f"{BRED}Max 20x ($200/mo){RESET} strongly recommended. {proj}")
        print(f"      {DIM}Saves ~{fmt_cost(mp - 200)}/mo vs API{RESET}")
        if mp > 500:
            print(f"      {BRED}Consider Enterprise / Team for volume{RESET}")


def _reco_openai(mp: float) -> None:
    """OpenAI side: ChatGPT (Plus/Pro) for chat usage, API for Codex etc."""
    head = f"    {BOLD}OpenAI (GPT){RESET} — "
    proj = f"{DIM}{fmt_cost(mp)}/mo projected{RESET}"
    if mp <= 5:
        print(head + f"{GREEN}Free tier{RESET} likely covers it. {proj}")
    elif mp <= 20:
        print(head + f"{GREEN}ChatGPT Plus ($20/mo){RESET} covers chat usage. {proj}")
        print(f"      {DIM}For Codex/API: pay-per-token cheaper at this volume{RESET}")
    elif mp <= 200:
        print(head + f"{BYELLOW}ChatGPT Plus ($20/mo){RESET} or "
              f"{BOLD}Pro ($200/mo){RESET} depending on usage type. {proj}")
        print(f"      {DIM}Pro = higher ChatGPT quotas; Codex stays API-billed{RESET}")
    else:
        print(head + f"{BRED}ChatGPT Pro ($200/mo){RESET} for chat, "
              f"API direct for Codex. {proj}")
        if mp > 500:
            print(f"      {DIM}Consider OpenAI Business / Enterprise{RESET}")


def _reco_google(mp: float) -> None:
    """Google side: Gemini Advanced for chat, API for Gemini CLI."""
    head = f"    {BOLD}Google (Gemini){RESET} — "
    proj = f"{DIM}{fmt_cost(mp)}/mo projected{RESET}"
    if mp <= 5:
        print(head + f"{GREEN}Free tier{RESET} covers it. {proj}")
    elif mp <= 20:
        print(head + f"{GREEN}Gemini Advanced ($20/mo){RESET} covers chat. {proj}")
    else:
        print(head + f"{BYELLOW}Gemini Advanced{RESET} for chat, "
              f"API for Gemini CLI. {proj}")


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
        api_calls   = len(period_exs)
        daily_calls = api_calls / days_span
        active_days = len(set(e["ts"].strftime("%Y-%m-%d") for e in period_exs))
        models = set(e.get("model") or "?" for e in period_exs)
        cache_ratio = (total_cache_r / (total_cache_r + total_cache_w)
                       if (total_cache_r + total_cache_w) > 0 else 0)

        # Monthly projection: base it on the most recent 30 days of *data*
        # rather than the whole calendar span. Over a long --period all
        # (e.g. a 3-year chat export) dividing by ~1200 days produces an
        # absurdly low figure that understates real recent spend.
        recent_cutoff = last_ts - timedelta(days=30)
        recent_exs = [e for e in period_exs if e["ts"] >= recent_cutoff]
        recent_days = max(1, min(30, (last_ts - min(e["ts"] for e in recent_exs)).days + 1)) \
            if recent_exs else 1
        recent_cost = sum(e.get("cost", 0) for e in recent_exs)
        monthly_projected = recent_cost / recent_days * 30

        model_costs = defaultdict(float)
        model_calls = defaultdict(int)
        model_cache = defaultdict(lambda: [0, 0])  # model -> [cache_r, cache_w]
        provider_costs = defaultdict(float)
        provider_recent = defaultdict(float)
        for e in period_exs:
            m = e.get("model") or "?"
            cost = e.get("cost", 0)
            model_costs[m] += cost
            model_calls[m] += 1
            model_cache[m][0] += e["tokens"]["cache_read"]
            model_cache[m][1] += e["tokens"]["cache_write"]
            provider_costs[_model_provider(m)] += cost
        for e in recent_exs:
            provider_recent[_model_provider(e.get("model") or "?")] += e.get("cost", 0)

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
            "model_cache": {m: tuple(v) for m, v in model_cache.items()},
            "provider_costs": dict(provider_costs),
            "provider_recent": dict(provider_recent),
            "recent_days": recent_days,
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
        cr, cw = a["model_cache"].get(model, (0, 0))
        m_cache = f"{cr / (cr + cw) * 100:.0f}%" if (cr + cw) > 0 else "—"
        rows.append([model, str(calls), fmt_cost(mc),
                     f"{fmt_cost(daily)}/d", f"{fmt_cost(daily*30)}/mo",
                     m_cache, f"{share:.0f}%"])
    rows.append([f"{BOLD}TOTAL{RESET}", f"{BOLD}{a['api_calls']}{RESET}",
                 f"{BOLD}{fmt_cost(a['total_cost'])}{RESET}",
                 f"{BOLD}{fmt_cost(a['daily_cost'])}/d{RESET}",
                 f"{BOLD}{fmt_cost(a['monthly_projected'])}/mo{RESET}",
                 f"{a['cache_ratio']*100:.0f}%", ""])
    print_table(headers, rows, aligns)
    print()

    # ─── Plan recommendation, scoped per upstream provider ─────────────
    # Projections use spend over the most recent 30 days of data, so a long
    # historical period doesn't dilute the recommendation.
    print(f"  {BOLD}Plan{RESET} {DIM}(projected from last {a['recent_days']}d of activity){RESET}")
    print(f"  {'─' * 60}")
    provider_recent = a["provider_recent"]
    recent_days = a["recent_days"]
    # Skip Local (no subscription) and providers with negligible spend.
    relevant = sorted(
        ((p, c) for p, c in provider_recent.items()
         if p in ("Anthropic", "OpenAI", "Google", "Other") and c > 0.10),
        key=lambda x: -x[1],
    )
    if not relevant:
        print(f"    {DIM}No billable usage in the recent window "
              f"(local models / zero-cost only).{RESET}\n")
    for provider, recent in relevant:
        mp = recent / recent_days * 30
        if provider == "Anthropic":
            _reco_anthropic(mp)
        elif provider == "OpenAI":
            _reco_openai(mp)
        elif provider == "Google":
            _reco_google(mp)
        else:
            print(f"    {BOLD}{provider}{RESET}: {fmt_cost(mp)}/mo projected")
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


# ─── Shared display: activity calendar ────────────────────────────────────

# GitHub-style 5-level intensity ramp (256-color greens, dark → bright).
_ACTIVITY_COLORS = ("\033[38;5;238m", "\033[38;5;22m", "\033[38;5;28m",
                    "\033[38;5;34m", "\033[38;5;40m")
_ACTIVITY_GLYPH = "■"


def _activity_level(count: int, thresholds: list[int]) -> int:
    """Map a daily count to a 0-4 intensity level using precomputed thresholds."""
    if count <= 0:
        return 0
    for i, t in enumerate(thresholds):
        if count <= t:
            return i + 1
    return 4


def show_activity(collect_fn, period_name: str | None = None,
                  tool_filter: str | None = None):
    """Render a GitHub-style contribution calendar of activity over the period.

    Cells are colored by the number of prompts per day; the summary reports
    total prompts, turns and tokens. Respects --period and --tool.
    """
    print(f"\n{BOLD} Activity Overview{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    print(f"{DIM}  Scanning exchanges...{RESET}\n")

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

    from datetime import date as _date, timedelta as _td

    all_exchanges, _ = collect_fn(cutoff, tool_filter, cutoff_end)
    all_exchanges = [e for e in all_exchanges if e.get("ts")]
    if not all_exchanges:
        print(f"  {YELLOW}No activity found.{RESET}\n")
        return

    # Per-day aggregation (local date) from the freshly-scanned exchanges.
    day_prompts: dict[str, int] = defaultdict(int)
    day_turns:   dict[str, int] = defaultdict(int)
    day_tokens:  dict[str, int] = defaultdict(int)
    for e in all_exchanges:
        d = e["ts"].astimezone().strftime("%Y-%m-%d")
        tok = e.get("tokens") or {}
        day_prompts[d] += 1
        day_turns[d]   += e.get("num_turns", 0) or 0
        day_tokens[d]  += (tok.get("input", 0) + tok.get("output", 0)
                           + tok.get("cache_read", 0) + tok.get("cache_write", 0))

    # Date range: clamp to the data, but no more than ~53 weeks (GitHub width).
    all_days = sorted(day_prompts)
    first = datetime.strptime(all_days[0], "%Y-%m-%d").date()
    last  = datetime.strptime(all_days[-1], "%Y-%m-%d").date()
    max_span = _td(weeks=53)
    if last - first > max_span:
        first = last - max_span

    # Align the grid to start on a Monday (rows Mon..Sun, columns = weeks).
    grid_start = first - _td(days=first.weekday())
    grid_end   = last + _td(days=(6 - last.weekday()))
    n_weeks = ((grid_end - grid_start).days // 7) + 1

    # Intensity thresholds from the nonzero daily prompt counts (quartiles).
    counts = sorted(c for c in day_prompts.values() if c > 0)
    if counts:
        def q(p):
            return counts[min(len(counts) - 1, int(len(counts) * p))]
        thresholds = [max(1, q(0.25)), max(2, q(0.50)), max(3, q(0.75))]
    else:
        thresholds = [1, 2, 3]

    # Header: a year row above a month row. The year is placed (once, and
    # again whenever it changes) at the column of its first month, so a grid
    # spanning a year boundary isn't ambiguous — without crowding the months.
    width = n_weeks * 2 + 4
    year_row  = [" "] * width
    month_row = [" "] * width
    last_month = None
    last_year = None
    for w in range(n_weeks):
        col_date = grid_start + _td(weeks=w)
        if col_date.month != last_month:
            for i, ch in enumerate(col_date.strftime("%b")):
                if w * 2 + i < width:
                    month_row[w * 2 + i] = ch
            if col_date.year != last_year:
                for i, ch in enumerate(str(col_date.year)):
                    if w * 2 + i < width:
                        year_row[w * 2 + i] = ch
                last_year = col_date.year
            last_month = col_date.month
    print("       " + "".join(year_row).rstrip())
    print("       " + "".join(month_row).rstrip())

    # Day rows.
    day_labels = ["Mon", "   ", "Wed", "   ", "Fri", "   ", "Sun"]
    today = _date.today()
    for dow in range(7):
        cells = []
        for w in range(n_weeks):
            cell_date = grid_start + _td(weeks=w, days=dow)
            if cell_date > today or cell_date > last or cell_date < first:
                cells.append("  ")
                continue
            key = cell_date.strftime("%Y-%m-%d")
            lvl = _activity_level(day_prompts.get(key, 0), thresholds)
            cells.append(f"{_ACTIVITY_COLORS[lvl]}{_ACTIVITY_GLYPH}{RESET} ")
        print(f"  {DIM}{day_labels[dow]}{RESET}  " + "".join(cells))

    # Legend.
    ramp = " ".join(f"{c}{_ACTIVITY_GLYPH}{RESET}" for c in _ACTIVITY_COLORS)
    print(f"\n  {DIM}Less{RESET} {ramp} {DIM}More{RESET}  "
          f"{DIM}(intensity = prompts/day){RESET}")

    # Summary.
    total_prompts = sum(day_prompts.values())
    total_turns   = sum(day_turns.values())
    total_tokens  = sum(day_tokens.values())
    active_days   = len(day_prompts)
    busiest = max(day_prompts.items(), key=lambda kv: kv[1])
    print(f"\n  {BOLD}{total_prompts}{RESET} prompts · {BOLD}{total_turns}{RESET} turns · "
          f"{BOLD}{fmt_tokens(total_tokens)}{RESET} tokens "
          f"over {BOLD}{active_days}{RESET} active day(s)")
    print(f"  {DIM}Busiest day: {busiest[0]} ({busiest[1]} prompts){RESET}")

    # Energy & CO2 estimate (EcoLogits, usage phase) — one-line summary.
    e_mid, g_mid, region = _estimate_total_impact(all_exchanges)
    if e_mid is not None:
        print(f"  {DIM}≈ {e_mid:.1f} kWh · {g_mid:.1f} kg CO₂e "
              f"(usage est., {region} mix — see --impact){RESET}")
    print()


# ─── Shared display: total ─────────────────────────────────────────────────

def show_total(collect_fn, period_name: str | None = None,
               tool_filter: str | None = None):
    """Compact totals: tokens and cost for the selected period/tool, plus the
    actual date span covered by the data and a per-tool breakdown."""
    print(f"\n{BOLD} Total{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    print(f"{DIM}  Scanning exchanges...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    all_exchanges, _ = collect_fn(cutoff, tool_filter, cutoff_end)
    all_exchanges = [e for e in all_exchanges if e.get("ts")]
    if not all_exchanges:
        print(f"  {YELLOW}No data found.{RESET}\n")
        return

    inp = out = cr = cw = 0
    cost = 0.0
    turns = 0
    per_tool = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "prompts": 0})
    days = set()
    for e in all_exchanges:
        tok = e.get("tokens") or {}
        i = tok.get("input", 0); o = tok.get("output", 0)
        rr = tok.get("cache_read", 0); ww = tok.get("cache_write", 0)
        inp += i; out += o; cr += rr; cw += ww
        c = e.get("cost", 0) or 0
        cost += c
        turns += e.get("num_turns", 0) or 0
        day = e["ts"].astimezone().strftime("%Y-%m-%d")
        days.add(day)
        t = per_tool[e.get("tool", "?")]
        t["tokens"] += i + o + rr + ww
        t["cost"]   += c
        t["prompts"] += 1
        if not t.get("first") or day < t["first"]:
            t["first"] = day
        if not t.get("last") or day > t["last"]:
            t["last"] = day

    total_tokens = inp + out + cr + cw
    prompts = len(all_exchanges)
    first = min(e["ts"] for e in all_exchanges).astimezone().strftime("%Y-%m-%d")
    last  = max(e["ts"] for e in all_exchanges).astimezone().strftime("%Y-%m-%d")

    scope = period_label + (f" · {tool_filter}" if tool_filter else "")

    # Badge: a bordered card with the headline cost + tokens, then details.
    # Keep the text at normal brightness for legibility; only the cost is
    # accented (green) and the headline figures bold.
    inner = [
        f"{BOLD}TOTAL · {scope}{RESET}",
        "",
        f"{BOLD}{GREEN}{fmt_cost(cost)}{RESET}    {BOLD}{fmt_tokens(total_tokens)}{RESET} tokens",
        f"in {fmt_tokens(inp)} · out {fmt_tokens(out)} · "
        f"cache {fmt_tokens(cr)}/{fmt_tokens(cw)}",
        "",
        f"{prompts} prompts · {turns} turns · {len(days)} active day(s)",
        f"{first} → {last}",
    ]
    w = max(len(_strip_ansi(s)) for s in inner)
    print(f"  ╭─{'─' * w}─╮")
    for s in inner:
        pad = " " * (w - len(_strip_ansi(s)))
        print(f"  │ {s}{pad} │")
    print(f"  ╰─{'─' * w}─╯")

    if not tool_filter and len(per_tool) > 1:
        print(f"\n  {DIM}By tool:{RESET}")
        for name, d in sorted(per_tool.items(), key=lambda kv: -kv[1]["cost"]):
            color = TOOL_COLORS.get(name, "")
            cost_str = fmt_cost(d["cost"]) if d["tokens"] else "⚠ no data"
            span = f"{d.get('first')} → {d.get('last')}"
            print(f"    {color}{name:<12}{RESET} {cost_str:>10}   "
                  f"{DIM}{fmt_tokens(d['tokens']):>7} tokens · {d['prompts']:>4} prompts · "
                  f"{span}{RESET}")
    print()


# ─── Conversation quality audit ────────────────────────────────────────────

_AUDIT_SEV_LABEL = {0: "info", 1: "low", 2: "med", 3: "high"}
_AUDIT_SEV_COLOR = {0: DIM, 1: CYAN, 2: BYELLOW, 3: BRED}


def _audit_norm(s: str) -> str:
    return " ".join((s or "").lower().replace("*", "").replace("`", "").split())


def _excerpt_around(text: str, evidence: str, width: int = 800) -> str:
    """A window of the turn CENTERED on the evidence quote, not the turn's head.
    Long turns often carry the flagged statement far in; a head-only excerpt
    shows unrelated intro text and makes a verifier hallucinate contradictions.
    Falls back to the head when the quote isn't found."""
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    probe = " ".join((evidence or "").split())[:40]
    pos = flat.lower().find(probe.lower()) if len(probe) >= 12 else -1
    if pos < 0:
        return flat[:width]
    start = max(0, pos - width // 2)
    end = min(len(flat), pos + len(probe) + width // 2)
    return ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _audit_finding_context(conv, turn_index, evidence=""):
    """Pull the real transcript context for a finding so the user can judge it.

    Small models often report a WRONG turn index while quoting real text, so we
    locate the turn by matching the evidence quote against the assistant turns
    (falling back to the reported index). If no turn contains the evidence, the
    'quote' was likely fabricated — signalled via turn_ok=False.
    Returns (excerpt, user_ask, user_reaction, turn_ok, resolved_index), where
    user_reaction is the FIRST user turn AFTER the flagged one — so a verifier
    can see whether a claim was retracted only after the user pushed back."""
    turns = conv.turns
    probe = _audit_norm(evidence)[:60]
    usable = len(probe) >= 12
    def _has(t):
        return usable and probe in _audit_norm(t.text)

    idx = turn_index if 0 <= turn_index < len(turns) else None
    if usable:
        # Verify/locate by the quote: trust the index only if its text contains
        # the quote; otherwise find the assistant turn that does. If none does,
        # the quote is fabricated/paraphrased → unlocatable.
        if idx is not None and _has(turns[idx]):
            turn_ok = True
        else:
            match = next((j for j, t in enumerate(turns)
                          if t.role == "assistant" and _has(t)), None)
            idx, turn_ok = (match, True) if match is not None else (idx, False)
    else:
        # No usable quote to verify — fall back to a valid reported index.
        turn_ok = idx is not None
    excerpt = ask = reaction = ""
    if turn_ok:
        excerpt = _excerpt_around(turns[idx].text, evidence, width=800)
        for j in range(idx - 1, -1, -1):            # the preceding user request
            if turns[j].role == "user" and turns[j].text:
                ask = " ".join(turns[j].text.split())[:220]
                break
        for j in range(idx + 1, len(turns)):         # the following user reaction
            if turns[j].role == "user" and turns[j].text:
                reaction = " ".join(turns[j].text.split())[:220]
                break
    return excerpt, ask, reaction, turn_ok, (idx if turn_ok else turn_index)


def show_audit(collect_fn, period_name: str | None = None,
               tool_filter: str | None = None, judge_model: str | None = None,
               judge_max: int | None = None, limit: int = 20,
               verify: bool = False,
               ollama_judge: bool = False,
               claude_judge: bool = False, claude_model: str | None = None,
               codex_judge: bool = False, codex_model: str | None = None):
    """Audit conversations across all tools for behavioural/quality issues.

    The judge is chosen explicitly — at least one of these must be passed, or
    the audit errors out (there is no implicit default judge):

      • `ollama_judge`  — judge LOCALLY via Ollama (nothing leaves the machine).
                          `judge_model` picks the model(s), comma-separated for
                          a multi-model panel; auto-picks one if omitted.
      • `claude_judge`  — judge via the Claude CLI (`claude_model` picks it).
      • `codex_judge`   — judge via the Codex CLI (`codex_model` picks it).

    Each selected backend is an AUTONOMOUS judge that runs the full audit and
    produces its own findings; the CLI judges are NOT arbiters of Ollama's
    output. When several are selected they form a PANEL and findings are
    aggregated by vote (a finding flagged by more judges ranks higher). The CLI
    judges send transcript excerpts to their API — the non-local part — so they
    are opt-in and warned. `judge_max` caps how many (most recent) conversations
    are judged (None = no cap). `verify` re-checks findings with a skeptical
    second pass, each one via the SAME judge (engine + model) that raised it."""
    from tokstat import _audit

    print(f"\n{BOLD} Conversation Audit{RESET}")
    print(f"{DIM}  Scanning conversations across all tools...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    all_exchanges, _ = collect_fn(cutoff, tool_filter, cutoff_end)
    convs = _audit.conversations_from_exchanges(all_exchanges)
    if not convs:
        print(f"  {YELLOW}No conversations found for this period.{RESET}\n")
        return

    # ── Select judge backend(s). Each is an AUTONOMOUS judge (runs the full
    # audit, produces its own findings); several → a panel aggregated by vote.
    # At least one must be requested — there is no implicit default judge.
    if not (ollama_judge or claude_judge or codex_judge):
        print(f"  {RED}⚠ No judge selected — nothing to do.{RESET}")
        print(f"  {DIM}Pass at least one judge: {BOLD}--ollama-judge{RESET}{DIM} "
              f"(local, via Ollama), {BOLD}--claude-judge{RESET}{DIM} or "
              f"{BOLD}--codex-judge{RESET}{DIM} (off-machine, via that CLI). "
              f"Combine them for a voting panel.{RESET}\n")
        return

    # voters: each {label, kind, fn(conv)->(findings, stats)}
    voters: list = []
    if ollama_judge:
        installed = _audit.list_ollama_models()
        if not installed:
            print(f"  {YELLOW}⚠ --ollama-judge: Ollama unreachable on "
                  f"{_audit.OLLAMA_HOST} (or no model installed) — skipped.{RESET}")
        else:
            if judge_model:
                oms = list(dict.fromkeys(m.strip() for m in judge_model.split(",")
                                         if m.strip()))
            else:
                auto = _audit.pick_ollama_model()
                oms = [auto] if auto else []
            missing = [m for m in oms if not _audit.ollama_has_model(m)]
            if missing:
                print(f"  {YELLOW}⚠ Ollama model(s) not installed: "
                      f"{', '.join(missing)} — skipped.{RESET}")
                print(f"  {DIM}Installed: {', '.join(installed)}{RESET}")
            for m in oms:
                if m and m not in missing:
                    voters.append({"label": m, "kind": "ollama", "model": m,
                                   "fn": (lambda c, _m=m:
                                          _audit.judge_conversation_ollama(c, _m))})
            if not any(v["kind"] == "ollama" for v in voters):
                print(f"  {YELLOW}⚠ --ollama-judge: no usable model "
                      f"(pass one with --model).{RESET}")
    if claude_judge:
        if _audit.claude_cli_available():
            voters.append({"label": "Claude", "kind": "claude",
                           "model": claude_model,
                           "fn": (lambda c: _audit.judge_conversation_claude(
                               c, claude_model))})
        else:
            print(f"  {YELLOW}⚠ --claude-judge: 'claude' CLI not found on PATH — "
                  f"skipped.{RESET}")
    if codex_judge:
        if _audit.codex_cli_available():
            voters.append({"label": "Codex", "kind": "codex",
                           "model": codex_model,
                           "fn": (lambda c: _audit.judge_conversation_codex(
                               c, codex_model))})
        else:
            print(f"  {YELLOW}⚠ --codex-judge: 'codex' CLI not found on PATH — "
                  f"skipped.{RESET}")

    if not voters:
        print(f"\n  {RED}⚠ No usable judge — nothing was audited.{RESET}\n")
        return

    def _in_window(f):
        # Keep only findings whose offending turn falls in the selected period.
        if f.ts is None:
            return True
        return f.ts >= cutoff and (cutoff_end is None or f.ts < cutoff_end)

    # Judge the most recent conversations first; judge_max=None means all.
    ordered = sorted(convs, key=lambda c: c.ts_last or c.ts, reverse=True)
    to_judge = ordered if judge_max is None else ordered[:judge_max]
    judged_n = len(to_judge)
    cap_note = "" if judge_max is None else f" (capped at {judge_max})"
    labels = ", ".join(v["label"] for v in voters)
    kinds = {v["kind"] for v in voters}
    where = ("Fully local — nothing leaves the machine."
             if kinds == {"ollama"} else "")
    head = "Judge panel" if len(voters) > 1 else "Judge"
    print(f"{DIM}  {head}: {BOLD}{labels}{RESET}{DIM} on "
          f"{judged_n}/{len(convs)} conversations{cap_note}. {where}{RESET}")
    cli_labels = [v["label"] for v in voters if v["kind"] in ("claude", "codex")]
    if cli_labels:
        names = " + ".join(cli_labels)
        print(f"  {BYELLOW}⚠ {names}: conversation excerpts are sent to the "
              f"provider API{RESET}{DIM} — off-machine. Ctrl-C now to cancel.{RESET}")

    # Aggregate findings across judges: same (session, metric, turn) → one
    # issue, with the set of judges that flagged it (its "votes").
    #
    # One WAVE PER JUDGE (judge in the outer loop): for Ollama this keeps a
    # model warm in VRAM between consecutive calls; for the CLI judges it just
    # runs them in turn.
    agg: dict = {}
    verrors: dict = {v["label"]: 0 for v in voters}
    # Per-judge stats: Ollama timing (p_tok/…) OR CLI cost/tokens (cost/in/…).
    vstats: dict = {v["label"]: {"p_tok": 0, "p_ns": 0, "o_tok": 0, "o_ns": 0,
                                 "cost": 0.0, "in": 0, "out": 0, "tokens": 0}
                    for v in voters}
    live = []           # judge labels that ran at least partly
    live_ollama = []    # Ollama judge labels (for --verify + speed table)
    for vi, v in enumerate(voters, 1):
        label = v["label"]
        if len(voters) > 1:
            print(f"{DIM}  Judge {vi}/{len(voters)}: {BOLD}{label}{RESET}",
                  flush=True)
        broke = False
        for k, c in enumerate(to_judge, 1):
            proj = normalize_project(c.project) if c.project else "?"
            proj = "/".join(proj.rstrip("/").split("/")[-2:]) or proj
            day = c.ts.date().isoformat() if c.ts else "?"
            tcol = TOOL_COLORS.get(c.tool, "")
            # Pass-1 trace: print the [k/N] line, then append the result of
            # judging THIS conversation (findings raised, or "clean" / failure).
            print(f"{DIM}    [{k}/{judged_n}] {tcol}{c.tool}{RESET}{DIM} · "
                  f"{proj} · {day} · {len(c.turns)} turns{RESET}",
                  end="", flush=True)
            try:
                res, stats = v["fn"](c)
            except _audit.JudgeError as e:
                verrors[label] += 1
                print(f"{DIM} → {RESET}{YELLOW}⚠ failed: {e}{RESET}")
                # A judge that fails on its very first call can't run at all —
                # skip the rest of its wave instead of repeating the error.
                if k == 1:
                    print(f"      {YELLOW}↳ skipping {label}.{RESET}")
                    broke = True
                    break
                continue
            s = vstats[label]
            if v["kind"] == "ollama":
                for key_ in ("p_tok", "p_ns", "o_tok", "o_ns"):
                    s[key_] += stats.get(key_, 0)
            else:
                s["cost"] += stats.get("cost_usd", 0.0)
                s["in"] += stats.get("in_tok", 0)
                s["out"] += stats.get("out_tok", 0)
                s["tokens"] += stats.get("tokens", 0)
            inwin = [f for f in res if _in_window(f)]
            if inwin:
                mc: dict = {}
                for f in inwin:
                    mc[f.metric] = mc.get(f.metric, 0) + 1
                summ = ", ".join(f"{m}×{n}" if n > 1 else m
                                 for m, n in mc.items())
                print(f"{DIM} → {RESET}{BYELLOW}{len(inwin)} finding"
                      f"{'s' if len(inwin) != 1 else ''}{RESET}{DIM}: {summ}{RESET}")
            else:
                print(f"{DIM} → {GREEN}clean{RESET}")
            for f in inwin:
                excerpt, ask, reaction, turn_ok, ridx = _audit_finding_context(
                    c, f.turn_index, f.evidence)
                # Key on the RESOLVED turn so panel votes aggregate even when
                # judges report different (wrong) indices for the same issue.
                key = (f.session_id, f.metric, ridx if turn_ok else f.turn_index)
                rec = agg.get(key)
                if rec is None:
                    agg[key] = {"best": f, "voters": {label}, "excerpt": excerpt,
                                "ask": ask, "reaction": reaction,
                                "turn_ok": turn_ok, "turn": ridx,
                                # engine+model that raised the kept finding, so
                                # --verify re-checks it with the SAME judge.
                                "best_kind": v["kind"], "best_model": v.get("model")}
                else:
                    rec["voters"].add(label)
                    if f.severity > rec["best"].severity:
                        rec["best"] = f
                        rec["best_kind"] = v["kind"]
                        rec["best_model"] = v.get("model")
        if not broke:
            live.append(label)
            if v["kind"] == "ollama":
                live_ollama.append(label)

    if not live:
        print(f"\n  {RED}⚠ Every judge failed — no audit was performed "
              f"(not a clean result).{RESET}\n")
        return

    all_records = list(agg.values())   # full set (incl. verify-dropped) for trace
    for r in all_records:
        r["verified"] = None           # None = kept (verify off / error / no verifier)
        r["pass2"] = "not_run"         # pass-2 outcome for the trace
    records = all_records

    # ── Adversarial verify pass (opt-in): re-check each finding with a narrow,
    # skeptical prompt and drop the ones it can't confirm. Kills the "clarifi-
    # cation/repetition mistaken for a defect" over-flagging.
    if verify and records:
        # Re-check each finding with the SAME judge (engine + model) that raised
        # it — a weak model verifies its own over-flags, a strong judge's
        # finding is re-checked by that strong judge. (Verifiers are cached per
        # engine+model.)
        _vcache: dict = {}

        def _verifier_for(kind, model):
            k = (kind, model)
            if k not in _vcache:
                _vcache[k] = _audit.make_verifier(kind, model)
            return _vcache[k]

        total = len(all_records)
        engines = sorted({r.get("best_kind") for r in all_records})
        offm = "" if engines == ["ollama"] else " · some off-machine (API)"
        print(f"{DIM}  Verifying {total} findings — each re-checked by the judge "
              f"that flagged it{offm}…{RESET}", flush=True)
        kept_n = confirmed_n = refined_n = dropped = 0
        # One trace line per verification: LABEL + the verifier's result.
        _lbl = {"CONFIRMED": GREEN, "AFFINÉ": CYAN, "DROPPED": RED,
                "KEPT": DIM}

        def _vline(i, metric, model, label, reason):
            col = _lbl.get(label, DIM)
            who = f" {DIM}({model}){RESET}" if model else ""
            tail = f"{DIM}: {reason}{RESET}" if reason else ""
            print(f"    {DIM}[{i}/{total}]{RESET} {metric}{who} → "
                  f"{col}{label}{RESET} {tail}")

        for i, r in enumerate(all_records, 1):
            f = r["best"]
            vmodel = r.get("best_model") or r.get("best_kind")
            # A contradiction whose quote matches no real turn can't be
            # confirmed (both conflicting statements must exist in the
            # transcript) — drop it rather than let the verifier guess.
            if f.metric == "contradiction" and not r.get("turn_ok"):
                r["verified"] = False
                r["pass2"] = "dropped_unlocatable"
                dropped += 1
                _vline(i, f.metric, vmodel, "DROPPED",
                       "quote not found in any assistant turn")
                continue
            chat = _verifier_for(r.get("best_kind", "ollama"),
                                 r.get("best_model"))
            if chat is None:            # unknown engine → keep (don't hide)
                r["pass2"] = "kept_no_verifier"
                kept_n += 1
                _vline(i, f.metric, vmodel, "KEPT", "no verifier for this engine")
                continue
            v = _audit.verify_finding(
                chat, f.metric, f.evidence, r.get("excerpt", ""),
                r.get("ask", ""), r.get("reaction", ""))
            if v is None:               # verifier errored → keep (don't hide)
                r["pass2"] = "kept_verifier_error"
                kept_n += 1
                _vline(i, f.metric, vmodel, "KEPT", "verifier error")
                continue
            verdict, reason = v
            r["verify_reason"] = reason
            if verdict == "dropped":
                r["verified"] = False
                r["pass2"] = "refuted"
                dropped += 1
                _vline(i, f.metric, vmodel, "DROPPED", reason)
            elif verdict == "refined":
                r["verified"] = True
                r["pass2"] = "refined"
                kept_n += 1
                refined_n += 1
                _vline(i, f.metric, vmodel, "AFFINÉ", reason)
            else:                       # confirmed
                r["verified"] = True
                r["pass2"] = "confirmed"
                kept_n += 1
                confirmed_n += 1
                _vline(i, f.metric, vmodel, "CONFIRMED", reason)
        # Displayed/kept set excludes only the explicitly-refuted findings.
        records = [r for r in all_records if r.get("verified") is not False]
        print(f"{DIM}  Verify: {GREEN}{confirmed_n} confirmed{RESET}{DIM}, "
              f"{CYAN}{refined_n} refined{RESET}{DIM}, {RED}{dropped} dropped"
              f"{RESET}{DIM} (kept {len(records)}/{total}).{RESET}")

    err_note = ""
    if any(verrors.values()):
        err_note = ("  ·  " + YELLOW + "; ".join(
            f"{lbl}: {verrors[lbl]} errors" for lbl in verrors if verrors[lbl])
            + RESET)
    print(f"\n  Period: {BOLD}{period_label}{RESET}  ·  "
          f"{len(convs)} conversations ({judged_n} judged)  ·  "
          f"{len(records)} findings{err_note}")

    # ── Judge speed on this machine (Ollama judges only, from timing stats) ──
    speed_rows = []
    for m in live_ollama:
        p = vstats[m]
        pf = (p["p_tok"] / (p["p_ns"] / 1e9)) if p["p_ns"] else None
        dc = (p["o_tok"] / (p["o_ns"] / 1e9)) if p["o_ns"] else None
        if pf or dc:
            speed_rows.append((m, pf, dc, p["p_tok"], p["o_tok"]))
    if speed_rows:
        print(f"\n  {BOLD}Judge speed{RESET} {DIM}(this machine, via Ollama){RESET}")
        for m, pf, dc, ptok, otok in speed_rows:
            pf_s = f"{pf:.0f}" if pf else "—"
            dc_s = f"{dc:.1f}" if dc else "—"
            print(f"    {DIM}{m:<22}{RESET} prefill {BOLD}{pf_s}{RESET} tok/s"
                  f"{DIM} · {RESET}decode {BOLD}{dc_s}{RESET} tok/s"
                  f"  {DIM}({fmt_tokens(ptok)} in / {fmt_tokens(otok)} out){RESET}")

    # ── Summary by metric ──────────────────────────────────────────────────
    by_metric: dict[str, list] = defaultdict(list)
    for r in records:
        by_metric[r["best"].metric].append(r)

    n_judges = len(live)   # judges that actually ran → vote denominator
    panel_note = (" · vote = judges agreeing" if n_judges > 1 else "")
    print(f"\n  {BOLD}By metric{RESET} {DIM}({', '.join(live)}{panel_note}){RESET}")
    for metric in _audit.ALL_METRICS:
        rs = by_metric.get(metric, [])
        n = len(rs)
        maxsev = max((r["best"].severity for r in rs), default=0)
        color = BRED if maxsev >= 3 else (
            BYELLOW if maxsev >= 2 else (CYAN if maxsev >= 1 else DIM))
        desc = _audit.METRIC_DESC.get(metric, "")
        print(f"    {color}{metric:<22}{RESET} {n:>3}  {DIM}{desc}{RESET}")

    # ── Top findings (consensus first when there's a panel) ─────────────────
    if records:
        records.sort(key=lambda r: (-len(r["voters"]), -r["best"].severity))
        print(f"\n  {BOLD}Top findings{RESET} {DIM}(most agreed / severe first, "
              f"max {limit}){RESET}")
        for r in records[:limit]:
            f = r["best"]
            sev = _AUDIT_SEV_COLOR[f.severity]
            lbl = _AUDIT_SEV_LABEL[f.severity]
            when = f.ts.strftime("%Y-%m-%d") if f.ts else "?"
            proj = normalize_project(f.project) if f.project else "?"
            tcol = TOOL_COLORS.get(f.tool, "")
            vote = (f"{BOLD}{len(r['voters'])}/{n_judges}{RESET}{DIM} · "
                    if n_judges > 1 else "")
            turn = (f" · turn {r.get('turn')}" if r.get("turn_ok")
                    else " · turn ? (unlocatable)")
            print(f"    {sev}●{RESET} {sev}{lbl:<4}{RESET} "
                  f"{BOLD}{f.metric}{RESET} {DIM}· {vote}{tcol}{f.tool}{RESET}"
                  f"{DIM} · {when} · {proj}{turn}{RESET}")
            if n_judges > 1:
                print(f"        {DIM}votes:{RESET} {', '.join(sorted(r['voters']))}")
            print(f"        {DIM}why:  {RESET}{f.rationale}")
            if f.evidence:
                ev = " ".join(str(f.evidence).split())[:200]
                print(f"        {DIM}judge flagged:{RESET} \"{ev}\"")
            # The REAL transcript context, so you can verify the finding yourself.
            if r.get("ask"):
                print(f"        {DIM}user asked:    {r['ask']}{RESET}")
            if r.get("excerpt"):
                print(f"        {DIM}assistant said: \"{r['excerpt']}\"{RESET}")
            elif not r.get("turn_ok"):
                print(f"        {DIM}(the flagged quote matches no assistant turn "
                      f"— likely fabricated/paraphrased; treat with suspicion){RESET}")

    # ── Off-machine judge cost / usage ──────────────────────────────────────
    cost_lines = []
    for v in voters:
        if v["label"] not in live or v["kind"] not in ("claude", "codex"):
            continue
        s = vstats[v["label"]]
        if v["kind"] == "claude":
            cost_lines.append(f"{v['label']}: ${s['cost']:.4f} · "
                              f"{fmt_tokens(s['in'])} in / {fmt_tokens(s['out'])} out")
        else:
            usage = f"~{fmt_tokens(s['tokens'])} tokens · " if s["tokens"] else ""
            cost_lines.append(f"{v['label']}: {usage}covered by your plan")
    if cost_lines:
        print(f"\n  {DIM}Off-machine judge usage: " + "  ·  ".join(cost_lines)
              + RESET)

    # ── Honesty caveat ──────────────────────────────────────────────────────
    local = kinds == {"ollama"}
    src = ("local LLM judge(s)" if local
           else "LLM judge(s) — some off-machine (Claude/Codex API)")
    tip = (" A panel agreeing raises confidence; a lone vote is weak."
           if n_judges > 1 else "")
    print(f"\n  {DIM}⚠ Findings come from {src} — leads to review, "
          f"not verdicts.{tip} Factual hallucination in particular needs "
          f"external ground truth beyond the transcript.{RESET}\n")


def show_bench(judge_model: str | None = None):
    """Benchmark the local judge model(s) on this machine: prefill and decode
    tokens/s (from Ollama's own timing stats) — to compare hardware/models."""
    from tokstat import _audit

    print(f"\n{BOLD} Judge model benchmark{RESET}")
    print(f"{DIM}  Local Ollama on {_audit.OLLAMA_HOST}. prefill = prompt "
          f"processing, decode = generation.{RESET}\n")

    installed = _audit.list_ollama_models()
    if not installed:
        print(f"  {YELLOW}⚠ Ollama unreachable on {_audit.OLLAMA_HOST} "
              f"(or no model installed).{RESET}\n")
        return
    if judge_model:
        models = list(dict.fromkeys(m.strip() for m in judge_model.split(",")
                                    if m.strip()))
    else:
        auto = _audit.pick_ollama_model()
        models = [auto] if auto else []
    missing = [m for m in models if not _audit.ollama_has_model(m)]
    if missing or not models:
        print(f"  {YELLOW}⚠ Not installed: {', '.join(missing) or '(none given)'}"
              f"{RESET}")
        print(f"  {DIM}Installed: {', '.join(installed)}{RESET}\n")
        return

    # Column width adapts to the longest model name (some are 40+ chars).
    nw = min(max((len(m) for m in models), default=5), 48)

    def _fit(name):
        return name if len(name) <= nw else name[:nw - 1] + "…"

    print(f"    {DIM}{'model':<{nw}}{'prompt':>8}{'prefill':>11}"
          f"{'output':>8}{'decode':>10}{'load':>8}{RESET}")
    print(f"    {DIM}{'':<{nw}}{'tok':>8}{'tok/s':>11}{'tok':>8}{'tok/s':>10}"
          f"{'s':>8}{RESET}")
    for m in models:
        print(f"{DIM}    {_fit(m)} …{RESET}", end="", flush=True)
        try:
            r = _audit.benchmark_ollama(m)
        except _audit.JudgeError as e:
            print(f"\r    {_fit(m):<{nw}}{RED}  benchmark failed: {e}{RESET}")
            continue
        pf = f"{r['prefill_tps']:.0f}" if r['prefill_tps'] else "—"
        dc = f"{r['decode_tps']:.1f}" if r['decode_tps'] else "—"
        pfc = BRED if (r['prefill_tps'] or 0) < 200 else (
            BYELLOW if (r['prefill_tps'] or 0) < 800 else GREEN)
        dcc = BRED if (r['decode_tps'] or 0) < 15 else (
            BYELLOW if (r['decode_tps'] or 0) < 40 else GREEN)
        print(f"\r    {_fit(m):<{nw}}{(r['prompt_tokens'] or 0):>8}"
              f"{pfc}{pf:>11}{RESET}{(r['output_tokens'] or 0):>8}"
              f"{dcc}{dc:>10}{RESET}{r['load_s']:>8.1f}")
    print(f"\n  {DIM}Tip: for the audit, prefill speed matters most (long "
          f"transcripts, short JSON output).{RESET}\n")


# ─── Data-retention checks ─────────────────────────────────────────────────
# tokstat can only report what each tool still keeps on disk. Some tools purge
# old local data on a rolling window, so history depth is capped regardless of
# --period. We warn when a tool's retention is NOT effectively "forever" so the
# numbers (and the "since" dates) aren't mistaken for full lifetime usage.

# A retention this long (10 years) is treated as "keeps everything".
_RETENTION_FOREVER_DAYS = 3650


def _claude_retention_days() -> int:
    """Claude Code purges transcripts older than `cleanupPeriodDays`
    (~/.claude/settings.json), defaulting to 30. Return the effective value."""
    days = 30  # Claude Code's built-in default
    try:
        cfg = json.loads((_Path.home() / ".claude" / "settings.json").read_text())
        v = cfg.get("cleanupPeriodDays")
        if isinstance(v, (int, float)) and v > 0:
            days = int(v)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return days


def retention_alerts(tools) -> list[str]:
    """Return warning lines for any present tool whose local data is NOT
    retained ~forever. `tools` is an iterable of tool display names.

    Only tools with a known, finite retention window are flagged; tools that
    keep everything (e.g. Codex session rollouts, which are never auto-pruned)
    and tools with no known pruning mechanism produce no alert."""
    tset = set(tools)
    alerts: list[str] = []
    if "Claude Code" in tset:
        days = _claude_retention_days()
        if days < _RETENTION_FOREVER_DAYS:
            alerts.append(
                f"Claude Code keeps only the last {days} days of transcripts "
                f"(cleanupPeriodDays) — older sessions are purged and "
                f"unrecoverable. Raise it in ~/.claude/settings.json to keep "
                f"full history."
            )
    # Codex: ~/.codex/sessions/ rollouts have no retention setting and are not
    # auto-pruned → effectively forever, so no alert.
    return alerts


def print_retention_alerts(tools) -> None:
    """Print a retention warning block for the given tools, if any apply."""
    alerts = retention_alerts(tools)
    for msg in alerts:
        print(f"  {YELLOW}⚠ Retention: {msg}{RESET}")
    if alerts:
        print()


# ─── Shared display: environmental impact ─────────────────────────────────

# Electricity-mix GWP presets (kgCO2eq/kWh) and a config override.
_IMPACT_MIX_PRESETS = {
    "world": 0.418, "france": 0.056, "eu": 0.250, "europe": 0.250,
    "us": 0.369, "usa": 0.369, "green": 0.040,
}
_IMPACT_CONFIG = _Path.home() / ".config" / "tokstat" / "impact.json"


def _coerce_factor(v):
    """Coerce a config value to a (lo, hi) factor tuple, or None if invalid.
    Accepts a scalar (→ (v, v)) or a 2-item [lo, hi] list."""
    if isinstance(v, (int, float)):
        return (float(v), float(v))
    if isinstance(v, (list, tuple)) and len(v) == 2 \
            and all(isinstance(x, (int, float)) for x in v):
        lo, hi = float(v[0]), float(v[1])
        return (min(lo, hi), max(lo, hi))
    return None


def _load_impact_config(region_override: str | None = None):
    """Return (pue, mix_gwp, region_label). Config keys: 'region' (preset) or
    'electricity_mix_gwp' (explicit), optional 'pue', and optional
    'prefill_factor' / 'cache_read_factor' (scalar or [lo, hi]) which override
    the EcoLogits prefill/cache energy multipliers. A region_override takes
    precedence over the config file."""
    import tokstat._ecologits as _eco
    pue, mix, region = _eco.DEFAULT_PUE, _eco.DEFAULT_MIX_GWP, "world"
    try:
        cfg = json.loads(_IMPACT_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        cfg = {}
    if isinstance(cfg.get("pue"), (int, float)):
        pue = float(cfg["pue"])
    # Prefill / cache energy factors: applied to the _ecologits module globals
    # that impact_for() reads, so all call sites pick up the override.
    pf = _coerce_factor(cfg.get("prefill_factor"))
    if pf:
        _eco.PREFILL_FACTOR = pf
    cf = _coerce_factor(cfg.get("cache_read_factor"))
    if cf:
        _eco.CACHE_READ_FACTOR = cf
    if isinstance(cfg.get("electricity_mix_gwp"), (int, float)):
        mix = float(cfg["electricity_mix_gwp"]); region = "custom"
    elif isinstance(cfg.get("region"), str):
        r = cfg["region"].lower().strip()
        if r in _IMPACT_MIX_PRESETS:
            mix = _IMPACT_MIX_PRESETS[r]; region = r
    if region_override:
        r = region_override.lower().strip()
        if r in _IMPACT_MIX_PRESETS:
            mix = _IMPACT_MIX_PRESETS[r]; region = r
        else:
            try:
                mix = float(region_override); region = "custom"
            except ValueError:
                valid = ", ".join(sorted(_IMPACT_MIX_PRESETS))
                print(f"  {YELLOW}Unknown region '{region_override}'. "
                      f"Using '{region}'. Available: {valid}{RESET}")
    return pue, mix, region


def _estimate_total_impact(exchanges, region: str | None = None):
    """Return (energy_mid_kWh, gwp_mid_kgCO2e, region_label) for the given
    exchanges using EcoLogits, or (None, None, region) if nothing matched.
    Midpoint of the min/max range. Used by --activity for a one-line summary."""
    from tokstat._ecologits import impact_for
    pue, mix_gwp, region_label = _load_impact_config(region)
    # per model: [output, prefill (input + cache_write), cache_read]
    tok_by_model: dict[str, list] = defaultdict(lambda: [0, 0, 0])
    for e in exchanges:
        tok = e.get("tokens") or {}
        agg = tok_by_model[e.get("model") or "?"]
        agg[0] += tok.get("output", 0) or 0
        agg[1] += (tok.get("input", 0) or 0) + (tok.get("cache_write", 0) or 0)
        agg[2] += tok.get("cache_read", 0) or 0
    e_lo = e_hi = g_lo = g_hi = 0.0
    matched = False
    for model, (out, prefill, cread) in tok_by_model.items():
        if out <= 0 and prefill <= 0 and cread <= 0:
            continue
        imp = impact_for(model, out, prefill, cread, pue=pue, mix_gwp=mix_gwp)
        if not imp:
            continue
        matched = True
        e_lo += imp["energy"][0]; e_hi += imp["energy"][1]
        g_lo += imp["gwp"][0];    g_hi += imp["gwp"][1]
    if not matched:
        return None, None, region_label
    return (e_lo + e_hi) / 2, (g_lo + g_hi) / 2, region_label


def show_impact(collect_fn, period_name: str | None = None,
                tool_filter: str | None = None, region: str | None = None):
    """Estimate the energy and CO2 (usage phase) of the observed activity,
    using the EcoLogits methodology and model database. Order-of-magnitude.
    `region` (e.g. eu/world/france) overrides the configured electricity mix."""
    from tokstat._ecologits import impact_for, load_ecologits_db

    print(f"\n{BOLD} Environmental Impact{RESET}  {DIM}(usage phase, EcoLogits){RESET}")
    print(f"{DIM}  Loading model database (EcoLogits)...{RESET}")
    load_ecologits_db()
    print(f"{DIM}  Scanning exchanges...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    all_exchanges, _ = collect_fn(cutoff, tool_filter, cutoff_end)
    all_exchanges = [e for e in all_exchanges if e.get("ts")]
    if not all_exchanges:
        print(f"  {YELLOW}No data found.{RESET}\n")
        return

    pue, mix_gwp, region = _load_impact_config(region)

    # Aggregate output tokens per (tool, model); track each tool's full data
    # span and which tools carry each model.
    # per (tool, model): [output, prefill (input + cache_write), cache_read]
    out_by_tool_model: dict[tuple, list] = defaultdict(lambda: [0, 0, 0])
    tool_span: dict[str, list] = {}
    model_tools: dict[str, set] = defaultdict(set)
    for e in all_exchanges:
        tok = e.get("tokens") or {}
        tool = e.get("tool", "?")
        model = e.get("model") or "?"
        agg3 = out_by_tool_model[(tool, model)]
        agg3[0] += tok.get("output", 0) or 0
        agg3[1] += (tok.get("input", 0) or 0) + (tok.get("cache_write", 0) or 0)
        agg3[2] += tok.get("cache_read", 0) or 0
        model_tools[model].add(tool)
        day = e["ts"].astimezone().strftime("%Y-%m-%d")
        s = tool_span.setdefault(tool, [day, day])
        if day < s[0]: s[0] = day
        if day > s[1]: s[1] = day

    def _model_measurable_span(model: str) -> list:
        # A model's measurable period = the union of the data spans of every
        # tool that carries it (each tool's traces define what's measurable).
        firsts = [tool_span[t][0] for t in model_tools.get(model, ()) if t in tool_span]
        lasts  = [tool_span[t][1] for t in model_tools.get(model, ()) if t in tool_span]
        return [min(firsts), max(lasts)] if firsts else ["?", "?"]

    e_lo = e_hi = g_lo = g_hi = 0.0
    ed_mid = 0.0                    # decode-only energy (for frugality verdict)
    covered_out = total_out = 0
    matched_any = False
    _zero = lambda: {"e_lo": 0.0, "e_hi": 0.0, "g_lo": 0.0, "g_hi": 0.0,
                     "out": 0, "covered": 0, "matched": False}
    per_tool: dict[str, dict] = defaultdict(_zero)
    per_model: dict[str, dict] = defaultdict(_zero)
    for (tool, model), (out, prefill, cread) in out_by_tool_model.items():
        if out <= 0 and prefill <= 0 and cread <= 0:
            continue
        total_out += out
        pt, pm = per_tool[tool], per_model[model]
        pt["out"] += out; pm["out"] += out
        # Total energy includes the prefill/context term (input + cache).
        imp = impact_for(model, out, prefill, cread, pue=pue, mix_gwp=mix_gwp)
        if not imp:
            continue
        # A model "matched" if it resolved and produced energy — regardless of
        # whether this exchange had output tokens (prefill/cache alone counts).
        matched_any = True
        covered_out += out
        for agg in (pt, pm):
            agg["matched"] = True
            agg["covered"] += out
            agg["e_lo"] += imp["energy"][0]; agg["e_hi"] += imp["energy"][1]
            agg["g_lo"] += imp["gwp"][0];    agg["g_hi"] += imp["gwp"][1]
        e_lo += imp["energy"][0]; e_hi += imp["energy"][1]
        g_lo += imp["gwp"][0];    g_hi += imp["gwp"][1]
        # Decode-only energy drives the frugality verdict so the mascot grades
        # the model mix (context-independent), not how much context you feed.
        imp_d = impact_for(model, out, pue=pue, mix_gwp=mix_gwp)
        if imp_d:
            ed_mid += (imp_d["energy"][0] + imp_d["energy"][1]) / 2

    if not matched_any:
        print(f"  {YELLOW}No models matched the EcoLogits database.{RESET}\n")
        return

    scope = period_label + (f" · {tool_filter}" if tool_filter else "")
    e_mid = (e_lo + e_hi) / 2
    g_mid = (g_lo + g_hi) / 2
    car_km = g_mid / 0.12           # ~120 gCO2/km petrol car
    charges = e_mid / 0.012         # ~12 Wh per smartphone charge

    # Uncertainty as a single ± percentage (range symmetric around midpoint).
    pct = (e_hi - e_mid) / e_mid * 100 if e_mid else 0
    avg_frug = ed_mid * 1e6 / covered_out if covered_out else 0  # Wh / 1k out tok (decode)

    # Mascot animal by frugality (model-mix weight) — comparable across users.
    # Anchors (Wh/1k out, via EcoLogits): small models (haiku/mini) ~0.1,
    # mid-tier (sonnet/gpt-4o) ~2, current frontier (opus-4-7/4-8, gpt-5.x) ~5-6,
    # legacy dense giants (opus-4-1, gemini-2.5-pro) ~25. A mostly-Opus diet
    # reads "heavy"; "very heavy" is the old dense-600B-class tier.
    for thr, emoji, word in ((1, "🐜", "very light"), (2.5, "🦥", "frugal"),
                             (4, "🦊", "moderate"), (10, "🐘", "heavy"),
                             (float("inf"), "🦣", "very heavy")):
        if avg_frug < thr:
            animal, level = emoji, word
            break
    # Trend: split the window in half and compare total energy.
    days_sorted = sorted(e["ts"].astimezone().date() for e in all_exchanges)
    mid_date = days_sorted[0] + (days_sorted[-1] - days_sorted[0]) / 2
    fh = [e for e in all_exchanges if e["ts"].astimezone().date() <= mid_date]
    sh = [e for e in all_exchanges if e["ts"].astimezone().date() > mid_date]
    eh1 = _estimate_total_impact(fh, region)[0] if fh else None
    eh2 = _estimate_total_impact(sh, region)[0] if sh else None
    if eh1 and eh2:
        r = eh2 / eh1
        tp = (r - 1) * 100
        # Quote a % only in a sane range; past a ~5x swing the baseline is too
        # small for a percentage to mean anything (e.g. adoption ramp over
        # --period all), so describe the direction instead of a giant number.
        if r >= 5:     arrow = f"{BRED}↗ ramping up{RESET}"
        elif tp > 10:  arrow = f"{BRED}↗ growing (+{tp:.0f}%){RESET}"
        elif r <= 0.2: arrow = f"{GREEN}↘ winding down{RESET}"
        elif tp < -10: arrow = f"{GREEN}↘ shrinking ({tp:.0f}%){RESET}"
        else:          arrow = f"{DIM}→ stable{RESET}"
    else:
        arrow = f"{DIM}n/a{RESET}"

    def _w(s):  # visible width, counting emoji as 2 cells
        return len(_strip_ansi(s)) + sum(1 for c in s if ord(c) >= 0x1F000)

    inner = [
        f"{BOLD}ENERGY & CO₂ · {scope}{RESET}",
        "",
        f"{animal}  {BOLD}{GREEN}~{e_mid:.1f} kWh{RESET}  ·  "
        f"{BOLD}~{g_mid:.1f} kg CO₂e{RESET}   {BOLD}{level}{RESET}",
        f"{DIM}± {pct:.0f}% · {avg_frug:.1f} Wh/1k · trend {RESET}{arrow}",
        "",
        f"≈ {car_km:.0f} km by car · {charges:.0f} phone charges",
        f"{DIM}mix: {region} ({mix_gwp:.3f} kgCO₂e/kWh) · PUE {pue}{RESET}",
    ]
    w = max(_w(s) for s in inner)
    print(f"  ╭─{'─' * w}─╮")
    for s in inner:
        print(f"  │ {s}{' ' * (w - _w(s))} │")
    print(f"  ╰─{'─' * w}─╯")

    # ─── Trend over time (granularity adapts to the period span) ──────────
    from datetime import timedelta as _td2
    span_days = (days_sorted[-1] - days_sorted[0]).days
    if span_days <= 31:
        gran, gran_label = "day", "day"
    elif span_days <= 182:
        gran, gran_label = "week", "week"
    else:
        gran, gran_label = "month", "month"

    def _bucket(d):
        if gran == "day":
            return d.isoformat()
        if gran == "week":
            return (d - _td2(days=d.weekday())).isoformat()
        return d.strftime("%Y-%m")

    # per bucket, per model: [output, prefill (input + cache_write), cache_read]
    bucket_out: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    bucket_tokens: dict[str, int] = defaultdict(int)   # total tokens per bucket
    for e in all_exchanges:
        tok = e.get("tokens") or {}
        bk = _bucket(e["ts"].astimezone().date())
        bucket_tokens[bk] += (tok.get("input", 0) + tok.get("output", 0)
                              + tok.get("cache_read", 0) + tok.get("cache_write", 0))
        agg3 = bucket_out[bk][e.get("model") or "?"]
        agg3[0] += tok.get("output", 0) or 0
        agg3[1] += (tok.get("input", 0) or 0) + (tok.get("cache_write", 0) or 0)
        agg3[2] += tok.get("cache_read", 0) or 0

    if len(bucket_out) > 1:
        # Per bucket: energy/CO₂ include prefill (consistent with the headline);
        # frugality (Wh/1k) stays decode-only (consistent with the verdict).
        series = []
        for bkey in sorted(bucket_out):
            be_lo = be_hi = bg_lo = bg_hi = bed_mid = 0.0
            bout = 0
            for model, (out, prefill, cread) in bucket_out[bkey].items():
                imp = impact_for(model, out, prefill, cread, pue=pue, mix_gwp=mix_gwp)
                if not imp:
                    continue
                be_lo += imp["energy"][0]; be_hi += imp["energy"][1]
                bg_lo += imp["gwp"][0];    bg_hi += imp["gwp"][1]
                bout += out
                imp_d = impact_for(model, out, pue=pue, mix_gwp=mix_gwp)
                if imp_d:
                    bed_mid += (imp_d["energy"][0] + imp_d["energy"][1]) / 2
            if bout <= 0:
                continue
            bem = (be_lo + be_hi) / 2
            series.append({
                "bucket": bkey, "tokens": bucket_tokens.get(bkey, 0),
                "energy": bem, "gwp": (bg_lo + bg_hi) / 2,
                "wh_1k": bed_mid * 1e6 / bout,    # frugality: decode Wh / 1k out tok
            })

        def _delta(cur, prev):
            # colored "±X%" vs previous bucket; less = greener (better).
            if prev is None or prev == 0:
                return f"{DIM}    —{RESET}"
            pc = (cur - prev) / prev * 100
            color = GREEN if pc < 0 else (BYELLOW if pc < 25 else BRED)
            return f"{color}{pc:+4.0f}%{RESET}"

        print(f"\n  {DIM}Trend (per {gran_label}) — Δ vs previous {gran_label}:{RESET}")
        print(f"    {DIM}{'bucket':<11}{'tokens':>8}{'energy':>9}{'Δ':>6}   "
              f"{'CO₂e':>8}   {'Wh/1k':>6}{'Δ':>6}{RESET}")
        prev_e = prev_f = None
        for s in series:
            de = _delta(s["energy"], prev_e)
            df = _delta(s["wh_1k"], prev_f)
            print(f"    {s['bucket']:<11}{fmt_tokens(s['tokens']):>8}"
                  f"{s['energy']:>6.2f}kWh {de}   "
                  f"{s['gwp']:>6.2f}kg   {s['wh_1k']:>5.1f} {df}")
            prev_e, prev_f = s["energy"], s["wh_1k"]

        # ─── Narrative analysis (first half vs second half of the series) ──
        if len(series) >= 4:
            half = len(series) // 2
            def _avg(seq, key):
                vals = [x[key] for x in seq]
                return sum(vals) / len(vals) if vals else 0.0
            e1, e2 = _avg(series[:half], "energy"), _avg(series[half:], "energy")
            f1, f2 = _avg(series[:half], "wh_1k"),  _avg(series[half:], "wh_1k")

            def _word(cur, prev):
                # Beyond a ~5x swing the baseline is too small to quote a %,
                # so describe the direction (avoids "rose 15036%").
                if prev <= 0:
                    return f"{DIM}held roughly steady{RESET}"
                r = cur / prev
                p = (r - 1) * 100
                if r >= 5:    return f"{BRED}rose sharply{RESET}"
                if p > 10:    return f"{BRED}rose {p:.0f}%{RESET}"
                if r <= 0.2:  return f"{GREEN}dropped sharply{RESET}"
                if p < -10:   return f"{GREEN}fell {abs(p):.0f}%{RESET}"
                return f"{DIM}held roughly steady{RESET}"

            pf = (f2 - f1) / f1 * 100 if f1 else 0
            if f1 > 0 and f2 / f1 >= 5:
                frug = f"{BRED}worsened sharply{RESET} (much heavier model mix)"
            elif pf > 10:
                frug = f"{BRED}worsened {pf:.0f}%{RESET} (heavier model mix)"
            elif f1 > 0 and f2 / f1 <= 0.2:
                frug = f"{GREEN}improved sharply{RESET} (much lighter model mix)"
            elif pf < -10:
                frug = f"{GREEN}improved {abs(pf):.0f}%{RESET} (lighter model mix)"
            else:
                frug = f"{DIM}stayed flat{RESET} (similar model mix)"

            print(f"\n  {BOLD}Analysis{RESET} {DIM}(first vs second half of the period){RESET}")
            print(f"    • Electricity use {_word(e2, e1)} "
                  f"({e1:.2f} → {e2:.2f} kWh per {gran_label}).")
            print(f"    • CO₂ followed the same path — {BOLD}~{g_mid:.1f} kg CO₂e{RESET} "
                  f"total over the window.")
            print(f"    • Frugality {frug}: "
                  f"{f1:.1f} → {f2:.1f} Wh per 1k output tokens.")

    def _metric(agg):
        if not agg["matched"]:
            return f"{DIM}not in EcoLogits DB{RESET}"
        em = (agg["e_lo"] + agg["e_hi"]) / 2
        gm = (agg["g_lo"] + agg["g_hi"]) / 2
        return f"{em:>6.2f} kWh · {gm:>6.2f} kg CO₂e"

    print(f"\n  {DIM}By tool (data span used):{RESET}")
    for tool, pt in sorted(per_tool.items(), key=lambda kv: -((kv[1]['g_lo']+kv[1]['g_hi'])/2)):
        color = TOOL_COLORS.get(tool, "")
        span = " → ".join(tool_span.get(tool, ["?", "?"]))
        print(f"    {color}{tool:<12}{RESET} {_metric(pt)}   {DIM}{span}{RESET}")

    print(f"\n  {DIM}By model (measurable span):{RESET}")
    for model, pm in sorted(per_model.items(), key=lambda kv: -((kv[1]['g_lo']+kv[1]['g_hi'])/2))[:15]:
        span = " → ".join(_model_measurable_span(model))
        print(f"    {model:<28} {_metric(pm)}   {DIM}{span}{RESET}")

    if covered_out < total_out:
        miss = (total_out - covered_out) / total_out * 100
        print(f"\n  {DIM}⚠ {miss:.0f}% of output tokens are from models not in the "
              f"EcoLogits DB (excluded).{RESET}")
    print(f"  {DIM}⚠ Usage phase only (excludes hardware manufacturing). "
          f"Order-of-magnitude estimate.{RESET}\n")


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

def _parse_region(args: list[str]) -> str | None:
    """Electricity-mix region for --impact, given positionally as
    `--impact <region>`; returns None → defaults to world."""
    if "--impact" in args:
        idx = args.index("--impact")
        if idx + 1 < len(args) and not args[idx + 1].startswith("-"):
            return args[idx + 1]
    return None


_PERIOD_UNITS = ("hour", "hours", "day", "days", "week", "weeks",
                 "month", "months", "year", "years")


def _parse_period(args: list[str]) -> str | None:
    for flag in ("--period", "--since"):
        if flag in args:
            idx = args.index(flag)
            if idx + 1 >= len(args):
                return None
            value = args[idx + 1]
            # Allow an unquoted "3 months" → shell splits it into "3" "months";
            # rejoin a following bare unit word so it resolves correctly.
            if (value.isdigit() and idx + 2 < len(args)
                    and args[idx + 2].lower() in _PERIOD_UNITS):
                value = f"{value} {args[idx + 2]}"
            return value
    return None
