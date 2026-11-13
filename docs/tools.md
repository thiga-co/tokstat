# Tools

[← back to README](../README.md) · [Modes](modes.md) · [Filters](filters.md) · [Impact](impact.md) · [Reference](reference.md)

`tokstat` runs every scanner below and aggregates their records into a single
overview. Use `--tool <name>` to scope to one tool, or run a per-tool command
for a focused view.

| Command | Agent | Data source | Tokens | Cost | Status |
|---------|-------|-------------|--------|------|--------|
| `tokstat` | **all of the below** | combined | all of the below | ✓ | stable |
| `claude-token-usage` | Claude Code | `~/.claude/projects/` | ✓ exact | ✓ | stable |
| `codex-token-usage` | Codex (OpenAI) | `~/.codex/sessions/` | ✓ exact | ✓ | stable |
| `antigravity-token-usage` / `agy-token-usage` | Antigravity | `~/.gemini/antigravity-cli/` | ✓ exact | ✓ | experimental |
| `cursor-token-usage` | Cursor | `globalStorage/state.vscdb` | n.a. | n.a. | stable |
| `kiro-token-usage` | Kiro | `Kiro/.../workspace-sessions/` | n.a. | n.a. | stable |
| `copilot-token-usage` | GitHub Copilot (Chat) | VS Code `github.copilot-chat/session-store.db` + native chat | n.a. | n.a. | experimental |
| `copilot-cli-token-usage` | GitHub Copilot CLI | `~/.copilot/session-store.db` | ✓ exact | ✓ | experimental |
| `gemini-token-usage` | Gemini CLI | `~/.gemini/tmp/` | ✓ exact | ✓ | experimental |
| `opencode-token-usage` | opencode | `~/.local/share/opencode/` | ✓ exact | ✓ | experimental |
| `pi-token-usage` | pi | `~/.pi/agent/sessions/` | ✓ exact | ✓ | experimental |
| `qwen-token-usage` | Qwen Code | `~/.qwen/projects/` | ✓ exact | ✓ | experimental |
| `claude-web-token-usage` | claude.ai (web export) | `--import` of official ZIP | ~ estimated | ~ | experimental |
| `chatgpt-web-token-usage` | chatgpt.com (web export) | `--import` of official ZIP | ~ estimated | ~ | experimental |

> **Experimental tools** parse undocumented local formats that may change without notice. Data may be incomplete or inaccurate.
>
> **Cursor note:** tokstat reads Cursor's local SQLite store (`globalStorage/state.vscdb`). Some sessions have token counts recorded locally — these are reported exactly (`[exact]`). Others store no token counts (the local values are zero); those are tagged `[no tokens]`, counted as activity (prompts/turns), and never estimated — their cost shows `⚠ no data`. For authoritative totals use the [Cursor dashboard](https://cursor.com/dashboard).
>
> **Kiro note:** Kiro stores no usable token counts locally (its token log is always zero), so `kiro-token-usage` reports **activity only** — prompts and turns — with tokens and cost left blank. It does not estimate.
>
> **GitHub Copilot note:** `copilot-token-usage` reads **two** VS Code chat stores (across Code / Insiders / VSCodium / Cursor): the Copilot extension's `…/globalStorage/github.copilot-chat/session-store.db`, **and** the built-in VS Code chat's native session files (`…/workspaceStorage/<hash>/chatSessions/*.json` + `emptyWindowChatSessions/`). Copilot is a flat-rate subscription and records no token counts, model or cost locally (premium requests are metered only on GitHub's cloud billing), so it reports **activity only** (`[no tokens]`) — prompts, turns, projects and the tool/file timeline. Inline completions leave no local trace and aren't covered, and empty chat sessions (a panel opened with no prompt sent) don't appear.
>
> **pi note:** `pi-token-usage` reads the transcripts of the open-source [pi coding agent](https://pi.dev) (`@earendil-works/pi-coding-agent`) from `~/.pi/agent/sessions/`. pi records **exact** token counts, model, provider, tool calls, generation duration and the working directory per session, so it supports the full feature set (overview, `--prompts`, `--tool-use`, `--impact`, `--plan`, …). Cost uses live LiteLLM pricing keyed on the model id; **local models (Ollama, …) report exact tokens but have no price, so they correctly show `$0.00`** (not `⚠ no data`), and are excluded from the `--impact` estimate when absent from the EcoLogits database.
>
> **Copilot CLI note:** `copilot-cli-token-usage` reads the **GitHub Copilot CLI**'s store (`~/.copilot/session-store.db`) — a different product and store from the Copilot **Chat** extension above. Running `copilot` in a terminal (including VS Code's integrated agent) lands here. Unlike Chat, the CLI records **exact** token usage (`assistant_usage_events`: input/output/cache read+write/reasoning, plus TTFT and duration per request), the working directory, conversation turns and the tool/file timeline — so it supports the full feature set. `input_tokens` includes cache reads, so tokstat bills fresh input = input − cache_read. Cost uses live LiteLLM pricing keyed on Copilot's model ids.
>
> **Qwen Code note:** `qwen-token-usage` reads the transcripts of [Qwen Code](https://github.com/QwenLM/qwen-code) (Alibaba's coding agent, a Gemini-CLI fork, `@qwen-code/qwen-code`) from `~/.qwen/projects/<cwd>/chats/*.jsonl`. Each turn carries **exact** token usage (Gemini-API `usageMetadata`), model, the working directory, message text and tool calls, so Qwen Code supports the full feature set. Output speed comes from the exact `apiDurationMs` in Qwen's token ledger (`~/.qwen/usage/`). Cost uses live LiteLLM pricing keyed on the model id; **local models (Ollama, …) report exact tokens but have no price, so they correctly show `$0.00`** and are excluded from `--impact` when absent from the EcoLogits database.
>
> **No-data flag:** any row whose tool/session has activity but no reliable local token data shows `⚠ no data` in the cost column (instead of a misleading `$0.00`). This is normal for Kiro, GitHub Copilot and recent Cursor sessions.

## Web exports (claude.ai / chatgpt.com)

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
