# AGENTS.md — coding on tokstat

Guidance for any AI agent (or human) writing code in this repo. Read it before
touching `src/`. Keep it current: when you change a convention below, update it.

## What tokstat is

A single CLI that scans the **local** on-disk data of ~13 AI coding assistants
(Claude Code, Codex, Cursor, Kiro, Copilot, Copilot CLI, Gemini CLI,
Antigravity, opencode, pi, Qwen Code, plus claude.ai / ChatGPT web exports) and
renders token usage, cost, and carbon as terminal tables. PyPI package
`tokstat`, repo `github.com/thiga-co/tokstat`.

## Hard constraints — do not break these

- **Zero runtime dependencies.** Standard library only. `pyproject.toml` lists
  no `dependencies`. Do not add one — not for HTTP (use `urllib.request`), not
  for JSON, not for tables, not for color. `pytest` is the only extra, under
  `[project.optional-dependencies].test`.
- **Python 3.7+.** No `match`, no `:=` where 3.7 would choke, no `tomllib`,
  no `from __future__`-only features beyond `annotations` (which every module
  already imports). CI/users run old interpreters.
- **Nothing leaves the machine.** The only outbound network calls tokstat makes
  are fetching pricing (cached): the LiteLLM catalog, and the repo's own Ollama
  Cloud price list from GitHub. Both are public, data-only, and carry no user
  data. A scanner must never phone home. Reads are strictly local files.
- **Read-only on the user's real stores.** Scanners open the assistants' data
  files read-only. Never write, move, or delete anything under `~/.claude`,
  `~/.codex`, `~/.kiro`, etc.

## Architecture

```
src/tokstat/
  _core.py          Shared engine: pricing, cost, period parsing, filtering,
                    ANSI colors, and ALL the table renderers (show_overview_tables,
                    show_prompts, show_total, show_impact, show_anomalies, …).
                    ~128KB — the heart. Scanners import from here; they never
                    re-implement rendering.
  <tool>_cli.py     One module per assistant. Two responsibilities:
                      1. scan_<tool>() and friends — turn that tool's local
                         files into tokstat records.
                      2. cli() — a standalone `<tool>-token-usage` command that
                         wires argv → scanner → _core renderers.
  unified_cli.py    The `tokstat` command: imports every scan_* and aggregates
                    across all tools into one overview.
  _ecologits.py     Energy/carbon model (for --impact).
  _web.py           Shared web-export helpers.
  __init__.py       Re-exports __version__ only.
```

Entry points live in `pyproject.toml` under `[project.scripts]`: `tokstat` →
`unified_cli:cli`, and one `<tool>-token-usage` → `<tool>_cli:cli` per tool.

## The scanner contract

Each `<tool>_cli.py` exposes `scan_<tool>() -> list[dict]`. One record per
assistant message that carries usage. The record dict is:

```python
{
    "tool":       "pi",          # stable lowercase tool label (stamped on every record)
    "model":      model,         # display model string, after _label()/normalize
    "project":    project,       # cwd / workspace, or "unknown"
    "ts":         ts,            # a timezone-aware datetime (drop records with no ts)
    "session_id": session_id,    # stable id for the conversation
    "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,   # the four token counts
    "cost":       compute_cost(tokens, model),   # from _core, live pricing
}
```

Rules that matter:

- **Token families differ — this is the #1 source of silent bugs.** For Claude
  Code, `input` is **disjoint** from the cache counts (cache tokens are billed
  separately and not included in input). For pi / codex / qwen / opencode /
  antigravity, the provider's `input` **already includes** cache tokens — do not
  double-count. Get this wrong and cost is off by a large factor. When wiring a
  new tool, verify empirically which family it is (see Tests) — never assume.
- **Activity-only tools.** Kiro meters in opaque *credits* and GitHub Copilot
  Chat exposes no counts. Their scanners emit records with `output == 0` on
  purpose; tokstat reports activity, not invented tokens. Keep it honest — do
  not back-fill estimates for these.
- **Skip, don't crash.** A missing store (`if not BASE.exists(): return []`),
  an unreadable file, or a malformed line is normal on a machine that doesn't
  use that tool. Wrap per-file work in `try/except (OSError, IOError)` and
  continue; never let one bad JSONL line abort the scan.
- Also expose `scan_speed_<tool>()` (tokens/sec, where the tool records
  duration) and the exchange collectors used by `--prompts`/`--tool-use`
  (see how pi wires `_collect_all_exchanges`).
- Register the tool's color once at import: `TOOL_COLORS["<tool>"] = <COLOR>`.

Import everything shared from `_core` (`compute_cost`, `load_pricing`,
`resolve_period`, the `show_*` renderers, colors). Don't reinvent them.

## Adding a new tool

1. Create `src/tokstat/<tool>_cli.py` modeled on the simplest existing one
   (`pi_cli.py` is a good template — single JSONL store).
2. Implement `scan_<tool>()` returning the record dict above, plus `cli()`.
3. Register it in `unified_cli.py` (import `scan_<tool>` and add it to the
   aggregation) and add the `<tool>-token-usage` entry point in `pyproject.toml`.
4. Add it to the supported-tools table in `README.md` and `docs/tools.md` with
   its accuracy class (exact / activity-only / estimated).
5. **Wire a live e2e harness** (see below) so the parser can't silently rot.

## Tests — live, empirical, no mocks

`tests/` is a **live end-to-end** suite: a real agent binary writes a session
headless, tokstat reads it back, and the test asserts
`sum(tokstat.output) == the tokens the binary itself recorded` (the fidelity
oracle, computed **independently of tokstat** — from the raw file field or the
CLI's own stdout JSON, never via tokstat's own parser, which would be circular).

- Read `tests/README.md` first — it documents the harness contract, the two
  tiers (fidelity vs activity-only), and the two isolation strategies (redirect
  the store via a flag/env, or capture-and-clean for tools that can't be
  redirected without breaking auth).
- Run free (local Ollama, zero cost):  `pytest -m 'e2e and not billed'`
- Run everything incl. real paid providers: `pytest -m e2e`  (the `billed`
  marker; tiny one-word prompts, artifacts auto-cleaned from the real store)
- Skip live tests entirely:            `pytest -m 'not e2e'`
- A test **skips (never fails)** when its binary/model/provider is absent, so
  the suite is green on any machine.
- **Decide by experiment, not by memory.** Which token family a tool uses,
  whether it persists anything locally, whether it records tokens at all — prove
  it by running the binary and inspecting the bytes it wrote, then encode that
  in a harness. Several "obvious" verdicts here were overturned by a probe
  (opencode writes exact tokens; cursor-agent persists nothing locally).

## Conventions

- **Version lives in two places** and must match: `src/tokstat/cli.py`
  (`__version__ = "X.Y.Z"`) and `pyproject.toml` (`version`). Bump both in the
  same change.
- **Commit messages** follow `type(scope): summary — X.Y.Z`, e.g.
  `feat: add Qwen Code support (qwen-token-usage) — 1.30.0`. One feature per
  version bump.
- Style: 4-space indent, module docstring with `SPDX-License-Identifier`, ANSI
  color via the `_core` constants (`BOLD`, `DIM`, `GREEN`, …), f-strings, type
  hints with `from __future__ import annotations`.
- Keep user data private: never log or transmit a path, project name, or prompt
  content outside the local terminal.

## Environment gotchas

- A command classifier in this environment blocks `curl | bash` installers and
  `--dangerously-skip-permissions`. The tests don't need either; if you need an
  agent installed or a permissioned run, ask the user to run it themselves.
- `rtk` wraps common dev commands transparently (token-saving proxy); ignore it
  for correctness — it doesn't change behavior.
