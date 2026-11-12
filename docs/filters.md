# Filters

[← back to README](../README.md) · [Tools](tools.md) · [Modes](modes.md) · [Impact](impact.md) · [Reference](reference.md)

## `--period`

All modes support `--period`:

```sh
--period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days",
                     "1 month", "2 months", "3 months", "6 months", year
                     default: today — partial match works ("7" = "Last 7 days")
```

With `--period all`, the **CONSUMPTION BY PERIOD** table shows every window from
*Last hour* through *Last year*, plus a **Forever** row aggregating the entire
available history.

## `--tool`, `--project`, `--session`

On the unified `tokstat` command, three scoping filters apply to **every** mode
(overview, `--prompts`, `--tool-use`, `--impact`, `--export`, `--anomalies`,
`--plan`, `--activity`, `--total`):

```sh
--tool    <name>     claude, codex, cursor, kiro, copilot, gemini, antigravity,
                     opencode, claude.ai, chatgpt   (default: all)
--project <name>     one project — case-insensitive substring on the repo path,
                     e.g. --project tokstat   (all tools)
--session <id>       one conversation — full session id or the short 8-char
                     handle, e.g. 019f6a75   (Claude Code, Codex, GitHub Copilot;
                     other tools carry no session id on their records)
```

They combine freely, e.g.:

```sh
tokstat --impact  --session 019f6a75
tokstat --prompts --tool copilot --project benchmark-slm
tokstat --export out.json --project tokstat --period "30 days"
```
