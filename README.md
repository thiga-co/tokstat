# token-usage

Single-file CLI tool that aggregates and displays token consumption across AI coding assistants. Scans local log files, estimates costs using live [LiteLLM](https://github.com/BerriAI/litellm) pricing data, and prints everything in a color-coded terminal table.

## Supported tools

All 8 tools are supported in all modes (default, --prompts, --audit, --anomalies, --plan, --export). The table below shows what data is available from each tool's data sources:

| Tool | Data source | Tokens | Text | Tools | Speed |
|------|-------------|--------|------|-------|-------|
| Claude Code | `~/.claude/projects/` | ✓ | ✓ | ✓ | ✓ |
| Codex (OpenAI) | `~/.codex/sessions/` | ✓ | ✓ | — | ✓ |
| Gemini CLI | `~/.gemini/` | ✓ | — | — | ✓ |
| Cline | `~/.cline/data/sessions/` | ✓ | — | — | — |
| OpenCode | `~/.local/share/opencode/` | ✓ | ✓ | ✓ | — |
| Qwen Coder | `~/.qwen/` | ✓ | ✓ | ✓ | — |
| Cursor | `~/Library/Application Support/Cursor/` | ✓ | — | — | — |
| Kiro | `~/Library/Application Support/Kiro/` | — | ✓ | ✓ | — |

Tools not installed are silently skipped. Missing data is handled gracefully (e.g., tool calls default to empty, text exchanges with no tokens work fine).

## Installation

No dependencies. Requires Python 3.10+.

```sh
chmod +x token-usage
ln -s $(pwd)/token-usage ~/.local/bin/token-usage
```

## Global options

All modes support these filters:

```sh
--period <period>    Time filter — all, hour, "5 hours", today, yesterday, "7 days", "30 days", year (default: today)
--tool <name>        Tool filter — claude, codex, gemini, cline, opencode, qwen, cursor, kiro (default: all)
```

**Periods**: Partial match works (`"7"` = `"Last 7 days"`).

**Tools**: Aliases work (`openai` = Codex, `claude-code` = Claude Code). All tools work in all modes (all 6 modes support all 8 tools).

## Modes

### Default — aggregated overview

```sh
token-usage                              # all tools, all time
token-usage --period today               # all tools, today only
token-usage --tool claude                # Claude Code only, all time
token-usage --tool claude --period "7 days"
```

Displays: consumption by period, by project, by model, output speed, and grand total.

### `--prompts` — per-exchange detail (all tools)

```sh
token-usage --prompts
token-usage -p --period today
token-usage --prompts --tool cursor --period "7 days"
```

Per-exchange breakdown: user text, model, turns, tokens (input/output/cache), tool calls, cost. Works for all 8 tools.

### `--audit` — behavioral anti-pattern detection

```sh
token-usage --audit
token-usage -a --tool opencode --period "30 days"
```

Scans assistant transcripts for 11 categories of behavioral anti-patterns across all 8 tools (Claude Code, Codex, Gemini CLI, Cline, OpenCode, Qwen Coder, Cursor, Kiro). Each finding is tagged with tool and model. Summary tables show breakdown by category, by tool, and by model with incident rates.

#### Detection categories

| Abbr. | Category | What it detects |
|-------|----------|----------------|
| Gaslt | Gaslighting contextuel | Denying previous statements, rewriting history |
| Anthr | Anthropomorphisme / fausse empathie | False emotions, fake experience claims |
| Hedge | Dilution par prudence | Dense hedging clusters in a single sentence |
| Lazy | Paresse intellectuelle | Deflecting to docs, generic non-answers, filler |
| Overc | Aplomb trompeur | Confident assertions followed by tool errors |
| Sycop | Flagornerie / sycophancy | Excessive praise, performative agreement |
| Compl | Acquiescement performatif | "You're right, but..." patterns |
| Prem. | Solution prematuree | Declaring victory before verification |
| Loop | Boucle d'echec | User reports same failure 3+ times |
| Verb. | Verbosite creuse | Long structured response to short question |
| FakeU | Comprehension feinte | "I understand" without addressing the issue |

All patterns detect both French and English. Metalanguage and code blocks are filtered out.

### `--anomalies` — technical anomaly detection

```sh
token-usage --anomalies
token-usage --anomalies --tool claude --period "30 days"
```

Detects unusual patterns in per-exchange token data across all 8 tools (Claude Code, Codex, Gemini CLI, Cline, OpenCode, Qwen Coder, Cursor, Kiro). Results grouped by project with worktree resolution.

| Anomaly | Trigger | Severity |
|---------|---------|----------|
| Runaway cost | Prompt costs 10x+ the P90 | HIGH |
| High cost | Prompt costs 5x+ the P90 | MEDIUM |
| Tool storm | 30+ tool calls in a single prompt | HIGH >60, MEDIUM >30 |
| Turn spiral | API turns 5x+ the P90 | HIGH >10x, MEDIUM >5x |
| Cache thrashing | High cache writes with <50% read-back | MEDIUM |
| Context bloat | Input/output ratio >50:1 with >10K input | LOW |
| Empty exchange | 5+ turns but <100 output tokens | MEDIUM |

Thresholds are computed dynamically from the user's own data (median, P90).

### `--plan` — plan & optimization recommendations

```sh
token-usage --plan
token-usage --plan --tool claude --period "30 days"
```

Cost breakdown by model, plan recommendation, and data-driven optimization advice.

- **Cost table** — per-model: calls, cost, avg/day, projected monthly, cache efficiency, share
- **Plan mapping** — Free (<$5/mo), Pro (<$18/mo), Max 5x (<$100/mo), Max 20x (<$200/mo), Enterprise (>$500/mo)
- **Optimization recommendations** (conditional, only when data supports them):
  - **Model selection** — if top model takes >80% of spend, suggests cheaper alternative from same family (looked up dynamically from LiteLLM pricing)
  - **Cache optimization** — if hit rate <70%, suggests longer sessions
  - **Guardrails** — if runaway agents detected, suggests max_turns and hooks
  - **Context reduction** — if cache writes >5M, suggests CLAUDE.md, RTK, Repomix, .claudeignore
  - **Spending hygiene** — if peak day >5x average, suggests budget alerts
  - **Tool diversification** — if using single tool, suggests alternatives with free tiers

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

```sh
token-usage --export
token-usage --export out.json --tool kiro --period year
```

Exports all exchanges to a single JSON file. Works for all 8 tools. Applies all filters (--period, --tool).

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

## Pricing

Model pricing is fetched from [LiteLLM's model pricing database](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and cached locally at `~/.cache/token-usage/litellm_prices.json` for 24 hours. Falls back to stale cache if fetch fails.

## Project normalization

Git worktrees (including Cline worktrees under `~/.cline/worktrees/`) are automatically resolved to their main project, so usage from multiple worktrees is aggregated under a single entry.
