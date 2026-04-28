"""
token_tracker.py
────────────────
Lightweight per-request token usage tracker for the Mapping Intelligence Agent.

Each call to dispatcher.dispatch() creates a fresh tracker via new_tracker().
Every engine that calls an LLM records its usage via get_tracker().record().
The dispatcher reads the accumulated stats via get_tracker().summary() and
returns them in the result dict under the key "token_usage".

app.py then merges per-request stats into session-level cumulative stats stored
in st.session_state.token_stats, and renders them in the sidebar.

Thread safety: a module-level threading.local() is used so that concurrent
Streamlit sessions don't share the same tracker instance.

Usage:
    # In dispatcher.py — at the top of dispatch():
    from .token_tracker import new_tracker
    tracker = new_tracker()

    # In any engine — after a Groq/OpenAI API call:
    from .token_tracker import get_tracker
    get_tracker().record(engine="simulate", model=model, usage=response.usage)

    # In dispatcher.py — before returning result:
    result["token_usage"] = get_tracker().summary()
"""

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class _CallRecord:
    engine:            str   # e.g. "explain", "simulate", "modify", "intent_router"
    model:             str
    prompt_tokens:     int
    completion_tokens: int
    total_tokens:      int


class TokenTracker:
    """Accumulates LLM API call statistics for a single dispatch() turn."""

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self.calls: List[_CallRecord] = []

    def record(self, engine: str, model: str, usage: Any) -> None:
        """
        Record token usage from an API response.

        Args:
            engine:  Human-readable label for the calling engine.
                     Use one of: "explain", "simulate", "modify", "audit",
                     "generate", "intent_router".
            model:   Model identifier string (e.g. "llama-3.3-70b-versatile").
            usage:   The .usage object from the Groq/OpenAI API response.
                     Must have prompt_tokens, completion_tokens, total_tokens
                     attributes (or they default to 0 if missing).
        """
        if usage is None:
            return
        with self._lock:
            self.calls.append(_CallRecord(
                engine=engine,
                model=model,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
            ))

    def summary(self) -> Dict:
        """
        Return aggregated stats for this tracker's recorded calls.

        Returns a dict with:
            total_prompt_tokens:      int
            total_completion_tokens:  int
            total_tokens:             int
            total_calls:              int
            by_engine:                {engine_name: {prompt, completion, total, calls}}
        """
        by_engine: Dict[str, Dict] = {}
        total_prompt = total_completion = total = 0

        with self._lock:
            for c in self.calls:
                e = by_engine.setdefault(c.engine, {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "calls": 0,
                    "model": c.model,
                })
                e["prompt_tokens"]     += c.prompt_tokens
                e["completion_tokens"] += c.completion_tokens
                e["total_tokens"]      += c.total_tokens
                e["calls"]             += 1
                total_prompt      += c.prompt_tokens
                total_completion  += c.completion_tokens
                total             += c.total_tokens

        return {
            "total_prompt_tokens":     total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens":            total,
            "total_calls":             len(self.calls),
            "by_engine":               by_engine,
        }

    def reset(self) -> None:
        with self._lock:
            self.calls = []


# ── Thread-local singleton ─────────────────────────────────────────────────────
# Each OS thread (= each Streamlit session worker) gets its own tracker.

_tls = threading.local()


def get_tracker() -> TokenTracker:
    """Return the tracker for the current thread, creating one if needed."""
    if not hasattr(_tls, "tracker"):
        _tls.tracker = TokenTracker()
    return _tls.tracker


def new_tracker() -> TokenTracker:
    """
    Reset and return a fresh tracker for the current thread.
    Call this at the start of every dispatch() to clear stats from prior turns.
    """
    _tls.tracker = TokenTracker()
    return _tls.tracker


# ── Session-level accumulator helpers (used by app.py) ────────────────────────

_EMPTY_SESSION_STATS: Dict = {
    "total_prompt_tokens":     0,
    "total_completion_tokens": 0,
    "total_tokens":            0,
    "total_calls":             0,
    "turns":                   0,
    "by_engine":               {},
}


def empty_session_stats() -> Dict:
    """Return a fresh zero-valued session stats dict (deep copy)."""
    import copy
    return copy.deepcopy(_EMPTY_SESSION_STATS)


def merge_into_session(session_stats: Dict, turn_usage: Dict) -> None:
    """
    Merge a single turn's token_usage dict into the cumulative session_stats
    dict in-place.  Both dicts have the same schema as .summary() returns,
    plus a 'turns' counter in session_stats.
    """
    if not turn_usage:
        return
    session_stats["total_prompt_tokens"]     += turn_usage.get("total_prompt_tokens", 0)
    session_stats["total_completion_tokens"] += turn_usage.get("total_completion_tokens", 0)
    session_stats["total_tokens"]            += turn_usage.get("total_tokens", 0)
    session_stats["total_calls"]             += turn_usage.get("total_calls", 0)
    session_stats["turns"]                   += 1

    for engine, stats in turn_usage.get("by_engine", {}).items():
        dest = session_stats["by_engine"].setdefault(engine, {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
            "model": stats.get("model", ""),
        })
        dest["prompt_tokens"]     += stats.get("prompt_tokens", 0)
        dest["completion_tokens"] += stats.get("completion_tokens", 0)
        dest["total_tokens"]      += stats.get("total_tokens", 0)
        dest["calls"]             += stats.get("calls", 0)
