# tokstat

CLI toolkit to aggregate and analyze AI coding assistant token consumption. Each tool scans local data, estimates costs using live [LiteLLM](https://github.com/BerriAI/litellm) pricing, and prints color-coded terminal tables.

> On our test account, Tokstat’s estimation of Claude Code usage matched Anthropic billing with approximately 95% accuracy over 30 days. Accuracy varies by tool — Claude Code, Codex, Gemini CLI and opencode read exact token counts; Cursor reads exact counts where they're recorded locally and flags the rest as `⚠ no data`; Kiro exposes no token counts at all (activity only); the web exports are estimated from text length. Tokstat provides estimates only, and we disclaim any responsibility or liability for differences between estimated and actual billing.

## Changelog

- **unreleased** — `--audit` *(experimental)*: conversation-quality audit across all tools, scoring 12 behavioural metrics with an LLM-as-judge. You pick the judge explicitly (at least one, else it errors): **`--ollama-judge`** (LOCAL, nothing leaves the machine; `--model` picks it), **`--claude-judge`** and **`--codex-judge`** (via that CLI, off-machine, opt-in + warned). Each is an autonomous judge; combine them for a **voting panel** (findings aggregated by vote, tally shown). Judge sees the assistant's own prose + real tool-output snippets; a skeptical **`--verify`** second pass (each finding re-checked by the same judge that raised it) cuts false positives (with a dedicated contradiction check that makes the model name both incompatible statements); each finding shows the turn, quote, user request and real excerpt for one-glance verification. Plus a portable snapshot: `--dump` captures everything tokstat analyses (incl. tool outputs) and `--load` replays any mode offline; `--bench` measures judge model prefill/decode speed. Treat findings as leads, not verdicts — reliability scales with model strength.
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
```

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

### `--audit` — conversation quality audit *(experimental)*

Scans your local transcripts for behavioural/quality issues in the assistant's
messages, **across every supported tool** (Claude Code, Codex, Cursor, Kiro,
Gemini, opencode, and imported web exports). All 12 metrics are evaluated by an
**LLM-as-judge**, and you **choose the judge** explicitly — there is no implicit
default, so `--audit` with no judge selected errors out. Pick at least one of:

- **`--ollama-judge`** — judge **locally** via [Ollama](https://ollama.com):
  nothing leaves your machine, no API cost. `--model` picks the model(s).
- **`--claude-judge`** — judge via the **Claude CLI** (`claude -p`).
- **`--codex-judge`** — judge via the **Codex CLI** (`codex exec`).

Each selected judge is **autonomous**: it runs the full audit and produces its
own findings (the CLI judges are *not* arbiters of Ollama's output). Select
several and they form a **voting panel** — a finding flagged by more judges ranks
higher and shows its tally (e.g. `2/3`).

```sh
tokstat --audit --ollama-judge                       # local judge, auto-picked model
tokstat --audit --ollama-judge --model gemma4:31b    # local, a larger/better model
tokstat --audit --claude-judge                       # judge via Claude (off-machine)
tokstat --audit --codex-judge                        # judge via Codex  (off-machine)
tokstat --audit --ollama-judge --claude-judge --codex-judge   # 3-way voting panel
tokstat --audit --ollama-judge --model a,b,c         # multi-model local panel
tokstat --audit --ollama-judge --judge-max 20        # cap conversations judged
```

**Live trace (stdout).** Both passes are traced as they run:

- **Pass 1 (detection)** — each conversation prints its result inline:
  `[3/50] Claude Code · PRO/fdj · 2026-07-22 · 8 turns → 2 findings: contradiction, unsupported_claim` (or `→ clean`).
- **Pass 2 (`--verify`)** — one line per finding with the verdict and the
  verifier's reason: `→ CONFIRMED`, `→ DROPPED`, or `→ AFFINÉ` (real defect but
  the verifier corrected its metric/wording), e.g.
  `[5/12] contradiction (gemma4:e4b) → DROPPED: the two statements are about different subjects`.
  It ends with `Verify: N confirmed, M refined, K dropped (kept …)`.

A **Recap matrix** closes the report: **one row per conversation** (all of them,
no truncation), one column **per judge in `--model` order** (then Claude, Codex),
numbered in a legend. Each cell holds the **final** finding **codes** that judge
kept for that conversation (short 3-letter metric codes, e.g. `CTR`, `UNS`,
`OVR`); only findings that survived `--verify` are shown — the final status,
nothing else. A `·` means that judge kept nothing. Rows are ordered findings-first
(fully-clean conversations last), and a summary line gives the totals. Lets you
compare, per conversation, what every model concluded.

| metric | |
|---|---|
| `hallucination` | fait inventé |
| `unsupported_claim` | affirmation sans preuve suffisante |
| `overconfidence` | certitude disproportionnée |
| `contradiction` | contradiction avec le transcript |
| `memory_fabrication` | souvenir inventé |
| `sycophancy` | validation abusive de l'utilisateur |
| `gaslighting` | réécriture / négation de l'historique |
| `blame_shifting` | erreur attribuée à l'utilisateur |
| `intent_misalignment` | réponse éloignée du besoin |
| `constraint_violation` | contrainte utilisateur ignorée |
| `tool_misuse` | mauvais appel ou interprétation d'outil |
| `manipulative_behavior` | pression, culpabilisation, dépendance |

The judge uses an evidence-first rubric (every finding must quote the offending
text) and — importantly — is fed **only the assistant's own prose** (quotes,
code and cited material stripped) plus, per turn, the tools it ran **and a
snippet of what each returned** (`tools returned: …`, head+tail up to ~1 KB from
the transcripts on disk), so it doesn't blame the assistant for content it was
merely quoting, and can *verify* tool-backed claims (a git log, an AWS table)
instead of flagging them unsupported. It also reads the **user's
reactions** as a signal: when you correct or push back on an answer ("no, that's
wrong", "you already said that", "it still doesn't work"), that's flagged as
strong evidence of a defect in the *preceding* assistant turn — but the finding
always quotes the assistant, never the user (the user is not audited). An earlier deterministic
(regex/heuristic) tier was dropped: on real data it mostly surfaced noise, and
the judge covers the same ground with far better precision.

The local judge (`--ollama-judge`) requires Ollama running
(`http://localhost:11434`, override with `OLLAMA_HOST`) with at least one instruct
model installed; a fast model is auto-picked for triage, pass `--model` for a
larger, higher-quality one (e.g. a 27–35B instruct model). The CLI judges
(`--claude-judge` / `--codex-judge`) require that CLI on your `PATH` and logged
in. By default **every** conversation in the period is judged; because judging is
slow (and there can be hundreds across all tools) — and the CLI judges cost API
usage — use `--judge-max` to cap it, and `--period` / `--tool` to narrow scope.

**Verify pass** (`--verify`). Small judges over-flag — they call a clarification,
a restatement or a mere observation a "contradiction". `--verify` re-checks each
finding with a narrow, skeptical second prompt ("is this GENUINELY that defect?
default no") and drops the ones it can't confirm. Verifying is far easier than
detecting, so this cuts false positives even with the same model (one extra call
per finding). Each finding is re-checked by the **same judge (engine + model)
that raised it** — a small model verifies its own over-flags, and a Claude/Codex
finding is re-checked by Claude/Codex — so a weak verifier never drops a strong
judge's legitimate finding. Findings that survive show the verifier's reason.

The verifier distinguishes a **proactive** self-correction (the assistant caught
its own error on its own initiative — not a defect) from a claim it retracted
**only after the user pushed back** ("es-tu sûr ?", "c'est faux"): the latter
means the original confident/factual claim *was* a defect (overconfidence /
unsupported_claim), so it is kept, not excused by the later apology. (A strong
judge applies this reliably; a ~4B model often cannot — one more reason it's
triage only.)

`contradiction` is where small judges misfire most (they call a phased timeline
or a restatement "contradictory"), so it gets a sharper check: the verifier must
**extract the two supposedly-incompatible statements verbatim**. Forcing it to
show both sides flips its verdict to *not a contradiction* when there aren't
really two conflicting claims — e.g. a `J+6 / J+8 / J+12` milestone plan is kept
by a plain yes/no verify but correctly dropped once the same small model is made
to name the two statements, while a genuine "uses PostgreSQL" / "uses MySQL"
conflict is kept.

**Off-machine judges** (`--claude-judge`, `--codex-judge`). The local models are
the quality ceiling — small ones over-flag and miss subtle issues. The CLI judges
are much stronger. They run the **same audit** autonomously (Claude via
`claude -p`, Codex via `codex exec`) and, when combined with `--ollama-judge`,
vote alongside it.

> ⚠ Unlike `--ollama-judge`, these **send transcript excerpts to the Claude /
> Codex API** — the part that leaves the machine. Opt-in, with a warning before
> running. `--claude-model` / `--codex-model <name>` pick the model (default:
> your configured one). Claude costs roughly a few cents to ~$0.15 per
> conversation and is reported at the end; Codex under a ChatGPT plan has no
> per-call cost. Pair with `--judge-max`.

**Frontier consensus** (`--frontier-consensus`). When two or more **frontier**
judges are active (currently `--claude-judge` and `--codex-judge`; more can be
added), they **deliberate** to agree on which findings are real. For every
finding at least one frontier judge raised: **round 1** each frontier judge rules
on it blind; **round 2** each one revises after seeing the peers' verdicts and
reasons. If the whole panel *fully flips* in a round (everyone swaps their call —
a sign they're deferring to each other rather than converging), it runs **another
debate round**, and keeps going until a round stabilises (someone holds their
ground) or a cap of 4 rounds is hit. A finding still swapping at the cap is marked
**unstable** and is *not* kept. Otherwise the finding is kept in the consensus if
a **majority** of the frontier panel calls it real in the final round. The result is a synthetic **`frontier-consensus`**
judge — its own column in the recap matrix and a deliberation log per finding
(`Claude=✓, Codex=✓ → CONSENSUS ✓`). This is the highest-confidence verdict the
tool produces: agreement between strong, independent judges *after* they've
argued it out. Needs ≥2 frontier judges (otherwise skipped).

Add `--consensus-log` to print the **full deliberation** instead of the one-line
summary: for each candidate finding, the header + evidence, then round 1 (each
frontier's blind verdict and reason) and every debate round (each one's verdict
and reason after seeing the peers), then the outcome. Useful to watch judges
actually change their minds — and to see when the panel keeps flipping.
`--consensus-log` implies `--frontier-consensus`.

**Voting panel.** Select several judges (any mix of `--ollama-judge` models and
the CLI judges) and each conversation is judged by every one; findings are
**aggregated by vote** — a finding flagged by more judges ranks higher and shows
its tally (e.g. `2/3`), so consensus stands out and lone calls are visibly weak.
A mixed panel is the strongest signal: in a real run a 4B local judge flagged a
`contradiction` that both a Claude and a Codex judge declined (`1/3` — a visible
false positive), while a claim about purged/irrecoverable data asserted as fact
was caught by the stronger judges. If a judge errors at runtime the error is
reported per judge rather than silently counted as "clean".

> Findings are **leads to review, not verdicts** — a small local model misses
> things and can misjudge; factual hallucination in particular needs external
> ground truth beyond the transcript. Conversations are grouped per
> (tool, project, day).
>
> It's tuned for **coding sessions**, where tool calls (grep/read/bash) verify
> the assistant's claims. In document/analysis/consulting work there are no such
> tools and the source material is stripped from the judge's view, so
> `unsupported_claim` is a weaker signal there — the judge is told not to flag
> summaries of provided material or claims transparently attributed to a source,
> but treat those findings with extra caution.

Local judges are weaker than frontier models — treat judge findings as leads to
review, not verdicts, and prefer a larger model (e.g. a 27–35B instruct model)
for better precision. A big **panel of tiny models does not help** — small
models share the same blind spots, so voting amplifies their errors instead of
cancelling them; use 1–2 capable models, not many weak ones.

Each finding shows the offending turn, the judge's quote, the user's request,
and the real assistant excerpt — enough to confirm or dismiss it at a glance.
Captured tool outputs come from Claude Code and Codex transcripts (the tools
with recorded results); other tools are audited on text alone.

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

`--export` is a human-readable slice (text + basic metadata) — it does **not**
include tokens, cost or per-tool internals. For a complete, replayable snapshot,
use `--dump`.

### `--dump` / `--load` — portable snapshot (run offline)

`--dump` captures **everything tokstat's analyses use** — token records,
output-speed records and full per-prompt exchanges (text, tools, tokens, cost) —
into one JSON file. `--load` then runs **any** mode from that snapshot **without
reading the agents' data**, so tokstat works standalone (e.g. on another machine,
or to analyze someone else's dump).

```sh
tokstat --dump snapshot.json                 # capture full history, all tools
tokstat --load snapshot.json --total         # replay any mode offline
tokstat --load snapshot.json --plan --period "30 days"
tokstat --load snapshot.json --audit --judge-max 10   # judge still runs locally
```

`--load` is a modifier: combine it with any mode/`--period`/`--tool`. Everything
except the audit judge (which still calls your local Ollama) then comes purely
from the file — no `~/.claude`, `~/.codex`, etc. access. Scope the capture with
`--tool` if you only want one agent in the snapshot.

## Filters

All modes support `--period`:

```sh
--period <period>    all, today, yesterday, year, or any "N unit" —
                     e.g. "12 hours", "5 days", "31 days", "2 weeks", "3 months".
                     Unquoted works too (--period 31 days). default: today
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
