# token-usage

Single-file CLI tool that aggregates and displays token consumption across AI coding assistants. Scans local log files, estimates costs using live [LiteLLM](https://github.com/BerriAI/litellm) pricing data, and prints everything in a color-coded terminal table.

## Supported tools

| Tool | Data source |
|------|-------------|
| Claude Code | `~/.claude/projects/` (JSONL transcripts) |
| Codex (OpenAI) | `~/.codex/sessions/` |
| Gemini CLI | `~/.gemini/` |
| Cline | `~/.cline/data/sessions/sessions.db` (SQLite) |
| OpenCode | `~/.local/share/opencode/` (SQLite) |
| Qwen Coder | `~/.qwen/` |
| Cursor | `~/Library/Application Support/Cursor/` (SQLite) |
| Kiro | `~/Library/Application Support/Kiro/` |

Tools that are not installed are silently skipped.

## Installation

No dependencies. Requires Python 3.10+.

```sh
# Make executable
chmod +x token-usage

# Optionally symlink somewhere in your PATH
ln -s $(pwd)/token-usage ~/.local/bin/token-usage
```

## Usage

### Default view — aggregated overview

```sh
token-usage
```

Displays four sections:

1. **Consumption by period** — tokens and cost for last hour, 5 hours, today, 7 days, 30 days, year, broken down by tool
2. **Consumption by project** — per-project breakdown with tool-level detail, sorted by cost
3. **Cost by model** — total input/output tokens and cost per model
4. **Output speed** — median, average, P10, P90 tokens/sec per model (Claude Code, Codex, Gemini CLI)

Plus a grand total at the bottom.

### Prompt-level view — per-conversation detail (Claude Code)

```sh
token-usage --prompts                    # last 7 days (default)
token-usage -p                           # short form
token-usage --prompts --period today     # filter by period
token-usage --prompts --period "30 days" # last 30 days
token-usage --prompts --since hour       # partial match works
```

Shows each Claude Code session with its individual prompts:

- Prompt text (truncated)
- Model used
- Number of API turns
- Token breakdown: input, output, cache read, cache write
- Tool calls with counts (e.g. `Bash:3 Read:2 Edit`)
- Cost per prompt

Available periods: `hour`, `5 hours`, `today`, `7 days`, `30 days`, `year`.

## Pricing

Model pricing is fetched from [LiteLLM's model pricing database](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and cached locally at `~/.cache/token-usage/litellm_prices.json` for 24 hours. If the fetch fails, stale cache is used. If no pricing data is available, costs show as $0.

Cost calculation includes:
- Input tokens
- Output tokens
- Cache read tokens (discounted rate)
- Cache creation tokens

## Project normalization

Git worktrees (including those created by Cline under `~/.cline/worktrees/`) are automatically resolved to their main project, so usage from multiple worktrees of the same repository is aggregated under a single project entry.

## Example output

```
 Token Usage Aggregator
  Loading pricing from LiteLLM...
  2150 models loaded
  Scanning local AI coding tool data...

  ● Claude Code    5954 records from ~/.claude/
  ● Codex           249 records from ~/.codex/
  ● Gemini CLI      121 records from ~/.gemini/
  ● OpenCode       1116 records from ~/.local/share/opencode/

──────────────────────────────────────────────────────────────
 CONSUMPTION BY PERIOD
──────────────────────────────────────────────────────────────
  Period        Tool          Input  Output  Cache R  Cache W     Cost
  ────────────  ───────────  ──────  ──────  ───────  ───────  ───────
  Last hour     Claude Code      64    6.3K     1.2M    41.2K    $1.04
  Today         Claude Code     152   17.6K     2.1M   216.8K    $2.83
  ...
```

```
token-usage --prompts --period today

 Claude Code — Prompt-level Usage

  ~/Code/myproject  fuzzy-munching-narwhal  14 prompts  129 turns  $9.26
  #  Time   Prompt                               Turns  Input  Output  Cache R  Cache W  Tools              Cost
  ─  ─────  ───────────────────────────────────  ─────  ─────  ──────  ───────  ───────  ─────────────────  ──────
  1  06:55  write a python script that shows...      4     10    4.9K    49.6K    18.2K  Write              $0.26
  2  06:59  I want real-time updates with a...       7     13    7.2K   157.0K    19.4K  Bash:2 Write       $0.38
  3  07:03  add trajectory display with...          37     47   16.1K     1.7M    51.3K  Bash:9 Edit:6 +3   $1.55
  ...
```
