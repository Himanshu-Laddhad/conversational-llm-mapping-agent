"""
session.py
──────────
In-memory session object that gives all five intents (explain, simulate,
modify, generate, audit) shared conversation memory and file context across
multiple dispatch() calls.

Usage::

    from modules.session import Session
    from modules.dispatcher import dispatch

    session = Session()

    # First turn — provide a file
    result = dispatch(
        user_message="Explain what this mapping does.",
        file_path="MappingData/.../810_NordStrom_Xslt_11-08-2023.xml",
        session=session,
    )
    print(result["primary_response"])

    # Second turn — file context and prior conversation are remembered
    result = dispatch(
        user_message="Now audit it for production issues.",
        session=session,  # no file_path needed — session remembers it
    )
    print(result["primary_response"])

    # Third turn — context from both previous turns is injected
    result = dispatch(
        user_message="Modify the sender ID to PROD_SENDER_01.",
        session=session,
    )

Session internals
─────────────────
- ingested:   The last parsed file dict (set by dispatcher after ingest).
- agent:      The live FileAgent instance (explain intent only). Reused across
              turns so the full explain conversation history is preserved.
- history:    List of {"intent", "user", "assistant"} dicts, one per turn.
              Used by get_context_str() to build a compact context prefix that
              is injected into non-explain engine prompts.
- session_id: Short random hex ID for logging/debugging.

All state is in-memory only — no disk persistence required.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class Session:
    """
    Holds cross-turn state for a single user session.

    Create one instance per user conversation and pass it to every
    dispatch() call. The dispatcher reads from and writes to it in-place,
    so you always get the most up-to-date context without manual bookkeeping.
    """

    session_id: str = field(
        default_factory=lambda: uuid.uuid4().hex[:8]
    )

    # The most recently parsed file dict (from file_ingestion.ingest_file).
    # Re-used as the file context for subsequent turns that don't supply
    # a new file_path.
    ingested: Optional[dict] = None

    # Live FileAgent instance — only populated when the explain intent has run.
    # Preserved across turns so the full multi-turn explain conversation is kept.
    agent: Optional[Any] = None

    # Full turn history: list of dicts with keys intent / user / assistant.
    history: List[dict] = field(default_factory=list)

    # How many characters of recent history to inject into non-explain prompts.
    # Kept small to avoid blowing the token budget of the target engine.
    max_context_chars: int = 3_000

    # ── Public helpers ────────────────────────────────────────────────────────

    def add_turn(self, intent: str, user_msg: str, response: str) -> None:
        """Record one completed dispatch turn in the history."""
        # Truncate stored response to avoid unbounded growth
        _MAX_STORED = 1_500
        stored_response = (
            response[:_MAX_STORED] + " …[truncated]"
            if len(response) > _MAX_STORED
            else response
        )
        self.history.append({
            "intent":    intent,
            "user":      user_msg,
            "assistant": stored_response,
        })

    def get_context_str(self) -> str:
        """
        Return a compact string of recent conversation history suitable for
        prepending to an LLM prompt.

        Walks backwards through history collecting turns until
        max_context_chars is reached, then reverses to chronological order.
        Returns an empty string if history is empty.
        """
        if not self.history:
            return ""

        budget   = self.max_context_chars
        segments = []

        for turn in reversed(self.history):
            snippet = (
                f"[{turn['intent'].upper()}] "
                f"User: {turn['user']}\n"
                f"Assistant: {turn['assistant']}\n"
            )
            if len(snippet) > budget:
                # Partial inclusion to use remaining budget
                snippet = snippet[:budget] + " …"
                segments.append(snippet)
                break
            budget -= len(snippet)
            segments.append(snippet)
            if budget <= 0:
                break

        segments.reverse()
        return "\n".join(segments)

    def reset(self) -> None:
        """Clear all session state (keeps session_id and config unchanged)."""
        self.ingested = None
        self.agent    = None
        self.history  = []

    # ── Dunder helpers ────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        n_turns   = len(self.history)
        has_file  = self.ingested is not None
        has_agent = self.agent    is not None
        return (
            f"Session(id={self.session_id!r}, turns={n_turns}, "
            f"file={has_file}, agent={has_agent})"
        )
