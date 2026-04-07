"""
simulation_engine.py
────────────────────
Simulate / validate a mapping against source data.

Pipeline
────────
1. Parse the source input file (D365 XML) via file_ingestion.
2. Attempt an actual XSLT execution using lxml (XSLT 1.0 only).
   • Most MapForce XSLT files are 2.0 — lxml will raise an error for 2.0 features.
   • Catch that gracefully and fall back to LLM simulation.
3. LLM simulation path: send the parsed XSLT rules + source XML to Groq and ask it
   to reason through what the output would be, field-by-field.
   This is often more useful than raw XML for analyst users because the LLM can
   explain WHY each output field has its value.
4. Return (response_str, None) — mirrors the groq_agent.explain() return shape.
   Pass the returned agent as None; simulate is stateless (no multi-turn needed).

Usage (as module)::

    from modules.simulation_engine import simulate
    from modules.file_ingestion import ingest_file

    ingested = ingest_file("MappingData/.../810_NordStrom_Xslt_11-08-2023.xml")
    response, _ = simulate(
        ingested,
        source_file="MappingData/.../SourceFile.txt",
    )
    print(response)

Usage (standalone test)::

    python modules/simulation_engine.py [xslt_file] [source_file]
"""

import json
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

# TPM budget on Groq on-demand tier varies by model.
# llama-3.3-70b-versatile: ~12 000 TPM; llama-3.1-8b-instant: ~6 000 TPM.
# We target ≤ 4 000 input tokens (≈ 16 000 chars) so there is room for the
# 1 200-token output budget and a safety margin in both cases.

# Max characters of source XML to include in the LLM prompt (≈ 1 500 tokens).
_MAX_SOURCE_CHARS = 6_000

# Max characters of XSLT parsed content to include in the LLM context (≈ 2 000 tokens).
_MAX_XSLT_CHARS = 8_000

# Max tokens for LLM output — 1 500 gives good coverage; combined with
# ~3 000 input tokens stays well within the 12 000 TPM limit.
_MAX_OUTPUT_TOKENS = 1_500

_SYSTEM_PROMPT = """\
You are an expert EDI/XSLT transformation analyst working with Altova MapForce \
XSLT 2.0 stylesheets that transform D365 XML into X12 EDI XML.

When given:
  • XSLT mapping specification (templates, field mappings, conditionals, hardcoded values)
  • Source input XML (D365 XML)

Your task is to SIMULATE the transformation. Produce a structured, human-readable \
analysis that shows:

1. **Transformation Summary** — one sentence on what this mapping does.
2. **Key Output Fields** — for each important X12 segment (ISA, GS, ST, BIG/BEG, \
   N1 loops, IT1 lines, CTT/SE), show:
   - The output field name / segment element
   - The source of its value (mapped from source field / hardcoded / computed)
   - The actual value it would receive from this source data
3. **Conditional Logic Triggered** — list any xsl:if / xsl:choose conditions and \
   state whether each evaluates to true or false for this source data.
4. **Hardcoded Values** — list any values in the XSLT that are emitted regardless \
   of source data.
5. **Potential Issues** — note any source fields that are empty/missing that the \
   XSLT maps to required EDI fields.

Be concise but complete. Use a structured format with clear section headers.
"""


def simulate(
    ingested: dict,
    source_file: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[str, Any]:
    """
    Simulate / validate a mapping against source data.

    Args:
        ingested:     Output dict from file_ingestion.ingest_file() for the
                      XSLT / mapping file.
        source_file:  Path to the source XML input file (e.g. SourceFile.txt
                      from a MappingData test case). Optional — if not provided,
                      a dry-run analysis is performed instead.
        api_key:      Groq API key. Falls back to GROQ_API_KEY env var.
        model:        Groq model identifier. Falls back to GROQ_MODEL env var,
                      then llama-3.1-8b-instant.

    Returns:
        (response_str, None) where response_str is the simulation analysis.
        The second element is always None (simulate is stateless).

    Raises:
        TypeError:  If ingested is not a dict.
        ValueError: If ingested is missing required keys or no API key found.
    """
    # ── Validate inputs ───────────────────────────────────────────────────────
    if not isinstance(ingested, dict):
        raise TypeError(f"ingested must be a dict, got {type(ingested).__name__}")
    if "parsed_content" not in ingested:
        raise ValueError("ingested dict is missing 'parsed_content' key")

    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        raise ValueError(
            "Groq API key required. Pass api_key= or set GROQ_API_KEY in .env"
        )

    resolved_model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # ── Step 1: Try actual XSLT execution (lxml — XSLT 1.0 only) ─────────────
    xslt_result_xml: Optional[str] = None
    xslt_error: Optional[str] = None

    raw_xslt = ingested.get("parsed_content", {}).get("raw_xml") or \
               ingested.get("parsed_content", {}).get("raw_text")

    if raw_xslt and source_file and Path(source_file).exists():
        xslt_result_xml, xslt_error = _try_lxml_transform(
            raw_xslt, str(source_file)
        )

    # ── Step 2: Load source file content for LLM context ─────────────────────
    source_xml_text: Optional[str] = None
    if source_file and Path(source_file).exists():
        try:
            source_xml_text = Path(source_file).read_text(encoding="utf-8", errors="replace")
            if len(source_xml_text) > _MAX_SOURCE_CHARS:
                source_xml_text = (
                    source_xml_text[:_MAX_SOURCE_CHARS]
                    + f"\n... [truncated at {_MAX_SOURCE_CHARS} chars] ..."
                )
        except OSError as exc:
            source_xml_text = f"[Could not read source file: {exc}]"

    # ── Step 3: Build LLM user message ───────────────────────────────────────
    user_message = _build_user_message(
        ingested=ingested,
        source_xml_text=source_xml_text,
        xslt_result_xml=xslt_result_xml,
        xslt_error=xslt_error,
        source_file=source_file,
    )

    # ── Step 4: Call Groq ─────────────────────────────────────────────────────
    client = Groq(api_key=key)
    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        temperature=0.2,
        max_tokens=_MAX_OUTPUT_TOKENS,
    )

    return (response.choices[0].message.content or "").strip(), None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _try_lxml_transform(raw_xslt: str, source_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Attempt an XSLT transformation using lxml (XSLT 1.0 only).

    Returns (output_xml_str, None) on success.
    Returns (None, error_message)   on failure (e.g. XSLT 2.0 feature used).
    """
    try:
        from lxml import etree  # type: ignore

        xslt_tree   = etree.fromstring(raw_xslt.encode("utf-8"))
        source_tree = etree.parse(source_path)
        transform   = etree.XSLT(xslt_tree)
        result      = transform(source_tree)

        # Only discard the real output for fatal/error-level entries;
        # warnings (e.g. unsupported extension hints) do not invalidate the result.
        errors = transform.error_log
        fatal = [e for e in errors if e.level_name in ("FATAL_ERROR", "ERROR")]
        if fatal:
            msgs = "; ".join(str(e) for e in fatal)
            return None, f"lxml XSLT transform errors: {msgs}"

        return str(result), None

    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _build_user_message(
    ingested: dict,
    source_xml_text: Optional[str],
    xslt_result_xml: Optional[str],
    xslt_error: Optional[str],
    source_file: Optional[str],
) -> str:
    """Compose the user-turn message for the LLM simulation prompt."""

    parts: list[str] = []

    # ── XSLT mapping specification ────────────────────────────────────────────
    parsed    = ingested.get("parsed_content", {})
    meta      = ingested.get("metadata", {})
    file_type = meta.get("file_type", "unknown")
    file_name = meta.get("filename", "unknown")

    parts.append(f"## Mapping File\nName: {file_name}  |  Type: {file_type}\n")

    # For simulation we only need the mapping rules, not schema/namespace metadata.
    # This keeps the prompt well within the Groq TPM limits.
    structured_keys = ["field_mappings", "hardcoded_values", "templates"]
    structured = {k: parsed[k] for k in structured_keys if k in parsed and parsed[k]}

    if structured:
        structured_json = json.dumps(structured, indent=2, default=str)
        if len(structured_json) > _MAX_XSLT_CHARS:
            structured_json = (
                structured_json[:_MAX_XSLT_CHARS]
                + f"\n... [truncated at {_MAX_XSLT_CHARS} chars] ..."
            )
        parts.append(f"### XSLT Parsed Structure\n```json\n{structured_json}\n```\n")
    elif parsed.get("raw_xml"):
        raw = parsed["raw_xml"]
        if len(raw) > _MAX_XSLT_CHARS:
            raw = raw[:_MAX_XSLT_CHARS] + f"\n... [truncated] ..."
        parts.append(f"### XSLT Raw XML\n```xml\n{raw}\n```\n")

    # ── Source data ───────────────────────────────────────────────────────────
    if source_xml_text:
        src_name = Path(source_file).name if source_file else "source"
        parts.append(f"## Source Input File ({src_name})\n```xml\n{source_xml_text}\n```\n")
    else:
        parts.append(
            "## Source Input File\n"
            "No source file provided. Perform a **dry-run analysis** instead:\n"
            "describe what the mapping WOULD produce for a typical input, based on "
            "the XSLT templates and hardcoded values above.\n"
        )

    # ── Actual lxml transform result (if available) ───────────────────────────
    if xslt_result_xml:
        preview = xslt_result_xml[:3000]
        if len(xslt_result_xml) > 3000:
            preview += "\n... [truncated — full output is longer] ..."
        parts.append(
            f"## Actual lxml Transform Output (XSLT 1.0)\n"
            f"lxml successfully executed the stylesheet. Here is the actual output:\n"
            f"```xml\n{preview}\n```\n"
            f"Use this as ground truth for your analysis.\n"
        )
    elif xslt_error:
        parts.append(
            f"## lxml Transform Attempt\n"
            f"lxml could not execute this stylesheet (likely XSLT 2.0 features used by MapForce).\n"
            f"Error: `{xslt_error}`\n"
            f"Perform a **LLM-based simulation** instead.\n"
        )

    # ── Task instruction ──────────────────────────────────────────────────────
    if xslt_result_xml:
        task = (
            "The actual transformation output is provided above. "
            "Analyse it against the mapping rules and source data. "
            "Explain what happened for each key segment, and note any "
            "conditional logic that fired or was skipped."
        )
    else:
        task = (
            "Simulate this transformation: walk through the XSLT templates, "
            "apply them to the source data above, and show what the output "
            "would look like field-by-field. Identify any data quality issues."
        )

    parts.append(f"## Your Task\n{task}\n")

    return "\n".join(parts)


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("\n" + "=" * 80)
    print("  SIMULATION ENGINE — Mapping Transform Validator")
    print("=" * 80 + "\n")

    if len(sys.argv) < 2:
        print("Usage: python modules/simulation_engine.py <xslt_file> [source_file]\n")
        print("Examples:")
        print('  python modules/simulation_engine.py "MappingData/MappingData/810_C-000340_OUT/810_NordStrom_Xslt_11-08-2023.xml" "MappingData/MappingData/810_C-000340_OUT/810/0413e4fc-4bf0-4157-823b-e9fcd8b94d2b/SourceFile.txt"')
        sys.exit(0)

    xslt_path   = sys.argv[1]
    src_path    = sys.argv[2] if len(sys.argv) > 2 else None

    if not Path(xslt_path).exists():
        print(f"[ERROR] XSLT file not found: {xslt_path}")
        sys.exit(1)

    if src_path and not Path(src_path).exists():
        print(f"[ERROR] Source file not found: {src_path}")
        sys.exit(1)

    # Ingest the XSLT mapping file
    try:
        from .file_ingestion import ingest_file
    except ImportError:
        from file_ingestion import ingest_file  # standalone execution

    print(f"[INGEST] {xslt_path}")
    ingested = ingest_file(file_path=xslt_path)
    print(f"[TYPE  ] {ingested.get('file_type', 'unknown')}")
    if src_path:
        print(f"[SOURCE] {src_path}")
    print()

    print("[SIMULATE] Running …\n")
    response, _ = simulate(ingested, source_file=src_path)

    print("=" * 80)
    print(response)
    print("=" * 80 + "\n")
