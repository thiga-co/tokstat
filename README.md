# tokstat

CLI toolkit to aggregate and analyze AI coding assistant token consumption. Each tool scans local data, estimates costs using live [LiteLLM](https://github.com/BerriAI/litellm) pricing, and prints color-coded terminal tables.

> On our test account, Tokstat’s estimation of Claude Code usage matched Anthropic billing with approximately 95% accuracy over 30 days. Accuracy varies by tool — Claude Code, Codex, Gemini CLI and opencode read exact token counts; Cursor reads exact counts where they're recorded locally and flags the rest as `⚠ no data`; Kiro exposes no token counts at all (activity only); the web exports are estimated from text length. Tokstat provides estimates only, and we disclaim any responsibility or liability for differences between estimated and actual billing.

## Installation

```sh
pip install tokstat
```

Requires Python 3.7+. No dependencies. MIT License.

## Tools

| Command | Agent | Data source | Tokens | Cost | Status |
|---------|-------|-------------|--------|------|--------|
| `tokstat` | **all of the below** | combined | mixed | ✓ | new |
| `claude-token-usage` | Claude Code | `~/.claude/projects/` | ✓ exact | ✓ | stable |
| `codex-token-usage` | Codex (OpenAI) | `~/.codex/sessions/` | ✓ exact | ✓ | stable |
| `cursor-token-usage` | Cursor | `globalStorage/state.vscdb` | ✓ exact / n.a. | ~ | stable |
| `kiro-token-usage` | Kiro | `~/Library/.../Kiro/` | ✗ activity only | ✗ | stable |
| `gemini-token-usage` | Gemini CLI | `~/.gemini/tmp/` | ✓ exact | ✓ | experimental |
| `opencode-token-usage` | opencode | `~/.local/share/opencode/` | ✓ exact | ✓ | experimental |
| `claude-web-token-usage` | claude.ai (web export) | `--import` of official ZIP | ~ estimated | ~ | experimental |
| `chatgpt-web-token-usage` | chatgpt.com (web export) | `--import` of official ZIP | ~ estimated | ~ | experimental |

`tokstat` runs all scanners and aggregates their records into a single overview. Use `--tool <name>` to scope to one tool, or stick with the per-tool commands for detail.

> **Experimental tools** parse undocumented local formats that may change without notice. Data may be incomplete or inaccurate.
>
> **Cursor note:** tokstat reads Cursor's local SQLite store (`globalStorage/state.vscdb`). Some sessions have token counts recorded locally — these are reported exactly (`[exact]`). Others store no token counts (the local values are zero); those are tagged `[no tokens]`, counted as activity (prompts/turns), and never estimated — their cost shows `⚠ no data`. For authoritative totals use [cursor.com/settings/usage](https://cursor.com/settings/usage).
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
--period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days", year
                     default: today — partial match works ("7" = "Last 7 days")
```

With `--period all`, the **CONSUMPTION BY PERIOD** table shows every window from
*Last hour* through *Last year*, plus a **Forever** row aggregating the entire
available history.

## Pricing

Model pricing is fetched from [LiteLLM's model pricing database](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and cached at `~/.cache/token-usage/litellm_prices.json` for 24 hours. Falls back to stale cache if fetch fails.
