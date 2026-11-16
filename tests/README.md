# tokstat tests

Live **end-to-end** tests: a real harness binary writes a session, then tokstat
reads it back and must reproduce the exact token totals the harness recorded.

Nothing is mocked. Each test:

1. **Isolates** the harness — its session data lands in a throw-away temp
   directory (via the harness's own flag, or a redirected `HOME`/`CODEX_HOME`),
   **never your real `~/.pi`, `~/.codex`, `~/.qwen`, …**
2. **Runs the real binary** one-shot against a **local Ollama model** — so the
   suite costs nothing and needs no API key.
3. **Redirects tokstat** at the isolated store and runs the scanner.
4. Asserts tokstat saw the session, stamped the right tool/model, and that
   `sum(record.output) == ` the output tokens the harness itself wrote
   (the fidelity oracle, computed independently of tokstat).

## Running

```sh
python -m venv .venv && .venv/bin/pip install -e '.[test]'

.venv/bin/python -m pytest              # everything
.venv/bin/python -m pytest -m e2e       # only the live harness tests
.venv/bin/python -m pytest -m 'not e2e' # skip the live tests
.venv/bin/python -m pytest -rs          # show skip reasons
```

A test **skips** (never fails) when its binary isn't installed, no local Ollama
model is pulled, or a provider precondition is missing — so the suite is green on
any machine and only exercises what's actually available.

Prerequisites for the free layer: [Ollama](https://ollama.com) running with a
small instruct model pulled (e.g. `ollama pull llama3.1:8b`), and the harness
binary on `PATH`.

## Free vs billed

Harnesses that can run against a **local Ollama model** cost nothing. The rest
are run with the user's **own local config and account**, so they hit a real
paid provider — these carry the `billed` marker and are kept to a minimal
one-word prompt. Billed harnesses do **not** redirect `HOME` (that would break
the real auth): they run with real auth, then the just-written session is
captured by a before/after diff, copied into an isolated store for tokstat to
scan, and the original test artifact is deleted from the real store so it never
pollutes the user's own stats.

```sh
pytest -m 'e2e and not billed'   # free, local-only
pytest -m billed                 # real account, real (tiny) billing
```

## Coverage matrix

Eight harnesses are **wired** in `HARNESSES`. Two tiers:

- **Token fidelity** — the binary records exact token counts; the test asserts
  `sum(tokstat output) == the recorded total` (an oracle read independently of
  tokstat). This is the real parsing-regression guard.
- **Activity only** — the binary records no tokens; the test asserts only that
  tokstat read & typed the session (output is expected to be 0).

| Harness | Tier | Cost | How |
|---|---|---|---|
| **pi** | fidelity | free | `pi -p --session-dir <tmp> --model ollama/<m>`; isolated by the flag |
| **codex** | fidelity | free | `codex exec --oss --local-provider ollama`; isolated by `CODEX_HOME` |
| **Qwen Code** | fidelity | free | `qwen --auth-type openai --openai-base-url …:11434/v1`; isolated by `HOME` |
| **opencode** | fidelity | free | `opencode run -m ollama/<m> --format json`; isolated by `XDG_DATA_HOME` (config kept real so the local provider resolves). Tokens are plain JSON in `message.data`/`session_message.data` → oracle reads them straight. **Skips** when the configured local provider is unreachable. |
| **Claude Code** | fidelity | billed | `claude -p`, local account; new transcript captured by before/after diff, copied to an isolated HOME, artifact deleted from the real store |
| **Copilot CLI** | fidelity | billed | `copilot -p`, GitHub account; new session id-diffed, DB snapshotted, scan filtered to it, real-DB rows cleaned up |
| **Antigravity** (`agy`) | fidelity | billed | `agy --output-format json --print="…"` prints `conversation_id` + a `usage` object (**the oracle, from the CLI itself**). tokstat reads exact tokens from `conversations/<cid>.db`; artifact (db + `brain/<cid>/` + summaries row) deleted after |
| **Kiro** (`kiro-cli`) | activity only | billed | `kiro-cli chat --no-interactive`; writes `~/.kiro/sessions/cli/<uuid>.{json,jsonl}`. Kiro meters in opaque **credits**, records no tokens — asserts session read & typed only. New session captured to an isolated store (IDE store neutralized), artifact deleted after |

**Not wireable** (left out of `HARNESSES`, with the reason):

| Harness | Why not |
|---|---|
| Cursor (`cursor-agent`) | the headless CLI (`cursor-agent -p`) persists **nothing locally** — sessions live server-side (verified: a run changed no globalStorage/`~/.cursor`/`~/.local/share` file). tokstat's cursor scanner reads the **IDE** store, which the CLI never writes. No local data to read → no e2e, not even activity. |
| Gemini CLI | its on-disk `tmp/<hash>/logs.json` has no `tokens` and no `chats/session-*.json`; tokstat's gemini scanner reads a format this CLI version no longer writes. Fix the scanner first. |
| VS Code / Copilot **Chat** | tokstat's `copilot` scanner reads VS Code's GUI stores, written only by the **interactive extension** (the `code` CLI runs no headless agent), and records **no tokens**. The scriptable + tokenised counterpart is **Copilot CLI**, covered above. |

All **token-count families** are exercised: Claude (input *disjoint* from cache),
pi/codex/qwen/opencode/antigravity (input *includes* cache) — the per-tool
semantics most prone to silent regression.

## Adding a harness

Add a `Harness` subclass in [`harnesses.py`](harnesses.py) implementing three
methods, then list it in `HARNESSES`:

- `prepare(tmp_path, monkeypatch) -> session_dir` — isolate, run the binary
  live, redirect the tokstat path constant(s), return where the session landed.
  Call `pytest.skip(...)` for any missing precondition.
- `scan() -> list[dict]` — call the tokstat scanner.
- `raw_output_tokens(session_dir) -> int` — the fidelity oracle: read the output
  tokens straight from what the harness wrote, independently of tokstat.

Set `tool_label` when tokstat stamps a different string than `name` (e.g. Codex,
Qwen Code). The parametrized test in `test_e2e_harness.py` does the rest.
