"""
xslt_generator.py
─────────────────
Generate a new XSLT 2.0 mapping stylesheet from a plain-English description.

Pipeline
────────
1. Validate inputs (generation_request must be a non-empty string).
2. Optionally load a sample source XML file to give the LLM real field names.
3. Build an LLM prompt that instructs it to produce a functional XSLT 2.0 skeleton
   following the Altova MapForce style used across all MappingData files.
4. Call the configured LLM and return the full response as-is — the response
   contains the XSLT code block followed by a short customisation guide.
5. Return (response_str, None) — same shape as all other engines so the dispatcher
   treats them uniformly.

What is generated
─────────────────
The LLM generates a functional XSLT SKELETON (~100–150 lines), not a full
900-line MapForce export. The skeleton includes:
  • Correct xsl:stylesheet 2.0 declaration with MapForce namespaces
  • xsl:output declaration
  • ISA / GS / ST interchange and group envelope (hardcoded pattern)
  • Main business segments (BIG/BEG, N1, IT1/PO1, CTT/SE) using xsl:value-of
    mapped to source field names from the user's request or sample XML
  • SE / GE / IEA closing segments
The skeleton is a valid, runnable starting point that the user can refine with
the modify engine.

Token budget (default OpenAI gpt-4.1-mini):
  • System prompt    : ~400 tokens
  • User request     : ~200 tokens
  • Source XML sample: ≤ 4 000 chars ≈ 1 000 tokens
  • Output budget    : 2 000 tokens
  • Total            : ~3 600 tokens  ← within 12 000 TPM

Usage (as module)::

    from modules.xslt_generator import generate

    response, _ = generate("Create an X12 810 invoice mapping from D365 XML")
    print(response)

    # With a sample source XML to infer real field names:
    response, _ = generate(
        "Create an 810 invoice XSLT for Nordstrom",
        source_sample="MappingData/.../SourceFile.txt",
    )
    print(response)

Usage (standalone test)::

    python modules/xslt_generator.py "Create an 810 invoice mapping"
    python modules/xslt_generator.py "Create an 850 PO mapping" path/to/SourceFile.txt
"""

import os
import re
from pathlib import Path
from typing import Any, Optional, Tuple

from dotenv import load_dotenv

# Load .env from module directory or one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# ── Constants ─────────────────────────────────────────────────────────────────

# Max characters of sample source XML included in the prompt (≈ 1 000 tokens).
# Enough to show the key field names without exhausting the input budget.
_MAX_SOURCE_CHARS = 4_000

# Max LLM output tokens. 2 000 gives ~8 000 chars — enough for a 100–150 line
# XSLT skeleton plus a customisation note.
_MAX_OUTPUT_TOKENS = 2_000

_SYSTEM_PROMPT = """\
You are an expert XSLT 2.0 developer specialising in Altova MapForce stylesheets \
that transform D365 XML into X12 EDI XML for B2B integration.

The user will describe a new mapping they need. Your task is to generate a \
FUNCTIONAL XSLT 2.0 SKELETON that follows the MapForce coding style.

RULES:
1. The stylesheet declaration MUST use these exact namespaces:
   xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
   xmlns:core="http://www.altova.com/MapForce/UDF/core"
   xmlns:xs="http://www.w3.org/2001/XMLSchema"
   xmlns:fn="http://www.w3.org/2005/xpath-functions"
   exclude-result-prefixes="core xs fn"

2. Include xsl:output: method="xml" encoding="UTF-8" indent="yes" \
   omit-xml-declaration="yes"

3. The root template matches "/" and wraps everything in the correct X12 root \
   element (e.g. <X12_00401_810> for an 810, <X12_00401_850> for an 850).

4. Include ALL these envelope segments with realistic hardcoded values:
   ISA (ISA01–ISA16), GS (GS01–GS08), ST (ST01–ST02)
   SE (SE01–SE02), GE (GE01–GE02), IEA (IEA01–IEA02)

5. For business segments, use <xsl:value-of select="..."/> to map source fields.
   If the user provides source XML field names, use those exact names.
   If not, invent plausible D365 field names that match the transaction type.

6. Keep the skeleton CONCISE — aim for 120–160 lines. Do NOT generate a full \
   900-line MapForce export. The purpose is a clean starting point.

7. After the closing </xsl:stylesheet> tag, add a section titled:
   ## CUSTOMISATION GUIDE
   List 3–5 specific things the user must update before using this stylesheet \
   in production (e.g. sender/receiver IDs, field path corrections, \
   any conditional logic needed).

Return the XSLT inside a ```xml code fence, then the CUSTOMISATION GUIDE \
outside the fence. Nothing else — no preamble, no extra commentary.
"""


def _validate_generated_xslt(xslt_text: str) -> tuple[bool, str]:
    """
    Validate generated XSLT:
    1) well-formed XML
    2) Saxon compile check when saxonche is available
    """
    try:
        from lxml import etree  # noqa: PLC0415
        etree.fromstring(xslt_text.encode("utf-8"))
    except Exception as exc:
        return False, f"XML parse validation failed: {exc}"

    try:
        from saxonche import PySaxonProcessor  # type: ignore # noqa: PLC0415
        with PySaxonProcessor(license=False) as proc:
            xslt30 = proc.new_xslt30_processor()
            _ = xslt30.compile_stylesheet(stylesheet_text=xslt_text)
            if xslt30.exception_occurred:
                err = xslt30.error_message or "Unknown Saxon compile error"
                xslt30.clear_exception()
                return False, f"Saxon compile validation failed: {err}"
    except ImportError:
        # Environment may not have saxonche; keep XML-valid output but report note.
        return True, "XML valid; Saxon compile skipped (saxonche not installed)."
    except Exception as exc:
        return False, f"Saxon compile validation failed: {exc}"
    return True, "XML + Saxon compile validation passed."


def generate(
    generation_request: str,
    source_sample: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: str = "openai",
) -> Tuple[str, Any]:
    """
    Generate a new XSLT 2.0 mapping stylesheet from a plain-English description.

    Args:
        generation_request:  Plain-English description of the mapping to create.
                             E.g. "Create an X12 810 invoice XSLT that maps
                             D365 saleCustInvoice XML to EDI format for Nordstrom"
        source_sample:       Optional path to a sample source XML file (e.g. a
                             SourceFile.txt from MappingData). If provided, the
                             LLM sees real field names and generates more accurate
                             mappings.
        api_key:             LLM API key. Falls back to the provider's env var.
        model:               LLM model. Falls back to the provider's env var,
                             then the provider's default model.

    Returns:
        (response_str, None) where response_str contains:
          - A ```xml code block with the generated XSLT 2.0 skeleton
          - A CUSTOMISATION GUIDE listing what the user must update
        The second element is always None (generate is stateless).

    Raises:
        ValueError: If generation_request is empty or no API key is found.
    """
    if not generation_request or not generation_request.strip():
        raise ValueError("generation_request must be a non-empty string")

    from .llm_client import chat_complete, DEFAULT_MODELS, PROVIDERS, get_default_model
    env_key_name = PROVIDERS.get(provider, {}).get("env_key", "OPENAI_API_KEY")
    key = api_key or os.environ.get(env_key_name) or os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not key:
        raise ValueError(f"API key required for provider {provider!r}.")

    resolved_model = model or get_default_model(provider, engine="generate")

    # ── Optionally load source XML sample ────────────────────────────────────
    source_text: Optional[str] = None
    if source_sample and Path(source_sample).exists():
        try:
            raw = Path(source_sample).read_text(encoding="utf-8", errors="replace")
            if len(raw) > _MAX_SOURCE_CHARS:
                raw = (
                    raw[:_MAX_SOURCE_CHARS]
                    + f"\n... [truncated at {_MAX_SOURCE_CHARS} chars] ..."
                )
            source_text = raw
        except OSError as exc:
            source_text = f"[Could not read source file: {exc}]"

    # ── Build user message ────────────────────────────────────────────────────
    parts = [f"## Generation Request\n{generation_request.strip()}\n"]

    if source_text:
        src_name = Path(source_sample).name if source_sample else "source"
        parts.append(
            f"## Sample Source XML ({src_name})\n"
            f"Use the field names from this file for the source XPath expressions:\n"
            f"```xml\n{source_text}\n```\n"
        )
    else:
        parts.append(
            "## Source XML\n"
            "No sample source file provided. Infer plausible D365 field names "
            "based on the transaction type described above.\n"
        )

    parts.append(
        "Generate the XSLT 2.0 skeleton now. "
        "Follow all rules in the system prompt exactly."
    )

    user_message = "\n".join(parts)

    # ── Call LLM ──────────────────────────────────────────────────────────────
    llm_text = chat_complete(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        api_key=key,
        model=resolved_model,
        provider=provider,
        temperature=0.2,
        max_tokens=_MAX_OUTPUT_TOKENS,
        engine="generate",
    )

    # Extract the raw XSLT from the ```xml ... ``` fence the LLM is instructed
    # to use (system prompt line: "Return the XSLT inside a ```xml code fence").
    raw_xslt: Optional[str] = None
    m = re.search(r"```xml\s*([\s\S]*?)```", llm_text)
    if m:
        raw_xslt = m.group(1).strip()

    if raw_xslt:
        valid, note = _validate_generated_xslt(raw_xslt)
        if not valid:
            return (
                llm_text
                + "\n\n---\n"
                + f"**Generation validation failed:** {note}\n"
                + "Please refine the request and regenerate."
            ), None
        llm_text += "\n\n---\n" + f"**Generation validation:** {note}"

    return llm_text, raw_xslt


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("\n" + "=" * 80)
    print("  XSLT GENERATOR — New Mapping Stylesheet")
    print("=" * 80 + "\n")

    if len(sys.argv) < 2:
        print("Usage: python modules/xslt_generator.py <request> [source_file]\n")
        print("Examples:")
        print('  python modules/xslt_generator.py "Create an X12 810 invoice mapping"')
        print('  python modules/xslt_generator.py "Create an 850 PO mapping" '
              '"MappingData/MappingData/810_C-000340_OUT/810/0413e4fc-4bf0-4157-823b-e9fcd8b94d2b/SourceFile.txt"')
        sys.exit(0)

    request_str = sys.argv[1]
    src_path    = sys.argv[2] if len(sys.argv) > 2 else None

    if src_path and not Path(src_path).exists():
        print(f"[ERROR] Source file not found: {src_path}")
        sys.exit(1)

    print(f"[REQUEST] {request_str}")
    if src_path:
        print(f"[SOURCE ] {src_path}")
    print()

    print("[GENERATE] Building XSLT skeleton...\n")
    response, _ = generate(request_str, source_sample=src_path)

    print("=" * 80)
    print(response)
    print("=" * 80 + "\n")
