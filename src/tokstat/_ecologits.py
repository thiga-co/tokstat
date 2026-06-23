"""Environmental-impact estimation for LLM usage, reusing the EcoLogits
methodology and model database.

We do NOT depend on the `ecologits` library — instead we fetch and cache its
model database (parameter counts per model, including estimates + ranges for
closed models like Anthropic/OpenAI) exactly like the LiteLLM pricing cache,
and port its published usage-phase formula and constants.

Scope: USAGE phase only (the electricity to run inference). The embodied
(hardware-manufacturing) phase is intentionally excluded — it needs per-request
GPU provisioning data tokstat doesn't have. Figures are order-of-magnitude
estimates with a min/max range driven by the model's active-parameter range.

Constants and the usage-phase formula are ported from ecologits/impacts/llm.py
(github.com/genai-impact/ecologits). Because this file incorporates that
MPL-2.0 source, this file alone is licensed under the MPL-2.0 (the rest of
tokstat is MIT). See the NOTICE file.

This Source Code Form is subject to the terms of the Mozilla Public License,
v. 2.0. If a copy of the MPL was not distributed with this file, You can obtain
one at https://mozilla.org/MPL/2.0/.

SPDX-License-Identifier: MPL-2.0
Copyright (c) 2026 Olivier Bergeret
Portions derived from EcoLogits, Copyright (c) GenAI Impact contributors.
"""

from __future__ import annotations

import json
import math
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ECOLOGITS_URL = ("https://raw.githubusercontent.com/genai-impact/ecologits/"
                 "main/ecologits/data/models.json")
ECOLOGITS_CACHE = Path.home() / ".cache" / "token-usage" / "ecologits_models.json"
ECOLOGITS_CACHE_MAX_AGE = timedelta(hours=24)

# ─── EcoLogits constants (ecologits/impacts/llm.py) ────────────────────────
_GPU_ENERGY_ALPHA = 1.1665273170451914e-06
_GPU_ENERGY_BETA  = -0.011205921025579175
_GPU_ENERGY_GAMMA = 4.052928146734005e-05
_LATENCY_ALPHA = 0.0006785088094353663
_LATENCY_BETA  = 0.0003119310311688259
_LATENCY_GAMMA = 0.019473717579473387
_GPU_MEMORY  = 80      # GB per GPU
_SERVER_GPUS = 8
_SERVER_POWER = 1.2    # kW
_BATCH_SIZE = 64
_QUANT_BITS = 16

# Defaults (overridable via config). World electricity mix ≈ EcoLogits value.
DEFAULT_PUE = 1.2
DEFAULT_MIX_GWP = 0.418   # kgCO2eq / kWh (world average)

# ─── Prefill / cache energy, relative to one decode (output) token ─────────
# EcoLogits' formula bills energy from OUTPUT tokens only — it models the
# decode/generation phase. For chat that's fine (output ≈ input), but
# agentic/cache-heavy workloads feed orders of magnitude more context per
# generated token, so decode-only badly undercounts. We add an approximate
# prefill term.
#
# Physics: a transformer spends ~2·N_active FLOPs per token in BOTH prefill
# and decode. The difference is hardware utilization, not FLOPs:
#   • Decode is memory-bandwidth bound — one token at a time, weights reloaded
#     from HBM per step, low utilization. EcoLogits' per-output-token energy
#     already embeds this (expensive) regime.
#   • Prefill processes the whole prompt in parallel at high utilization, so
#     it costs far LESS energy per token despite identical FLOPs. Measured
#     prefill:decode throughput ratios are ~10–30× at comparable power draw,
#     i.e. ~0.03–0.12 of a decode token's energy.
#   • A cache_read token is a KV-cache hit: it skips the FFN/projection
#     recompute entirely (only attention + KV memory movement), so it is far
#     cheaper still — a small fraction of even a prefill token.
# These are deliberately wide ranges (lo→hi) that broaden the uncertainty band
# rather than pretend to precision. Tune in ~/.config/tokstat/impact.json.
PREFILL_FACTOR = (0.03, 0.12)        # input + cache_write tokens, vs a decode token
CACHE_READ_FACTOR = (0.0005, 0.006)  # cache_read tokens, vs a decode token

_DB: dict | None = None   # {model_name: {"active": (min,max), "total": float, "tps":, "ttft":}}


# ─── Database load (fetch + 24h cache, stale fallback) ─────────────────────

def _range(v):
    """Coerce a param value (scalar / {min,max} / {active,total}) to (min,max),
    or (None, None) if it can't be read."""
    if isinstance(v, (int, float)):
        return float(v), float(v)
    if isinstance(v, dict):
        lo = v.get("min", v.get("max"))
        hi = v.get("max", v.get("min"))
        if lo is not None and hi is not None:
            return float(lo), float(hi)
    return None, None


def _parse_params(arch: dict):
    """Return (active_min, active_max, total) in billions, or None.

    Handles every shape EcoLogits' models.json uses:
      • scalar `parameters` (dense)                → active = total = N
      • `parameters` {min,max} (dense range)       → active range, total = max
      • `parameters` {active,total} (MoE inline)   → active from `active`
      • scalar `parameters` + `active_parameters`  → MoE with a separate field
        (e.g. command-a-plus: parameters 218, active_parameters 25)
    """
    if not isinstance(arch, dict):
        return None
    params = arch.get("parameters")
    active_field = arch.get("active_parameters")
    if params is None and active_field is None:
        return None

    total = amin = amax = None
    if isinstance(params, (int, float)):
        total = float(params)
        amin = amax = total
    elif isinstance(params, dict):
        total = params.get("total")
        active = params.get("active")
        if active is not None:
            amin, amax = _range(active)
        elif "min" in params or "max" in params:
            amin, amax = _range(params)        # bare dense {min,max}
        if total is None:
            total = amax if amax is not None else amin

    # An explicit top-level active_parameters overrides the active estimate
    # (current EcoLogits MoE schema); `parameters` then carries the total.
    if active_field is not None:
        amin, amax = _range(active_field)
        if total is None and isinstance(params, (int, float)):
            total = float(params)
        if total is None:
            total = amax

    if amin is None or amax is None or total is None:
        return None
    return (float(amin), float(amax), float(total))


def _build_db(raw: dict) -> dict:
    db: dict = {}
    for m in raw.get("models", []):
        if not isinstance(m, dict):
            continue
        parsed = _parse_params(m.get("architecture") or {})
        if not parsed:
            continue
        dep = m.get("deployment") or {}
        entry = {
            "active": (parsed[0], parsed[1]),
            "total":  parsed[2],
            "tps":    dep.get("tps"),
            "ttft":   dep.get("ttft"),
        }
        name = m.get("name", "")
        if name:
            db[name.lower()] = entry
    # aliases: name → alias target
    for a in raw.get("aliases", []):
        if not isinstance(a, dict):
            continue
        src = (a.get("name") or "").lower()
        tgt = (a.get("alias") or "").lower()
        if src and tgt in db and src not in db:
            db[src] = db[tgt]
    return db


def load_ecologits_db() -> dict:
    """Load the EcoLogits model DB, fetching+caching for 24h with stale
    fallback (mirrors load_pricing). Returns {} on total failure."""
    global _DB
    if _DB is not None:
        return _DB
    # fresh cache?
    if ECOLOGITS_CACHE.exists():
        age = datetime.now() - datetime.fromtimestamp(ECOLOGITS_CACHE.stat().st_mtime)
        if age < ECOLOGITS_CACHE_MAX_AGE:
            try:
                _DB = _build_db(json.loads(ECOLOGITS_CACHE.read_text()))
                return _DB
            except (OSError, json.JSONDecodeError):
                pass
    # fetch
    try:
        req = urllib.request.Request(ECOLOGITS_URL, headers={"User-Agent": "tokstat"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
        ECOLOGITS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ECOLOGITS_CACHE.write_text(json.dumps(raw))
        _DB = _build_db(raw)
        return _DB
    except Exception:
        # stale fallback
        if ECOLOGITS_CACHE.exists():
            try:
                _DB = _build_db(json.loads(ECOLOGITS_CACHE.read_text()))
                return _DB
            except (OSError, json.JSONDecodeError):
                pass
    _DB = {}
    return _DB


# ─── Model resolution (tokstat name → EcoLogits entry) ─────────────────────

def _normalize(model: str) -> str:
    """Strip tokstat suffixes ([est], [xhigh], [no tokens], date stamps…)."""
    m = model.lower().split("[")[0].strip()
    return m


def resolve_model(model: str) -> dict | None:
    """Map a tokstat model string to an EcoLogits architecture entry.

    Exact match (incl. aliases) first, then a constrained base-name match: a DB
    key that prefixes our (more specific) model name at a version boundary —
    e.g. a dated "claude-opus-4-7-20250805" resolves to "claude-opus-4-7". We
    deliberately do NOT match in the other direction (a generic name onto an
    arbitrary more-specific variant), since that guessed e.g. "claude-sonnet-4"
    → "claude-sonnet-4-5" or "gemini-2.5" → "gemini-2.5-flash-image"."""
    db = load_ecologits_db()
    if not db:
        return None
    name = _normalize(model)
    if name in db:
        return db[name]
    # base-name match: db key k is a prefix of our name at a boundary char.
    cands = [k for k in db
             if name.startswith(k) and (len(name) == len(k) or name[len(k)] in "-.:/ ")]
    if cands:
        return db[max(cands, key=len)]
    return None


# ─── Usage-phase impact (ported from EcoLogits) ────────────────────────────

def _gpu_energy(output_tokens: float, active_params: float) -> float:
    per = (_GPU_ENERGY_ALPHA * math.exp(_GPU_ENERGY_BETA * _BATCH_SIZE) * active_params
           + _GPU_ENERGY_GAMMA) / 1000.0
    return output_tokens * per


def _generation_latency(output_tokens: float, active_params: float,
                        tps, ttft) -> float:
    if tps:
        lpt = 1.0 / tps
    else:
        lpt = (_LATENCY_ALPHA * active_params + _LATENCY_BETA * _BATCH_SIZE
               + _LATENCY_GAMMA)
    return output_tokens * lpt + (ttft or 0.0)


def _request_energy(output_tokens: float, active_params: float, total_params: float,
                    tps, ttft, pue: float) -> float:
    gpu_e = _gpu_energy(output_tokens, active_params)
    mem = 1.2 * total_params * _QUANT_BITS / 8.0
    gpu_nb = max(1, math.ceil(mem / _GPU_MEMORY))
    gpu_req = 2 ** math.ceil(math.log2(gpu_nb))
    lat = _generation_latency(output_tokens, active_params, tps, ttft)
    server_e = (lat / 3600.0) * _SERVER_POWER * (gpu_req / _SERVER_GPUS) * (1.0 / _BATCH_SIZE)
    it_energy = server_e + gpu_req * gpu_e
    return pue * it_energy


def impact_for(model: str, output_tokens: float,
               prefill_tokens: float = 0.0,
               cache_read_tokens: float = 0.0,
               pue: float = DEFAULT_PUE,
               mix_gwp: float = DEFAULT_MIX_GWP) -> dict | None:
    """Return usage-phase {energy:(min,max) kWh, gwp:(min,max) kgCO2eq} for
    `model`, or None if the model is unknown.

    `output_tokens` drive the decode phase (EcoLogits' formula). `prefill_tokens`
    (fresh input + cache writes) and `cache_read_tokens` add an approximate
    prefill/context term at a fraction of a decode token's energy — see
    PREFILL_FACTOR / CACHE_READ_FACTOR. The (min,max) band pairs the cheaper
    factors + smaller param estimate against the costlier factors + larger one,
    so the prefill assumption widens the range honestly."""
    arch = resolve_model(model)
    if not arch or (output_tokens <= 0 and prefill_tokens <= 0
                    and cache_read_tokens <= 0):
        return None
    amin, amax = arch["active"]
    total = arch["total"]
    tps, ttft = arch.get("tps"), arch.get("ttft")
    # decode-equivalent token counts: lo = cheapest assumption, hi = costliest.
    eff_lo = (output_tokens + PREFILL_FACTOR[0] * prefill_tokens
              + CACHE_READ_FACTOR[0] * cache_read_tokens)
    eff_hi = (output_tokens + PREFILL_FACTOR[1] * prefill_tokens
              + CACHE_READ_FACTOR[1] * cache_read_tokens)
    e1 = _request_energy(eff_lo, amin, total, tps, ttft, pue)
    e2 = _request_energy(eff_hi, amax, total, tps, ttft, pue)
    e_lo, e_hi = min(e1, e2), max(e1, e2)
    return {"energy": (e_lo, e_hi), "gwp": (e_lo * mix_gwp, e_hi * mix_gwp)}
