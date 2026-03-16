"""
modification_engine.py
──────────────────────
Propose targeted edits to an XSLT mapping stylesheet based on a user request.

Pipeline
────────
1. Validate inputs (ingested dict, modification request string).
2. Extract the raw XSLT source from the ingested dict, truncate to token budget.
3. Build a concise LLM prompt: the XSLT source + the user's modification request.
4. Call Groq and ask it to return a structured patch:
      • CHANGE SUMMARY  — one-sentence description
      • BEFORE BLOCK    — the exact XML lines to find in the original file
      • AFTER BLOCK     — the replacement XML
      • EXPLANATION     — why this change achieves the goal
5. Return (response_str, None) — same shape as simulate and explain so the
   dispatcher treats all engines uniformly.
   The caller is responsible for deciding whether to write the changes to disk.

Token budget (Groq on-demand, llama-3.3-70b-versatile at ~12 000 TPM):
  • System prompt    : ~300 tokens
  • XSLT content     : ≤ 10 000 chars ≈ 2 500 tokens
  • User request     : ~150 tokens
  • Output budget    : 1 500 tokens
  • Total            : ~4 450 tokens  ← well within 12 000 TPM

Usage (as module)::

    from modules.modification_engine import modify
    from modules.file_ingestion import ingest_file

    ingested = ingest_file("MappingData/.../810_NordStrom_Xslt_11-08-2023.xml")
    response, _ = modify(ingested, "Change the hardcoded sender ID to ACME001")
    print(response)

Usage (standalone test)::

    python modules/modification_engine.py [xslt_file] [\"modification request\"]
"""

import os
from pathlib import Path
from typing import Any, Optional, Tuple

from dotenv import load_dotenv
from groq import Groq

# Load .env from module directory or one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# ── Constants ─────────────────────────────────────────────────────────────────

# Max XSLT characters included in the prompt.
# Modifications need enough context to locate the correct block to change.
# 10 000 chars ≈ 2 500 tokens — generous for most MapForce XSLTs (~15–30 KB).
_MAX_XSLT_CHARS = 10_000

# Max tokens for LLM output.
# 1 500 tokens is enough for a clear patch block + explanation.
_MAX_OUTPUT_TOKENS = 1_500

_SYSTEM_PROMPT = """\
You are an expert XSLT 2.0 developer specialising in Altova MapForce stylesheets \
that transform D365 XML into X12 EDI XML.

The user will give you:
  1. The current XSLT source (possibly truncated for length).
  2. A modification request in plain English.

Your task is to produce a MINIMAL, SURGICAL change that fulfils the request \
without breaking any other part of the stylesheet.

Return your answer in EXACTLY this format — do not deviate, add no extra sections:

## CHANGE SUMMARY
<one sentence describing what you are changing and why>

## BEFORE
```xml
<exact XML lines from the original stylesheet that will be replaced>
```

## AFTER
```xml
<the replacement XML — valid XSLT 2.0>
```

## EXPLANATION
<2–4 sentences explaining the change: what it does, any caveats, and how to \
apply it (find the BEFORE block in the file and replace it with the AFTER block)>

Rules:
- If the request asks to ADD something new, the BEFORE block should be the \
  anchor element just before or after the insertion point, and the AFTER block \
  should be that same anchor element PLUS the new content.
- If no change is needed (the request is already satisfied), say so clearly in \
  the CHANGE SUMMARY and leave BEFORE/AFTER empty.
- Never modify the xsl:stylesheet declaration or the core:* utility templates \
  unless explicitly asked.
- Keep hardcoded values as string literals, not variables, unless asked to \
  parameterise.
- Do not return the entire modified file — only the changed block.
"""


def modify(
    ingested: dict,
    modification_request: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[str, Any]:
    """
    Propose a targeted edit to an XSLT mapping based on a natural-language request.

    Args:
        ingested:              Output dict from file_ingestion.ingest_file().
                               Must be an XSLT or XML mapping file.
        modification_request:  Plain-English description of the change to make.
                               E.g. "Change the hardcoded sender ID to ACME001"
                                    "Add a DTM segment with today's date after BIG"
                                    "Remove the N1 loop for the ship-to address"
        api_key:               Groq API key. Falls back to GROQ_API_KEY env var.
        model:                 Groq model. Falls back to GROQ_MODEL env var,
                               then llama-3.3-70b-versatile.

    Returns:
        (response_str, None) where response_str is the structured patch proposal
        containing CHANGE SUMMARY, BEFORE block, AFTER block, and EXPLANATION.
        The second element is always None (modify is stateless).

    Raises:
        TypeError:  If ingested is not a dict.
        ValueError: If ingested is missing required keys, the request is empty,
                    or no API key is found.
    """
    # ── Validate inputs ───────────────────────────────────────────────────────
    if not isinstance(ingested, dict):
        raise TypeError(f"ingested must be a dict, got {type(ingested).__name__}")
    if "parsed_content" not in ingested:
        raise ValueError("ingested dict is missing 'parsed_content' key")
    if not modification_request or not modification_request.strip():
        raise ValueError("modification_request must be a non-empty string")

    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        raise ValueError(
            "Groq API key required. Pass api_key= or set GROQ_API_KEY in .env"
        )

    resolved_model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # ── Extract XSLT source ───────────────────────────────────────────────────
    parsed = ingested.get("parsed_content", {})
    meta   = ingested.get("metadata", {})
    file_name = meta.get("filename", "unknown")
    file_type = meta.get("file_type", "unknown")

    # Prefer raw_xml (full source); fall back to raw_text for generic XML files.
    raw_xslt = parsed.get("raw_xml") or parsed.get("raw_text") or ""

    if not raw_xslt:
        return (
            "[modify] Could not extract XSLT source from the ingested file. "
            "Ensure the file is an XSLT or XML mapping and was parsed successfully."
        ), None

    truncated = False
    if len(raw_xslt) > _MAX_XSLT_CHARS:
        raw_xslt = raw_xslt[:_MAX_XSLT_CHARS]
        truncated = True

    # ── Build LLM user message ────────────────────────────────────────────────
    truncation_note = (
        "\n\n> NOTE: The stylesheet has been truncated to the first "
        f"{_MAX_XSLT_CHARS} characters due to length. If the block you need "
        "to change is beyond this point, indicate that in your EXPLANATION.\n"
        if truncated else ""
    )

    user_message = (
        f"## Mapping File\n"
        f"Name: {file_name}  |  Type: {file_type}\n\n"
        f"## Current XSLT Source\n"
        f"```xml\n{raw_xslt}\n```"
        f"{truncation_note}\n\n"
        f"## Modification Request\n"
        f"{modification_request.strip()}\n\n"
        f"Please propose the minimal change to fulfil this request."
    )

    # ── Call Groq ─────────────────────────────────────────────────────────────
    client = Groq(api_key=key)
    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        temperature=0.1,   # low temperature for precise, deterministic code edits
        max_tokens=_MAX_OUTPUT_TOKENS,
    )

    return response.choices[0].message.content.strip(), None


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("\n" + "=" * 80)
    print("  MODIFICATION ENGINE — XSLT Edit Proposal")
    print("=" * 80 + "\n")

    if len(sys.argv) < 3:
        print("Usage: python modules/modification_engine.py <xslt_file> <request>\n")
        print("Examples:")
        print('  python modules/modification_engine.py "MappingData/.../810_NordStrom_Xslt.xml" "Change the hardcoded sender ID to ACME001"')
        print('  python modules/modification_engine.py "MappingData/.../810_NordStrom_Xslt.xml" "Add a DTM segment with today\'s date after the BIG segment"')
        sys.exit(0)

    xslt_path   = sys.argv[1]
    request_str = sys.argv[2]

    if not Path(xslt_path).exists():
        print(f"[ERROR] XSLT file not found: {xslt_path}")
        sys.exit(1)

    try:
        from .file_ingestion import ingest_file
    except ImportError:
        from file_ingestion import ingest_file  # standalone execution

    print(f"[INGEST] {xslt_path}")
    ingested = ingest_file(file_path=xslt_path)
    print(f"[TYPE  ] {ingested['metadata']['file_type']}")
    print(f"[REQUEST] {request_str}")
    print()

    print("[MODIFY] Generating patch proposal...\n")
    response, _ = modify(ingested, modification_request=request_str)

    print("=" * 80)
    print(response)
    print("=" * 80 + "\n")
