"""
Groq Agent Module

Provides the stateless explain() function that intent_router dispatches to
when the primary intent is 'explain'. Wraps FileAgent for one-shot use while
keeping multi-turn conversations available via the returned FileAgent instance.
"""

import os
from pathlib import Path
from typing import Any, Optional, Tuple
from dotenv import load_dotenv

# Load .env from the module directory or one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break


def explain(
    ingested: dict,
    question: Optional[str] = None,
    api_key: Optional[str] = None,
    model: str = "llama-3.3-70b-versatile",
    provider: str = "groq",
) -> Tuple[str, Any]:
    """
    Explain a parsed file, optionally answering a specific question.

    This is the callable entry point referenced by intent_router's INTENT_META:
        "next_module": "groq_agent.explain()"

    Args:
        ingested:  Output dict from file_ingestion.ingest_file().
        question:  Optional follow-up question to answer after the initial
                   explanation. If None, only the initial explanation is returned.
        api_key:   Groq API key. Falls back to GROQ_API_KEY env var.
        model:     Groq model identifier.

    Returns:
        A (response, agent) tuple where:
          - response  is the explanation string (or the answer to `question`
                      if one was provided).
          - agent     is the live FileAgent instance with full conversation
                      history, ready for follow-up chat() calls.

    Raises:
        TypeError:  If ingested is not a dict.
        ValueError: If ingested is missing required keys or no API key is found.

    Example::

        from modules.file_ingestion import ingest_file
        from modules.groq_agent import explain

        ingested = ingest_file(file_path="MappingData/MappingData/850_IN_Graybar/Graybar_850_XSLT.xml")
        response, agent = explain(ingested)
        print(response)

        # Continue the conversation
        follow_up = agent.chat("What are the hardcoded customer account numbers?")
        print(follow_up)
    """
    try:
        from .file_agent import FileAgent
        from .llm_client import PROVIDERS, DEFAULT_MODELS
    except ImportError:
        from file_agent import FileAgent  # type: ignore
        from llm_client import PROVIDERS, DEFAULT_MODELS  # type: ignore

    env_key_name = PROVIDERS.get(provider, {}).get("env_key", "GROQ_API_KEY")
    key = api_key or os.environ.get(env_key_name) or os.environ.get("GROQ_API_KEY")
    if not key:
        raise ValueError(f"API key required for provider {provider!r}.")

    resolved_model = model if model != "llama-3.3-70b-versatile" else DEFAULT_MODELS.get(provider, model)
    agent = FileAgent(api_key=key, model=resolved_model, provider=provider)
    response = agent.load_file(ingested)

    if question:
        response = agent.chat(question)

    return response, agent


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Import file_ingestion from same package directory
    try:
        from file_ingestion import ingest_file
    except ImportError:
        print("[ERROR] Could not import file_ingestion module")
        print("        Run from the project root:")
        print("        python modules/groq_agent.py [file_path]")
        sys.exit(1)

    print("\n" + "=" * 80)
    print("  GROQ AGENT MODULE — Explain Engine Test")
    print("=" * 80 + "\n")

    if len(sys.argv) < 2:
        # Auto-discover from test_files/ or MappingData/
        test_files_dir = Path(__file__).parent.parent / "test_files"
        mapping_dir = Path(__file__).parent.parent / "MappingData" / "MappingData"

        candidates = []
        if test_files_dir.exists():
            for ext in ["*.edi", "*.x12", "*.edifact", "*.xml", "*.xsd", "*.xsl", "*.xslt"]:
                candidates.extend(test_files_dir.glob(ext))

        # Prefer a real XSLT mapping file for a richer demo
        if mapping_dir.exists() and not candidates:
            for xslt_file in mapping_dir.rglob("*.xml"):
                candidates.append(xslt_file)
                break

        if not candidates:
            print("[ERROR] No test files found. Provide a file path:")
            print("        python modules/groq_agent.py path/to/file\n")
            sys.exit(1)

        file_path = str(candidates[0])
        print(f"[DEMO] Auto-selected: {candidates[0].name}\n")
        print(f"       Usage: python modules/groq_agent.py <path_to_file>\n")
    else:
        file_path = sys.argv[1]
        if not Path(file_path).exists():
            print(f"[ERROR] File not found: {file_path}\n")
            sys.exit(1)

    print(f"[FILE] Ingesting: {file_path}\n")
    print("-" * 80 + "\n")

    try:
        ingested = ingest_file(file_path=file_path)

        print("[OK] Ingestion complete")
        print(f"     Type    : {ingested['metadata']['file_type']}")
        print(f"     Version : {ingested['metadata']['detected_version']}")
        print(f"     Status  : {ingested['metadata']['parse_status']}\n")

        if ingested["metadata"].get("parse_error"):
            print(f"[WARN] Parse error: {ingested['metadata']['parse_error']}\n")

        print("[AGENT] Running explain()...\n")
        print("-" * 80 + "\n")

        response, agent = explain(ingested)

        print("[EXPLANATION]")
        print("-" * 80)
        print(response)
        print("\n" + "-" * 80 + "\n")

        # Interactive follow-up
        print("[CHAT] Ask follow-up questions. Type 'quit' or 'exit' to stop.\n")
        print("=" * 80 + "\n")

        while True:
            try:
                user_input = input("You: ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ["quit", "exit", "q"]:
                    print("\nGoodbye!\n")
                    break

                reply = agent.chat(user_input)
                print(f"\nAgent: {reply}\n")
                print("-" * 80 + "\n")

            except KeyboardInterrupt:
                print("\n\nGoodbye!\n")
                break
            except Exception as e:
                print(f"\n[ERROR] {e}\n")

    except Exception as e:
        print(f"[ERROR] {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)
