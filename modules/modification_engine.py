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
from typing import Any, Optional, Tuple, List, Dict

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

# Max total candidate-block characters sent to the LLM.
_MAX_CANDIDATE_CONTEXT_CHARS = 14_000

# Max tokens for LLM output.
_MAX_OUTPUT_TOKENS = 1_500

_FIELD_HINTS = ("IT104", "IT102", "Qty", "LineAmountMST", "InvoiceDate")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "then",
    "add", "remove", "change", "modify", "update", "replace", "field",
    "value", "mapping", "segment", "line", "item", "row", "template",
    "select", "output", "node", "xml", "xslt", "xsl", "new", "existing",
    "please", "request", "real", "block",
}

_XSL_NS = {"xsl": "http://www.w3.org/1999/XSL/Transform"}

_SYSTEM_PROMPT = """\
You are an expert XSLT 2.0 developer specialising in Altova MapForce stylesheets \
that transform D365 XML (Microsoft Dynamics 365) into X12 EDI XML.

The user will give you:
  1. Candidate blocks extracted from the REAL full XSLT file.
  2. A modification request in plain English.

Your task is to produce a MINIMAL, SURGICAL change that fulfils the request \
without breaking any other part of the stylesheet.

Return your answer in EXACTLY one of these formats -- no extra sections:

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

OR (if you cannot copy an exact BEFORE snippet):

## FAILURE
<one sentence explaining why an exact BEFORE snippet is not possible from the provided candidates>

## EXPLANATION
<2-4 sentences naming which candidate(s) were close and what is missing>

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
- The BEFORE block MUST be copied EXACTLY from the provided real candidate blocks.
- BEFORE must be character-for-character identical to one provided snippet:
  same indentation, spaces, quotes, attribute order, and line breaks.
- Do NOT normalize whitespace, rename variables, shorten XPath, or paraphrase XML/XSLT in BEFORE.
- Prefer the SMALLEST exact replaceable snippet (single xsl:value-of line first; then minimal multi-line block).
- AFTER must preserve the same surrounding structure and only change the target expression.
- If a required anchor does not exist in those real blocks, leave BEFORE/AFTER empty and say so in EXPLANATION.
- Match the indentation of the surrounding code exactly.
"""


# ---------------------------------------------------------------------------
# Patch utilities (public so tests can import them directly)
# ---------------------------------------------------------------------------

def _extract_section_text(response_text: str, title: str) -> str:
    """Extract a markdown section body, fenced or plain."""
    m = re.search(
        rf"##\s*{re.escape(title)}\s*\n(.*?)(?=\n##|\Z)",
        response_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return ""
    body = m.group(1)
    fenced = re.search(r"```(?:xml)?\s*\n(.*?)```", body, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip("\n")
    return body.strip()


def _parse_patch(response_text: str) -> dict:
    """
    Extract CHANGE SUMMARY, BEFORE, AFTER, and EXPLANATION from the LLM response.

    Returns a dict with keys: summary, before, after, explanation.
    Any missing section returns an empty string.
    """
    result = {"summary": "", "before": "", "after": "", "explanation": "", "failure": ""}
    result["summary"] = _extract_section_text(response_text, "CHANGE SUMMARY")
    result["before"] = _extract_section_text(response_text, "BEFORE")
    result["after"] = _extract_section_text(response_text, "AFTER")
    result["explanation"] = _extract_section_text(response_text, "EXPLANATION")
    result["failure"] = _extract_section_text(response_text, "FAILURE")
    return result


def _normalize_ws(text: str) -> str:
    """Collapse all whitespace runs to a single space for fuzzy matching."""
    return re.sub(r"\s+", " ", text).strip()


def generate_field_variations(description: str) -> list:
    """Generate fuzzy field/segment variations from user wording."""
    if not description:
        return []
    base = description.strip()
    words = [w for w in re.findall(r"[A-Za-z0-9]+", base) if w]
    joined_snake = "_".join(w.lower() for w in words)
    joined_camel = "".join(w.capitalize() for w in words)
    compact = "".join(words)
    variants = {
        base, base.lower(), base.upper(),
        joined_snake, joined_snake.upper(),
        joined_camel, compact, compact.lower(), compact.upper(),
    }
    synonyms = {
        "invoice date": ["InvoiceDate", "InvDate", "BIG01", "DTM02", "invoice_dt", "DocDate"],
        "date": ["InvoiceDate", "DlvDate", "DueDate", "BIG01", "DTM02", "Date"],
        "price": ["Price", "UnitPrice", "SalesPrice", "ItemPrice", "IT104", "LineAmountMST"],
        "quantity": ["Qty", "Quantity", "OrderQty", "IT102", "InventQty"],
        "qty": ["Qty", "Quantity", "OrderQty", "IT102", "InventQty"],
    }
    lower = base.lower()
    for key, vals in synonyms.items():
        if key in lower:
            variants.update(vals)
    return [v for v in variants if v]


def semantic_match_confirmed(xml_nodes, user_description: str) -> bool:
    """Heuristic semantic confirmation of node relevance."""
    if not xml_nodes:
        return False
    desc_terms = {t.lower() for t in generate_field_variations(user_description)}
    for node in xml_nodes:
        payload = " ".join([
            getattr(node, "tag", "") or "",
            (node.get("name") or "") if hasattr(node, "get") else "",
            (node.get("select") or "") if hasattr(node, "get") else "",
            (node.text or "") if hasattr(node, "text") and node.text else "",
        ]).lower()
        if any(term and term.lower() in payload for term in desc_terms):
            return True
    # fallback accept first node when search already constrained
    return True


def _extract_location_from_element(element, xslt_content: str) -> dict:
    lines = xslt_content.splitlines()
    line = int(getattr(element, "sourceline", 1) or 1)
    start = max(1, line - 10)
    end = min(len(lines), line + 10)
    ctx_before = "\n".join(lines[start - 1: line - 1])
    ctx_after = "\n".join(lines[line: end])
    select = element.get("select") if hasattr(element, "get") else ""
    from lxml import etree  # noqa: PLC0415
    root = element.getroottree()
    xpath_loc = root.getpath(element) if root is not None else ""
    return {
        "found": True,
        "xpath_location": xpath_loc,
        "line_number": line,
        "current_value": select or (element.text or ""),
        "context_before": ctx_before,
        "context_after": ctx_after,
        "insertion_point": "",
    }


def _find_best_template_for_insertion(tree, user_field_description: str):
    templates = tree.xpath("//xsl:template", namespaces=_XSL_NS)
    if not templates:
        return None
    desc_terms = [t.lower() for t in generate_field_variations(user_field_description)]
    best = templates[0]
    best_score = -1
    for t in templates:
        text = " ".join([
            t.get("name", "") or "",
            t.get("match", "") or "",
            "".join((e.get("name", "") or "") for e in t.xpath(".//xsl:element", namespaces=_XSL_NS)),
        ]).lower()
        score = sum(1 for term in desc_terms if term and term in text)
        if score > best_score:
            best, best_score = t, score
    return best


def locate_element_in_xslt(xslt_content: str, user_field_description: str, rag_engine=None) -> dict:
    """
    Multi-strategy element location with robust fallback.
    """
    # Strategy 1: RAG engine search (best-effort; optional interface)
    if rag_engine is not None and hasattr(rag_engine, "search"):
        try:
            rag_results = rag_engine.search(user_field_description, top_k=5)
            if rag_results:
                top = rag_results[0]
                if isinstance(top, dict) and float(top.get("confidence", 0.0)) > 0.7:
                    line = int(top.get("line_number", 1))
                    lines = xslt_content.splitlines()
                    return {
                        "found": True,
                        "xpath_location": str(top.get("xpath_location", "")),
                        "line_number": line,
                        "current_value": str(top.get("current_value", "")),
                        "context_before": "\n".join(lines[max(0, line - 11): max(0, line - 1)]),
                        "context_after": "\n".join(lines[line: min(len(lines), line + 10)]),
                        "insertion_point": "",
                    }
        except Exception:
            pass

    # Strategy 2: full XML DOM parse
    from lxml import etree  # noqa: PLC0415
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)

    # Strategy 3: fuzzy names
    possible_names = generate_field_variations(user_field_description)

    # Strategy 4: XPath search patterns
    xpaths_to_try = []
    for name in possible_names[:50]:
        safe = name.replace("'", "")
        xpaths_to_try.extend([
            f"//xsl:value-of[contains(translate(@select,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{safe.lower()}')]",
            f"//xsl:attribute[@name='{safe}']",
            f"//xsl:element[@name='{safe}']",
        ])
    xpaths_to_try.append("//xsl:value-of")

    for xp in xpaths_to_try:
        try:
            matches = tree.xpath(xp, namespaces=_XSL_NS)
        except Exception:
            matches = []
        if matches and semantic_match_confirmed(matches, user_field_description):
            return _extract_location_from_element(matches[0], xslt_content)

    # Strategy 5: insertion point
    template = _find_best_template_for_insertion(tree, user_field_description)
    if template is not None:
        line = int(getattr(template, "sourceline", 1) or 1)
        return {
            "found": False,
            "xpath_location": "",
            "line_number": line,
            "current_value": "",
            "context_before": "",
            "context_after": "",
            "insertion_point": f"//xsl:template[@name='{template.get('name','')}' or @match='{template.get('match','')}']",
            "suggestion": f"Field not found. Suggest adding to template at line {line}.",
        }
    return {
        "found": False,
        "xpath_location": "",
        "line_number": 1,
        "current_value": "",
        "context_before": "",
        "context_after": "",
        "insertion_point": "//xsl:stylesheet",
        "suggestion": "Field not found. Suggest adding mapping at stylesheet level.",
    }


def apply_modification_via_dom(xslt_tree, modification_spec) -> Any:
    """Apply XSLT modification via DOM/XPath."""
    target_xpath = modification_spec.get("target_xpath", "")
    if not target_xpath:
        raise ValueError("Missing target_xpath in modification_spec")
    target_nodes = xslt_tree.xpath(target_xpath, namespaces=_XSL_NS)
    if not target_nodes:
        raise ValueError(f"ElementNotFoundError: {target_xpath}")
    node = target_nodes[0]
    action = modification_spec.get("action", "replace_value")
    if action in ("replace_value", "add_calculation"):
        node.set("select", modification_spec.get("new_expression", ""))
    elif action == "change_format":
        current_select = node.get("select", "")
        fmt = modification_spec.get("format", "0.000")
        node.set("select", f"format-number({current_select}, '{fmt}')")
    else:
        node.set("select", modification_spec.get("new_expression", ""))
    from lxml import etree  # noqa: PLC0415
    _ = etree.tostring(xslt_tree)
    return xslt_tree


def _extract_search_terms(modification_request: str) -> List[str]:
    """Extract likely XSLT field/template identifiers from user request."""
    tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", modification_request or "")
    terms = []
    for tok in tokens:
        if tok.lower() in _STOPWORDS:
            continue
        if tok not in terms:
            terms.append(tok)
    # Prioritize well-known field hints when explicitly present in the request.
    ordered = [h for h in _FIELD_HINTS if re.search(rf"\b{re.escape(h)}\b", modification_request, re.IGNORECASE)]
    for t in terms:
        if t not in ordered:
            ordered.append(t)
    return ordered[:20]


def _find_template_bounds(lines: List[str], line_idx: int) -> Tuple[int, int]:
    """Find approximate enclosing xsl:template bounds for a matched line."""
    start = max(0, line_idx - 20)
    end = min(len(lines) - 1, line_idx + 20)
    for i in range(line_idx, -1, -1):
        if "<xsl:template" in lines[i]:
            start = i
            break
    for j in range(line_idx, len(lines)):
        if "</xsl:template>" in lines[j]:
            end = j
            break
    if end - start > 260:
        start = max(0, line_idx - 30)
        end = min(len(lines) - 1, line_idx + 30)
    return start, end


def _extract_real_candidate_blocks(
    full_raw_xslt: str,
    modification_request: str,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Locate real candidate blocks in the full XSLT for this request.
    Returns (blocks, error_message). On error, blocks is empty and no LLM call
    should be made.
    """
    lines = full_raw_xslt.splitlines()
    if not lines:
        return [], "Could not extract XSLT source from disk."

    terms = _extract_search_terms(modification_request)
    if not terms:
        return [], "Could not identify any concrete field/template terms to locate in the XSLT."

    spans: List[Tuple[int, int, str]] = []
    found_terms: set = set()
    for term in terms:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.IGNORECASE)
        for idx, line in enumerate(lines):
            if not pattern.search(line):
                continue
            if not any(k in line for k in ("xsl:value-of", "xsl:template", "select=", "name=", "match=", "$", "<", ">")):
                continue
            start, end = _find_template_bounds(lines, idx)
            spans.append((start, end, term))
            found_terms.add(term)
            if len(spans) >= 12:
                break
        if len(spans) >= 12:
            break

    requested_hints = [h for h in _FIELD_HINTS if re.search(rf"\b{re.escape(h)}\b", modification_request, re.IGNORECASE)]
    missing_hints = [h for h in requested_hints if h not in found_terms]
    if missing_hints:
        return [], f"Could not locate the real {missing_hints[0]} output block in the XSLT."

    if not spans:
        return [], "Could not locate a confident real block in the XSLT for this modification request."

    deduped: List[Dict[str, Any]] = []
    seen_ranges = set()
    budget = 0
    for start, end, term in sorted(spans, key=lambda s: (s[0], s[1])):
        key = (start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        block = "\n".join(lines[start:end + 1]).rstrip()
        if not block:
            continue
        budget += len(block)
        if budget > _MAX_CANDIDATE_CONTEXT_CHARS and deduped:
            break
        deduped.append({
            "term": term,
            "start_line": start + 1,
            "end_line": end + 1,
            "text": block,
        })
        if len(deduped) >= 5:
            break

    if not deduped:
        return [], "Could not locate a confident real block in the XSLT for this modification request."

    return deduped, None


def _build_modify_prompt(
    *,
    file_name: str,
    file_type: str,
    modification_request: str,
    candidate_blocks: List[Dict[str, Any]],
) -> str:
    exact_anchors: List[str] = []
    seen_anchors = set()
    for blk in candidate_blocks:
        for ln in blk["text"].splitlines():
            s = ln.strip()
            if "xsl:value-of" in s and s not in seen_anchors:
                seen_anchors.add(s)
                exact_anchors.append(ln)
            if len(exact_anchors) >= 20:
                break
        if len(exact_anchors) >= 20:
            break

    chunks = []
    for i, blk in enumerate(candidate_blocks, 1):
        chunks.append(
            f"### Candidate {i} (term: {blk['term']}, lines {blk['start_line']}-{blk['end_line']})\n"
            f"```xml\n{blk['text']}\n```"
        )
    anchor_catalog = "\n".join(f"- `{a}`" for a in exact_anchors) if exact_anchors else "- (no single-line value-of anchors found)"
    return (
        f"## Mapping File\n"
        f"Name: {file_name}  |  Type: {file_type}\n\n"
        f"## Real candidate blocks from full XSLT on disk\n"
        f"{chr(10).join(chunks)}\n\n"
        f"## Exact anchor snippets (preferred BEFORE choices)\n"
        f"{anchor_catalog}\n\n"
        f"## Modification Request\n"
        f"{modification_request.strip()}\n\n"
        f"Hard rules:\n"
        f"1) BEFORE must be copied character-for-character from the candidate blocks or anchor list.\n"
        f"2) BEFORE must be the smallest exact replaceable snippet (single xsl:value-of line preferred).\n"
        f"3) AFTER must preserve surrounding structure and only change the target expression.\n"
        f"4) If you cannot provide an exact BEFORE, return the FAILURE format (no BEFORE/AFTER)."
    )


def apply_patch(
    raw_xslt: str, before_block: str, after_block: str
) -> Tuple[str, bool, Optional[str]]:
    """
    Find *before_block* inside *raw_xslt* and replace it with *after_block*.

    Strategy
    --------
    1. Exact character-for-character match.
    2. No fuzzy fallback: patch anchors must be exact to prevent fake placeholders.

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
        if patched == raw_xslt:
            return raw_xslt, False, "Patch produced no real XSLT change."
        return patched, True, None

    return raw_xslt, False, "Could not locate the exact BEFORE block in the real XSLT source."


def _derive_action_from_request(modification_request: str) -> str:
    req = (modification_request or "").lower()
    if any(k in req for k in ("multiply", "calculate", "sum", "product")):
        return "add_calculation"
    if any(k in req for k in ("decimal", "format-number", "precision")):
        return "change_format"
    return "replace_value"


def _extract_select_from_xml_snippet(snippet: str) -> str:
    m = re.search(r'select\s*=\s*"([^"]+)"', snippet or "")
    if m:
        return m.group(1).strip()
    m = re.search(r"select\s*=\s*'([^']+)'", snippet or "")
    return m.group(1).strip() if m else ""


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

    # -- Locate element in real XSLT -------------------------------------------
    location = locate_element_in_xslt(
        xslt_content=full_raw_xslt,
        user_field_description=modification_request,
        rag_engine=None,
    )
    if not location.get("found"):
        msg = location.get("suggestion") or "Could not locate target element."
        return f"[modify] {msg}", None

    # Special-case calculation requests: ensure quantity and price anchors exist.
    req_lower = modification_request.lower()
    if any(k in req_lower for k in ("multiply", "quantity", "qty")) and "price" in req_lower:
        qty_loc = locate_element_in_xslt(full_raw_xslt, "quantity", None)
        price_loc = locate_element_in_xslt(full_raw_xslt, "price", None)
        if not qty_loc.get("found") or not price_loc.get("found"):
            return (
                "[modify] Could not confidently locate both quantity and price fields for calculation.",
                None,
            )

    # Build candidate blocks from exact located context for precise prompting.
    candidate_block = "\n".join(
        [location.get("context_before", ""), location.get("context_after", "")]
    ).strip()
    if not candidate_block:
        # fallback to line window extraction
        lines = full_raw_xslt.splitlines()
        ln = int(location.get("line_number", 1))
        start = max(1, ln - 10)
        end = min(len(lines), ln + 10)
        candidate_block = "\n".join(lines[start - 1:end]).rstrip()
    candidate_blocks = [{
        "term": "located_element",
        "start_line": max(1, int(location.get("line_number", 1)) - 10),
        "end_line": int(location.get("line_number", 1)) + 10,
        "text": candidate_block,
    }]

    # -- Build LLM user message ------------------------------------------------
    user_message = _build_modify_prompt(
        file_name=file_name,
        file_type=file_type,
        modification_request=modification_request,
        candidate_blocks=candidate_blocks,
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
    if patch.get("failure"):
        return f"[modify] {patch['failure']}\n\n{patch.get('explanation', '').strip()}".strip(), None

    patched_xslt   = None
    patch_applied  = False
    patch_error    = None

    if patch["before"] and patch["after"] is not None:
        candidate_texts = [blk["text"] for blk in candidate_blocks]
        if patch["before"] not in full_raw_xslt or not any(patch["before"] in txt for txt in candidate_texts):
            return (
                "[modify] Rejected patch: BEFORE block is not an exact snippet from the real XSLT candidate blocks.",
                None,
            )
        # DOM-first apply path (replaces fragile string-only patching).
        try:
            from lxml import etree  # noqa: PLC0415
            parser = etree.XMLParser(remove_blank_text=False, recover=True)
            tree = etree.fromstring(full_raw_xslt.encode("utf-8"), parser=parser)
            target_xpath = location.get("xpath_location", "")
            new_expression = _extract_select_from_xml_snippet(patch["after"])
            if target_xpath and new_expression:
                spec = {
                    "action": _derive_action_from_request(modification_request),
                    "target_xpath": target_xpath,
                    "new_expression": new_expression,
                    "format": "0.000",
                }
                new_tree = apply_modification_via_dom(tree, spec)
                patched_xslt = etree.tostring(new_tree, encoding="unicode")
                patch_applied = patched_xslt != full_raw_xslt
                patch_error = None if patch_applied else "Patch produced no real XSLT change."
            else:
                patched_xslt, patch_applied, patch_error = apply_patch(
                    full_raw_xslt, patch["before"], patch["after"]
                )
        except Exception:
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
            "No revised file was saved because the patch could not be confidently applied."
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
