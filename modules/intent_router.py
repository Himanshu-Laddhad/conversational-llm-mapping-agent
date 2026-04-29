"""
intent_router.py
────────────────
Classifies a user message against ALL four intents independently,
returning a confidence score (0.0–1.0) for each. Supports multi-intent
messages where more than one intent is active simultaneously.

Intents:
  explain   – understand / describe a mapping or file
  generate  – create a new XSLT/EDI mapping from scratch
  modify    – edit/update an existing mapping
  simulate  – run/test/compare a mapping against data

Usage (as module):
    from intent_router import route

    result = route("Explain what the BEG segment does, then modify it to add a date field")
    # result["active_intents"]  → ["explain", "modify"]
    # result["scores"]["explain"] → 0.92
    # result["scores"]["modify"]  → 0.85
    # result["primary"]           → "explain"

Usage (standalone test):
    python intent_router.py
"""

import os
import json
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
# Looks for .env in the same directory as this file, then one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# ── Config (reads from .env, with sensible defaults) ──────────────────────────
MODEL     = os.getenv("INTENT_ROUTER_MODEL",     "llama-3.3-70b-versatile")
THRESHOLD = float(os.getenv("INTENT_ROUTER_THRESHOLD", "0.45"))

SYSTEM_PROMPT = """You are an intent scoring engine for a Conversational Mapping Intelligence Agent.

A single user message can contain MULTIPLE intents simultaneously. Your job is to score
each of the four intents independently on a scale of 0.0 to 1.0 based on how strongly
that intent is present in the message.

INTENT DEFINITIONS:

  explain  – User wants to understand, describe, or ask questions about an existing
             mapping, segment, field, or file.
             Signals: "what", "why", "how does", "explain", "describe", "which fields",
                      "tell me about", "what does X mean"

  generate – User wants to CREATE a new XSLT, EDI mapping, or transformation from scratch.
             Signals: "create", "generate", "write", "build", "new mapping", "from scratch",
                      "make a", "produce"

  modify   – User wants to CHANGE, UPDATE, ADD TO, or EDIT an existing mapping.
             Signals: "add", "remove", "change", "update", "edit", "fix", "rename",
                      "replace", "adjust", "delete", "insert"

  simulate – User wants to RUN, TEST, VALIDATE, or COMPARE a mapping against data.
             Signals: "run", "test", "simulate", "what happens if", "before and after",
                      "compare", "show output", "validate", "check result", "what would"

  audit    – User wants to AUDIT, REVIEW, or CHECK a mapping for correctness,
             misconfigurations, risky fields, or production-readiness issues.
             Signals: "audit", "check", "review", "is this correct", "any issues",
                      "flag problems", "verify", "safe to use", "production ready",
                      "what could go wrong", "validate this", "check for errors",
                      "review this mapping", "is anything wrong"

SCORING RULES:
- Score each intent from 0.0 (completely absent) to 1.0 (strongly present).
- Scores are INDEPENDENT — multiple intents can all score high simultaneously.
- A message like "Explain the BEG segment, then add a DTM segment" scores:
    explain=0.90, modify=0.85, generate=0.05, simulate=0.10, audit=0.05
- "What happens if I remove the N1 loop?" scores:
    simulate=0.88 (what-if), explain=0.40 (understanding), modify=0.30 (removal implied), audit=0.05
- "Check this mapping for issues before we go live" scores:
    audit=0.95, explain=0.20, simulate=0.15, modify=0.05, generate=0.0
- Be precise. Don't inflate scores. 0.0 means the intent is truly absent.

RAG RULE (needs_rag):
Set "needs_rag": true ONLY if the question requires context from OTHER mapping
files beyond the currently loaded file and the current conversation history.
Signals that need_rag = true:
  - "similar to", "like the 850 mapping", "how do other mappings handle"
  - "compare to another file", "across all our mappings", "in any mapping"
  - Referencing a file by name that is likely not the active file
  - "what do we normally use", "our standard approach", "our template"
Signals that need_rag = false (most questions fall here):
  - Questions about the currently loaded XSLT/EDI file
  - Comparing two versions of the same file (v1 vs v2 — uses session history)
  - Modify, generate, simulate, or audit requests on the active file
  - Any question answerable from the loaded file + conversation alone

Return ONLY valid JSON, no markdown fences, no extra text:
{
  "scores": {
    "explain":  <float 0.0–1.0>,
    "generate": <float 0.0–1.0>,
    "modify":   <float 0.0–1.0>,
    "simulate": <float 0.0–1.0>,
    "audit":    <float 0.0–1.0>
  },
  "reasoning": {
    "explain":  "<one phrase why this score>",
    "generate": "<one phrase why this score>",
    "modify":   "<one phrase why this score>",
    "simulate": "<one phrase why this score>",
    "audit":    "<one phrase why this score>"
  },
  "needs_rag": <true|false>
}"""

# ── Intent metadata ───────────────────────────────────────────────────────────

INTENT_META = {
    "explain": {
        "label":       "Explain / Q&A",
        "description": "Answer questions about the mapping in plain English.",
        "next_module": "explain_agent.explain()",
    },
    "generate": {
        "label":       "Generate Mapping",
        "description": "Create a new XSLT or EDI mapping from requirements.",
        "next_module": "xslt_generator.generate()",
    },
    "modify": {
        "label":       "Modify Mapping",
        "description": "Edit an existing mapping and produce a diff.",
        "next_module": "modification_engine.modify()",
    },
    "simulate": {
        "label":       "Simulate / Validate",
        "description": "Run mapping on sample data and show before/after output.",
        "next_module": "simulation_engine.simulate()",
    },
    "audit": {
        "label":       "Audit / Validate",
        "description": "Scan mapping for misconfigurations and flag risky fields.",
        "next_module": "audit_engine.audit()",
    },
}

ALL_INTENTS = list(INTENT_META.keys())

# ── Core function ─────────────────────────────────────────────────────────────

def route(
    user_message: str,
    api_key: Optional[str] = None,
    provider: str = "groq",
    model: Optional[str] = None,
    threshold: float = THRESHOLD,
) -> dict:
    """
    Score all four intents independently for a user message.

    Args:
        user_message: Raw message from the user.
        api_key:      API key for the selected provider.
        provider:     LLM provider: "groq", "openai", "nvidia_nim", "anthropic".
        model:        Model override. Falls back to DEFAULT_MODELS[provider].
        threshold:    Min score (0.0–1.0) for an intent to be considered active.

    Returns:
        {
          "scores":         { "explain": 0.92, "generate": 0.05, "modify": 0.85, "simulate": 0.10 },
          "reasoning":      { "explain": "...", "generate": "...", ... },
          "active_intents": ["explain", "modify"],
          "primary":        "explain",
          "is_multi":       True,
          "threshold_used": 0.45,
          "needs_rag":      False,   # True only when cross-file context is required
        }

    On failure returns safe fallback with explain active and needs_rag=False.
    """
    from .llm_client import chat_complete, DEFAULT_MODELS, PROVIDERS
    env_key_name = PROVIDERS.get(provider, {}).get("env_key", "GROQ_API_KEY")
    key = (
        api_key
        or os.environ.get(env_key_name)
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    if not key:
        raise ValueError(f"API key required for provider {provider!r}.")

    resolved_model = model or MODEL or DEFAULT_MODELS.get(provider, "llama-3.1-8b-instant")

    try:
        raw = chat_complete(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            api_key=key,
            model=resolved_model,
            provider=provider,
            temperature=0.0,
            max_tokens=300,   # slightly larger to accommodate needs_rag field
            engine="intent_router",
        )

        # Strip leading/trailing whitespace before checking for markdown fences
        # so a newline before ``` doesn't defeat the detection.
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        parsed = json.loads(raw)
        scores    = parsed.get("scores", {})
        reasoning = parsed.get("reasoning", {})
        needs_rag = bool(parsed.get("needs_rag", False))

        # Clamp all scores to [0.0, 1.0] and fill missing intents
        for intent in ALL_INTENTS:
            scores[intent]    = max(0.0, min(1.0, float(scores.get(intent, 0.0))))
            reasoning[intent] = reasoning.get(intent, "")

        # Derive active intents (above threshold), sorted by score descending
        active = sorted(
            [i for i in ALL_INTENTS if scores[i] >= threshold],
            key=lambda i: scores[i],
            reverse=True
        )

        # Fallback: if nothing clears threshold, take the highest scorer
        if not active:
            active = [max(ALL_INTENTS, key=lambda i: scores[i])]

        return {
            "scores":         scores,
            "reasoning":      reasoning,
            "active_intents": active,
            "primary":        active[0],
            "is_multi":       len(active) > 1,
            "threshold_used": threshold,
            "needs_rag":      needs_rag,
        }

    except json.JSONDecodeError as e:
        return _fallback(f"JSON parse error: {e}", threshold)
    except Exception as e:
        return _fallback(f"Router error: {e}", threshold)


def _fallback(error_msg: str, threshold: float) -> dict:
    """Return a safe fallback result defaulting to explain, needs_rag=False."""
    scores = {i: 0.0 for i in ALL_INTENTS}
    scores["explain"] = 0.5
    return {
        "scores":         scores,
        "reasoning":      {i: "" for i in ALL_INTENTS},
        "active_intents": ["explain"],
        "primary":        "explain",
        "is_multi":       False,
        "threshold_used": threshold,
        "needs_rag":      False,
        "error":          error_msg,
    }


def get_meta(intent: str) -> dict:
    """Return label, description, next_module for a given intent."""
    return INTENT_META.get(intent, INTENT_META["explain"])


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":

    test_cases = [
        # Single-intent
        "What does the BEG segment do in this mapping?",
        "Create an XSLT that transforms EDI 850 into XML",
        "Add a DTM segment to handle reschedule dates",
        "Run this mapping on the sample EDI file and show the output",
        # Multi-intent
        "Explain what the N1 loop does, then remove it and show me what breaks",
        "Why does this fail for partner XYZ? And can you fix it?",
        "Generate a new invoice mapping and then test it against this sample data",
        "What does the TDS segment mean? Also update it to support multi-currency and validate the output",
    ]

    print(f"\n{'='*72}")
    print(f"  Intent Router - Multi-Intent Test  (model: {MODEL})")
    print(f"  Threshold: {THRESHOLD}   |   Scores are independent per intent")
    print(f"{'='*72}\n")

    BAR = "#"

    for msg in test_cases:
        result = route(msg)
        scores  = result["scores"]
        active  = result["active_intents"]
        primary = result["primary"]
        multi   = result["is_multi"]

        needs_rag = result.get("needs_rag", False)
        print(f"  MSG : {msg}")
        print(f"  TAG : {'[MULTI-INTENT]' if multi else '[SINGLE-INTENT]'}  primary={primary.upper()}  needs_rag={needs_rag}")
        print()

        for intent in ALL_INTENTS:
            score = scores[intent]
            bar   = BAR * int(score * 20)
            flag  = " < ACTIVE" if intent in active else ""
            reason = result["reasoning"].get(intent, "")
            print(f"    {intent:8}  {score:.2f}  [{bar:<20}]{flag}")
            if reason:
                print(f"             {reason}")

        print(f"\n  -> Dispatch order: {' -> '.join(active)}\n")
        print(f"{'-'*72}\n")