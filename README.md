# tokstat

CLI tool that aggregates and displays Claude Code token consumption. Scans `~/.claude/projects/` JSONL transcripts, estimates costs using live [LiteLLM](https://github.com/BerriAI/litellm) pricing data, and prints everything in a color-coded terminal table.

## Installation

```sh
pip install tokstat
```

Requires Python 3.7+. No dependencies.

## Usage

```sh
claude-token-usage                        # overview for today
claude-token-usage --period all           # all time
claude-token-usage --period "7 days"
```

## Modes

### Default — aggregated overview

Displays consumption by period, by project, by model, output speed, and grand total.

```sh
claude-token-usage
claude-token-usage --period all
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

  Optimization Recommendations

  Model selection
    100% of spend is on claude-opus-4-6. claude-sonnet-4-6 is 2x cheaper.
      - Use claude-sonnet-4-6 for simple tasks
      - Switching 30% would save ~$49.78/mo
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

Model pricing is fetched from [LiteLLM's model pricing database](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and cached locally at `~/.cache/token-usage/litellm_prices.json` for 24 hours. Falls back to stale cache if fetch fails.
