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

Record schema
─────────────
  {
    "timestamp":        "2026-04-16T14:23:00.123456+00:00",  # UTC ISO-8601
    "provider":         "groq",
    "model":            "llama-3.3-70b-versatile",
    "caller":           "intent_router",      # module that triggered the call
    "prompt_tokens":    312,
    "completion_tokens": 88,
    "total_tokens":     400,
    "max_tokens_cap":   250,                  # max_tokens passed to the API
    "temperature":      0.0,
    "latency_ms":       1230.4
  }
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
_LOG_DIR  = Path(__file__).parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "llm_usage.jsonl"

# ── Thread safety ──────────────────────────────────────────────────────────────
_lock = threading.Lock()


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
    Append one usage record to logs/llm_usage.jsonl.

    All arguments are keyword-only to avoid positional mistakes.
    Silently swallows any I/O error so a logging failure never crashes the app.
    """
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
    }
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never let logging break the caller
