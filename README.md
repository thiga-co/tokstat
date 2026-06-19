# tokstat

CLI toolkit to aggregate and analyze AI coding assistant token consumption. Each tool scans local data, estimates costs using live [LiteLLM](https://github.com/BerriAI/litellm) pricing, and prints color-coded terminal tables.

> On our test account, Tokstat’s estimation of Claude Code usage matched Anthropic billing with approximately 95% accuracy over 30 days. Accuracy varies by tool — Claude Code, Codex, Gemini CLI and opencode read exact token counts; Cursor reads exact counts where they're recorded locally and flags the rest as `⚠ no data`; Kiro exposes no token counts at all (activity only); the web exports are estimated from text length. Tokstat provides estimates only, and we disclaim any responsibility or liability for differences between estimated and actual billing.

## Changelog

- **unreleased** — `--impact` mode: energy (kWh) and CO₂e estimate of the observed activity, reusing the EcoLogits methodology and model database (fetched + cached locally, no dependency). Usage phase only, with a min–max range and a configurable electricity mix.
- **1.7.0** — `--total` mode: a compact badge of total tokens + cost for the selected period/tool, with the data's actual date span and a per-tool breakdown (each tool's own date range). New `--period` options: `1 month`, `2 months`, `3 months`, `6 months` (unquoted `--period 3 months` works too). `--activity` shows the year on its own row above the months.
- **1.6.0** — `--activity` mode: a GitHub-style contribution calendar of daily activity over the period, colored by prompts/day, with the year shown at year boundaries and a summary of total prompts / turns / tokens and the busiest day. Reads directly from the scanned exchanges (history depth is limited by what each tool keeps on disk — see each tool's retention, e.g. Claude Code's `cleanupPeriodDays`, default 30).
- **1.5.1** — Codex token accounting fixed (cached input and reasoning tokens no longer double-counted); Cursor rewritten onto its SQLite store (exact counts where recorded, `⚠ no data` otherwise — never estimated); Kiro rewritten onto its per-session format (activity only); `⚠ no data` flag for rows without reliable token data; per-tool anomaly thresholds; per-provider plan recommendations. codex / cursor / kiro promoted to stable.
- **1.5.0** — Unified `tokstat` command across all tools; `--watch` live mode; Prompts / Turns / API columns + GRAND TOTAL block; added `opencode-token-usage`, `claude-web-token-usage`, `chatgpt-web-token-usage` (official-export import).
- **1.4.x** — `--version` flag; subagent sessions included in the Claude Code scan; update-check fix.

## Installation

```sh
pip install tokstat
```

Requires Python 3.7+. No dependencies. MIT License.

## Tools

| Command | Agent | Data source | Tokens | Cost | Status |
|---------|-------|-------------|--------|------|--------|
| `tokstat` | **all of the below** | combined | all of the below | ✓ | stable |
| `claude-token-usage` | Claude Code | `~/.claude/projects/` | ✓ exact | ✓ | stable |
| `codex-token-usage` | Codex (OpenAI) | `~/.codex/sessions/` | ✓ exact | ✓ | stable |
| `cursor-token-usage` | Cursor | `globalStorage/state.vscdb` | n.a. | n.a. | stable |
| `kiro-token-usage` | Kiro | `Kiro/.../workspace-sessions/` | n.a. | n.a. | stable |
| `gemini-token-usage` | Gemini CLI | `~/.gemini/tmp/` | ✓ exact | ✓ | experimental |
| `opencode-token-usage` | opencode | `~/.local/share/opencode/` | ✓ exact | ✓ | experimental |
| `claude-web-token-usage` | claude.ai (web export) | `--import` of official ZIP | ~ estimated | ~ | experimental |
| `chatgpt-web-token-usage` | chatgpt.com (web export) | `--import` of official ZIP | ~ estimated | ~ | experimental |

`tokstat` runs all scanners and aggregates their records into a single overview. Use `--tool <name>` to scope to one tool, or stick with the per-tool commands for detail.

> **Experimental tools** parse undocumented local formats that may change without notice. Data may be incomplete or inaccurate.
>
> **Cursor note:** tokstat reads Cursor's local SQLite store (`globalStorage/state.vscdb`). Some sessions have token counts recorded locally — these are reported exactly (`[exact]`). Others store no token counts (the local values are zero); those are tagged `[no tokens]`, counted as activity (prompts/turns), and never estimated — their cost shows `⚠ no data`. For authoritative totals use the [Cursor dashboard](https://cursor.com/dashboard).
>
> **Kiro note:** Kiro stores no usable token counts locally (its token log is always zero), so `kiro-token-usage` reports **activity only** — prompts and turns — with tokens and cost left blank. It does not estimate.
>
> **No-data flag:** any row whose tool/session has activity but no reliable local token data shows `⚠ no data` in the cost column (instead of a misleading `$0.00`). This is normal for Kiro and recent Cursor sessions.

### Web exports (claude.ai / chatgpt.com)

The two web tools work from the **official data export** each provider lets you request from your account settings. There is no live scraping — past attempts ran into 30-second per-request rate limits, anti-bot filters, and gray-area ToS questions. Stick to the export and tokstat reads it locally.

1. Request the export
   - **claude.ai**: Settings → Privacy → Export Data
   - **chatgpt.com**: Settings → Data controls → Export data
2. Wait for the email with the ZIP download link.
3. Import:
   ```sh
   claude-web-token-usage  --import path/to/claude-export.zip
   chatgpt-web-token-usage --import path/to/chatgpt-export.zip
   ```
4. Run normally; the cache under `~/.cache/tokstat/web/<service>/` is now the source of truth:
   ```sh
   claude-web-token-usage --period all
   chatgpt-web-token-usage --prompts --period "30 days"
   tokstat --tool chatgpt
   ```

Multiple accounts (perso + work) can coexist — add `--account <name>` on each `--import`. Each shows up as a separate row under **CONSUMPTION BY PROJECT**.

Cache management for the web tools:

```sh
claude-web-token-usage  --list-accounts          # show imported accounts
chatgpt-web-token-usage --clear-imports          # drop all imported conversations
chatgpt-web-token-usage --clear-imports --account work
chatgpt-web-token-usage --clean-cache            # drop legacy pre-import cache files
```

Token counts are **estimated** from message text length (`chars / 4`); models shown carry a `[est]` suffix. Real billing may differ.

## Modes

All tools support the same modes:

```sh
<tool>                          # Aggregated overview (period, project, model, speed)
<tool> --prompts   [-p]         # Per-exchange detail (text, turns, tokens, tools, cost)
<tool> --anomalies              # Technical anomaly detection
<tool> --activity               # GitHub-style activity calendar (by day) + tokens
<tool> --total                  # Compact totals (tokens + cost + data span)
<tool> --impact                 # Energy & CO₂ estimate (EcoLogits methodology)
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

### Default — aggregated overview

```sh
claude-token-usage
claude-token-usage --period all
codex-token-usage --period "7 days"
cursor-token-usage --period "30 days"
```

### `--prompts` — per-exchange detail

Per-exchange breakdown: user text, model, turns, tokens (input/output/cache), tool calls, cost.

```sh
claude-token-usage --prompts
claude-token-usage -p --period "7 days"
```

### `--anomalies` — technical anomaly detection

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

### `--activity` — activity calendar

A GitHub-style contribution calendar: one cell per day, colored by prompts/day,
with a summary of total prompts / turns / tokens and the busiest day.

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

### `--total` — compact totals

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

### `--impact` — energy & CO₂ estimate

Estimates the **environmental impact** of the observed activity, reusing the
[EcoLogits](https://github.com/genai-impact/ecologits) methodology and model
database (fetched and cached locally, like the pricing data — no extra
dependency).

```sh
tokstat --impact --period "30 days"
tokstat --impact --tool claude --period all
```

```
  ╭───────────────────────────────────────────╮
  │ ENERGY & CO₂ · Last 30 days                │
  │                                            │
  │ ~11.5 kWh    ~4.8 kg CO₂e                   │
  │ ± 31% (model-size uncertainty)             │
  │                                            │
  │ ≈ 40 km by car · 955 phone charges         │
  │ mix: world (0.418 kgCO₂e/kWh) · PUE 1.2     │
  ╰───────────────────────────────────────────╯

  By tool (data span used):
    Claude Code  9.82 kWh · 4.10 kg CO₂e   2026-04-14 → 2026-06-19
    Codex        9.61 kWh · 4.02 kg CO₂e   2026-01-21 → 2026-06-15
    ...
  By model (measurable span):
    gpt-5.5 [xhigh]   8.74 kWh · 3.65 kg CO₂e   2026-01-21 → 2026-06-15
    claude-opus-4-7   4.98 kWh · 2.08 kg CO₂e   2026-04-14 → 2026-06-19
    ...
```

The per-model span is the **measurable** period — the union of the data spans
of every tool that carries that model (e.g. a model used in both opencode and
Claude Code spans the union of both), since that's how far back its usage could
be observed.

> **⚠️ Order-of-magnitude estimate, usage phase only.** Energy is derived from
> output tokens × the model's (estimated) active parameters — for closed models
> like Claude/GPT, EcoLogits *estimates* the parameter count, hence the min–max
> range. It excludes hardware manufacturing (the embodied phase needs
> per-request GPU data tokstat doesn't have). Models absent from the EcoLogits
> database are excluded and reported.

Choose the electricity mix per run with `--region`:

```sh
tokstat --impact --region eu
tokstat --impact --region france --period "30 days"
```

Presets: `world` (default, 0.418), `eu` (0.250), `france` (0.056), `us` (0.369),
`green` (0.040) kgCO₂e/kWh — or pass an explicit factor (`--region 0.3`). To make
it permanent, set it in `~/.config/tokstat/impact.json`:

```json
{ "region": "france", "pue": 1.2 }
```

### `--plan` — plan & optimization recommendations

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

### `--export` — conversation export

Exports all exchanges to a JSON file.

```sh
claude-token-usage --export
claude-token-usage --export out.json --period "7 days"
```

```json
{
  "tool": "Claude Code",
  "model": "claude-opus-4-6",
  "timestamp": "2026-04-08T...",
  "user": "the user prompt text",
  "assistant": ["response 1", "response 2"],
  "turns": 25,
  "tools_used": {"Bash": 3, "Read": 7, "Edit": 2},
  "tool_errors": ["error message"]
}
```

## Filters

All modes support `--period`:

```sh
--period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days",
                     "1 month", "2 months", "3 months", "6 months", year
                     default: today — partial match works ("7" = "Last 7 days")
```

With `--period all`, the **CONSUMPTION BY PERIOD** table shows every window from
*Last hour* through *Last year*, plus a **Forever** row aggregating the entire
available history.

## Pricing

Model pricing is fetched from [LiteLLM's model pricing database](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and cached at `~/.cache/token-usage/litellm_prices.json` for 24 hours. Falls back to stale cache if fetch fails.

## Credits

Environmental-impact estimates (`--impact`) use the methodology and model database of [EcoLogits](https://github.com/genai-impact/ecologits) (MPL-2.0), fetched and cached locally.
