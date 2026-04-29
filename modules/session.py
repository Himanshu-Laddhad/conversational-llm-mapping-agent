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
import difflib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional


@dataclass
class XSLTRevision:
    id: str
    content: str
    timestamp: str
    description: str
    parent_id: Optional[str] = None
    diff_from_parent: Optional[str] = None


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

    # How many characters of recent history to inject into modify/simulate/audit/generate prompts.
    # Large enough that prior explain turns (with code snippets) survive for follow-up "apply that".
    max_context_chars: int = 24_000

    # Explicit file-role paths (source_path values from ingested metadata).
    active_xslt_file: Optional[str] = None
    active_source_file: Optional[str] = None
    active_target_file: Optional[str] = None
    xslt_revisions: List[XSLTRevision] = field(default_factory=list)

    # Per-file XSLT index dicts built by xslt_index.build_xslt_index().
    # Keyed by the file's metadata.source_path so the dispatcher can look them
    # up cheaply without re-building on every turn.
    xslt_indices: dict = field(default_factory=dict)

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
            # Include tokens of length >= 2 so short but meaningful EDI identifiers
            # like "GS", "ST", "N1", or document-type codes ("810") are scored.
            tokens = [t for t in fname.lower().replace("-", "_").split("_") if len(t) >= 2]
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
                old_path = f.get("metadata", {}).get("source_path", "")
                new_path = new_ingested.get("metadata", {}).get("source_path", "")
                if old_path and new_path:
                    if self.active_xslt_file == old_path:
                        self.active_xslt_file = new_path
                    if self.active_source_file == old_path:
                        self.active_source_file = new_path
                    if self.active_target_file == old_path:
                        self.active_target_file = new_path
                self.agent = None   # explain agent is file-specific — reset it
                return
        # Not found — treat as a new file
        self.add_file(new_ingested)

    def get_ingested_by_source_path(self, source_path: Optional[str]) -> Optional[dict]:
        """Return the ingested dict whose metadata.source_path matches source_path."""
        if not source_path:
            return None
        for f in reversed(self.ingested_files):
            if f.get("metadata", {}).get("source_path", "") == source_path:
                return f
        return None

    def set_xslt_index(self, source_path: str, index: dict) -> None:
        """Store a pre-built XSLT index dict for a given file source_path."""
        if source_path:
            self.xslt_indices[source_path] = index

    def get_xslt_index(self, source_path: Optional[str]) -> Optional[dict]:
        """Return the XSLT index for source_path, or None if not yet built."""
        if not source_path:
            return None
        return self.xslt_indices.get(source_path)

    def set_role_file(self, role: str, source_path: Optional[str]) -> None:
        """Set a file role path (xslt, source, target)."""
        role = (role or "").lower().strip()
        if role == "xslt":
            self.active_xslt_file = source_path or None
        elif role == "source":
            self.active_source_file = source_path or None
        elif role == "target":
            self.active_target_file = source_path or None

    def get_role_file(self, role: str) -> Optional[str]:
        """Get a role path (xslt, source, target)."""
        role = (role or "").lower().strip()
        if role == "xslt":
            return self.active_xslt_file
        if role == "source":
            return self.active_source_file
        if role == "target":
            return self.active_target_file
        return None

    def add_turn(self, intent: str, user_msg: str, response: Optional[str]) -> None:
        """Record one completed dispatch turn in the history."""
        resp = response if isinstance(response, str) else ""
        # Must fit code-level prior turns (variables, XPath) used by downstream modify —
        # 8000 chars avoids dropping the substantive tail.
        _MAX_STORED = 8_000
        stored_response = (
            resp[:_MAX_STORED] + " …[truncated]"
            if len(resp) > _MAX_STORED
            else resp
        )
        self.history.append({
            "intent":    intent,
            "user":      user_msg,
            "assistant": stored_response,
        })

    def save_xslt_revision(self, content: str, description: str) -> str:
        parent = self.xslt_revisions[-1] if self.xslt_revisions else None
        diff_text = None
        if parent is not None:
            diff = difflib.unified_diff(
                parent.content.splitlines(),
                content.splitlines(),
                fromfile="parent",
                tofile="current",
                lineterm="",
            )
            diff_text = "\n".join(diff)
        rev = XSLTRevision(
            id=uuid.uuid4().hex,
            content=content,
            timestamp=datetime.now().strftime("%Y-%m-%d %I:%M %p"),
            description=description or "XSLT modification",
            parent_id=parent.id if parent else None,
            diff_from_parent=diff_text,
        )
        self.xslt_revisions.append(rev)
        return rev.id

    def get_latest_xslt(self) -> Optional[XSLTRevision]:
        return self.xslt_revisions[-1] if self.xslt_revisions else None

    def compare_revisions(self, rev_id_1: str, rev_id_2: str) -> dict:
        rev1 = next((r for r in self.xslt_revisions if r.id == rev_id_1), None)
        rev2 = next((r for r in self.xslt_revisions if r.id == rev_id_2), None)
        if rev1 is None or rev2 is None:
            return {
                "diff_lines": [],
                "summary": "Revision(s) not found",
                "change_descriptions": [],
            }
        diff_lines = list(difflib.unified_diff(
            rev1.content.splitlines(),
            rev2.content.splitlines(),
            fromfile=rev1.id,
            tofile=rev2.id,
            lineterm="",
        ))
        changed = [
            ln for ln in diff_lines
            if (ln.startswith("+") and not ln.startswith("+++"))
            or (ln.startswith("-") and not ln.startswith("---"))
        ]
        return {
            "diff_lines": diff_lines,
            "summary": f"{len(changed)} changed line(s) between selected revisions",
            "change_descriptions": [
                rev1.description,
                rev2.description,
            ],
        }

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
        self.active_xslt_file = None
        self.active_source_file = None
        self.active_target_file = None
        self.xslt_revisions = []
        self.xslt_indices   = {}

    # ── Dunder helpers ────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        n_turns   = len(self.history)
        n_files   = len(self.ingested_files)
        has_agent = self.agent is not None
        return (
            f"Session(id={self.session_id!r}, turns={n_turns}, "
            f"files={n_files}, agent={has_agent})"
        )
