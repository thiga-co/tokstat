# Reference — pricing, caches, diagnostics

[← back to README](../README.md) · [Tools](tools.md) · [Modes](modes.md) · [Filters](filters.md) · [Impact](impact.md)

## Pricing

Model pricing is fetched from [LiteLLM's model pricing database](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and cached at `~/.cache/token-usage/litellm_prices.json` for 24 hours. Falls back to stale cache if fetch fails.

## Caches

tokstat writes a few regenerable caches under `~/.cache/` — all safe to delete:

- `~/.cache/token-usage/litellm_prices.json` — LiteLLM pricing (24 h TTL).
- `~/.cache/token-usage/ecologits_models.json` — EcoLogits model database (for `--impact`).
- `~/.cache/token-usage/update_check.json` — last seen PyPI version.
- `~/.cache/token-usage/antigravity_index.json` — parsed Antigravity step metadata, keyed by each DB's mtime + size so unchanged databases are not re-decoded on the next run. Contains **numeric aggregates only** (token counts, model names, project paths — no prompts or transcripts), is written **only if Antigravity data is present**, and is pruned to stay under 64 MB. Delete it to force a full re-parse.
- `~/.cache/tokstat/web/` — conversations imported from claude.ai / ChatGPT exports.

## Diagnostics

Set `TOKSTAT_TIMING=1` to add an elapsed-time marker to each progress line, so you can see where a slow run spends its time:

```sh
TOKSTAT_TIMING=1 tokstat
```

```text
[+   0.01s]   Loading pricing from LiteLLM...
    · pricing: cache in 0.01s
[+   0.03s]   3698 models loaded
[+   0.03s]  Token Usage — All tools
[+   0.03s]    Scanning all data sources...
    · Claude Code: 163 record(s) in 0.02s
    · Antigravity: 18262 record(s) in 2.16s
    · Antigravity (speed): 7542 record(s) in 0.01s
    · Antigravity (exchanges): 493 record(s) in 0.67s
    · opencode: 12295 record(s) in 1.91s
[+   5.80s]   ● Antigravity    651 records · ...
```

Off by default — without the variable, output is unchanged. The `pricing:` line reports whether pricing came from the local cache or a network fetch (a slow first line usually means a cold network fetch).
