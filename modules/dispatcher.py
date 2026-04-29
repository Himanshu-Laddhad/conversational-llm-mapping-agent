"""
dispatcher.py
─────────────
Central dispatch engine for the Conversational Mapping Intelligence Agent.

Accepts a user message and one or more optional files, routes intent via
intent_router, then calls the appropriate engine for each active intent.

Supports multi-file sessions: pass file_paths=[...] to ingest several files
in one call. The session tracks all uploaded files and picks the most relevant
one per turn via session.get_primary_ingested(user_message).

Auto-RAG: if a .rag_index/ directory exists alongside this project, the
dispatcher automatically queries it and prepends relevant context snippets to
all engine prompts (non-fatal if the index is missing or query fails).

Currently implemented:   explain  → explain_agent.explain()
                         simulate → simulation_engine.simulate()
                         modify   → modification_engine.modify()
                         generate → xslt_generator.generate()
                         audit    → audit_engine.audit()
                         folder   → rag_engine.index_folder() + query_folder()

Usage (as module):
    from modules.dispatcher import dispatch

    result = dispatch(
        user_message="What does the BEG segment do?",
        file_path="MappingData/MappingData/850_IN_Graybar/Graybar_850_XSLT.xml",
    )
    print(result["primary_response"])

    # Multi-file upload
    result = dispatch(
        user_message="Compare these two mappings.",
        file_paths=["path/to/810.xml", "path/to/850.xml"],
        session=session,
    )

Usage (standalone test):
    python modules/dispatcher.py
    python modules/dispatcher.py path/to/file.xml
"""

import os
import re
import time
import difflib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dotenv import load_dotenv

# Non-invasive audit logging (best-effort; never breaks dispatch()).
def _audit_log_event(**kwargs: Any) -> None:
    try:
        from .rules_store import RulesStore, utc_now  # type: ignore
    except Exception:
        try:
            from rules_store import RulesStore, utc_now  # type: ignore
        except Exception:
            return

    try:
        db_path = Path(__file__).resolve().parent.parent / "rules_store.db"
        with RulesStore(db_path) as store:
            now = utc_now()
            store.log_event(
                actor=str(kwargs.get("actor", "system")),
                action=str(kwargs.get("action", "dispatch")),
                target=str(kwargs.get("target", "dispatch")),
                status=str(kwargs.get("status", "success")),
                started_at=kwargs.get("started_at", now),
                finished_at=kwargs.get("finished_at", now),
                duration_ms=int(kwargs.get("duration_ms", 0)),
                why=str(kwargs.get("why", "")),
                error=kwargs.get("error"),
                metadata=kwargs.get("metadata"),
            )
    except Exception:
        return

# Load .env from module directory or one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# Placeholder responses for engines that are not yet built.
_UNBUILT: dict = {}   # all engines are now implemented

# ── Out-of-scope response (used when LLM classifies is_in_scope=False) ────────
# Scope detection is now handled by the intent router LLM — no keyword lists.

_OUT_OF_SCOPE_RESPONSE = (
    "I'm the **PartnerLinQ Mapping Intelligence Agent** — purpose-built for "
    "EDI/XSLT mapping analysis.\n\n"
    "I can help you with:\n"
    "- **Explain** — what an XSLT mapping does in plain English\n"
    "- **Simulate** — run a transformation against real source data (Saxon-HE / lxml)\n"
    "- **Modify** — add fields, change values, add line-item rows\n"
    "- **Audit** — check a mapping for misconfigurations and production-readiness issues\n"
    "- **Generate** — create a new XSLT mapping from requirements\n\n"
    "Please ask a question about your EDI or XSLT mapping files."
)

_MODIFY_PATTERNS: tuple[str, ...] = (
    r"change.*date.*format",
    r"format.*date",
    r"convert.*date",
    r"(yyyy|mm|dd|yy)",
    r"move.*decimal",
    r"decimal.*place",
    r"format.*price",
    r"format.*number",
    r"\d+\s*decimal",
    r"multiply.*by",
    r"multiply.*with",
    r"add.*to",
    r"subtract.*from",
    r"calculate",
    r"change.*to",
    r"update.*to",
    r"replace.*with",
    r"modify",
    r"set.*to",
)


def _classify_action(user_message: str) -> str:
    """
    Greedy action classifier.
    Critical rule: evaluate modify patterns before out-of-scope rejection.
    """
    msg_lower = (user_message or "").lower()

    for pattern in _MODIFY_PATTERNS:
        if re.search(pattern, msg_lower):
            return "modify"

    if any(word in msg_lower for word in ["audit", "review this", "check for issues",
                                           "flag problems", "production ready",
                                           "what could go wrong", "is anything wrong"]):
        return "audit"
    if any(word in msg_lower for word in ["simulate", "run", "test", "transform", "execute"]):
        return "simulate"
    if any(word in msg_lower for word in ["explain", "what does", "how does", "describe", "show me"]):
        return "explain"
    if any(word in msg_lower for word in ["generate", "create", "build", "make a new"]):
        return "generate"
    if "compar" in msg_lower and ("xslt" in msg_lower or "version" in msg_lower or "old" in msg_lower):
        return "compare"
    return "out_of_scope"


def _build_explain_prompt_with_roles(
    user_message: str,
    source_ingested: Optional[dict],
    target_ingested: Optional[dict],
) -> str:
    """
    Build explain prompt so XSLT explanation is grounded in source→target context.
    """
    src_name = (source_ingested or {}).get("metadata", {}).get("filename", "")
    tgt_name = (target_ingested or {}).get("metadata", {}).get("filename", "")
    if src_name or tgt_name:
        return (
            f"{user_message}\n\n"
            f"[EXPLAIN CONTEXT]\n"
            f"Explain the active XSLT in context of transforming source "
            f"`{src_name or '(not selected)'}` toward target "
            f"`{tgt_name or '(not selected)'}`.\n"
            f"Highlight key segment paths (ISA, GS, ST, BIG/REF/N1/IT1/CTT/SE) where possible."
        )
    return user_message


def _is_compare_xslt_request(user_message: str) -> bool:
    lower = (user_message or "").lower()
    return (
        ("compare" in lower and "xslt" in lower)
        or ("side by side" in lower and "xslt" in lower)
        or ("comparison" in lower and "mapping" in lower)
    )


def _extract_xslt_compare_facts(ingested: dict) -> Dict[str, Any]:
    parsed = (ingested or {}).get("parsed_content", {}) or {}
    meta = (ingested or {}).get("metadata", {}) or {}
    tcg = parsed.get("template_call_graph", []) or []
    output_elements: List[str] = []
    value_of: List[str] = []
    hardcoded = parsed.get("hardcoded_values", []) or []
    for t in tcg:
        for el in (t.get("output_elements") or []):
            if el not in output_elements:
                output_elements.append(el)
        for xp in (t.get("value_of") or []):
            if xp not in value_of:
                value_of.append(xp)
    return {
        "filename": meta.get("filename", ""),
        "templates": len(tcg),
        "segments": output_elements,
        "xpaths": value_of,
        "hardcoded_count": len(hardcoded),
        "raw_xml": parsed.get("raw_xml") or "",
    }


def _compare_two_xslts(old_ing: dict, new_ing: dict) -> Dict[str, Any]:
    oldf = _extract_xslt_compare_facts(old_ing)
    newf = _extract_xslt_compare_facts(new_ing)

    old_seg = set(oldf["segments"])
    new_seg = set(newf["segments"])
    missing_in_new = sorted(old_seg - new_seg)
    added_in_new = sorted(new_seg - old_seg)

    old_xp = set(oldf["xpaths"])
    new_xp = set(newf["xpaths"])
    mapping_divergence = sorted((old_xp ^ new_xp))[:40]

    old_lines = (oldf.get("raw_xml") or "").splitlines()
    new_lines = (newf.get("raw_xml") or "").splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile="old", tofile="new", lineterm=""))
    changed_lines = sum(1 for ln in diff if (ln.startswith("+") and not ln.startswith("+++")) or (ln.startswith("-") and not ln.startswith("---")))

    risk_points = 0
    risk_notes: List[str] = []
    if missing_in_new:
        risk_points += 2
        risk_notes.append(f"Missing segments in revised XSLT: {', '.join(missing_in_new[:10])}")
    if len(mapping_divergence) > 20:
        risk_points += 1
        risk_notes.append("Large field-mapping divergence between versions.")
    if newf["hardcoded_count"] > oldf["hardcoded_count"]:
        risk_points += 1
        risk_notes.append("Revised XSLT introduced additional hardcoded values.")
    risk_level = "low" if risk_points == 0 else ("medium" if risk_points <= 2 else "high")

    summary = (
        f"Compared `{oldf['filename']}` vs `{newf['filename']}`: "
        f"{changed_lines} changed line(s), {len(added_in_new)} segment(s) added, "
        f"{len(missing_in_new)} segment(s) removed, {len(mapping_divergence)} mapping divergence point(s)."
    )

    text = (
        "## XSLT Comparison\n"
        + summary
        + f"\nRisk assessment: **{risk_level}**."
    )
    if risk_notes:
        text += "\n" + "\n".join(f"- {n}" for n in risk_notes)

    return {
        "text": text,
        "summary": summary,
        "risk_level": risk_level,
        "missing_segments_in_revised": missing_in_new,
        "added_segments_in_revised": added_in_new,
        "mapping_divergence": mapping_divergence,
        "changed_lines": changed_lines,
        "old_file": oldf["filename"],
        "new_file": newf["filename"],
        "diff_preview": "\n".join(diff[:240]),
    }


def dispatch(
    user_message: str,
    file_path: Optional[str] = None,
    file_paths: Optional[List[str]] = None,
    source_file: Optional[str] = None,
    target_file: Optional[str] = None,
    active_xslt_file: Optional[str] = None,
    active_source_file: Optional[str] = None,
    active_target_file: Optional[str] = None,
    ingested: Optional[dict] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    session: Optional[Any] = None,
    provider: Optional[str] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Route a user message and call the appropriate engine(s).

    Args:
        user_message: The user's natural language request.
        file_path:    Path to a single mapping file (XSLT/XSD/XML) to parse.
                      Kept for backward compatibility. Prefer file_paths.
        file_paths:   List of paths to mapping files to ingest this turn.
                      All files are added to session.ingested_files. The most
                      recently ingested one becomes the primary for this turn.
        source_file:  Path to the source/input data file for simulate intent.
        target_file:  Optional path to expected target/output contract file.
        ingested:     Pre-parsed dict from ingest_file(). If provided,
                      file_path and file_paths are ignored.
        api_key:      LLM API key. Falls back to the provider's env var.
        model:        LLM model. Falls back to the provider's env var,
                      then the provider's default model.
        session:      Optional Session instance (from modules.session).
                      When provided:
                        - all uploaded files are tracked across turns
                        - the most relevant file is selected per turn
                        - conversation history is injected into prompts
                        - RAG context is auto-injected from .rag_index/ if present
                      Pass the same Session object on every subsequent call
                      to maintain memory across turns.
        on_progress: Optional callback invoked with short human-readable status
                     strings as work advances (for UI progress indicators).

    Returns:
        {
          "route":              Full route() result (scores, active_intents, etc.)
          "responses":          Dict of { intent: response_str }.
          "primary_response":   Response string from the primary intent.
          "agent":              Live FileAgent instance if explain was active.
          "ingested":           The primary parsed file dict for this turn.
          "audit_dict":         Structured audit data dict if audit ran, else None.
          "session":            The Session object (updated in-place).
          "primary_file_name":  Filename of the file used this turn (str or "").
        }

    Raises:
        ValueError: If no LLM API key is available for the active provider.
        FileNotFoundError: If a provided file path does not exist.
    """
    _t0 = time.time()
    _actor = getattr(session, "session_id", None) if session is not None else None
    _actor = str(_actor) if _actor else "system"
    forced_action = _classify_action(user_message)

    def _progress(msg: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(msg)
        except Exception:
            pass
    try:
        from .intent_router import route
        from .file_ingestion import ingest_file
        from .explain_agent import explain
        from .simulation_engine import simulate, compare_output_to_target, generate_autofix_suggestions, audit_simulate_findings
        from .modification_engine import modify, extract_modify_guidance
        from .xslt_generator import generate
        from .audit_engine import audit
        from .rag_engine import query_folder
        from .token_tracker import new_tracker, get_tracker
    except ImportError:
        from intent_router import route          # fallback for standalone execution
        from file_ingestion import ingest_file
        from explain_agent import explain
        from simulation_engine import simulate, compare_output_to_target, generate_autofix_suggestions, audit_simulate_findings
        from modification_engine import modify, extract_modify_guidance
        from xslt_generator import generate
        from audit_engine import audit           # type: ignore
        from rag_engine import query_folder      # type: ignore
        from token_tracker import new_tracker, get_tracker  # type: ignore

    # Reset token tracker for this turn so prior turns don't bleed through
    new_tracker()

    # ── Resolve model (caller > per-provider env var > built-in default) ────────
    try:
        from .llm_client import get_default_model as _gdm
    except ImportError:
        from llm_client import get_default_model as _gdm  # type: ignore
    _prov = provider or "openai"
    resolved_model = model or _gdm(_prov)

    def _engine_model(engine: str) -> str:
        """Return the engine-specific model, or fall back to resolved_model."""
        # If the caller forced a model explicitly, respect that over engine overrides.
        if model:
            return model
        return _gdm(_prov, engine=engine)

    # ── 1. Ingest new files ───────────────────────────────────────────────────
    # Merge legacy file_path with new file_paths list, deduplicate
    _all_new_paths: List[str] = list(file_paths or [])
    if file_path and file_path not in _all_new_paths:
        _all_new_paths.append(file_path)

    if ingested is None and _all_new_paths:
        _progress("Reading and parsing your uploaded file(s)…")

    if ingested is None:
        for p in _all_new_paths:
            _new = ingest_file(file_path=p)
            if session is not None:
                session.add_file(_new)
                # Build XSLT index once at upload time so explain/chat turns
                # can use the tool-calling path without rebuilding each turn.
                _new_ft   = (_new.get("metadata") or {}).get("file_type", "")
                _new_path = (_new.get("metadata") or {}).get("source_path", "")
                if _new_ft == "XSLT" and _new_path:
                    try:
                        from .xslt_index import build_xslt_index as _bxi
                    except ImportError:
                        from xslt_index import build_xslt_index as _bxi  # type: ignore
                    session.set_xslt_index(_new_path, _bxi(_new))
                    # Auto-assign XSLT role on first upload if none is set yet
                    if session.get_role_file("xslt") is None:
                        session.set_role_file("xslt", _new_path)
            ingested = _new   # last ingested = primary for this turn

    # Resolve explicit role paths from caller/session.
    resolved_xslt_path = active_xslt_file or (session.get_role_file("xslt") if session else None)
    resolved_source_path = active_source_file or source_file or (session.get_role_file("source") if session else None)
    resolved_target_path = active_target_file or target_file or (session.get_role_file("target") if session else None)

    # Keep session role paths synced when caller provides explicit values.
    if session is not None:
        if active_xslt_file is not None:
            session.set_role_file("xslt", active_xslt_file)
        if (active_source_file is not None) or (source_file is not None):
            session.set_role_file("source", active_source_file or source_file)
        if (active_target_file is not None) or (target_file is not None):
            session.set_role_file("target", active_target_file or target_file)
        # Refresh resolved paths from session in case they were just updated.
        resolved_xslt_path = session.get_role_file("xslt")
        resolved_source_path = session.get_role_file("source")
        resolved_target_path = session.get_role_file("target")

    # Find role ingested dicts from session.
    xslt_ingested = None
    source_ingested = None
    target_ingested = None
    if session is not None:
        xslt_ingested = session.get_ingested_by_source_path(resolved_xslt_path)
        source_ingested = session.get_ingested_by_source_path(resolved_source_path)
        target_ingested = session.get_ingested_by_source_path(resolved_target_path)

    # Backward compatibility fallback for older callers with ingested/file_path.
    if xslt_ingested is None:
        xslt_ingested = ingested
    if xslt_ingested is None and session is not None:
        xslt_ingested = session.get_primary_ingested(user_message)
    if ingested is None:
        ingested = xslt_ingested

    # If resolved_xslt_path is still None but we now have an ingested XSLT file,
    # back-fill the role so subsequent turns can look up the index correctly.
    if (
        resolved_xslt_path is None
        and xslt_ingested is not None
        and (xslt_ingested.get("metadata") or {}).get("file_type") == "XSLT"
        and session is not None
    ):
        _sp_backfill = (xslt_ingested.get("metadata") or {}).get("source_path", "")
        if _sp_backfill:
            resolved_xslt_path = _sp_backfill
            session.set_role_file("xslt", _sp_backfill)

    # ── 2. Build context prefix from session history ──────────────────────────
    _ctx_prefix = ""
    if session is not None and session.history:
        ctx = session.get_context_str()
        if ctx:
            _progress("Including earlier messages from this chat for context…")
            _ctx_prefix = f"[PRIOR CONTEXT]\n{ctx}\n\n[CURRENT REQUEST]\n"

    # ── 3. Auto-inject RAG context — only when intent router says it's needed ────
    # needs_rag is set True by the LLM classifier when the question requires
    # context from other files (e.g. "similar to our 850 mapping", "across all
    # mappings"). In-file questions, version comparisons, and modify/simulate
    # requests on the active file all get needs_rag=False — no wasted RAG call.
    # NOTE: route_result is not yet available at this point; we defer the RAG
    # query to after step 5 (classification) and apply it before step 6 (dispatch).
    _index_dir = Path(__file__).resolve().parent.parent / ".rag_index"
    _rag_deferred = _index_dir.exists()   # will query after classification if needed

    _has_new_files = bool(_all_new_paths) or (ingested is not None and not session)

    # ── 4. Session-aware bypass: if a file is already loaded, always proceed ──
    # When the user has a file in context any question is implicitly about it.
    _has_loaded_file = (
        (session is not None and bool(session.ingested_files))
        or xslt_ingested is not None
    )

    _progress("Preparing to interpret your question…")

    # ── 5. Classify user intent ───────────────────────────────────────────────
    # Direct compare mode for side-by-side XSLT comparison.
    if _is_compare_xslt_request(user_message) and session is not None:
        _progress("Comparing XSLT mappings as you asked…")
        if len(session.xslt_revisions) >= 2:
            # Default: compare the two most recent consecutive revisions so the
            # diff reflects the latest change, not the entire history.
            rev_old = session.xslt_revisions[-2]
            rev_new = session.xslt_revisions[-1]
            rev_comp = session.compare_revisions(rev_old.id, rev_new.id)
            comp_text = (
                "## XSLT Revision Comparison\n"
                f"{rev_comp.get('summary', '')}\n"
                f"Previous: `{rev_old.timestamp}` | Latest: `{rev_new.timestamp}`"
            )
            return {
                "route": {"scores": {}, "active_intents": ["compare"], "primary": "compare", "is_multi": False, "threshold_used": 0.45},
                "responses": {"compare": comp_text},
                "primary_response": comp_text,
                "agent": None,
                "ingested": xslt_ingested,
                "audit_dict": None,
                "patched_xslt": None,
                "generated_xslt": None,
                "updated_xslt": None,
                "change_summary": "",
                "comparison_data": None,
                "latest_version_path": "",
                "test_readiness_status": "",
                "simulate_output": None,
                "modify_file_used": (xslt_ingested or {}).get("metadata", {}).get("filename", ""),
                "source_file_used": (source_ingested or {}).get("metadata", {}).get("filename", ""),
                "target_file_used": (target_ingested or {}).get("metadata", {}).get("filename", ""),
                "target_match_status": "no_target",
                "target_match_summary": "",
                "missing_target_segments": [],
                "extra_output_segments": [],
                "mismatched_fields": [],
                "autofix_suggestions": [],
                "simulate_audit_findings": [],
                "xslt_compare_data": {
                    "risk_level": "info",
                    "diff_preview": "\n".join(rev_comp.get("diff_lines", [])[:240]),
                    "added_segments_in_revised": [],
                    "missing_segments_in_revised": [],
                    "mapping_divergence": [],
                },
                "session": session,
                "primary_file_name": (xslt_ingested or {}).get("metadata", {}).get("filename", ""),
            }
        xslt_files = [
            f for f in session.ingested_files
            if f.get("metadata", {}).get("file_type") == "XSLT"
        ]
        if len(xslt_files) >= 2:
            comp = _compare_two_xslts(xslt_files[-2], xslt_files[-1])
            return {
                "route": {"scores": {}, "active_intents": ["compare"], "primary": "compare", "is_multi": False, "threshold_used": 0.45},
                "responses": {"compare": comp["text"]},
                "primary_response": comp["text"],
                "agent": None,
                "ingested": xslt_files[-1],
                "audit_dict": None,
                "patched_xslt": None,
                "generated_xslt": None,
                "updated_xslt": None,
                "change_summary": "",
                "comparison_data": None,
                "latest_version_path": "",
                "test_readiness_status": "",
                "simulate_output": None,
                "modify_file_used": xslt_files[-1].get("metadata", {}).get("filename", ""),
                "source_file_used": (source_ingested or {}).get("metadata", {}).get("filename", ""),
                "target_file_used": (target_ingested or {}).get("metadata", {}).get("filename", ""),
                "target_match_status": "no_target",
                "target_match_summary": "",
                "missing_target_segments": [],
                "extra_output_segments": [],
                "mismatched_fields": [],
                "autofix_suggestions": [],
                "simulate_audit_findings": [],
                "xslt_compare_data": comp,
                "session": session,
                "primary_file_name": xslt_files[-1].get("metadata", {}).get("filename", ""),
            }

    if forced_action in {"modify", "simulate", "explain", "generate", "audit"}:
        _progress(
            f"Picked **{forced_action}** from keywords in your message — "
            "skipping broad intent routing."
        )
        route_result = {
            "scores": {
                "explain":  1.0 if forced_action == "explain"  else 0.0,
                "generate": 1.0 if forced_action == "generate" else 0.0,
                "modify":   1.0 if forced_action == "modify"   else 0.0,
                "simulate": 1.0 if forced_action == "simulate" else 0.0,
                "audit":    1.0 if forced_action == "audit"    else 0.0,
            },
            "reasoning": {"forced": "pattern classifier"},
            "active_intents": [forced_action],
            "primary": forced_action,
            "is_multi": False,
            "threshold_used": 0.45,
            "needs_rag":   False,   # pattern-classified actions never need cross-file RAG
            "is_in_scope": True,    # pattern-classified actions are always in-scope
        }
    else:
        _progress(
            "Analyzing your message to choose explain, modify, simulate, audit, "
            "or generate (intent routing)…"
        )
        # Intent routing always uses Groq (llama-3.1-8b-instant) for speed and cost
        # efficiency — classification doesn't require the stronger OpenAI models.
        # Fall back to the active provider's key if no Groq key is configured.
        _groq_key = os.getenv("GROQ_API_KEY")
        if _groq_key:
            route_result = route(
                user_message,
                api_key=_groq_key,
                provider="groq",
                model=os.getenv("INTENT_ROUTER_MODEL", "llama-3.1-8b-instant"),
            )
        else:
            route_result = route(
                user_message,
                api_key=api_key,
                provider=provider or "openai",
                model=os.getenv("INTENT_ROUTER_MODEL"),
            )

    # ── 5a. Post-classification out-of-scope check ────────────────────────────
    # The intent router LLM decides is_in_scope, so this runs after routing.
    # Bypassed entirely when: a file is already loaded (#1), a new file was just
    # uploaded, or the LLM classified the message as in-scope.
    if (
        not _has_loaded_file
        and not _has_new_files
        and not route_result.get("is_in_scope", True)
    ):
        _audit_log_event(
            actor=_actor,
            action="dispatch",
            target="out_of_scope",
            status="success",
            duration_ms=int((time.time() - _t0) * 1000),
            why="llm_out_of_scope_guardrail",
            metadata={"user_message_chars": len(user_message)},
        )
        return {
            "route":             {"scores": {}, "active_intents": ["out_of_scope"],
                                  "primary": "out_of_scope", "is_multi": False,
                                  "threshold_used": 0.45},
            "responses":         {"out_of_scope": _OUT_OF_SCOPE_RESPONSE},
            "primary_response":  _OUT_OF_SCOPE_RESPONSE,
            "agent":             None,
            "ingested":          ingested,
            "audit_dict":        None,
            "patched_xslt":      None,
            "session":           session,
            "primary_file_name": "",
        }

    # ── 5b. RAG context — query only when the router flagged needs_rag=True ─────
    if _rag_deferred and route_result.get("needs_rag", False):
        _progress("Searching your indexed mappings for cross-file reference snippets…")
        try:
            _rag_text, _ = query_folder(
                question=user_message,
                persist_dir=str(_index_dir),
                top_k=3,
                api_key=api_key,
                model=resolved_model,
            )
            if _rag_text and "[no results]" not in _rag_text.lower():
                _ctx_prefix = (
                    f"[REFERENCE CONTEXT FROM SIMILAR MAPPINGS]\n{_rag_text}\n\n"
                    + _ctx_prefix
                )
        except Exception:
            pass   # RAG failure is non-fatal — continue without it

    _primary_intent = route_result.get("primary", "explain")
    _engine_step_msg = {
        "explain": "Explaining your mapping (main model — may take a little while)…",
        "modify": "Updating the XSLT — locating the right code and applying your edits…",
        "simulate": "Running your transform and interpreting the output…",
        "audit": "Checking the mapping — automated rules plus AI scan…",
        "generate": "Generating a new XSLT from your instructions…",
        "compare": "Finishing comparison output…",
    }.get(_primary_intent, "Working on your request…")
    _progress(_engine_step_msg)

    # ── 6. Dispatch to each active engine in priority order ───────────────────
    responses: Dict[str, str] = {}
    agent: Any = None
    audit_dict: Optional[Dict] = None
    patched_xslt: Optional[str] = None
    generated_xslt: Optional[str] = None
    simulate_output: Optional[str] = None
    updated_xslt: Optional[str] = None
    change_summary: str = ""
    comparison_data: Optional[Dict[str, Any]] = None
    latest_version_path: str = ""
    test_readiness_status: str = "no revised XSLT available"
    modify_file_used: str = (xslt_ingested or {}).get("metadata", {}).get("filename", "")
    source_file_used: str = (source_ingested or {}).get("metadata", {}).get("filename", "")
    target_file_used: str = (target_ingested or {}).get("metadata", {}).get("filename", "")
    target_match_status: str = "no_target"
    target_match_summary: str = "No target file selected for comparison."
    missing_target_segments: List[str] = []
    extra_output_segments: List[str] = []
    mismatched_fields: List[Dict[str, Any]] = []
    autofix_suggestions: List[Dict[str, Any]] = []
    simulate_audit_findings: List[Dict[str, Any]] = []
    modify_status: str = "completed"
    modify_guidance: Dict[str, Any] = {}
    status: str = ""

    # Re-use the existing FileAgent from session if explain was run before,
    # so the full conversation history inside the agent is preserved.
    if session is not None and session.agent is not None:
        agent = session.agent

    for intent in route_result["active_intents"]:

        if intent == "explain":
            if xslt_ingested is None:
                responses[intent] = (
                    "[explain] No active XSLT file selected."
                )
            elif agent is not None:
                # Session already has a live FileAgent — continue that conversation.
                # Prepend any RAG context so cross-file answers are grounded.
                _explain_q = _build_explain_prompt_with_roles(
                    user_message=user_message,
                    source_ingested=source_ingested,
                    target_ingested=target_ingested,
                )
                response = agent.chat(_ctx_prefix + _explain_q if _ctx_prefix else _explain_q)
                responses[intent] = response
            else:
                _xidx = (
                    session.get_xslt_index(resolved_xslt_path)
                    if session is not None else None
                )
                _explain_q = _build_explain_prompt_with_roles(
                    user_message=user_message,
                    source_ingested=source_ingested,
                    target_ingested=target_ingested,
                )
                response, agent = explain(
                    xslt_ingested,
                    question=_ctx_prefix + _explain_q if _ctx_prefix else _explain_q,
                    api_key=api_key,
                    model=_engine_model("explain"),
                    provider=provider or "openai",
                    xslt_index=_xidx,
                )
                responses[intent] = response
            modify_file_used = (xslt_ingested or {}).get("metadata", {}).get("filename", "")

        elif intent == "simulate":
            if xslt_ingested is None:
                responses[intent] = (
                    "[simulate] No active XSLT file selected for simulation."
                )
            else:
                msg = _ctx_prefix + user_message

                resolved_source = resolved_source_path
                if not resolved_source or not Path(resolved_source).exists():
                    responses[intent] = (
                        "[simulate] No active source file selected for simulation."
                    )
                    continue

                simulate_ingested = xslt_ingested
                revision_used = ""
                if session is not None:
                    latest_rev = session.get_latest_xslt()
                    if latest_rev is not None:
                        try:
                            _uploads = Path(__file__).resolve().parent.parent / "data" / "uploads"
                            _uploads.mkdir(parents=True, exist_ok=True)
                            _sid = getattr(session, "session_id", "session")
                            _rev_path = _uploads / f"{_sid}_latest_revision.xml"
                            _rev_path.write_text(latest_rev.content, encoding="utf-8")
                            simulate_ingested = ingest_file(file_path=str(_rev_path))
                            revision_used = f"Using revision {len(session.xslt_revisions)} ({latest_rev.timestamp})"
                        except Exception:
                            simulate_ingested = xslt_ingested

                response, simulate_output = simulate(
                    simulate_ingested,
                    source_file=resolved_source,
                    api_key=api_key,
                    model=_engine_model("simulate"),
                    provider=provider or "openai",
                    session=session,
                    conversation_context=msg,
                )
                if (response or "").lstrip().startswith("[validation_only]"):
                    status = "validation_only"
                    response = (response or "").replace("[validation_only]\n", "", 1)
                target_text = None
                if target_ingested is not None:
                    target_parsed = target_ingested.get("parsed_content", {})
                    target_text = target_parsed.get("raw_xml") or target_parsed.get("raw_text")
                    target_file_used = target_ingested.get("metadata", {}).get("filename", "")
                comparison = compare_output_to_target(simulate_output, target_text)
                target_match_status = comparison.get("target_match_status", "no_target")
                target_match_summary = comparison.get("target_match_summary", "")
                missing_target_segments = comparison.get("missing_target_segments", [])
                extra_output_segments = comparison.get("extra_output_segments", [])
                mismatched_fields = comparison.get("mismatched_fields", [])
                raw_xslt_for_fix = (simulate_ingested.get("parsed_content") or {}).get("raw_xml") or ""
                autofix_suggestions = generate_autofix_suggestions(comparison, raw_xslt_for_fix)
                simulate_audit_findings = audit_simulate_findings(
                    comparison,
                    raw_xslt_for_fix,
                    output_xml=simulate_output or "",
                    target_text=target_text or "",
                )

                match_line = ""
                if target_match_status == "matches_target":
                    match_line = "\n\n## Target Comparison\nStatus: **matches the target**\n"
                elif target_match_status == "partial_match":
                    match_line = "\n\n## Target Comparison\nStatus: **partially matches the target**\n"
                elif target_match_status == "does_not_match":
                    match_line = "\n\n## Target Comparison\nStatus: **does not match the target**\n"
                elif target_match_status == "no_output":
                    match_line = "\n\n## Target Comparison\nStatus: **comparison unavailable (no real output)**\n"
                else:
                    match_line = "\n\n## Target Comparison\nStatus: **no target file selected**\n"
                match_line += f"{target_match_summary}\n"
                if revision_used:
                    response = f"{response}\n\n---\n{revision_used}"
                responses[intent] = response + match_line
                source_file_used = Path(resolved_source).name
                modify_file_used = (simulate_ingested or {}).get("metadata", {}).get("filename", "")

        elif intent == "modify":
            if xslt_ingested is None:
                responses[intent] = (
                    "[modify] No active XSLT file selected for modification."
                )
            else:
                msg = _ctx_prefix + user_message
                _modify_idx = (
                    session.get_xslt_index(resolved_xslt_path)
                    if session is not None else None
                )
                response, patched_xslt = modify(
                    xslt_ingested,
                    modification_request=msg,
                    api_key=api_key,
                    model=_engine_model("modify"),
                    provider=provider or "openai",
                    xslt_index=_modify_idx,
                )
                modify_guidance = extract_modify_guidance(response)
                if modify_guidance.get("status"):
                    modify_status = str(modify_guidance.get("status"))
                modify_file_used = (xslt_ingested or {}).get("metadata", {}).get("filename", "")
                if source_ingested is not None:
                    source_file_used = source_ingested.get("metadata", {}).get("filename", "")
                if target_ingested is not None:
                    target_file_used = target_ingested.get("metadata", {}).get("filename", "")
                updated_xslt = None
                try:
                    from .modification_engine import _parse_patch
                    from .xslt_revision_store import XsltRevisionStore, build_comparison

                    patch = _parse_patch(response)
                    change_summary = patch.get("summary", "")

                    full_raw_xslt = (xslt_ingested.get("parsed_content") or {}).get("raw_xml") or ""
                    if patched_xslt and full_raw_xslt:
                        if patched_xslt == full_raw_xslt:
                            test_readiness_status = "modify produced no real change; no revised XSLT was saved"
                        else:
                            meta = xslt_ingested.get("metadata", {})
                            source_path_for_revision = meta.get("source_path") or ""
                            file_name_for_revision = meta.get("filename", "mapping.xml")

                            if source_path_for_revision and Path(source_path_for_revision).exists():
                                store = XsltRevisionStore(
                                    Path(__file__).resolve().parent.parent / "data" / "revisions"
                                )
                                rev = store.save_revision(
                                    source_path=source_path_for_revision,
                                    filename=file_name_for_revision,
                                    xslt_text=patched_xslt,
                                    change_summary=change_summary or "Updated XSLT revision",
                                )
                                if session is not None:
                                    session.save_xslt_revision(
                                        content=patched_xslt,
                                        description=change_summary or msg,
                                    )
                                latest_version_path = rev.latest_version_path
                                updated_xslt = patched_xslt
                                comparison_data = build_comparison(full_raw_xslt, patched_xslt)

                                sample_source_available = False
                                if session is not None:
                                    for _f in reversed(session.ingested_files):
                                        _ftype = _f.get("metadata", {}).get("file_type", "")
                                        _fpath = _f.get("metadata", {}).get("source_path", "")
                                        if _ftype != "XSLT" and _fpath and Path(_fpath).exists():
                                            sample_source_available = True
                                            break
                                test_readiness_status = (
                                    "ready" if sample_source_available else "ready when sample XML is available"
                                )
                            else:
                                test_readiness_status = (
                                    "revised XSLT generated, but original source path was unavailable"
                                )
                    elif patched_xslt:
                        test_readiness_status = (
                            "revised XSLT generated, but no original XSLT was available for comparison"
                        )
                except Exception as exc:
                    test_readiness_status = f"revision persistence warning: {exc}"
                responses[intent] = response
                # Auto-audit: run against the patched XSLT when available so
                # the audit reflects the actual change, not the original file.
                _audit_ingested = xslt_ingested
                if patched_xslt:
                    _audit_ingested = {
                        **xslt_ingested,
                        "parsed_content": {
                            **(xslt_ingested.get("parsed_content") or {}),
                            "raw_xml": patched_xslt,
                        },
                    }
                _audit_resp, _ = audit(
                    _audit_ingested,
                    context=response,
                    conversation_context=msg,
                    api_key=api_key,
                    model=_engine_model("audit"),
                )
                responses[intent] += f"\n\n---\n## AUTO-AUDIT\n{_audit_resp}"

        elif intent == "generate":
            msg = _ctx_prefix + user_message
            response, generated_xslt = generate(
                generation_request=msg,
                source_sample=resolved_source_path,
                api_key=api_key,
                model=_engine_model("generate"),
            )
            responses[intent] = response
            # After generate, register the new XSLT in the session so that
            # subsequent simulate/explain/modify calls use the generated file.
            if generated_xslt and session is not None:
                _uploads = Path(__file__).resolve().parent.parent / "data" / "uploads"
                _uploads.mkdir(parents=True, exist_ok=True)
                _sid = getattr(session, "session_id", "session")
                _gen_path = _uploads / f"{_sid}_generated.xml"
                try:
                    _gen_path.write_text(generated_xslt, encoding="utf-8")
                    try:
                        from .file_ingestion import ingest_file as _ingest
                    except ImportError:
                        from file_ingestion import ingest_file as _ingest
                    _gen_ing = _ingest(file_path=str(_gen_path))
                    session.add_file(_gen_ing)
                    updated_xslt = generated_xslt
                except Exception:
                    pass   # non-fatal — caller can still download the XSLT
            # Auto-audit: append audit findings to the generated XSLT
            if xslt_ingested is not None:
                _audit_resp, _ = audit(
                    xslt_ingested,
                    context=response,
                    conversation_context=(_ctx_prefix + user_message).strip() or None,
                    api_key=api_key,
                    model=_engine_model("audit"),
                )
                responses[intent] += f"\n\n---\n## AUTO-AUDIT\n{_audit_resp}"

        elif intent == "audit":
            if xslt_ingested is None:
                responses[intent] = (
                    "[audit] No active XSLT file selected."
                )
            else:
                _audit_focus = (_ctx_prefix + user_message).strip()
                response, audit_dict = audit(
                    xslt_ingested,
                    conversation_context=_audit_focus or None,
                    api_key=api_key,
                    model=_engine_model("audit"),
                )
                responses[intent] = response
                modify_file_used = (xslt_ingested or {}).get("metadata", {}).get("filename", "")

    primary = route_result["primary"]
    primary_response = responses.get(primary, "")

    # ── 7. Update session with this turn's results ────────────────────────────
    if session is not None:
        # ingested is already registered via session.add_file() above for new
        # files; only update the primary alias here for files loaded via the
        # legacy ingested= kwarg path (which bypasses add_file).
        if xslt_ingested is not None and xslt_ingested not in session.ingested_files:
            session.add_file(xslt_ingested)
        session.ingested = xslt_ingested or session.ingested
        if agent is not None:
            session.agent = agent
        session.add_turn(primary, user_message, primary_response)

    _primary_file_name = (
        (xslt_ingested or {}).get("metadata", {}).get("filename", "")
    )

    result = {
        "route":              route_result,
        "responses":          responses,
        "primary_response":   primary_response,
        "agent":              agent,
        "ingested":           xslt_ingested,
        "audit_dict":         audit_dict,
        "patched_xslt":       patched_xslt,
        "generated_xslt":     generated_xslt,
        "updated_xslt":       updated_xslt,
        "change_summary":     change_summary,
        "comparison_data":    comparison_data,
        "latest_version_path": latest_version_path,
        "test_readiness_status": test_readiness_status,
        "simulate_output":    simulate_output,
        "modify_file_used":   modify_file_used or _primary_file_name,
        "source_file_used":   source_file_used or (source_ingested or {}).get("metadata", {}).get("filename", ""),
        "target_file_used":   target_file_used or (target_ingested or {}).get("metadata", {}).get("filename", ""),
        "target_match_status": target_match_status,
        "target_match_summary": target_match_summary,
        "missing_target_segments": missing_target_segments,
        "extra_output_segments": extra_output_segments,
        "mismatched_fields": mismatched_fields,
        "autofix_suggestions": autofix_suggestions,
        "simulate_audit_findings": simulate_audit_findings,
        "xslt_compare_data": None,
        "status": status,
        "modify_status": modify_status,
        "modify_guidance": modify_guidance,
        "session":            session,
        "primary_file_name":  _primary_file_name,
        "token_usage":        get_tracker().summary(),
    }

    _audit_log_event(
        actor=_actor,
        action="dispatch",
        target=route_result.get("primary", "unknown"),
        status="success",
        duration_ms=int((time.time() - _t0) * 1000),
        why="user_request",
        metadata={
            "active_intents": route_result.get("active_intents", []),
            "primary": route_result.get("primary"),
            "file_path": file_path,
            "file_paths": file_paths,
            "primary_file_name": _primary_file_name,
        },
    )
    return result


def dispatch_folder(
    user_message: str,
    folder_path: str,
    persist_dir: str = ".rag_index",
    force_reindex: bool = False,
    top_k: int = 5,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """
    Index a folder of mapping files (if not already indexed) and answer a
    cross-file question using RAG.

    This is the multi-file equivalent of dispatch(). It does not perform
    single-file intent routing — it always uses the RAG engine.

    Args:
        user_message:   The user's question about the folder of mappings.
        folder_path:    Path to folder containing mapping files to index/query.
        persist_dir:    Directory where the ChromaDB index is persisted.
        force_reindex:  If True, re-index all files even if already indexed.
        top_k:          Number of chunks to retrieve from the index (default 5).
        api_key:        LLM API key. Falls back to the provider's env var.
        model:          LLM model. Falls back to the provider's env var,
                        then the provider's default model.

    Returns:
        {
          "responses":        {"rag": response_str}
          "primary_response": response_str
          "agent":            None  (RAG is stateless)
          "ingested":         None  (multi-file, not a single ingested dict)
          "index_result":     {"indexed": N, "skipped": M, "errors": [...]}
        }

    Raises:
        ValueError: If user_message is empty, folder_path invalid, or no API key.
    """
    try:
        from .rag_engine import index_folder, query_folder
    except ImportError:
        from rag_engine import index_folder, query_folder  # type: ignore

    try:
        from .llm_client import get_default_model as _gdm
    except ImportError:
        from llm_client import get_default_model as _gdm  # type: ignore
    resolved_model = model or _gdm(provider or "openai")

    index_result = index_folder(
        folder_path=folder_path,
        persist_dir=persist_dir,
        force_reindex=force_reindex,
    )

    response, _ = query_folder(
        question=user_message,
        persist_dir=persist_dir,
        top_k=top_k,
        api_key=api_key,
        model=resolved_model,
    )

    return {
        "responses":        {"rag": response},
        "primary_response": response,
        "agent":            None,
        "ingested":         None,
        "index_result":     index_result,
    }


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("\n" + "=" * 80)
    print("  DISPATCHER — Conversational Mapping Intelligence Agent")
    print("=" * 80 + "\n")

    file_path   = sys.argv[1] if len(sys.argv) > 1 else None
    source_file = sys.argv[2] if len(sys.argv) > 2 else None

    if file_path:
        if not Path(file_path).exists():
            print(f"[ERROR] File not found: {file_path}\n")
            sys.exit(1)
        print(f"[MAPPING FILE] {file_path}")
        if source_file:
            if not Path(source_file).exists():
                print(f"[ERROR] Source file not found: {source_file}\n")
                sys.exit(1)
            print(f"[SOURCE FILE ] {source_file}")
        print()
    else:
        print("[INFO] No file provided — intent routing only.\n")
        print("       Usage: python modules/dispatcher.py [mapping_file] [source_file]\n")

    print("[CHAT] Type your message. Type 'quit' or 'exit' to stop.\n")
    print("=" * 80 + "\n")

    current_agent = None
    current_ingested = None

    while True:
        try:
            user_input = input("You: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["quit", "exit", "q"]:
                print("\nGoodbye!\n")
                break

            # If agent already loaded from a previous explain, use it for
            # conversational follow-ups.  When the user types an action keyword
            # (modify/simulate/audit/generate) fall through to full dispatch so
            # the right engine is used.
            if current_agent is not None:
                _cli_action = _classify_action(user_input)
                if _cli_action not in {"modify", "simulate", "generate", "audit"}:
                    reply = current_agent.chat(user_input)
                    print(f"\nAgent: {reply}\n")
                    print("-" * 80 + "\n")
                    continue
                # Action keyword detected — reset agent and dispatch normally
                current_agent = None

            # First message — run full dispatch
            result = dispatch(
                user_message=user_input,
                file_path=file_path,
                source_file=source_file,
                ingested=current_ingested,
            )

            # Cache ingested for subsequent turns
            if result["ingested"] is not None:
                current_ingested = result["ingested"]

            # Print routing summary
            r = result["route"]
            scores = r["scores"]
            print(f"\n[ROUTE] primary={r['primary'].upper()}  "
                  f"multi={r['is_multi']}  "
                  f"active={r['active_intents']}")
            print(f"        scores: "
                  + "  ".join(f"{k}={v:.2f}" for k, v in scores.items()))
            print()

            # Print response(s)
            for intent, response in result["responses"].items():
                label = intent.upper()
                print(f"[{label}]")
                print("-" * 80)
                print(response)
                print()

            # If explain ran, keep the agent for follow-up turns
            if result["agent"] is not None:
                current_agent = result["agent"]
                print("[CHAT] Agent is loaded. Follow-up questions go directly to the agent.\n")

            print("-" * 80 + "\n")

        except (KeyboardInterrupt, EOFError):
            print("\n\nGoodbye!\n")
            break
        except Exception as e:
            print(f"\n[ERROR] {e}\n")
            import traceback
            traceback.print_exc()
