"""
modification_engine.py
----------------------
Propose AND apply targeted edits to an XSLT mapping stylesheet based on a
user request written in plain English.

Pipeline
--------
1. Validate inputs (ingested dict, modification request string).
2. Extract the raw XSLT source from the ingested dict.
   - A truncated copy (~10 000 chars) is sent to the LLM for the prompt.
   - The full source is kept in memory for patch application.
3. Build a concise LLM prompt: the XSLT source + the user's modification request.
4. Call Groq and ask it to return a structured patch:
      CHANGE SUMMARY  -- one-sentence description
      BEFORE BLOCK    -- the exact XML lines to find in the original file
      AFTER BLOCK     -- the replacement XML (valid XSLT 2.0)
      EXPLANATION     -- why this change achieves the goal
5. Parse the BEFORE/AFTER blocks from the LLM response (_parse_patch).
6. Apply the patch to the full XSLT source (apply_patch):
      - Exact-string match first.
      - Normalised-whitespace sliding-window match as fallback.
7. Validate the patched XSLT is well-formed XML (validate_xslt_wellformed).
8. Return (response_str, patched_xslt_or_None):
      - response_str  -- the full LLM proposal text + patch status note
      - patched_xslt  -- the modified XSLT string if applied OK, else None
   The dispatcher passes patched_xslt to app.py for the Download button.

Supported modification types
-----------------------------
- Change a hardcoded value (sender ID, qualifier, account number)
- Add a new field mapping  (new xsl:value-of inside an existing template)
- Add a new item/line row  (new xsl:for-each loop + child value-of elements)
- Add a new named template (new xsl:template block + xsl:call-template hook)
- Remove an existing block (any element or group of elements)
- Rename or reorder segments

Token budget (Groq on-demand, llama-3.3-70b-versatile):
  System prompt    : ~450 tokens
  XSLT content     : <= 10 000 chars ~= 2 500 tokens
  User request     : ~150 tokens
  Output budget    : 1 500 tokens
  Total            : ~4 600 tokens

Usage (as module)::

    from modules.modification_engine import modify
    from modules.file_ingestion import ingest_file

    ingested = ingest_file("MappingData/.../810_NordStrom_Xslt_11-08-2023.xml")
    response, patched = modify(ingested, "Add a new IT1 line item row with ItemId and Qty")
    print(response)
    if patched:
        with open("patched.xml", "w") as f:
            f.write(patched)

Usage (standalone test)::

    python modules/modification_engine.py [xslt_file] ["modification request"]
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max XSLT characters sent to the LLM (token budget).
# The FULL source is kept separately for patch application.
_MAX_XSLT_CHARS = 10_000

# Max tokens for LLM output.
_MAX_OUTPUT_TOKENS = 1_500

_SYSTEM_PROMPT = """\
You are an expert XSLT 2.0 developer specialising in Altova MapForce stylesheets \
that transform D365 XML (Microsoft Dynamics 365) into X12 EDI XML.

The user will give you:
  1. The current XSLT source (possibly truncated for length).
  2. A modification request in plain English.

Your task is to produce a MINIMAL, SURGICAL change that fulfils the request \
without breaking any other part of the stylesheet.

Return your answer in EXACTLY this format -- no extra sections, no deviation:

## CHANGE SUMMARY
<one sentence describing what you are changing and why>

## BEFORE
```xml
<the exact XML lines from the original stylesheet that will be replaced -- \
copy them character-for-character including indentation>
```

## AFTER
```xml
<the replacement XML -- valid XSLT 2.0, same indentation style as the original>
```

## EXPLANATION
<2-4 sentences: what the change does, any caveats, and how to apply it>

RULES FOR COMMON MODIFICATION TYPES
-------------------------------------

1. CHANGE A HARDCODED VALUE
   - Locate the xsl:value-of or xsl:attribute that outputs it.
   - BEFORE = that single line. AFTER = same line with new value.

2. ADD A NEW FIELD MAPPING (single field in an existing segment)
   - Find the template that builds the target segment/element.
   - BEFORE = the last xsl:value-of (or closing tag) inside that template.
   - AFTER  = that same anchor line PLUS a new <xsl:value-of select="SOURCE_PATH"/>
     or <xsl:element name="TARGET_ELEMENT"><xsl:value-of .../></xsl:element>
   - The SOURCE_PATH must be a valid XPath relative to the template context node.

3. ADD A NEW ITEM / LINE-ROW (repeating element like invoice lines, IT1 rows)
   - Wrap the loop in a new named template if one does not exist.
   - BEFORE = the closing </xsl:template> of the parent template that should call it.
   - AFTER  = that closing tag PLUS a new self-contained block like:
       <xsl:call-template name="NEW_TEMPLATE_NAME"/>
     </xsl:template>
     <xsl:template name="NEW_TEMPLATE_NAME">
       <xsl:for-each select="SOURCE_NODE_SET">
         <xsl:element name="TARGET_SEGMENT">
           <xsl:value-of select="FIELD_1_XPATH"/>
           <xsl:value-of select="FIELD_2_XPATH"/>
         </xsl:element>
       </xsl:for-each>
     </xsl:template>

4. ADD A NEW NAMED TEMPLATE
   - BEFORE = the closing </xsl:stylesheet> tag.
   - AFTER  = the new <xsl:template name="...">...</xsl:template> block inserted
     just before </xsl:stylesheet>, plus a <xsl:call-template> hook in the
     appropriate existing template.

5. REMOVE AN EXISTING BLOCK
   - BEFORE = the complete element(s) to remove.
   - AFTER  = empty string (omit the block entirely).

General rules:
- For ADD requests: BEFORE = the anchor element at insertion point;
  AFTER = anchor + new content.
- If no change is needed: say so in CHANGE SUMMARY and leave BEFORE/AFTER empty.
- Never modify <xsl:stylesheet> declaration or core:* utility templates unless asked.
- Keep hardcoded values as string literals unless asked to parameterise.
- Do not return the entire modified file -- only the changed block.
- Match the indentation of the surrounding code exactly.
"""


# ---------------------------------------------------------------------------
# Patch utilities (public so tests can import them directly)
# ---------------------------------------------------------------------------

def _parse_patch(response_text: str) -> dict:
    """
    Extract CHANGE SUMMARY, BEFORE, AFTER, and EXPLANATION from the LLM response.

    Returns a dict with keys: summary, before, after, explanation.
    Any missing section returns an empty string.
    """
    result = {"summary": "", "before": "", "after": "", "explanation": ""}

    m = re.search(
        r"##\s*CHANGE SUMMARY\s*\n(.*?)(?=\n##|\Z)",
        response_text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        result["summary"] = m.group(1).strip()

    m = re.search(
        r"##\s*BEFORE\s*\n```(?:xml)?\s*\n(.*?)```",
        response_text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        result["before"] = m.group(1).rstrip()

    m = re.search(
        r"##\s*AFTER\s*\n```(?:xml)?\s*\n(.*?)```",
        response_text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        result["after"] = m.group(1).rstrip()

    m = re.search(
        r"##\s*EXPLANATION\s*\n(.*?)(?=\n##|\Z)",
        response_text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        result["explanation"] = m.group(1).strip()

    return result


def _normalize_ws(text: str) -> str:
    """Collapse all whitespace runs to a single space for fuzzy matching."""
    return re.sub(r"\s+", " ", text).strip()


def apply_patch(
    raw_xslt: str, before_block: str, after_block: str
) -> Tuple[str, bool, Optional[str]]:
    """
    Find *before_block* inside *raw_xslt* and replace it with *after_block*.

    Strategy
    --------
    1. Exact character-for-character match.
    2. Normalised-whitespace sliding-window match (handles minor indent
       differences between the LLM copy and the actual file).

    Returns
    -------
    (patched_xslt, success, error_message)
    - success=True  -> patched_xslt is the modified XSLT.
    - success=False -> patched_xslt equals raw_xslt unchanged;
                       error_message explains why.
    """
    if not before_block:
        return raw_xslt, False, "BEFORE block is empty -- nothing to locate in the file."
    if not after_block and after_block != "":
        return raw_xslt, False, "AFTER block is missing."

    # Strategy 1: exact match
    if before_block in raw_xslt:
        patched = raw_xslt.replace(before_block, after_block, 1)
        return patched, True, None

    # Strategy 2: normalised-whitespace sliding-window
    norm_before  = _normalize_ws(before_block)
    before_lines = before_block.strip().splitlines()
    window_size  = max(len(before_lines), 1)
    file_lines   = raw_xslt.splitlines()

    for i in range(len(file_lines) - window_size + 1):
        window = "\n".join(file_lines[i : i + window_size])
        if _normalize_ws(window) == norm_before:
            # Preserve the indentation level of the first matched line
            leading_spaces = len(file_lines[i]) - len(file_lines[i].lstrip())
            indent = " " * leading_spaces

            # Re-indent the AFTER block to match the original indentation
            after_lines = after_block.splitlines()
            if after_lines:
                first_stripped = after_lines[0].lstrip()
                after_base = len(after_lines[0]) - len(first_stripped) if first_stripped else 0
                reindented = []
                for ln in after_lines:
                    stripped = ln[after_base:] if len(ln) >= after_base and ln[:after_base] == " " * after_base else ln.lstrip()
                    reindented.append((indent + stripped) if stripped else "")
                after_reindented = "\n".join(reindented)
            else:
                after_reindented = after_block

            patched_lines = (
                file_lines[:i]
                + after_reindented.splitlines()
                + file_lines[i + window_size :]
            )
            return "\n".join(patched_lines), True, None

    return raw_xslt, False, (
        "Could not locate the BEFORE block in the XSLT source. "
        "The XSLT may be truncated in the prompt (only the first "
        f"{_MAX_XSLT_CHARS} characters were sent to the LLM). "
        "Copy the BEFORE block into your editor, locate it manually, "
        "and apply the AFTER block there."
    )


def validate_xslt_wellformed(xslt_text: str) -> Tuple[bool, Optional[str]]:
    """
    Confirm the patched XSLT is well-formed XML using lxml.

    Returns (True, None) if valid, or (False, error_str) if not.
    """
    try:
        from lxml import etree  # noqa: PLC0415
        etree.fromstring(xslt_text.encode("utf-8"))
        return True, None
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Main modify() function
# ---------------------------------------------------------------------------

def modify(
    ingested: dict,
    modification_request: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: str = "groq",
) -> Tuple[str, Optional[str]]:
    """
    Propose and auto-apply a targeted edit to an XSLT mapping based on a
    natural-language request.

    Args
    ----
    ingested:              Output dict from file_ingestion.ingest_file().
                           Must be an XSLT or XML mapping file.
    modification_request:  Plain-English description of the change.
                           E.g. "Change the hardcoded sender ID to ACME001"
                                "Add a new IT1 line item row with ItemId and Qty"
                                "Add ShipDate field to the DTM segment"
    api_key:               Groq API key. Falls back to GROQ_API_KEY env var.
    model:                 Groq model. Falls back to GROQ_MODEL env var,
                           then llama-3.3-70b-versatile.

    Returns
    -------
    (response_str, patched_xslt)
    - response_str  -- full LLM proposal text + auto-apply status note.
    - patched_xslt  -- the modified XSLT string if patch was applied and
                       validated successfully, or None if it could not be applied.
    """
    # -- Validate inputs -------------------------------------------------------
    if not isinstance(ingested, dict):
        raise TypeError(f"ingested must be a dict, got {type(ingested).__name__}")
    if "parsed_content" not in ingested:
        raise ValueError("ingested dict is missing 'parsed_content' key")
    if not modification_request or not modification_request.strip():
        raise ValueError("modification_request must be a non-empty string")

    from .llm_client import chat_complete, DEFAULT_MODELS, PROVIDERS
    env_key_name = PROVIDERS.get(provider, {}).get("env_key", "GROQ_API_KEY")
    key = api_key or os.environ.get(env_key_name) or os.environ.get("GROQ_API_KEY")
    if not key:
        raise ValueError(f"API key required for provider {provider!r}.")

    resolved_model = model or os.getenv("GROQ_MODEL") or DEFAULT_MODELS.get(provider, "llama-3.3-70b-versatile")

    # -- Extract XSLT source ---------------------------------------------------
    parsed    = ingested.get("parsed_content", {})
    meta      = ingested.get("metadata", {})
    file_name = meta.get("filename", "unknown")
    file_type = meta.get("file_type", "unknown")

    # raw_xml = full XSLT source; raw_text = fallback for generic XML
    full_raw_xslt = parsed.get("raw_xml") or parsed.get("raw_text") or ""

    if not full_raw_xslt:
        return (
            "[modify] Could not extract XSLT source from the ingested file. "
            "Ensure the file is an XSLT or XML mapping and was parsed successfully."
        ), None

    # Truncated copy for the LLM prompt (token budget)
    truncated = len(full_raw_xslt) > _MAX_XSLT_CHARS
    prompt_xslt = full_raw_xslt[:_MAX_XSLT_CHARS] if truncated else full_raw_xslt

    # -- Build LLM user message ------------------------------------------------
    truncation_note = (
        f"\n\n> NOTE: Stylesheet truncated to first {_MAX_XSLT_CHARS} chars "
        "for token budget. If the target block is beyond this point, note it "
        "in EXPLANATION and I will apply the patch manually.\n"
        if truncated else ""
    )

    # Include XSLT structure hints from parsed_content so the LLM can reference
    # real template names even when the full source is truncated
    parsed_hints = ""
    tcg = parsed.get("template_call_graph", [])
    if tcg:
        names = [t.get("name") or t.get("match", "") for t in tcg[:15]]
        names = [n for n in names if n]
        if names:
            parsed_hints = (
                "\n\n> TEMPLATE INVENTORY (from parsed structure): "
                + ", ".join(names)
                + "\n"
            )

    user_message = (
        f"## Mapping File\n"
        f"Name: {file_name}  |  Type: {file_type}\n"
        f"{parsed_hints}"
        f"\n## Current XSLT Source\n"
        f"```xml\n{prompt_xslt}\n```"
        f"{truncation_note}\n\n"
        f"## Modification Request\n"
        f"{modification_request.strip()}\n\n"
        f"Please propose the minimal change to fulfil this request."
    )

    # -- Call LLM --------------------------------------------------------------
    llm_text = chat_complete(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        api_key=key,
        model=resolved_model,
        provider=provider,
        temperature=0.1,
        max_tokens=_MAX_OUTPUT_TOKENS,
    )

    # -- Parse the proposed patch ----------------------------------------------
    patch = _parse_patch(llm_text)

    patched_xslt   = None
    patch_applied  = False
    patch_error    = None

    if patch["before"] and patch["after"] is not None:
        patched_xslt, patch_applied, patch_error = apply_patch(
            full_raw_xslt, patch["before"], patch["after"]
        )
        if patch_applied:
            is_valid, validation_error = validate_xslt_wellformed(patched_xslt)
            if not is_valid:
                patch_applied = False
                patch_error   = f"Patched XSLT failed XML validation: {validation_error}"
                patched_xslt  = None

    # -- Append patch status to response text ----------------------------------
    if patch_applied:
        status_note = (
            "\n\n---\n"
            "**Patch applied and validated.**  "
            "The modified XSLT is ready to download using the button below."
        )
    elif patch_error:
        status_note = (
            f"\n\n---\n"
            f"**Auto-apply note:** {patch_error}  \n"
            "Use the BEFORE/AFTER blocks above to apply the change manually in your editor."
        )
    else:
        # No BEFORE/AFTER produced (e.g. LLM said no change needed)
        status_note = ""

    return llm_text + status_note, patched_xslt


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("\n" + "=" * 80)
    print("  MODIFICATION ENGINE -- XSLT Edit Proposal + Auto-Apply")
    print("=" * 80 + "\n")

    if len(sys.argv) < 3:
        print("Usage: python modules/modification_engine.py <xslt_file> <request>\n")
        print("Examples:")
        print('  python modules/modification_engine.py "MappingData/810.xml" "Change sender ID to ACME001"')
        print('  python modules/modification_engine.py "MappingData/810.xml" "Add a new IT1 line item row with ItemId and Qty"')
        print('  python modules/modification_engine.py "MappingData/810.xml" "Add ShipDate field to the DTM segment"')
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

    print(f"[INGEST ] {xslt_path}")
    ingested = ingest_file(file_path=xslt_path)
    print(f"[TYPE   ] {ingested['metadata']['file_type']}")
    print(f"[REQUEST] {request_str}")
    print()

    print("[MODIFY ] Generating and applying patch...\n")
    response, patched = modify(ingested, modification_request=request_str)

    print("=" * 80)
    print(response)
    print("=" * 80 + "\n")

    if patched:
        out_path = Path(xslt_path).stem + "_patched.xml"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(patched)
        print(f"[SAVED  ] Patched XSLT written to: {out_path}")
    else:
        print("[INFO   ] No patch applied -- apply the BEFORE/AFTER blocks manually.")
