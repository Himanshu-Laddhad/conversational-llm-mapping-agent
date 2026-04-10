"""
session.py
──────────
In-memory session object that gives all five intents (explain, simulate,
modify, generate, audit) shared conversation memory and file context across
multiple dispatch() calls.

Supports multi-file sessions: any number of files can be added at any point
via add_file(). The session picks the most relevant file per turn using
get_primary_ingested().

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

    # Second turn — add another file mid-conversation
    result = dispatch(
        user_message="Now compare this 850 PO mapping.",
        file_path="MappingData/.../850_Graybar_XSLT.xml",
        session=session,
    )

    # Third turn — session picks the most relevant file automatically
    result = dispatch(
        user_message="Audit the NordStrom invoice mapping.",
        session=session,  # no file_path needed — session remembers all files
    )

Session internals
─────────────────
- ingested_files: All parsed file dicts added this session (via add_file).
- ingested:       Alias for the most recently added file (backward compat).
- agent:          Live FileAgent instance (explain intent only).
- history:        List of {"intent", "user", "assistant"} dicts, one per turn.
- session_id:     Short random hex ID for logging/debugging.

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

    Multiple files can be added at any point via add_file(). Use
    get_primary_ingested(user_message) to retrieve the most relevant file
    for a given turn (keyword match against filenames, fallback to last added).
    """

    session_id: str = field(
        default_factory=lambda: uuid.uuid4().hex[:8]
    )

    # All files ingested this session. Each entry is an ingest_file() dict.
    ingested_files: List[dict] = field(default_factory=list)

    # Alias: most recently added file dict (kept for backward compatibility
    # with code that reads session.ingested directly).
    ingested: Optional[dict] = None

    # Live FileAgent instance — only populated when the explain intent has run.
    # Reset when a new file is added so the agent is always tied to the current
    # primary file.
    agent: Optional[Any] = None

    # Full turn history: list of dicts with keys intent / user / assistant.
    history: List[dict] = field(default_factory=list)

    # How many characters of recent history to inject into non-explain prompts.
    # Kept small to avoid blowing the token budget of the target engine.
    max_context_chars: int = 3_000

    # ── Public helpers ────────────────────────────────────────────────────────

    def add_file(self, ingested_dict: dict) -> None:
        """
        Register a newly ingested file in the session.

        The file is appended to ingested_files and becomes the new primary
        (session.ingested). The explain agent is reset because it is tied to
        a single file; it will be re-created on the next explain turn.
        """
        self.ingested_files.append(ingested_dict)
        self.ingested = ingested_dict
        self.agent    = None   # explain agent is file-specific — reset it

    def get_primary_ingested(self, user_message: str = "") -> Optional[dict]:
        """
        Return the most relevant ingested file for the given user message.

        Strategy:
          1. If no files, return None.
          2. If only one file or no message, return the last added file.
          3. Score each file by counting how many underscore-separated tokens
             from its filename appear in the (lowercased) user message.
             Return the highest-scoring file; break ties by recency.

        This lets users reference files by name naturally, e.g.
        "audit the NordStrom mapping" → picks 810_NordStrom_Xslt.xml.
        """
        if not self.ingested_files:
            return None
        if len(self.ingested_files) == 1 or not user_message:
            return self.ingested_files[-1]

        msg_lower = user_message.lower()
        best       = self.ingested_files[-1]
        best_score = 0

        for f in self.ingested_files:
            fname = f.get("metadata", {}).get("filename", "")
            # Split on common separators and score word matches
            tokens = [t for t in fname.lower().replace("-", "_").split("_") if len(t) > 2]
            score  = sum(1 for t in tokens if t in msg_lower)
            if score >= best_score:
                best, best_score = f, score

        return best

    def replace_file(self, old_filename: str, new_ingested: dict) -> None:
        """
        Replace an existing ingested file by filename with a new version.

        Useful after modify/generate: the patched XSLT supersedes the original
        so subsequent turns automatically use the updated content.

        If old_filename is not found in the session, the new file is appended
        as a brand-new entry (same behaviour as add_file).
        """
        for i, f in enumerate(self.ingested_files):
            if f.get("metadata", {}).get("filename", "") == old_filename:
                self.ingested_files[i] = new_ingested
                self.ingested = new_ingested
                self.agent = None   # explain agent is file-specific — reset it
                return
        # Not found — treat as a new file
        self.add_file(new_ingested)

    def add_turn(self, intent: str, user_msg: str, response: str) -> None:
        """Record one completed dispatch turn in the history."""
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
        self.ingested_files = []
        self.ingested       = None
        self.agent          = None
        self.history        = []

    # ── Dunder helpers ────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        n_turns   = len(self.history)
        n_files   = len(self.ingested_files)
        has_agent = self.agent is not None
        return (
            f"Session(id={self.session_id!r}, turns={n_turns}, "
            f"files={n_files}, agent={has_agent})"
        )
