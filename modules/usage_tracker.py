"""
usage_tracker.py
────────────────
Thread-safe token-usage logger for all LLM API calls.

Each call appends one JSON record to  logs/llm_usage.jsonl  at the project
root.  The log directory is created automatically on first write.

Public API
──────────
  log_usage(provider, model, caller,
            prompt_tokens, completion_tokens, total_tokens,
            max_tokens, temperature, latency_ms)
  get_session_stats() -> dict
  reset_session_stats() -> None

Record schema
─────────────
  {
    "timestamp":        "2026-04-16T14:23:00.123456+00:00",  # UTC ISO-8601
    "provider":         "openai",
    "model":            "llama-3.3-70b-versatile",
    "caller":           "intent_router",      # module that triggered the call
    "prompt_tokens":    312,
    "completion_tokens": 88,
    "total_tokens":     400,
    "max_tokens_cap":   250,                  # max_tokens passed to the API
    "temperature":      0.0,
    "latency_ms":       1230.4,
    "cost_usd":         0.000312              # estimated cost
  }
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

# ── Paths ──────────────────────────────────────────────────────────────────────
_LOG_DIR  = Path(__file__).parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "llm_usage.jsonl"

# ── Thread safety ──────────────────────────────────────────────────────────────
_lock = threading.Lock()

# ── Pricing table (USD per 1 million tokens) ───────────────────────────────────
# Format: model_id -> (input_per_1M, output_per_1M)
PRICING: Dict[str, tuple] = {
    # Groq — Llama models
    "llama-3.3-70b-versatile":       (0.59,  0.79),
    "llama-3.1-8b-instant":          (0.05,  0.08),
    # OpenAI — legacy
    "gpt-4o-mini":                   (0.15,  0.60),
    "gpt-4o":                        (2.50, 10.00),
    # OpenAI — GPT-4.1 family (Apr 2025+)
    "gpt-4.1":                       (2.00,  8.00),
    "gpt-4.1-mini":                  (0.40,  1.60),
    "gpt-4.1-nano":                  (0.10,  0.40),
    # Anthropic
    "claude-3-5-haiku-20241022":     (0.80,  4.00),
    "claude-3-5-sonnet-20241022":    (3.00, 15.00),
    # Meta (via NVIDIA NIM)
    "meta/llama-3.3-70b-instruct":   (0.77,  0.77),
    # Qwen (via Together AI / reference)
    "qwen2.5-72b-instruct":          (0.90,  0.90),
}

# Pricing comparison table shown in the UI
PRICING_COMPARISON = [
    {"model": "gpt-4.1",                  "provider": "OpenAI",           "input_per_1M": 2.00,  "output_per_1M": 8.00},
    {"model": "gpt-4.1-mini",             "provider": "OpenAI",           "input_per_1M": 0.40,  "output_per_1M": 1.60},
    {"model": "gpt-4.1-nano",             "provider": "OpenAI",           "input_per_1M": 0.10,  "output_per_1M": 0.40},
    {"model": "llama-3.3-70b-versatile",  "provider": "Groq",             "input_per_1M": 0.59,  "output_per_1M": 0.79},
    {"model": "llama-3.1-8b-instant",     "provider": "Groq",             "input_per_1M": 0.05,  "output_per_1M": 0.08},
    {"model": "claude-3-5-haiku-20241022","provider": "Anthropic",        "input_per_1M": 0.80,  "output_per_1M": 4.00},
    {"model": "llama-3.3-70b-instruct",   "provider": "Meta (NVIDIA NIM)","input_per_1M": 0.77,  "output_per_1M": 0.77},
]

# ── In-memory session accumulator ──────────────────────────────────────────────
_session_stats: Dict[str, Any] = {
    "calls":             0,
    "prompt_tokens":     0,
    "completion_tokens": 0,
    "total_tokens":      0,
    "estimated_cost_usd": 0.0,
    "last_call":         None,
}


def _compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p_in, p_out = PRICING.get(model, (0.0, 0.0))
    return (prompt_tokens * p_in + completion_tokens * p_out) / 1_000_000


def get_session_stats() -> Dict[str, Any]:
    with _lock:
        return dict(_session_stats)


def reset_session_stats() -> None:
    with _lock:
        _session_stats.update(
            calls=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost_usd=0.0,
            last_call=None,
        )


# ── Public function ────────────────────────────────────────────────────────────

def log_usage(
    *,
    provider:           str,
    model:              str,
    caller:             str,
    prompt_tokens:      int,
    completion_tokens:  int,
    total_tokens:       int,
    max_tokens:         int,
    temperature:        float,
    latency_ms:         float,
) -> None:
    """
    Append one usage record to logs/llm_usage.jsonl and update session stats.

    All arguments are keyword-only to avoid positional mistakes.
    Silently swallows any I/O error so a logging failure never crashes the app.
    """
    cost = _compute_cost(model, prompt_tokens, completion_tokens)

    record = {
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "provider":          provider,
        "model":             model,
        "caller":            caller,
        "prompt_tokens":     prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens":      total_tokens,
        "max_tokens_cap":    max_tokens,
        "temperature":       temperature,
        "latency_ms":        round(latency_ms, 1),
        "cost_usd":          round(cost, 6),
    }

    with _lock:
        # Update in-memory session accumulator
        _session_stats["calls"]             += 1
        _session_stats["prompt_tokens"]     += prompt_tokens
        _session_stats["completion_tokens"] += completion_tokens
        _session_stats["total_tokens"]      += total_tokens
        _session_stats["estimated_cost_usd"] = round(
            _session_stats["estimated_cost_usd"] + cost, 6
        )
        _session_stats["last_call"] = {
            "provider":         provider,
            "model":            model,
            "caller":           caller,
            "prompt_tokens":    prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":     total_tokens,
            "cost_usd":         round(cost, 6),
            "latency_ms":       round(latency_ms, 1),
        }

        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception:
            pass  # never let logging break the caller
