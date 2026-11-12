# `--impact` — energy & CO₂ estimate

[← back to README](../README.md) · [Tools](tools.md) · [Modes](modes.md) · [Filters](filters.md) · [Reference](reference.md)

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

## Prefill / context energy

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

## Electricity mix

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
