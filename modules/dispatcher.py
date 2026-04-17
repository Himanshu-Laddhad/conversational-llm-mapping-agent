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

Currently implemented:   explain  → groq_agent.explain()
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
from pathlib import Path
from typing import Any, Dict, List, Optional
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

# ── Out-of-scope guardrail ────────────────────────────────────────────────────
# The agent is purpose-built for EDI / XSLT / MapForce mapping intelligence.
# Any message that clearly contains none of these domain signals is rejected
# with a friendly redirect — without spending LLM tokens on routing.

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

# Substring keywords — matched anywhere in the lowercased message.
_IN_SCOPE_SUBSTRINGS: tuple[str, ...] = (
    "xslt", "xsl:", "stylesheet", "transformation", "transform",
    "edi", "x12", "edifact", "flatfile", "flat-file", "flat file",
    "mapping", "mapforce", "altova", "trading partner",
    "xml", "d365", "dynamics",
    "810", "850", "856", "855", "997", "940", "945",
    "simulate", "simulation", "audit", "segment", "value-of",
    "apply-template", "partnerlinq", "source file", "target file",
    "invoice", "purchase order", "ship notice",
)

# Whole-word keywords — only match when surrounded by word boundaries
# (avoids "list" matching "st", "sort" matching "or", etc.)
_IN_SCOPE_WORDS: tuple[str, ...] = (
    "isa", "gs", "ge", "iea", "beg", "big", "dtm",
    "n1", "n3", "n4", "it1", "tds", "ctt", "bsn",
    "xsd", "erp", "sap", "edi", "xml", "xsl",
    "explain", "modify", "generate",
    "template", "segment", "mapping", "loop", "field",
)

_WORD_PATTERN: re.Pattern = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _IN_SCOPE_WORDS) + r")\b",
    re.IGNORECASE,
)


def _is_in_scope(user_message: str) -> bool:
    """
    Return True if the message contains at least one EDI/XSLT domain signal.
    Uses substring matching for long/unambiguous terms and whole-word matching
    for short EDI keywords (ISA, GS, ST…) to avoid false positives.
    No LLM tokens spent.
    """
    lower = user_message.lower()
    # Fast substring check for unambiguous long keywords
    if any(kw in lower for kw in _IN_SCOPE_SUBSTRINGS):
        return True
    # Whole-word check for short EDI codes that could appear inside other words
    if _WORD_PATTERN.search(user_message):
        return True
    return False


def dispatch(
    user_message: str,
    file_path: Optional[str] = None,
    file_paths: Optional[List[str]] = None,
    source_file: Optional[str] = None,
    ingested: Optional[dict] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    session: Optional[Any] = None,
    provider: Optional[str] = None,   # accepted for forward-compat; currently Groq only
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
        ingested:     Pre-parsed dict from ingest_file(). If provided,
                      file_path and file_paths are ignored.
        api_key:      Groq API key. Falls back to GROQ_API_KEY env var.
        model:        Groq model used for all LLM calls. Falls back to
                      GROQ_MODEL env var, then llama-3.3-70b-versatile.
        session:      Optional Session instance (from modules.session).
                      When provided:
                        - all uploaded files are tracked across turns
                        - the most relevant file is selected per turn
                        - conversation history is injected into prompts
                        - RAG context is auto-injected from .rag_index/ if present
                      Pass the same Session object on every subsequent call
                      to maintain memory across turns.

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
        ValueError: If no Groq API key is available.
        FileNotFoundError: If a provided file path does not exist.
    """
    _t0 = time.time()
    _actor = getattr(session, "session_id", None) if session is not None else None
    _actor = str(_actor) if _actor else "system"
    try:
        from .intent_router import route
        from .file_ingestion import ingest_file
        from .groq_agent import explain
        from .simulation_engine import simulate
        from .modification_engine import modify
        from .xslt_generator import generate
        from .audit_engine import audit
        from .rag_engine import query_folder
    except ImportError:
        from intent_router import route          # fallback for standalone execution
        from file_ingestion import ingest_file
        from groq_agent import explain
        from simulation_engine import simulate
        from modification_engine import modify
        from xslt_generator import generate
        from audit_engine import audit           # type: ignore
        from rag_engine import query_folder      # type: ignore

    # ── Resolve model (caller > env var > default) ────────────────────────────
    resolved_model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # ── 1. Ingest new files ───────────────────────────────────────────────────
    # Merge legacy file_path with new file_paths list, deduplicate
    _all_new_paths: List[str] = list(file_paths or [])
    if file_path and file_path not in _all_new_paths:
        _all_new_paths.append(file_path)

    if ingested is None:
        for p in _all_new_paths:
            _new = ingest_file(file_path=p)
            if session is not None:
                session.add_file(_new)
            ingested = _new   # last ingested = primary for this turn

    # Fall back to session's most-relevant file when no new files uploaded
    if ingested is None and session is not None:
        ingested = session.get_primary_ingested(user_message)

    # ── 2. Build context prefix from session history ──────────────────────────
    _ctx_prefix = ""
    if session is not None and session.history:
        ctx = session.get_context_str()
        if ctx:
            _ctx_prefix = f"[PRIOR CONTEXT]\n{ctx}\n\n[CURRENT REQUEST]\n"

    # ── 3. Auto-inject RAG context if index exists ────────────────────────────
    _index_dir = Path(__file__).resolve().parent.parent / ".rag_index"
    if _index_dir.exists():
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

    # ── 4. Out-of-scope guardrail ─────────────────────────────────────────────
    # Reject questions clearly outside the EDI/XSLT domain before routing.
    # Skip the check if a file was just uploaded (the upload itself is in-scope).
    _has_new_files = bool(_all_new_paths) or (ingested is not None and not session)
    if not _has_new_files and not _is_in_scope(user_message):
        _audit_log_event(
            actor=_actor,
            action="dispatch",
            target="out_of_scope",
            status="success",
            duration_ms=int((time.time() - _t0) * 1000),
            why="out_of_scope_guardrail",
            metadata={"user_message_chars": len(user_message)},
        )
        return {
            "route":              {"scores": {}, "active_intents": ["out_of_scope"],
                                   "primary": "out_of_scope", "is_multi": False,
                                   "threshold_used": 0.45},
            "responses":          {"out_of_scope": _OUT_OF_SCOPE_RESPONSE},
            "primary_response":   _OUT_OF_SCOPE_RESPONSE,
            "agent":              None,
            "ingested":           ingested,
            "audit_dict":         None,
            "patched_xslt":       None,
            "session":            session,
            "primary_file_name":  "",
        }

    # ── 5. Classify user intent ───────────────────────────────────────────────
    route_result = route(user_message, api_key=api_key)

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

    # Re-use the existing FileAgent from session if explain was run before,
    # so the full conversation history inside the agent is preserved.
    if session is not None and session.agent is not None:
        agent = session.agent

    for intent in route_result["active_intents"]:

        if intent == "explain":
            if ingested is None:
                responses[intent] = (
                    "[explain] No file provided. "
                    "Pass a file_path or ingested dict so the agent has something to explain."
                )
            elif agent is not None:
                # Session already has a live FileAgent — continue that conversation
                response = agent.chat(user_message)
                responses[intent] = response
            else:
                response, agent = explain(
                    ingested,
                    question=user_message,
                    api_key=api_key,
                    model=resolved_model,
                )
                responses[intent] = response

        elif intent == "simulate":
            if ingested is None:
                responses[intent] = (
                    "[simulate] No mapping file provided. "
                    "Pass file_path pointing to an XSLT/mapping file."
                )
            else:
                msg = _ctx_prefix + user_message

                # ── Auto-resolve source_file from session ──────────────────────
                # app.py never passes source_file= explicitly.  When a non-XSLT
                # file (D365_XML, X12_EDI, X12_XML, etc.) is in the session we
                # pull its stored disk path so Saxon can execute the real XSLT.
                resolved_source = source_file
                if resolved_source is None and session is not None:
                    _source_types = {"D365_XML", "X12_EDI", "X12_XML",
                                     "EDIFACT", "XSD", "XML"}
                    for _f in reversed(session.ingested_files):
                        _ftype = _f.get("metadata", {}).get("file_type", "")
                        _fpath = _f.get("metadata", {}).get("source_path", "")
                        if _ftype in _source_types and _fpath and Path(_fpath).exists():
                            resolved_source = _fpath
                            break

                response, simulate_output = simulate(
                    ingested,
                    source_file=resolved_source,
                    api_key=api_key,
                    model=resolved_model,
                )
                responses[intent] = response

        elif intent == "modify":
            if ingested is None:
                responses[intent] = (
                    "[modify] No mapping file provided. "
                    "Pass file_path pointing to an XSLT/mapping file to modify."
                )
            else:
                msg = _ctx_prefix + user_message
                # FIX: capture patched_xslt (second return value) — was wrongly
                # discarded as `_mod_agent` in earlier versions.
                response, patched_xslt = modify(
                    ingested,
                    modification_request=msg,
                    api_key=api_key,
                    model=resolved_model,
                )
                updated_xslt = patched_xslt
                try:
                    from .modification_engine import _parse_patch
                    from .xslt_revision_store import XsltRevisionStore, build_comparison

                    patch = _parse_patch(response)
                    change_summary = patch.get("summary", "")

                    full_raw_xslt = (ingested.get("parsed_content") or {}).get("raw_xml") or ""
                    if patched_xslt and full_raw_xslt:
                        comparison_data = build_comparison(full_raw_xslt, patched_xslt)

                        meta = ingested.get("metadata", {})
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
                            latest_version_path = rev.latest_version_path

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
                # Auto-audit: append audit findings to the proposed modification
                _audit_resp, _ = audit(
                    ingested,
                    context=response,
                    api_key=api_key,
                    model=resolved_model,
                )
                responses[intent] += f"\n\n---\n## AUTO-AUDIT\n{_audit_resp}"

        elif intent == "generate":
            msg = _ctx_prefix + user_message
            response, generated_xslt = generate(
                generation_request=msg,
                source_sample=source_file,
                api_key=api_key,
                model=resolved_model,
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
            if ingested is not None:
                _audit_resp, _ = audit(
                    ingested,
                    context=response,
                    api_key=api_key,
                    model=resolved_model,
                )
                responses[intent] += f"\n\n---\n## AUTO-AUDIT\n{_audit_resp}"

        elif intent == "audit":
            if ingested is None:
                responses[intent] = (
                    "[audit] No mapping file provided. "
                    "Pass file_path pointing to an XSLT/mapping file to audit."
                )
            else:
                response, audit_dict = audit(
                    ingested,
                    api_key=api_key,
                    model=resolved_model,
                )
                responses[intent] = response

    primary = route_result["primary"]
    primary_response = responses.get(primary, "")

    # ── 7. Update session with this turn's results ────────────────────────────
    if session is not None:
        # ingested is already registered via session.add_file() above for new
        # files; only update the primary alias here for files loaded via the
        # legacy ingested= kwarg path (which bypasses add_file).
        if ingested is not None and ingested not in session.ingested_files:
            session.add_file(ingested)
        if agent is not None:
            session.agent = agent
        session.add_turn(primary, user_message, primary_response)

    _primary_file_name = (
        ingested.get("metadata", {}).get("filename", "") if ingested else ""
    )

    result = {
        "route":              route_result,
        "responses":          responses,
        "primary_response":   primary_response,
        "agent":              agent,
        "ingested":           ingested,
        "audit_dict":         audit_dict,
        "patched_xslt":       patched_xslt,
        "generated_xslt":     generated_xslt,
        "updated_xslt":       updated_xslt,
        "change_summary":     change_summary,
        "comparison_data":    comparison_data,
        "latest_version_path": latest_version_path,
        "test_readiness_status": test_readiness_status,
        "simulate_output":    simulate_output,
        "session":            session,
        "primary_file_name":  _primary_file_name,
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
        api_key:        Groq API key. Falls back to GROQ_API_KEY env var.
        model:          Groq model. Falls back to GROQ_MODEL env var,
                        then llama-3.3-70b-versatile.

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

    resolved_model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

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

            # If agent already loaded from a previous explain, use it directly
            if current_agent is not None:
                reply = current_agent.chat(user_input)
                print(f"\nAgent: {reply}\n")
                print("-" * 80 + "\n")
                continue

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
