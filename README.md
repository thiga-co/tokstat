# tokstat

CLI toolkit to aggregate and analyze AI coding assistant token consumption. Each tool scans local data, estimates costs using live [LiteLLM](https://github.com/BerriAI/litellm) pricing, and prints color-coded terminal tables.

> On our test account, Tokstat’s estimation of Claude Code usage matched Anthropic billing with approximately 95% accuracy over 30 days. Accuracy varies by tool — Claude Code, Codex, Antigravity, Gemini CLI and opencode read exact token counts; Cursor reads exact counts where they're recorded locally and flags the rest as `⚠ no data`; Kiro exposes no token counts at all (activity only); the web exports are estimated from text length. Tokstat provides estimates only, and we disclaim any responsibility or liability for differences between estimated and actual billing.

## Changelog

- **1.19.0** — Added **Antigravity** *(experimental)* (`antigravity-token-usage` / `agy-token-usage`): scans `~/.gemini/antigravity-cli/` SQLite databases + transcript logs for **exact** prompt/output/cached token metrics, tool-call timeline, models, speed and duration. The token metrics live in protobuf blobs with no public schema, so the reader is reverse-engineered and **fails loudly** if the format drifts (it warns when steps stop parsing, and flags models with no LiteLLM price rather than silently costing $0). Adds `--period 24h` / `1 day`. Thanks **@louisvolant** ([#3](https://github.com/thiga-co/tokstat/pull/3)).
- **1.18.2** — Fix (opencode): read opencode's new **SQLite storage** (`opencode.db`). Recent opencode versions replaced the JSON `storage/message/…` layout, so tokstat found no data on up-to-date installs. The reader now auto-detects the backend (current `session_message` schema → intermediate `message`+`part` → legacy JSON), opens the DB read-only, and also surfaces opencode tool calls in `--tool-use`. Thanks **@louisvolant** ([#2](https://github.com/thiga-co/tokstat/pull/2)).
- **1.18.1** — Fix: the **per-session tables** (`--by-session` and `--impact`'s By-session) now show each session's **actual working directory** instead of `normalize_project()`'s git-repo-root collapse — so distinct sub-projects of one repo (e.g. `…/M5/m5agent`, `…/M5/m5agent-arduino`) no longer merge into a single row. The per-project / per-workspace aggregations still collapse by repo, as intended. A bare `~` means the session ran from your home directory.
- **1.18.0** — `--tool-use` gains a **`--session <id>`** filter (Claude Code + Codex): scope the timeline to a single conversation by its session id — the full id or the short handle prefix that `--by-session` / the timeline show (e.g. `--session 019f6a75`). Each timeline line now also tags its `[session]`.
- **1.17.0** — New **`--tool-use`** mode (Claude Code + Codex): a chronological **timeline of every tool call** — which tool, what it targeted (the file for Read/Edit/Write, the shell command for Bash/exec, the pattern/query for search), and when — grouped by conversation, with failed calls marked `✗`. Answers "which files and tools were touched, and in what order".
- **1.16.0** — Exchange **duration** (Claude Code + Codex): `--export` entries carry **`duration_s`** (wall-clock seconds from the prompt to the exchange's last activity) and `--prompts` gains a **Dur** column (`4s` · `3m` · `1h12m`). Note it's wall-clock, so an exchange left idle mid-way counts the gap.
- **1.15.0** — Context/compaction tracking now covers **Codex** too: `context_tokens` (peak from `token_count`) and `compactions` (each `compacted` event, with `pre_tokens` → `post_tokens` derived from the surrounding token counts). Codex doesn't record a `trigger` or duration, so those stay `null` and the `--prompts` marker reads `⇩ 187K→36K` (no trigger) vs Claude Code's `⇩auto 1.0M→10K`.
- **1.14.0** — Context tracking (Claude Code): `--export` entries now carry **`context_tokens`** (peak context size the exchange reached — input + cache read + cache write) and **`compactions`** (each auto-compact / `/compact` event with `trigger`, `pre_tokens` → `post_tokens`, duration and timestamp). `--prompts` gains matching **Context** and **Compaction** columns (e.g. `⇩auto 1.0M→10.4K`). Other tools don't expose compaction, so the fields stay empty there.
- **1.13.0** — `--export` entries now include **`session`** (the source transcript id / session name), **`cwd`** (the working directory the session ran in) and **`worktree`** (the git main worktree `cwd` resolves to), so exports carry the full provenance of each exchange.
- **1.12.1** — `--by-session` now lists **every** session (no `… +N more` truncation) — the table header shows `all N, by cost`.
- **1.12.0** — New **`--by-session`** flag on the default overview (all tools + unified `tokstat`): adds a **Consumption by session** table under the by-project table — the heaviest conversations by cost, with tool, project, prompts/turns, tokens and cost. The default overview is unchanged unless the flag is passed; works with `--watch` too.
- **1.11.0** — `--impact` gains a **By workspace** breakdown: energy/CO₂ grouped **per project** (the repo/dir the work ran in), with each workspace's distinct-session count — sitting alongside the per-date Trend and per-session tables (top 15 + `… +N more`). Workspaces group sessions across tools.
- **1.10.0** — `--impact` gains a **By session** breakdown: alongside the per-date Trend table, the heaviest conversations are now listed individually (energy, CO₂e, tokens, exchange count, date span, project — top 15 + a `… +N more` remainder). Exchanges are tagged with their source transcript as `session_id` across every tool (Claude Code, Codex, Cursor, Kiro, Gemini, opencode, web exports), so the split works everywhere.
- **1.9.0** — Overview now shows, per tool, the **data span** and the **"since" date** of its records in the selected period (next to the record count), so history depth is visible at a glance. Added **data-retention alerts**: when a tool purges old local data on a rolling window (e.g. Claude Code's `cleanupPeriodDays`, default 30), a warning appears at the top of both `tokstat` and the per-tool commands so the numbers and "since" dates aren't mistaken for full lifetime usage. Tools that keep everything (e.g. Codex session rollouts) produce no alert.
- **1.8.2** — `--impact` correctness fixes: (1) honor EcoLogits' `active_parameters` field for MoE models given as a scalar total + separate active count (e.g. `command-a-plus`: 218B total / 25B active — was counted as 218B active); (2) constrain model matching to exact + version-boundary base names, so a generic name no longer resolves to an arbitrary specific variant (`claude-sonnet-4` → `claude-sonnet-4-5`, `gemini-2.5` → `gemini-2.5-flash-image`); (3) base the "matched / not in DB" accounting on computed energy, so a known model with only prefill/cache tokens (no output) is no longer reported as unmatched; (4) actually read `prefill_factor` / `cache_read_factor` from `impact.json` (previously documented but ignored).
- **1.8.1** — `--impact`: add a prefill/context energy term. EcoLogits' formula bills energy from output tokens only (decode phase), which badly undercounts cache-heavy agentic use where output is ~0.4% of token traffic. Input + cache writes are now counted at a reduced prefill rate and cache reads at a small memory-movement rate (physics-grounded fractions of a decode token, widening the ± band). Typically lifts the headline ~2–4×. The frugality verdict stays decode-only so the mascot still grades model choice, not context volume.
- **1.8.0** — `--impact` mode: energy (kWh) and CO₂e estimate of the observed activity, reusing the EcoLogits methodology and model database (fetched + cached locally, no dependency). Usage phase only, with a single headline figure + ±% uncertainty and a configurable electricity mix (`--impact eu`, `france`, …). Includes a mascot-graded frugality verdict (Wh per 1k output tokens), a per-bucket Trend table (Δ vs previous day/week/month), a plain-language Analysis, and per-tool / per-model breakdowns with measurable data spans. Large swings (> ~5×) are described ("ramping up", "rose sharply") rather than quoted as misleading percentages. An energy/CO₂ line also appears on `--activity`.
- **1.7.0** — `--total` mode: a compact badge of total tokens + cost for the selected period/tool, with the data's actual date span and a per-tool breakdown (each tool's own date range). New `--period` options: `1 month`, `2 months`, `3 months`, `6 months` (unquoted `--period 3 months` works too). `--activity` shows the year on its own row above the months.
- **1.6.0** — `--activity` mode: a GitHub-style contribution calendar of daily activity over the period, colored by prompts/day, with the year shown at year boundaries and a summary of total prompts / turns / tokens and the busiest day. Reads directly from the scanned exchanges (history depth is limited by what each tool keeps on disk — see each tool's retention, e.g. Claude Code's `cleanupPeriodDays`, default 30).
- **1.5.1** — Codex token accounting fixed (cached input and reasoning tokens no longer double-counted); Cursor rewritten onto its SQLite store (exact counts where recorded, `⚠ no data` otherwise — never estimated); Kiro rewritten onto its per-session format (activity only); `⚠ no data` flag for rows without reliable token data; per-tool anomaly thresholds; per-provider plan recommendations. codex / cursor / kiro promoted to stable.
- **1.5.0** — Unified `tokstat` command across all tools; `--watch` live mode; Prompts / Turns / API columns + GRAND TOTAL block; added `opencode-token-usage`, `claude-web-token-usage`, `chatgpt-web-token-usage` (official-export import).
- **1.4.x** — `--version` flag; subagent sessions included in the Claude Code scan; update-check fix.

## Installation

```sh
pip install tokstat
```

Requires Python 3.7+. No dependencies. MIT (one MPL-2.0 file — see [License](#license)).

## Tools

| Command | Agent | Data source | Tokens | Cost | Status |
|---------|-------|-------------|--------|------|--------|
| `tokstat` | **all of the below** | combined | all of the below | ✓ | stable |
| `claude-token-usage` | Claude Code | `~/.claude/projects/` | ✓ exact | ✓ | stable |
| `codex-token-usage` | Codex (OpenAI) | `~/.codex/sessions/` | ✓ exact | ✓ | stable |
| `antigravity-token-usage` / `agy-token-usage` | Antigravity | `~/.gemini/antigravity-cli/` | ✓ exact | ✓ | experimental |
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
<tool> --impact    [region]     # Energy & CO₂ estimate (EcoLogits); region = world (default), eu, …
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

### `--tool-use` — tool-call timeline

Chronological list of every tool call (Claude Code & Codex), grouped by
conversation: the tool, what it targeted (file for Read/Edit/Write, command for
Bash/exec, pattern/query for search) and the time it ran. Failed calls are
marked `✗`. Filterable by `--period` / `--tool`, and by **`--session <id>`** to
scope to one conversation (full id or the short handle each line shows in `[…]`).

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
with a summary of total prompts / turns / tokens, the busiest day, and a
one-line energy & CO₂ estimate (see `--impact` for the detailed breakdown).

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
  │ 🐘  ~34.5 kWh  ·  ~14.4 kg CO₂e   heavy     │
  │ ± 69% · 4.8 Wh/1k · trend ↗ growing (+12%)  │
  │                                            │
  │ ≈ 120 km by car · 2875 phone charges       │
  │ mix: world (0.418 kgCO₂e/kWh) · PUE 1.2     │
  ╰───────────────────────────────────────────╯

  Trend (per week) — Δ vs previous week:
    bucket       tokens   energy     Δ       CO₂e    Wh/1k     Δ
    2026-04-13    42.1M  1.74kWh    —      0.73kg     4.6     —
    2026-04-20    38.7M  1.56kWh  -11%     0.65kg     4.9   +12%
    ...

  Analysis (first vs second half of the period)
    • Electricity use rose sharply (0.79 → 7.89 kWh per week).
    • CO₂ followed the same path — ~14.4 kg CO₂e total over the window.
    • Frugality worsened 18% (heavier model mix): 4.1 → 4.8 Wh per 1k output tokens.
  By tool (data span used):
    Claude Code  16.2 kWh · 6.77 kg CO₂e   2026-04-14 → 2026-06-19
    Codex        14.1 kWh · 5.90 kg CO₂e   2026-01-21 → 2026-06-15
    ...
  By model (measurable span):
    gpt-5.5 [xhigh]  12.9 kWh · 5.39 kg CO₂e   2026-01-21 → 2026-06-15
    claude-opus-4-7   7.3 kWh · 3.05 kg CO₂e   2026-04-14 → 2026-06-19
    ...
  By workspace (heaviest projects):
    …erbergeret/Code/tokstat  33.4 kWh · 13.9 kg CO₂e   999.1M tok · 873 sess · 2026-06-17→09-11
    …rs/olivierbergeret/Code  14.1 kWh ·  5.9 kg CO₂e   238.3M tok ·  21 sess · 2026-06-15→09-07
    ...
  By session (heaviest conversations):
    e25ea4e5  17.47 kWh ·  7.30 kg CO₂e   483.7M tok · 167 ex · 2026-08-25→09-11 · Code/tokstat
    476311e5   0.43 kWh ·  0.18 kg CO₂e     2.3M tok ·   6 ex · 2026-08-25→09-07 · Code/PRO/fdj
    ...
```

Three complementary breakdowns of the same total: a **Trend** table **per date**
(day / week / month, auto-granularity), a **By workspace** table **per project**
(the repo/dir the work ran in, with its distinct-session count), and a **By
session** table **per conversation** — heaviest first in each, with energy, CO₂e,
tokens, counts and date span (top 15 each, with a `… +N more` remainder). A
session is one source transcript; a workspace groups all sessions sharing a
project, across tools.

The headline kWh/CO₂, Trend `energy`/`CO₂e` and the per-tool/per-model rows
**include the prefill/context term** (below); the Trend `Wh/1k` and the verdict's
frugality stay **decode-only**, which is why they look unchanged while the energy
columns are several times larger.

The **Trend** section buckets the period by **day** (≤ ~1 month), **week**
(≤ ~6 months) or **month** (longer) — granularity follows `--period` — and
shows the **period-over-period change (Δ %)** for both consumption (energy) and
**frugality** (Wh per 1000 output tokens). Green = down/better, red = a sharp
increase, so you can see whether you're consuming more and whether your model
mix is getting lighter or heavier. A short **Analysis** then spells out the
trajectory in plain language (electricity, CO₂, frugality), comparing the first
half of the period to the second. When a swing is larger than ~5×, the baseline
is too small for a percentage to mean anything (e.g. an adoption ramp over
`--period all`), so the wording becomes descriptive — "rose/dropped sharply" in
the Analysis, "ramping up"/"winding down" on the badge — instead of a misleading
number like "+99041%".

The per-model span is the **measurable** period — the union of the data spans
of every tool that carries that model (e.g. a model used in both opencode and
Claude Code spans the union of both), since that's how far back its usage could
be observed.

The badge headline carries a mascot animal for the footprint weight and a trend
arrow (↘ shrinking / → stable / ↗ growing, first half vs second half of the
period). The animal grades your **frugality** — Wh per 1k output tokens, weighted
across your whole model mix — so it's comparable across users regardless of volume:

| Wh / 1k output | verdict | typical models |
|---|---|---|
| < 1   | 🐜 very light  | haiku, gpt-4o-mini |
| < 2.5 | 🦥 frugal      | sonnet, gpt-4o |
| < 4   | 🦊 moderate    | light mixes |
| < 10  | 🐘 heavy       | current frontier: opus-4-7/4-8, gpt-5.x (~5–6) |
| ≥ 10  | 🦣 very heavy  | legacy dense giants: opus-4-1, gemini-2.5-pro (~25) |

The thresholds are anchored to EcoLogits' active-parameter estimates: a
mostly-Opus diet reads **heavy**, and "very heavy" is the old dense-600B-class
tier. Because closed-model parameter counts are *estimated*, the exact band can
shift as EcoLogits updates its database. (The verdict uses **decode-only**
energy — energy per generated token — so it grades your model choice, not how
much context you feed; the headline kWh/CO₂ figure does include the context.)

#### Prefill / context energy

EcoLogits' published formula bills energy from **output tokens only** — it
models the decode phase, which is fine for chat (output ≈ input) but badly
undercounts agentic/cache-heavy use, where each generated token rides on orders
of magnitude more context (for Claude Code, cache reads alone are often 95 %+ of
all token traffic). tokstat adds an approximate **prefill term**: fresh input +
cache writes, and cache reads, each counted at a fraction of a decode token's
energy. The fractions are grounded in transformer physics — prefill does the
same ~2·N_active FLOPs per token as decode but at far higher hardware
utilization (≈ 0.03–0.12×), and a cache-read token skips the FFN recompute
entirely (≈ 0.0005–0.006×). These are deliberately wide ranges that widen the
± band rather than feign precision; override them in `impact.json` if you have
better numbers. Typically this lifts the headline ~2–4× versus decode-only.

> **⚠️ Order-of-magnitude estimate, usage phase only.** Energy is derived from
> token counts × the model's (estimated) active parameters — output tokens at
> the decode rate, plus input/cache at the reduced prefill rates above. For
> closed models like Claude/GPT, EcoLogits *estimates* the parameter count,
> hence the min–max range. It excludes hardware manufacturing (the embodied
> phase needs per-request GPU data tokstat doesn't have). Models absent from the
> EcoLogits database are excluded and reported.

Choose the electricity mix by passing a region to `--impact` (default `world`):

```sh
tokstat --impact eu
tokstat --impact france --period "30 days"
```

Presets: `world` (default, 0.418), `eu` (0.250), `france` (0.056), `us` (0.369),
`green` (0.040) kgCO₂e/kWh — or pass an explicit factor (`--impact 0.3`). To make
it permanent, set it in `~/.config/tokstat/impact.json`:

```json
{ "region": "france", "pue": 1.2,
  "prefill_factor": [0.03, 0.12], "cache_read_factor": [0.0005, 0.006] }
```

`prefill_factor` and `cache_read_factor` override the prefill/cache energy
multipliers (each a scalar or a `[lo, hi]` range); omit them to keep the
defaults above.

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

Exports all exchanges to a JSON file. Each entry carries its `session` (the
source transcript id), `cwd` (the working directory the session ran in) and
`worktree` (the git main worktree that `cwd` resolves to — same as `cwd` unless
the session ran in a linked worktree). For **Claude Code and Codex** it also
carries `context_tokens` (the peak context size the exchange reached) and
`compactions` (each compaction event, with `pre_tokens` → `post_tokens`; Claude
Code also gives `trigger` auto/manual and duration, Codex leaves those `null`);
these same signals appear as the **Context** and **Compaction** columns in
`--prompts`.

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
  "duration_s": 182.4,
  "context_tokens": 999900,
  "compactions": [
    {"trigger": "auto", "pre_tokens": 1000000, "post_tokens": 10400,
     "duration_ms": 87092, "timestamp": "2026-04-17T20:21:40.697Z"}
  ],
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

Environmental-impact estimates (`--impact`) port the usage-phase energy formula and constants of [EcoLogits](https://github.com/genai-impact/ecologits) (MPL-2.0) and use its model database, fetched and cached locally.

## License

tokstat is MIT (see [LICENSE](LICENSE)), **except** `src/tokstat/_ecologits.py`, which is licensed under the **MPL-2.0** because it ports MPL-2.0 source from EcoLogits. The MPL-2.0 is a file-level copyleft and governs only that one file; everything else is MIT. See [NOTICE](NOTICE) for details.
