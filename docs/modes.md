# Modes

[← back to README](../README.md) · [Tools](tools.md) · [Filters](filters.md) · [Impact](impact.md) · [Reference](reference.md)

All tools support the same modes:

```sh
<tool>                          # Aggregated overview (period, project, model, speed)
<tool> --prompts   [-p]         # Per-exchange detail (text, turns, tokens, tools, cost)
<tool> --tool-use               # Chronological tool-call timeline
<tool> --anomalies              # Technical anomaly detection
<tool> --activity               # GitHub-style activity calendar (by day) + tokens
<tool> --total                  # Compact totals (tokens + cost + data span)
<tool> --impact    [region]     # Energy & CO₂ estimate (EcoLogits) — see impact.md
<tool> --plan                   # Cost breakdown + per-provider plan recommendation
<tool> --export    [file.json]  # Export all exchanges to JSON
<tool> --version   [-V]         # Print version
<tool> --help      [-h]         # Usage
```

The overview, project, and model tables include **Prompts** (user inputs),
**Turns** (assistant turns per exchange), and **API** (raw API calls) columns,
plus a **GRAND TOTAL** block with the rolling-hour token rate and the active
agents. Rows that have activity but no reliable local token data show
`⚠ no data` in the cost column rather than a misleading `$0.00`.

`tokstat` additionally supports a live mode:

```sh
tokstat --watch        [-w]     # Refresh the overview in place (default 5s)
tokstat --watch 10              # ...every 10 seconds
```

Changed rows are flagged with a ◆ between refreshes; press Ctrl+C to stop.

All modes accept the scoping filters — `--period`, `--tool`, `--project`,
`--session` — documented in [Filters](filters.md).

## Default — aggregated overview

Consumption tables by period, by project and by model.

```sh
tokstat
tokstat --period all
codex-token-usage --period "7 days"
cursor-token-usage --period "30 days"
```

Two optional breakdowns can be added to the overview:

```sh
tokstat --by-session      # + a per-session table (heaviest conversations by cost)
tokstat --by-project      # + a project → sessions drill-down
```

## `--prompts` — per-exchange detail

Per-exchange breakdown: user text, model, turns, tokens (input/output/cache), tool calls, cost.

```sh
claude-token-usage --prompts
claude-token-usage -p --period "7 days"
```

## `--tool-use` — tool-call timeline

Chronological list of every tool call (Claude Code, Codex, opencode, Antigravity,
GitHub Copilot, pi), grouped by conversation: the tool, what it targeted (file for
Read/Edit/Write, command for Bash/exec, pattern/query for search) and the time it
ran. Failed calls are marked `✗`. Filterable by `--period` / `--tool`, and by
**`--session <id>`** to scope to one conversation (full id or the short handle
each line shows in `[…]`).

```sh
claude-token-usage --tool-use --period "7 days"
tokstat --tool-use --tool codex
tokstat --tool-use --session 019f6a75
```

```
  Claude Code ~/Code/tokstat  328 calls
    09-11 15:48 › sur quelle branche es-tu ?
       15:48:15   Bash         git branch --show-current
       15:48:19   Read         src/tokstat/_core.py
       15:48:24   Edit         src/tokstat/_core.py
```

## `--anomalies` — technical anomaly detection

Detects unusual patterns in per-exchange token data. Results grouped by project.

```sh
claude-token-usage --anomalies
claude-token-usage --anomalies --period "30 days"
```

| Anomaly | Trigger | Severity |
|---------|---------|----------|
| Runaway cost | Prompt costs 10x+ the tool's P90 | HIGH |
| High cost | Prompt costs 5x+ the tool's P90 | MEDIUM |
| Tool storm | 30+ tool calls in a single prompt | HIGH >60, MEDIUM >30 |
| Turn spiral | API turns 5x+ the tool's P90 | HIGH >10x, MEDIUM >5x |
| Cache thrashing | High cache writes with <50% read-back | MEDIUM |
| Context bloat | Input/output ratio 2x+ the tool's P90 (min 50:1) | LOW |
| Empty exchange | 5+ turns but <100 output tokens | MEDIUM |

Thresholds are computed dynamically **per tool** (median, P90) — a costly
Codex prompt is judged against Codex, not against the whole fleet — so
structurally input-heavy or expensive tools don't drown the report in
false positives.

## `--activity` — activity calendar

A GitHub-style contribution calendar: one cell per day, colored by prompts/day,
with a summary of total prompts / turns / tokens, the busiest day, and a
one-line energy & CO₂ estimate (see [Impact](impact.md) for the detailed breakdown).

```sh
tokstat --activity --period all
tokstat --activity --tool claude --period "30 days"
```

> **⚠️ History depth depends on each tool's retention.** tokstat can only show
> days whose transcripts are still on disk. **Claude Code prunes its transcripts
> after `cleanupPeriodDays` (default 30 days)** — so by default the Claude
> activity calendar goes back ~30 days only, and older days are gone for good.
> To keep more, raise the limit in `~/.claude/settings.json`, e.g.
> `{ "cleanupPeriodDays": 365 }`. Codex, by contrast, keeps all sessions (no
> automatic cleanup).

## `--total` — compact totals

A one-glance summary of total tokens and cost for the selected period/tool,
with the actual date span the data covers and a per-tool breakdown.

```sh
tokstat --total --period "30 days"
tokstat --total --tool codex --period all
```

```
  ╭───────────────────────────────────────────────╮
  │ TOTAL · Last 30 days                            │
  │                                                 │
  │ $697.03    953.1M tokens                        │
  │ in 9.4M · out 2.4M · cache 922.6M/18.7M         │
  │                                                 │
  │ 577 prompts · 2614 turns · 25 active day(s)     │
  │ 2026-05-19 → 2026-06-18                         │
  ╰───────────────────────────────────────────────╯

  By tool:
    Claude Code    $517.32   717.8M tokens · 422 prompts · 2026-05-19 → 2026-06-18
    Codex          $179.70   235.3M tokens · 147 prompts · 2026-05-23 → 2026-06-15
```

## `--impact` — energy & CO₂ estimate

Estimates the environmental impact of the observed activity (EcoLogits
methodology). It has its own page: **[Impact](impact.md)**.

## `--plan` — plan & optimization recommendations

Cost breakdown by model, a plan recommendation **per upstream provider**
(Anthropic, OpenAI, Google — local/no-cost models are ignored), and
data-driven optimization advice. With `tokstat` this spans every tool; with a
per-tool command it's scoped to that one.

```sh
tokstat --plan --period "30 days"
claude-token-usage --plan --period all
```

```
  Last 30 days — 21 active days / 30

  Model              Calls     Cost   Avg/day  Projected/mo  Cache  Share
  ─────────────────  ─────  ───────  ────────  ────────────  ─────  ─────
  gpt-5.5 [xhigh]     1132  $783.28   $26.11/d    $783.28/mo    98%    51%
  claude-opus-4-7      298  $277.99    $9.27/d    $277.99/mo    98%    18%
  ...
  TOTAL               1176  $1290.51  $44.50/d   $1335.01/mo    98%

  Plan (based on Last 30 days)
    OpenAI (GPT)        — ChatGPT Pro ($200/mo) for chat, API direct for Codex. $1056.32/mo projected
    Anthropic (Claude)  — Max 20x ($200/mo) strongly recommended. $277.99/mo projected
```

## `--export` — conversation export

Exports all exchanges to a JSON file. Each entry carries its `session` (the
source transcript id), `cwd` (the working directory the session ran in) and
`worktree` (the git main worktree that `cwd` resolves to — same as `cwd` unless
the session ran in a linked worktree). For **Claude Code, Codex and pi** it also
carries `context_tokens` (the peak context size the exchange reached). **Claude
Code and Codex** additionally carry `compactions` (each compaction event, with
`pre_tokens` → `post_tokens`; Claude Code also gives `trigger` auto/manual and
duration, Codex leaves those `null`; pi records no compaction events); these same
signals appear as the **Context** and **Compaction** columns in `--prompts`.

```sh
claude-token-usage --export
claude-token-usage --export out.json --period "7 days"
```

```json
{
  "tool": "Claude Code",
  "model": "claude-opus-4-6",
  "session": "e25ea4e5-4de5-40ca-be90-f99371b220be",
  "cwd": "/Users/me/Code/tokstat/.worktrees/feature-x",
  "worktree": "/Users/me/Code/tokstat",
  "timestamp": "2026-04-08T...",
  "user": "the user prompt text",
  "assistant": ["response 1", "response 2"],
  "turns": 25,
  "tokens": {"input": 1200, "output": 3700, "cache_read": 886000, "cache_write": 12000},
  "cost": 5.78,
  "duration_s": 182.4,
  "speed_tps": 20.3,
  "context_tokens": 999900,
  "context_growth": 24100,
  "compactions": [
    {"trigger": "auto", "pre_tokens": 1000000, "post_tokens": 10400,
     "duration_ms": 87092, "timestamp": "2026-04-17T20:21:40.697Z"}
  ],
  "tools_used": {"Bash": 3, "Read": 7, "Edit": 2},
  "tool_errors": ["error message"]
}
```
