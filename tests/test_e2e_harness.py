"""End-to-end: a real harness binary writes a session, tokstat reads it back.

The harness runs live in an isolated directory (never the user's real store),
and the test asserts tokstat not only *sees* the session but reproduces the
exact token totals the harness itself recorded.

Run everything:        pytest
Only these live tests:  pytest -m e2e
Skip the live tests:    pytest -m 'not e2e'
"""
from pathlib import Path

import pytest

from harnesses import HARNESSES


def _param(h):
    marks = [pytest.mark.billed] if getattr(h, "billed", False) else []
    return pytest.param(h, id=h.name, marks=marks)


@pytest.mark.e2e
@pytest.mark.parametrize("harness", [_param(h) for h in HARNESSES])
def test_harness_roundtrip(harness, tmp_path, monkeypatch):
    if not harness.available():
        pytest.skip(f"{harness.binary} not installed")

    # 1. Isolate + run the harness live; it writes a real session, and tokstat
    #    is redirected at the isolated store. Returns where the session landed.
    session_dir = harness.prepare(tmp_path, monkeypatch)
    wrote = [f for f in session_dir.rglob("*") if f.is_file()]
    assert wrote, f"{harness.name} wrote no session file"

    # 2. tokstat scans the redirected location.
    records = harness.scan()

    # 3. tokstat read what the harness wrote.
    assert records, f"tokstat read no records from {harness.name}'s session"
    assert all(r.get("model") for r in records), "every record carries a model"
    assert all(r.get("tool") == harness.label for r in records)
    assert all(r["input"] >= 0 and r["cache_read"] >= 0 for r in records)

    if getattr(harness, "activity_only", False):
        # Activity-only harness: the binary records no tokens, so there is no
        # fidelity oracle — only assert tokstat found & typed the session, and
        # that it honestly reports zero tokens rather than inventing them.
        assert all(r["output"] == 0 for r in records), (
            f"{harness.name} is activity-only but tokstat reported output tokens"
        )
        return

    assert any(r["output"] > 0 for r in records), "some assistant output tokens"

    # 4. Fidelity: tokstat's output total == the harness's own recorded total.
    expected = harness.raw_output_tokens(session_dir)
    got = sum(r["output"] for r in records)
    assert got == expected, (
        f"{harness.name}: tokstat output {got} != harness-written {expected}"
    )
