"""Live harness drivers for the end-to-end tests.

Each :class:`Harness` knows how to drive one real agent binary in a fully
*isolated* one-shot run (its session data lands in a throw-away directory, never
the user's real store), which tokstat scanner reads it back, and how to read the
raw token totals the harness itself wrote. The e2e test then asserts tokstat
reproduces those totals.

Runs use a **local Ollama model** wherever the harness supports one, so the
suite costs nothing and needs no API key. A harness whose binary, local model,
or isolation hook is missing raises :func:`pytest.skip` with a clear reason.

Adding a harness = add a subclass implementing ``prepare`` / ``scan`` /
``raw_output_tokens`` and list it in ``HARNESSES``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Small instruct models, in order of preference. The first one actually pulled
# locally is used. Guard/judge/classifier models are avoided — they do not
# reliably emit plain chat text.
_PREFERRED_MODELS = [
    "llama3.1:8b",
    "qwen3:8b",
    "mistral-nemo:12b",
    "granite4.2:8b",
    "gemma4:12b",
]
_MODEL_DENY = ("guard", "shield", "minicheck", "judge", "lynx", "halluguard")

_PROMPT = "Reply with exactly: OK"
_RUN_TIMEOUT = 300  # cold model load can be slow


def _ollama_models() -> list[str]:
    if not shutil.which("ollama"):
        return []
    try:
        out = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=15
        ).stdout
    except Exception:
        return []
    models = []
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models


def pick_local_model() -> str | None:
    """A small, chat-capable Ollama model pulled locally, or None."""
    have = _ollama_models()
    have_set = set(have)
    for m in _PREFERRED_MODELS:
        if m in have_set:
            return m
    for m in have:
        if not any(d in m.lower() for d in _MODEL_DENY):
            return m
    return None


class Harness:
    """Base class: one agent binary, driven live into an isolated dir."""

    name: str = ""          # test id + default tool label
    binary: str = ""
    tool_label: str = ""    # the "tool" value tokstat stamps on records
    billed: bool = False    # True => runs against a real paid provider (local config)
    activity_only: bool = False  # True => harness records NO tokens; the e2e can
                                 # only assert the session was read & typed, not
                                 # token fidelity (output is expected to be 0)

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    @property
    def label(self) -> str:
        return self.tool_label or self.name

    # --- to implement per harness -------------------------------------------
    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        """Isolate the harness, run it live one-shot, redirect tokstat at the
        isolated store, and return the directory tokstat/raw-reader should read.

        May call ``pytest.skip`` when a precondition (local model, auth) is
        missing. Must raise on an actual run failure.
        """
        raise NotImplementedError

    def scan(self) -> list[dict]:
        """Run the tokstat scanner (reads the now-redirected location)."""
        raise NotImplementedError

    def raw_output_tokens(self, session_dir: Path) -> int:
        """Sum assistant output tokens straight from what the harness wrote,
        independent of tokstat — the fidelity oracle."""
        raise NotImplementedError


class Pi(Harness):
    """Isolation via pi's own ``--session-dir`` flag; no HOME games needed."""

    name = "pi"
    binary = "pi"

    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        model = pick_local_model()
        if model is None:
            pytest.skip("no suitable local Ollama model pulled")
        ready = subprocess.run(
            ["pi", "auth", "check", "--provider", "ollama"],
            capture_output=True, text=True, timeout=30,
        )
        if "ready" not in ready.stdout:
            pytest.skip(f"pi ollama provider not ready: {ready.stdout.strip()}")

        session_dir = tmp_path / "pi-sessions"
        session_dir.mkdir()
        subprocess.run(
            [
                "pi", "-p",
                "--session-dir", str(session_dir),
                "--model", f"ollama/{model}",
                "--no-tools", "--no-mcp", "--thinking", "off",
                "--no-context-files",
                _PROMPT,
            ],
            check=True, capture_output=True, text=True, timeout=_RUN_TIMEOUT,
        )

        from tokstat import pi_cli
        monkeypatch.setattr(pi_cli, "PI_SESSIONS", session_dir)
        return session_dir

    def scan(self) -> list[dict]:
        from tokstat import pi_cli
        return pi_cli.scan_pi()

    def raw_output_tokens(self, session_dir: Path) -> int:
        total = 0
        for f in session_dir.glob("*.jsonl"):
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                msg = rec.get("message")
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    total += int((msg.get("usage") or {}).get("output", 0))
        return total


class Codex(Harness):
    """Isolation via HOME + CODEX_HOME. codex_cli reads ``Path.home()/.codex/
    sessions`` inline, and codex writes to ``$CODEX_HOME/sessions``; pointing both
    at ``<tmp>/.codex`` lines them up. Local model via ``codex exec --oss``.
    """

    name = "codex"
    binary = "codex"
    tool_label = "Codex"

    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        model = pick_local_model()
        if model is None:
            pytest.skip("no suitable local Ollama model pulled")

        home = tmp_path / "home"
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        # HOME drives both the codex subprocess store and tokstat's Path.home().
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        workdir = tmp_path / "work"
        workdir.mkdir()
        proc = subprocess.run(
            [
                "codex", "exec",
                "--oss", "--local-provider", "ollama", "-m", model,
                "--skip-git-repo-check",
                "-C", str(workdir),
                _PROMPT,
            ],
            capture_output=True, text=True, timeout=_RUN_TIMEOUT,
            env=None,  # inherits the monkeypatched environment
        )
        sessions = codex_home / "sessions"
        if proc.returncode != 0 or not sessions.exists():
            pytest.skip(
                "codex exec --oss did not produce a session "
                f"(rc={proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        return sessions

    def scan(self) -> list[dict]:
        from tokstat import codex_cli
        return codex_cli.scan_codex()

    def raw_output_tokens(self, session_dir: Path) -> int:
        """codex records cumulative token_count events; the last one's output
        is the session total. Sum the per-session maxima."""
        total = 0
        for f in session_dir.rglob("*.jsonl"):
            session_max = 0
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                info = _codex_token_info(rec)
                if info is not None:
                    out = int(info.get("output_tokens", 0) or 0)
                    session_max = max(session_max, out)
            total += session_max
        return total


def _codex_token_info(rec: dict):
    """Pull the token_count usage dict out of a codex rollout record, tolerating
    the known nestings (payload.info.total_token_usage / last_token_usage)."""
    payload = rec.get("payload") if isinstance(rec, dict) else None
    node = payload if isinstance(payload, dict) else rec
    info = node.get("info") if isinstance(node, dict) else None
    if isinstance(info, dict):
        for key in ("total_token_usage", "last_token_usage"):
            usage = info.get(key)
            if isinstance(usage, dict):
                return usage
    usage = node.get("total_token_usage") if isinstance(node, dict) else None
    return usage if isinstance(usage, dict) else None


class Qwen(Harness):
    """Qwen Code (a gemini-cli fork). Driven against Ollama's OpenAI-compatible
    endpoint. Isolation via HOME (qwen writes ``$HOME/.qwen/...``) plus patching
    the three module path constants, which were frozen from the real home at
    import time.
    """

    name = "qwen"
    binary = "qwen"
    tool_label = "Qwen Code"

    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        model = pick_local_model()
        if model is None:
            pytest.skip("no suitable local Ollama model pulled")
        if not shutil.which("ollama"):
            pytest.skip("ollama not available for qwen's OpenAI endpoint")

        home = tmp_path / "home"
        qwen_home = home / ".qwen"
        qwen_home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("QWEN_CODE_SUPPRESS_YOLO_WARNING", "1")

        from tokstat import qwen_cli
        monkeypatch.setattr(qwen_cli, "QWEN_PROJECTS", qwen_home / "projects")
        monkeypatch.setattr(qwen_cli, "QWEN_USAGE", qwen_home / "usage")
        monkeypatch.setattr(
            qwen_cli, "QWEN_USAGE_RECORD", qwen_home / "usage_record.jsonl"
        )

        workdir = tmp_path / "work"
        workdir.mkdir()
        proc = subprocess.run(
            [
                "qwen", "-m", model,
                "--auth-type", "openai",
                "--openai-base-url", "http://localhost:11434/v1",
                "--openai-api-key", "ollama",
                "--approval-mode", "yolo",
                _PROMPT,
            ],
            cwd=str(workdir),
            capture_output=True, text=True, timeout=_RUN_TIMEOUT,
        )
        projects = qwen_home / "projects"
        if proc.returncode != 0 or not list(projects.glob("*/chats/*.jsonl")):
            pytest.skip(
                "qwen did not produce a chat transcript "
                f"(rc={proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        return projects

    def scan(self) -> list[dict]:
        from tokstat import qwen_cli
        return qwen_cli.scan_qwen()

    def raw_output_tokens(self, session_dir: Path) -> int:
        total = 0
        for f in session_dir.glob("*/chats/*.jsonl"):
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                meta = rec.get("usageMetadata") if isinstance(rec, dict) else None
                if isinstance(meta, dict):
                    total += int(meta.get("candidatesTokenCount", 0) or 0)
        return total


def _is_assistant_blob(data: str) -> bool:
    try:
        return json.loads(data).get("role") == "assistant"
    except Exception:
        return False


def _snapshot_files(base: Path) -> set:
    return {p for p in base.rglob("*") if p.is_file()} if base.exists() else set()


def _sqlite_backup(src_db: Path, dst_db: Path) -> None:
    """Consistent read-only snapshot of a (possibly WAL-active) SQLite DB."""
    import sqlite3
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dst_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


class Claude(Harness):
    """Claude Code, run with the user's **local config and account** (real
    billing). Auth lives under the real HOME/keychain, so HOME is *not*
    redirected for the run; instead the session Claude writes is captured by
    before/after diff, copied into an isolated HOME for tokstat to scan, and the
    original test artifact is deleted from the real store.
    """

    name = "claude"
    binary = "claude"
    tool_label = "Claude Code"
    billed = True

    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        real_base = Path.home() / ".claude" / "projects"
        workdir = tmp_path / "work"
        workdir.mkdir()

        before = _snapshot_files(real_base)
        proc = subprocess.run(
            ["claude", "-p", _PROMPT],
            cwd=str(workdir),
            capture_output=True, text=True, timeout=_RUN_TIMEOUT,
        )
        after = _snapshot_files(real_base)
        new = sorted(f for f in (after - before) if f.suffix == ".jsonl")
        if proc.returncode != 0 or not new:
            pytest.skip(
                "claude -p produced no transcript "
                f"(rc={proc.returncode}): {proc.stderr.strip()[:300]}"
            )

        # Copy the new transcript(s) into an isolated HOME, preserving layout.
        iso_home = tmp_path / "home"
        iso_base = iso_home / ".claude" / "projects"
        for f in new:
            dst = iso_base / f.relative_to(real_base)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)

        # Remove the test artifact from the real store so it never pollutes the
        # user's own tokstat stats. Only the exact files our run created.
        for f in new:
            try:
                f.unlink()
                parent = f.parent
                if parent != real_base and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

        monkeypatch.setenv("HOME", str(iso_home))
        return iso_base

    def scan(self) -> list[dict]:
        from tokstat import cli
        return cli.scan_claude_code()

    def raw_output_tokens(self, session_dir: Path) -> int:
        total = 0
        for f in session_dir.rglob("*.jsonl"):
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                msg = rec.get("message") if isinstance(rec, dict) else None
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    total += int((msg.get("usage") or {}).get("output_tokens", 0))
        return total


class CopilotCLI(Harness):
    """GitHub Copilot CLI, run with the user's local GitHub account (real
    billing). Its store is a single shared SQLite DB (``~/.copilot/
    session-store.db``) with no relocation env var, so the just-written session
    is identified by an id diff, the DB is snapshotted into an isolated copy for
    tokstat, the scan is filtered to that session, and the session's rows are
    deleted from the real DB afterward.
    """

    name = "copilot-cli"
    binary = "copilot"
    tool_label = "Copilot CLI"
    billed = True

    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        import sqlite3
        real_db = Path.home() / ".copilot" / "session-store.db"
        if not real_db.exists():
            pytest.skip("~/.copilot/session-store.db missing (run copilot once)")

        def _sids() -> set:
            con = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
            try:
                return {r[0] for r in con.execute("SELECT id FROM sessions")}
            finally:
                con.close()

        workdir = tmp_path / "work"
        workdir.mkdir()
        before = _sids()
        proc = subprocess.run(
            ["copilot", "-p", _PROMPT],
            cwd=str(workdir),
            capture_output=True, text=True, timeout=_RUN_TIMEOUT,
        )
        new = _sids() - before
        if proc.returncode != 0 or len(new) != 1:
            pytest.skip(
                "copilot -p did not create exactly one new session "
                f"(rc={proc.returncode}, new={len(new)}): {proc.stderr.strip()[:200]}"
            )
        self._sid = next(iter(new))

        # Snapshot the DB into an isolated copy and point tokstat at it.
        iso_db = tmp_path / "copilot" / "session-store.db"
        iso_db.parent.mkdir(parents=True)
        _sqlite_backup(real_db, iso_db)
        from tokstat import copilotcli_cli
        monkeypatch.setattr(copilotcli_cli, "COPILOT_CLI_DB", iso_db)

        # Clean the test session out of the real DB (best effort, by id).
        self._cleanup_real(real_db, self._sid)
        return iso_db.parent

    @staticmethod
    def _cleanup_real(real_db: Path, sid: str) -> None:
        import sqlite3
        try:
            con = sqlite3.connect(str(real_db), timeout=10)
        except sqlite3.Error:
            return
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tables:
                try:
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info('{t}')")}
                    if "session_id" in cols:
                        con.execute(f"DELETE FROM '{t}' WHERE session_id=?", (sid,))
                except sqlite3.Error:
                    pass
            try:
                con.execute("DELETE FROM sessions WHERE id=?", (sid,))
            except sqlite3.Error:
                pass
            con.commit()
        finally:
            con.close()

    def scan(self) -> list[dict]:
        from tokstat import copilotcli_cli
        records = copilotcli_cli.scan_copilotcli()
        return [r for r in records if r.get("session_id") == self._sid]

    def raw_output_tokens(self, session_dir: Path) -> int:
        import sqlite3
        db = session_dir / "session-store.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(output_tokens),0) FROM assistant_usage_events "
                "WHERE session_id=?", (self._sid,),
            ).fetchone()
            return int(row[0] or 0)
        finally:
            con.close()


class OpenCode(Harness):
    """opencode, run against a local provider (free). Isolation via
    ``XDG_DATA_HOME`` — opencode stores everything under
    ``$XDG_DATA_HOME/opencode`` (verified), so a per-test data dir keeps the run
    out of the user's real ~400 MB ``opencode.db``. The provider config stays in
    the real ``$XDG_CONFIG_HOME`` (``~/.config/opencode``), so the configured
    local models resolve. Tokens live as plain JSON in each ``message`` row, read
    straight for the oracle.
    """

    name = "opencode"
    binary = "opencode"
    tool_label = "opencode"

    @staticmethod
    def _pick_model() -> str | None:
        """First locally-served ``opencode`` model (a non-cloud ``ollama/*`` /
        ``local/*`` id), from ``opencode models``."""
        try:
            out = subprocess.run(
                ["opencode", "models"], capture_output=True, text=True, timeout=30
            ).stdout
        except Exception:
            return None
        for line in out.splitlines():
            m = line.strip()
            if "cloud" in m.lower():
                continue
            if m.startswith("ollama/") or m.startswith("local/"):
                return m
        return None

    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        model = self._pick_model()
        if model is None:
            pytest.skip("no local opencode (ollama/*) model configured")

        data_home = tmp_path / "xdg-data"
        data_home.mkdir()
        env = dict(os.environ, XDG_DATA_HOME=str(data_home))
        workdir = tmp_path / "work"
        workdir.mkdir()
        proc = subprocess.run(
            ["opencode", "run", "-m", model, "--format", "json", _PROMPT],
            cwd=str(workdir), env=env,
            capture_output=True, text=True, timeout=_RUN_TIMEOUT,
        )
        # opencode exits 0 even on a provider error it prints as a JSON event.
        if '"type":"error"' in proc.stdout:
            pytest.skip(
                "opencode provider error (local model unreachable): "
                f"{proc.stdout.strip().splitlines()[-1][:200]}"
            )
        base = data_home / "opencode"
        db = base / "opencode.db"
        if proc.returncode != 0 or not db.exists():
            pytest.skip(
                "opencode run produced no store "
                f"(rc={proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}"
            )
        self._db = db

        from tokstat import opencode_cli
        monkeypatch.setattr(opencode_cli, "_BASE", base)
        monkeypatch.setattr(opencode_cli, "_DB", db)
        monkeypatch.setattr(opencode_cli, "_MSG_BASE", base / "storage" / "message")
        monkeypatch.setattr(opencode_cli, "_SESS_BASE", base / "storage" / "session")
        monkeypatch.setattr(opencode_cli, "_DB_MSG_CACHE", {})
        return base

    def scan(self) -> list[dict]:
        from tokstat import opencode_cli
        return opencode_cli.scan_opencode()

    def raw_output_tokens(self, session_dir: Path) -> int:
        """Sum assistant ``tokens.output`` (+ ``reasoning``) straight from the
        message JSON blobs — matching the scanner's output definition, but read
        independently of its parsing path. Reads whichever schema the store uses:
        the new ``session_message`` table (modern opencode) or the legacy
        ``message`` table."""
        import sqlite3
        con = sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)
        try:
            has_new = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='session_message'").fetchone() is not None
            if has_new and con.execute(
                    "SELECT 1 FROM session_message LIMIT 1").fetchone():
                datas = [d for (d,) in con.execute(
                    "SELECT data FROM session_message WHERE type='assistant'")]
            else:
                datas = [d for (d,) in con.execute("SELECT data FROM message")
                         if _is_assistant_blob(d)]
            total = 0
            for d in datas:
                try:
                    m = json.loads(d)
                except Exception:
                    continue
                tok = m.get("tokens")
                if isinstance(tok, dict):
                    total += int(tok.get("output", 0) or 0) + int(tok.get("reasoning", 0) or 0)
            return total
        finally:
            con.close()


class Antigravity(Harness):
    """Google Antigravity CLI (``agy``), run with the user's local Google account
    (real billing). ``agy --output-format json`` prints the ``conversation_id``
    and a ``usage`` object on stdout — **the oracle, reported by the CLI itself,
    independent of tokstat's blob decoder**. The conversation's SQLite DB is
    captured into an isolated dir for tokstat, and the test artifact (DB + brain
    dir + summaries row) is deleted from the real store afterward.
    """

    name = "antigravity"
    binary = "agy"
    tool_label = "Antigravity"
    billed = True

    # Cheapest/lowest-reasoning model keeps billing minimal and avoids thinking
    # tokens that would split output across fields.
    _MODEL = "gemini-3.8-flash-low"

    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        base = Path.home() / ".gemini" / "antigravity-cli"
        workdir = tmp_path / "work"
        workdir.mkdir()
        proc = subprocess.run(
            ["agy", "--output-format", "json", "--model", self._MODEL,
             f"--print={_PROMPT}"],
            cwd=str(workdir),
            capture_output=True, text=True, timeout=_RUN_TIMEOUT,
        )
        cid = None
        usage: dict = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("conversation_id"):
                cid = obj["conversation_id"]
                usage = obj.get("usage") or {}
        if proc.returncode != 0 or not cid:
            pytest.skip(
                "agy produced no conversation "
                f"(rc={proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        self._cid = cid
        self._oracle = int(usage.get("output_tokens", 0) or 0)

        src_db = base / "conversations" / f"{cid}.db"
        if not src_db.exists():
            pytest.skip(f"agy conversation db missing: {src_db}")

        iso_base = tmp_path / "antigravity"
        iso_conv = iso_base / "conversations"
        iso_conv.mkdir(parents=True)
        _sqlite_backup(src_db, iso_conv / f"{cid}.db")

        from tokstat import antigravity_cli
        monkeypatch.setattr(antigravity_cli, "_BASE", iso_base)
        monkeypatch.setattr(antigravity_cli, "_CONV_DIR", iso_conv)
        monkeypatch.setattr(antigravity_cli, "_BRAIN_DIR", iso_base / "brain")
        monkeypatch.setattr(
            antigravity_cli, "_SUMMARIES_DB", iso_base / "conversation_summaries.db")
        monkeypatch.setattr(
            antigravity_cli, "_INDEX_PATH", tmp_path / "antigravity_index.json")
        monkeypatch.setattr(antigravity_cli, "_DB_CACHE", {})
        monkeypatch.setattr(antigravity_cli, "_DISK_INDEX", {})

        self._cleanup_real(base, cid)
        return iso_conv

    @staticmethod
    def _cleanup_real(base: Path, cid: str) -> None:
        import sqlite3
        try:
            (base / "conversations" / f"{cid}.db").unlink()
        except OSError:
            pass
        shutil.rmtree(base / "brain" / cid, ignore_errors=True)
        sdb = base / "conversation_summaries.db"
        if sdb.exists():
            try:
                con = sqlite3.connect(str(sdb), timeout=10)
                con.execute(
                    "DELETE FROM conversation_summaries WHERE conversation_id=?", (cid,))
                con.commit()
                con.close()
            except sqlite3.Error:
                pass

    def scan(self) -> list[dict]:
        from tokstat import antigravity_cli
        return antigravity_cli.scan_antigravity()

    def raw_output_tokens(self, session_dir: Path) -> int:
        return self._oracle


class Kiro(Harness):
    """Kiro CLI (``kiro-cli``), run with the user's local account (real billing).
    Kiro records **no token counts** (it meters in opaque credits), so this is an
    **activity-only** harness: the e2e asserts tokstat reads the session back and
    types it, not token fidelity. Non-interactive mode writes a flat
    ``~/.kiro/sessions/cli/<uuid>.{json,jsonl}`` pair; the new one is captured
    into an isolated store (with the IDE store neutralized so the scan sees only
    it) and deleted from the real store afterward.
    """

    name = "kiro"
    binary = "kiro-cli"
    tool_label = "Kiro"
    billed = True
    activity_only = True

    def prepare(self, tmp_path: Path, monkeypatch) -> Path:
        real_cli = Path.home() / ".kiro" / "sessions" / "cli"

        def _stems() -> set:
            return ({p.stem for p in real_cli.glob("*.jsonl")}
                    if real_cli.exists() else set())

        workdir = tmp_path / "work"
        workdir.mkdir()
        before = _stems()
        proc = subprocess.run(
            ["kiro-cli", "chat", "--no-interactive", "--trust-tools=", _PROMPT],
            cwd=str(workdir),
            capture_output=True, text=True, timeout=_RUN_TIMEOUT,
        )
        new = _stems() - before
        if proc.returncode != 0 or len(new) != 1:
            pytest.skip(
                "kiro-cli produced no single new session "
                f"(rc={proc.returncode}, new={len(new)}): {proc.stderr.strip()[:200]}"
            )
        stem = next(iter(new))

        iso_sessions = tmp_path / "kiro" / "sessions"
        iso_cli = iso_sessions / "cli"
        iso_cli.mkdir(parents=True)
        for ext in (".json", ".jsonl"):
            src = real_cli / f"{stem}{ext}"
            if src.exists():
                shutil.copy2(src, iso_cli / f"{stem}{ext}")

        from tokstat import kiro_cli
        monkeypatch.setattr(kiro_cli, "_KIRO_CLI_SESSIONS", iso_sessions)
        # Neutralize the IDE store so the scan returns only our captured session.
        monkeypatch.setattr(
            kiro_cli, "_KIRO_BASE", tmp_path / "noexist" / "kiro.kiroagent")

        # Clean the test artifact out of the real store.
        for ext in (".json", ".jsonl"):
            try:
                (real_cli / f"{stem}{ext}").unlink()
            except OSError:
                pass
        return iso_cli

    def scan(self) -> list[dict]:
        from tokstat import kiro_cli
        return kiro_cli.scan_kiro()

    def raw_output_tokens(self, session_dir: Path) -> int:
        return 0  # activity-only: Kiro records no tokens


# Registry. Add a harness here once its live driver is verified.
# Free (local model) harnesses first, then billed (real account) ones.
HARNESSES: list[Harness] = [
    Pi(),
    Codex(),
    Qwen(),
    OpenCode(),
    Claude(),
    CopilotCLI(),
    Antigravity(),
    Kiro(),
]
