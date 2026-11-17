"""Tests for Ollama Cloud on-demand pricing.

Pure, offline unit tests: they inject a fixed price table (never hitting the
network) and assert that a model string carrying an Ollama Cloud marker is
priced from it, with the peak / off-peak rate chosen by the record timestamp.
One test exercises the HTML table parser directly against a markup snippet.
"""
from datetime import datetime, timezone

import pytest

import json

from tokstat import _core
from tokstat._core import (
    match_model, compute_cost, _is_offpeak, _apply_ollama_pricing,
    parse_ollama_pricing_page, OLLAMA_BUNDLED_PATH,
)


# Monday 2026-11-16 14:00 UTC → peak (weekday, 12:00–18:00 UTC).
PEAK = datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)
# Monday 2026-11-16 20:00 UTC → off-peak (weekday, outside the window).
OFF = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)
# Sunday 2026-11-15 14:00 UTC → off-peak (weekend, all day).
SUN = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)

# Values in USD per *million* tokens, as the pricing table stores them.
_FIXTURE = {
    "deepseek-v4.1-flash": {"peak": [0.30, 0.006, 1.20], "offpeak": [0.15, 0.003, 0.60]},
    "kimi-k2.7-code":      {"peak": [0.95, 0.19, 4.00], "offpeak": None},
    "gpt-oss:20b":         {"peak": [0.07, 0.035, 0.30], "offpeak": None},
    "clef":                {"peak": [0.24, None, None], "offpeak": None},
}


@pytest.fixture(autouse=True)
def _inject_prices(monkeypatch):
    monkeypatch.setattr(_core, "OLLAMA_PRICING", dict(_FIXTURE))


def test_is_offpeak_window():
    assert _is_offpeak(PEAK) is False
    assert _is_offpeak(OFF) is True
    assert _is_offpeak(SUN) is True
    assert _is_offpeak(None) is False      # unknown ts → treat as peak


def test_deepseek_cloud_peak_vs_offpeak():
    peak = match_model("deepseek-v4.1-flash:cloud", PEAK)
    off = match_model("deepseek-v4.1-flash:cloud", OFF)
    assert round(peak["input"] * 1e6, 3) == 0.300
    assert round(peak["output"] * 1e6, 3) == 1.200
    assert round(off["input"] * 1e6, 3) == 0.150
    assert round(off["output"] * 1e6, 3) == 0.600


def test_reasoning_suffix_and_provider_prefix_are_stripped():
    # Codex stamps "[high]"; opencode stamps an "ollama/" prefix.
    a = match_model("deepseek-v4.1-flash:cloud [high]", PEAK)
    b = match_model("ollama/deepseek-v4.1-flash:cloud", PEAK)
    assert round(a["input"] * 1e6, 3) == 0.300
    assert round(b["input"] * 1e6, 3) == 0.300


def test_bare_cloud_only_name_is_priced():
    # Claude Code records the model without the ":cloud" marker; a cloud-only
    # model name (no free local weights) must still be priced.
    p = match_model("deepseek-v4.1-flash", PEAK)
    assert round(p["output"] * 1e6, 3) == 1.200


def test_free_tier_is_zero():
    p = match_model("deepseek-v4.1-flash-free", PEAK)
    assert p["input"] == 0 and p["output"] == 0


def test_local_also_model_needs_explicit_marker():
    # gpt-oss also runs locally for free: without a marker it stays $0,
    # with the cloud marker it is billed.
    assert match_model("gpt-oss:20b", PEAK)["output"] == 0
    assert round(match_model("gpt-oss:20b-cloud", PEAK)["output"] * 1e6, 3) == 0.300


def test_cache_read_uses_cached_rate():
    tk = {"input": 0, "output": 0, "cache_read": 1_000_000, "cache_write": 0}
    assert round(compute_cost(tk, "deepseek-v4.1-flash:cloud", PEAK), 4) == 0.006


def test_missing_cached_rate_falls_back_to_input():
    # clef lists only an input price; cache reads bill at the input rate.
    p = match_model("clef:cloud", PEAK)
    assert round(p["cache_read"] * 1e6, 3) == 0.240
    assert p["output"] == 0


def test_non_ollama_model_unaffected_by_ts():
    assert match_model("claude-opus-4-8", PEAK) == match_model("claude-opus-4-8", OFF)


def test_parse_ollama_pricing_page():
    html = """
    <table><thead><tr>
      <th>Model</th><th>Input</th><th>Cached input</th><th>Output</th>
    </tr></thead><tbody>
      <tr><td><a href="/library/deepseek-v4.1-flash">deepseek-v4.1-flash</a></td>
          <td>$0.30</td><td>$0.006</td><td>$1.20</td></tr>
      <tr><td><a href="/library/deepseek-v4.1-flash">deepseek-v4.1-flash (Off-Peak)</a></td>
          <td>$0.15</td><td>$0.003</td><td>$0.60</td></tr>
      <tr><td>clef</td><td>$0.24</td><td>-</td><td>-</td></tr>
    </tbody></table>
    """
    d = parse_ollama_pricing_page(html)
    assert d["deepseek-v4.1-flash"]["peak"] == [0.30, 0.006, 1.20]
    assert d["deepseek-v4.1-flash"]["offpeak"] == [0.15, 0.003, 0.60]
    assert d["clef"]["peak"] == [0.24, None, None]
    assert d["clef"]["offpeak"] is None


def test_bundled_price_list_loads_and_prices(monkeypatch):
    # The JSON shipped in the package must parse and price a known model, so an
    # offline install still shows on-demand cost (the final fallback).
    doc = json.loads(OLLAMA_BUNDLED_PATH.read_text())
    monkeypatch.setattr(_core, "OLLAMA_PRICING", {})
    n = _apply_ollama_pricing(doc)
    assert n >= 15
    p = match_model("deepseek-v4.1-flash:cloud", PEAK)
    assert p["input"] > 0 and p["output"] > 0


def test_price_list_document_shape():
    doc = json.loads(OLLAMA_BUNDLED_PATH.read_text())
    assert set(doc["offpeak_utc_hours"]) and len(doc["offpeak_utc_hours"]) == 2
    assert isinstance(doc["local_also"], list)
    for name, row in doc["models"].items():
        assert isinstance(row["peak"], list) and len(row["peak"]) == 3, name
        assert row["offpeak"] is None or len(row["offpeak"]) == 3, name
