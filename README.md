# tokstat

CLI toolkit to aggregate and analyze AI coding assistant token consumption. Each tool scans local data, estimates costs using live [LiteLLM](https://github.com/BerriAI/litellm) pricing, and prints color-coded terminal tables.

> On our test account, Tokstat’s estimation matched Anthropic billing with approximately 95% accuracy over 30 days of usage. That said, Tokstat provides estimates only, and we disclaim any responsibility or liability for differences between estimated and actual billing.

## Installation

```sh
pip install tokstat
```

Requires Python 3.7+. No dependencies. MIT License.

## Tools

| Command | Agent | Data source | Tokens | Cost | Status |
|---------|-------|-------------|--------|------|--------|
| `claude-token-usage` | Claude Code | `~/.claude/projects/` | ✓ exact | ✓ | stable |
| `codex-token-usage` | Codex (OpenAI) | `~/.codex/sessions/` | ✓ exact | ✓ | experimental |
| `cursor-token-usage` | Cursor | `~/.cursor/projects/` | ~ estimated | ~ | experimental |
| `kiro-token-usage` | Kiro | `~/Library/.../Kiro/` | ~ estimated | ~ | experimental |
| `gemini-token-usage` | Gemini CLI | `~/.gemini/tmp/` | ✓ exact | ✓ | experimental |

> **Experimental tools** parse undocumented local formats that may change without notice. Data may be incomplete or inaccurate.
>
> **Cursor note:** token counts are tracked server-side and not stored locally. Estimates can be 5–15× lower than reality. For exact counts use [cursor.com/settings/usage](https://cursor.com/settings/usage).

## Modes

All tools support the same modes:

```sh
<tool>                          # Aggregated overview (period, project, model, speed)
<tool> --prompts   [-p]         # Per-exchange detail (text, turns, tokens, tools, cost)
<tool> --anomalies              # Technical anomaly detection
<tool> --plan                   # Cost breakdown + plan recommendation
<tool> --export    [file.json]  # Export all exchanges to JSON
```

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
| Runaway cost | Prompt costs 10x+ the P90 | HIGH |
| High cost | Prompt costs 5x+ the P90 | MEDIUM |
| Tool storm | 30+ tool calls in a single prompt | HIGH >60, MEDIUM >30 |
| Turn spiral | API turns 5x+ the P90 | HIGH >10x, MEDIUM >5x |
| Cache thrashing | High cache writes with <50% read-back | MEDIUM |
| Context bloat | Input/output ratio >50:1 with >10K input | LOW |
| Empty exchange | 5+ turns but <100 output tokens | MEDIUM |

Thresholds are computed dynamically from your own data (median, P90).

### `--plan` — plan & optimization recommendations

Cost breakdown by model, plan recommendation, and data-driven optimization advice.

```sh
claude-token-usage --plan
claude-token-usage --plan --period all
```

```
  All time — 17 active days / 30

  Model              Calls     Cost   Avg/day  Projected/mo  Cache  Share
  ─────────────────  ─────  ───────  ────────  ────────────  ─────  ─────
  claude-opus-4-6      321  $475.19  $15.84/d    $475.19/mo    96%   100%
  claude-sonnet-4-6      8   $0.811  $0.027/d     $0.811/mo    96%     0%
  TOTAL                329  $476.00  $15.87/d    $476.00/mo    96%

  Plan (based on All time)
    Max 20x ($200/mo) strongly recommended.
    Projected API cost: $476.00/mo — you'd save ~$276.00/mo
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

## Pricing

Model pricing is fetched from [LiteLLM's model pricing database](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and cached at `~/.cache/token-usage/litellm_prices.json` for 24 hours. Falls back to stale cache if fetch fails.
