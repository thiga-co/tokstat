# tokstat

[![PyPI](https://img.shields.io/pypi/v/tokstat.svg)](https://pypi.org/project/tokstat/)
[![Python](https://img.shields.io/pypi/pyversions/tokstat.svg)](https://pypi.org/project/tokstat/)
[![License](https://img.shields.io/pypi/l/tokstat.svg)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/tokstat.svg)](https://pypi.org/project/tokstat/)

**One command to see how many tokens — and how much money — your AI coding assistants are burning.**

`tokstat` scans the **local** data of every assistant you use — Claude Code, Codex, Cursor, Kiro, GitHub Copilot, Gemini CLI, Antigravity, opencode, plus claude.ai / ChatGPT web exports — estimates cost with live [LiteLLM](https://github.com/BerriAI/litellm) pricing, and prints clear, color-coded terminal tables. Zero dependencies, nothing leaves your machine.

```sh
pip install tokstat     # Python 3.7+, no dependencies
tokstat                 # aggregated overview across all tools
```

> **Accuracy.** On our test account, tokstat's Claude Code estimate matched Anthropic billing within ~5% over 30 days. It varies by tool: Claude Code, Codex, Antigravity, Gemini CLI and opencode read **exact** token counts; Cursor reads exact counts where recorded and flags the rest `⚠ no data`; **Kiro and GitHub Copilot expose no token counts (activity only)**; web exports are estimated from text length. Estimates only — see [Tools](docs/tools.md) for per-tool accuracy.

---

## Quickstart

```sh
tokstat                              # overview: by period, project, model
tokstat --period "30 days"           # pick a window (default: today)
tokstat --prompts                    # per-exchange detail (tokens, tools, cost)
tokstat --total --tool codex         # compact totals for one tool
tokstat --impact                     # energy & CO₂ estimate
tokstat --watch                      # live, refreshing overview
```

Scope any view with `--tool`, `--project`, `--session` and `--period` — e.g.
`tokstat --impact --session 019f6a75` or `tokstat --prompts --project tokstat`.
See [Filters](docs/filters.md).

## Supported tools

`tokstat` aggregates everything; each assistant also has its own `*-token-usage` command.

| Agent | Command | Tokens |
|---|---|---|
| Claude Code | `claude-token-usage` | ✓ exact |
| Codex (OpenAI) | `codex-token-usage` | ✓ exact |
| Gemini CLI | `gemini-token-usage` | ✓ exact |
| opencode | `opencode-token-usage` | ✓ exact |
| Antigravity | `antigravity-token-usage` | ✓ exact |
| Cursor | `cursor-token-usage` | exact / `⚠ no data` |
| Kiro | `kiro-token-usage` | activity only |
| GitHub Copilot | `copilot-token-usage` | activity only |
| claude.ai / ChatGPT (web) | `claude-web-token-usage` / `chatgpt-web-token-usage` | ~ estimated (import) |

Full table, accuracy notes and the web-export workflow → **[docs/tools.md](docs/tools.md)**.

## Documentation

- **[Tools](docs/tools.md)** — every supported assistant, data sources, accuracy, web exports.
- **[Modes](docs/modes.md)** — overview, `--prompts`, `--tool-use`, `--anomalies`, `--activity`, `--total`, `--plan`, `--export`, `--watch`.
- **[Filters](docs/filters.md)** — `--period`, `--tool`, `--project`, `--session`.
- **[Impact](docs/impact.md)** — the `--impact` energy & CO₂ estimate (EcoLogits), in depth.
- **[Reference](docs/reference.md)** — pricing, caches, diagnostics (`TOKSTAT_TIMING`).
- **[Changelog](CHANGELOG.md)** — release history.

## Credits

Environmental-impact estimates (`--impact`) port the usage-phase energy formula and constants of [EcoLogits](https://github.com/genai-impact/ecologits) (MPL-2.0) and use its model database, fetched and cached locally.

## License

tokstat is MIT (see [LICENSE](LICENSE)), **except** `src/tokstat/_ecologits.py`, which is licensed under the **MPL-2.0** because it ports MPL-2.0 source from EcoLogits. The MPL-2.0 is a file-level copyleft and governs only that one file; everything else is MIT. See [NOTICE](NOTICE) for details.
