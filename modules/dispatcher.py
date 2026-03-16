"""
dispatcher.py
─────────────
Central dispatch engine for the Conversational Mapping Intelligence Agent.

Accepts a user message and an optional file, routes intent via intent_router,
then calls the appropriate engine for each active intent.

Currently implemented:   explain  → groq_agent.explain()
                         simulate → simulation_engine.simulate()
Stubbed (not yet built): generate → xslt_generator.generate()
                         modify   → modification_engine.modify()

Usage (as module):
    from modules.dispatcher import dispatch

    result = dispatch(
        user_message="What does the BEG segment do?",
        file_path="MappingData/MappingData/850_IN_Graybar/Graybar_850_XSLT.xml",
    )
    print(result["primary_response"])

    # Continue the conversation if explain ran
    if result["agent"]:
        follow_up = result["agent"].chat("What are the hardcoded values?")
        print(follow_up)

Usage (standalone test):
    python modules/dispatcher.py
    python modules/dispatcher.py path/to/file.xml
"""

import os
from pathlib import Path
from typing import Any, Optional
from dotenv import load_dotenv

# Load .env from module directory or one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# Placeholder responses for engines that are not yet built.
# These are shown in the dispatch result instead of crashing.
_UNBUILT = {
    "generate": (
        "[generate] The Generate engine is not yet implemented. "
        "Planned: xslt_generator.generate() — create a new XSLT from requirements."
    ),
    "modify": (
        "[modify] The Modify engine is not yet implemented. "
        "Planned: modification_engine.modify() — edit an existing mapping and produce a diff."
    ),
}


def dispatch(
    user_message: str,
    file_path: Optional[str] = None,
    source_file: Optional[str] = None,
    ingested: Optional[dict] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """
    Route a user message and call the appropriate engine(s).

    Args:
        user_message: The user's natural language request.
        file_path:    Path to a mapping file (XSLT/XSD/XML) to parse.
                      Ignored if ingested is already provided.
        source_file:  Path to the source/input data file for simulate intent
                      (e.g. a D365 XML SourceFile.txt from MappingData).
        ingested:     Pre-parsed dict from ingest_file(). If provided,
                      file_path is ignored.
        api_key:      Groq API key. Falls back to GROQ_API_KEY env var.
        model:        Groq model used for all LLM calls. Falls back to
                      GROQ_MODEL env var, then llama-3.1-8b-instant.

    Returns:
        {
          "route":            Full route() result (scores, reasoning, active_intents,
                              primary, is_multi, threshold_used).
          "responses":        Dict of { intent: response_str } for each active intent.
          "primary_response": Response string from the primary (highest-scoring) intent.
          "agent":            Live FileAgent instance if explain was active, else None.
                              Use agent.chat(msg) for follow-up questions.
          "ingested":         The parsed file dict (None if no file was provided).
        }

    Raises:
        ValueError: If no Groq API key is available.
        FileNotFoundError: If file_path is given but does not exist.
    """
    try:
        from .intent_router import route
        from .file_ingestion import ingest_file
        from .groq_agent import explain
        from .simulation_engine import simulate
    except ImportError:
        from intent_router import route          # fallback for standalone execution
        from file_ingestion import ingest_file
        from groq_agent import explain
        from simulation_engine import simulate

    # ── Resolve model (caller > env var > default) ────────────────────────────
    resolved_model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # ── 1. Ingest file if a path was given and ingested not already provided ──
    if ingested is None and file_path is not None:
        ingested = ingest_file(file_path=file_path)

    # ── 2. Classify user intent ───────────────────────────────────────────────
    route_result = route(user_message, api_key=api_key)

    # ── 3. Dispatch to each active engine in priority order ───────────────────
    responses: dict[str, str] = {}
    agent: Any = None

    for intent in route_result["active_intents"]:

        if intent == "explain":
            if ingested is None:
                responses[intent] = (
                    "[explain] No file provided. "
                    "Pass a file_path or ingested dict so the agent has something to explain."
                )
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
                response, _sim_agent = simulate(
                    ingested,
                    source_file=source_file,
                    api_key=api_key,
                    model=resolved_model,
                )
                responses[intent] = response

        elif intent in _UNBUILT:
            responses[intent] = _UNBUILT[intent]

    primary = route_result["primary"]
    primary_response = responses.get(primary, "")

    return {
        "route":            route_result,
        "responses":        responses,
        "primary_response": primary_response,
        "agent":            agent,
        "ingested":         ingested,
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
