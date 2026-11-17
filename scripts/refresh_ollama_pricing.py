#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regenerate the versioned Ollama Cloud price list from the live pricing page.

tokstat reads on-demand Ollama Cloud rates from
``src/tokstat/data/ollama_cloud_prices.json`` (versioned in the repo, refreshed
at runtime from GitHub like the LiteLLM list). This script rebuilds that file by
scraping https://ollama.com/pricing. Run it when Ollama changes its published
prices, review the diff, and commit the result.

    python scripts/refresh_ollama_pricing.py          # write the file
    python scripts/refresh_ollama_pricing.py --check   # fail if it would change

Keeps the file's hand-maintained metadata (local_also, offpeak_utc_hours) and
only refreshes the scraped model rates.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import urllib.request
from collections import OrderedDict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from tokstat._core import parse_ollama_pricing_page  # noqa: E402

PAGE_URL = "https://ollama.com/pricing"
OUT_PATH = _ROOT / "src" / "tokstat" / "data" / "ollama_cloud_prices.json"

# Preserved across refreshes (the page does not express these).
DEFAULT_LOCAL_ALSO = ["gpt-oss:20b", "gpt-oss:120b", "gemma4"]
DEFAULT_OFFPEAK_HOURS = [12, 18]

_COMMENT = (
    "Ollama Cloud on-demand pricing for tokstat. USD per MILLION tokens as "
    "[input, cached_input, output]; a value may be null when the page omits it. "
    "'offpeak' is null when the model has no off-peak tier. Regenerate with "
    "scripts/refresh_ollama_pricing.py."
)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "tokstat/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def build() -> OrderedDict:
    models = parse_ollama_pricing_page(_fetch(PAGE_URL))
    if not models:
        raise SystemExit("error: parsed 0 models — the pricing page layout may have changed")

    prev = {}
    if OUT_PATH.exists():
        try:
            prev = json.loads(OUT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            prev = {}

    doc = OrderedDict()
    doc["_comment"] = _COMMENT
    doc["source"] = PAGE_URL
    doc["updated"] = _dt.date.today().isoformat()
    doc["offpeak_utc_hours"] = prev.get("offpeak_utc_hours", DEFAULT_OFFPEAK_HOURS)
    doc["local_also"] = prev.get("local_also", DEFAULT_LOCAL_ALSO)
    doc["models"] = OrderedDict(sorted(models.items()))
    return doc


def _serialize(doc) -> str:
    return json.dumps(doc, indent=2) + "\n"


def main(argv) -> int:
    check = "--check" in argv
    doc = build()
    new_text = _serialize(doc)
    if check:
        old_text = OUT_PATH.read_text() if OUT_PATH.exists() else ""
        # Ignore the volatile "updated" date when comparing rates.
        def _strip_date(t):
            try:
                d = json.loads(t)
                d.pop("updated", None)
                return json.dumps(d, sort_keys=True)
            except json.JSONDecodeError:
                return t
        if _strip_date(old_text) != _strip_date(new_text):
            print(f"out of date: {OUT_PATH} differs from the live page")
            return 1
        print("up to date")
        return 0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(new_text)
    print(f"wrote {len(doc['models'])} models to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
