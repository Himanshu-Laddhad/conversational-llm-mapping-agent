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
import json
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
_EDI_SEGMENT_ORDER = {
    "ST": 0, "BIG": 1, "CUR": 2, "REF": 3, "N1": 4, "ITD": 5,
    "DTM": 6, "IT1": 7, "TDS": 8, "CAD": 9, "ISS": 10, "CTT": 11, "SE": 12
}
EDI_SEGMENT_SPECS: Dict[str, Dict[str, Any]] = {
    "BIG": {
        "name": "Beginning Segment for Invoice",
        "required_fields": {
            "BIG01": {"name": "Invoice Date", "type": "date", "format": "YYYYMMDD"},
            "BIG02": {"name": "Invoice Number", "type": "string"},
        },
        "optional_fields": {
            "BIG03": {"name": "Purchase Order Date", "type": "date"},
            "BIG04": {"name": "Purchase Order Number", "type": "string", "warning": "This is for PO numbers, not amounts"},
            "BIG07": {"name": "Invoice Amount", "type": "numeric", "recommended_for": ["Amount", "Total", "InvoiceNet"]},
        },
    },
    "TDS": {"name": "Total Dollar Summary", "required_fields": {"TDS01": {"name": "Total Invoice Amount", "type": "numeric", "recommended_for": ["Amount", "Total", "InvoiceNet"]}}, "optional_fields": {}},
    "ST": {"name": "Transaction Set Header", "required_fields": {"ST01": {"name": "Transaction Set ID", "type": "string", "default": "810"}, "ST02": {"name": "Transaction Control Number", "type": "string", "default": "0001"}}, "optional_fields": {}},
}
EDI_TRANSACTION_SPECS: Dict[str, Dict[str, Any]] = {
    "810": {"name": "Invoice", "segments": EDI_SEGMENT_SPECS},
    "850": {
        "name": "Purchase Order",
        "segments": {
            "BEG": {
                "name": "Beginning Segment for Purchase Order",
                "required_fields": {
                    "BEG01": {"name": "Transaction Set Purpose Code", "type": "string"},
                    "BEG02": {"name": "Purchase Order Type Code", "type": "string"},
                    "BEG03": {"name": "Purchase Order Number", "type": "string"},
                },
                "optional_fields": {},
            },
            "REF": {
                "name": "Reference Identification",
                "required_fields": {},
                "optional_fields": {
                    "REF01": {"name": "Reference Identification Qualifier", "type": "string"},
                    "REF02": {"name": "Reference Identification", "type": "string"},
                },
            },
        },
    },
    "856": {
        "name": "Ship Notice/Manifest",
        "segments": {
            "BSN": {
                "name": "Beginning Segment for Ship Notice",
                "required_fields": {
                    "BSN01": {"name": "Transaction Set Purpose Code", "type": "string"},
                    "BSN02": {"name": "Shipment Identification", "type": "string"},
                    "BSN03": {"name": "Date", "type": "date", "format": "YYYYMMDD"},
                },
                "optional_fields": {},
            }
        },
    },
}

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
    """Heuristic semantic confirmation of node relevance.

    Returns True only when at least one node's tag, name, select, or text
    contains a token derived from the user description.  Returns False when no
    node matches so the caller can fall through to the next search strategy.
    """
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
    return False


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


def parse_add_request(user_description: str, api_key: Optional[str] = None, model: Optional[str] = None) -> tuple:
    """Enhanced parser for varied non-EDI and EDI add phrasing."""
    text = (user_description or "").strip()
    src = ""
    seg = ""
    fld: Optional[str] = None
    fmt = ""

    # Pattern: "Add X to FIELD in FORMAT format"
    # Example: "Add ShipmentDate to DTM02 in YYYYMMDD format"
    m = re.search(r"add\s+(\w+)\s+to\s+([A-Z]{2,4}\d{2})\s+in\s+(\w+)\s+format", text, re.IGNORECASE)
    if m:
        source_field = m.group(1)
        target_field = m.group(2).upper()
        format_spec = m.group(3)
        segment_match = re.match(r"([A-Z]{2,4})", target_field)
        target_segment = segment_match.group(1) if segment_match else target_field
        return source_field, target_segment, target_field, format_spec

    # Pattern: Add X to Y as Z
    m = re.search(r"add\s+([A-Za-z_][A-Za-z0-9_]*)\s+to\s+([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.IGNORECASE)
    if m:
        src, seg, fld = m.group(1), m.group(2), m.group(3)
    # Pattern: Add X to Y field in Z segment
    if not src:
        m = re.search(r"add\s+([A-Za-z_][A-Za-z0-9_]*)\s+to\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+field)?\s+in\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.IGNORECASE)
        if m:
            src, fld, seg = m.group(1), m.group(2), m.group(3)
    # Pattern: Add X to Y / Y section
    if not src:
        m = re.search(r"add\s+([A-Za-z_][A-Za-z0-9_]*)\s+to\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+(?:section|segment|node|element))?", text, re.IGNORECASE)
        if m:
            src, seg = m.group(1), m.group(2)
            fld = None

    # EDI-friendly patterns
    if not seg:
        m = re.search(r"\bto\s+(?:the\s+)?([A-Za-z]{2,3})\s+segment\b", text, re.IGNORECASE)
        if m:
            seg = m.group(1)
    if not fld:
        m = re.search(r"\bas\s+([A-Za-z]{2,3}\d{2})\b", text, re.IGNORECASE)
        if m:
            fld = m.group(1)
    if not fld:
        m = re.search(r"\bto\s+([A-Za-z]{2,3}\d{2})\b", text, re.IGNORECASE)
        if m:
            fld = m.group(1)
    if not seg and fld:
        seg = re.sub(r"\d.*$", "", fld)
    # If parser captured field token as segment (e.g., BIG04), split it.
    if seg and re.match(r"^[A-Za-z]{2,4}\d{2}$", seg):
        if not fld:
            fld = seg.upper()
        if fld and fld.upper() == seg.upper():
            seg = re.sub(r"\d.*$", "", seg)

    low = text.lower()
    if re.search(r"\byyyy\s*/?\s*mm\s*/?\s*dd\b", low) or "yyyymmdd" in low or "ccyymmdd" in low:
        fmt = "YYYYMMDD"
    elif re.search(r"\bmm\s*/\s*dd\s*/\s*yy\b", low):
        fmt = "MM/DD/YY"
    elif re.search(r"\bmm\s*/\s*dd\s*/\s*yyyy\b", low):
        fmt = "MM/DD/YYYY"
    elif re.search(r"(\d+)\s*(?:decimal|decimals|decimal places?)", low):
        dm = re.search(r"(\d+)\s*(?:decimal|decimals|decimal places?)", low)
        fmt = f"{dm.group(1)} decimals" if dm else ""

    src = src or "UnknownField"
    seg = seg or "UnknownSegment"
    if not fld:
        fld = None

    seg_out = seg.upper() if re.match(r"^[A-Za-z]{2,3}$", seg) else seg
    if fld and re.match(r"^[A-Za-z]{2,3}\d{2}$", fld):
        fld = fld.upper()
    return src, seg_out, fld, fmt


def find_source_field_in_xslt(xslt_content: str, field_name: str) -> Optional[dict]:
    from lxml import etree  # noqa: PLC0415
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    for name in generate_field_variations(field_name):
        safe = name.replace("'", "")
        nodes = tree.xpath(
            f"//xsl:variable[contains(translate(@select,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{safe.lower()}')]",
            namespaces=_XSL_NS,
        )
        if nodes:
            v = nodes[0]
            vn = v.get("name", "")
            return {"type": "variable", "name": vn, "line": int(getattr(v, "sourceline", 1) or 1), "usage": f"${vn}" if vn else (v.get("select", "") or "")}
    for name in generate_field_variations(field_name):
        safe = name.replace("'", "")
        nodes = tree.xpath(
            f"//xsl:value-of[contains(translate(@select,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{safe.lower()}')]",
            namespaces=_XSL_NS,
        )
        if nodes:
            n = nodes[0]
            return {"type": "value_of", "name": field_name, "line": int(getattr(n, "sourceline", 1) or 1), "usage": n.get("select", field_name)}
    return None


def find_segment_insertion_point(xslt_content: str, target_segment: str) -> int:
    from lxml import etree  # noqa: PLC0415
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    seg = (target_segment or "").upper().strip()
    target_pos = _EDI_SEGMENT_ORDER.get(seg, 999)
    best_line = None
    best_pos = -1
    for elem in tree.iter():
        if not isinstance(elem.tag, str):
            continue
        local = etree.QName(elem).localname.upper()
        pos = _EDI_SEGMENT_ORDER.get(local, -1)
        if 0 <= pos < target_pos:
            ln = int(getattr(elem, "sourceline", 1) or 1)
            if pos > best_pos or (pos == best_pos and (best_line is None or ln > best_line)):
                best_pos = pos
                best_line = ln
    for xel in tree.xpath("//xsl:element[@name]", namespaces=_XSL_NS):
        nm = (xel.get("name") or "").upper()
        pos = _EDI_SEGMENT_ORDER.get(nm, -1)
        if 0 <= pos < target_pos:
            ln = int(getattr(xel, "sourceline", 1) or 1)
            if pos > best_pos or (pos == best_pos and (best_line is None or ln > best_line)):
                best_pos = pos
                best_line = ln
    if best_line is not None:
        return best_line + 1
    for xel in tree.xpath("//xsl:element[@name='ST'] | //ST", namespaces=_XSL_NS):
        return int(getattr(xel, "sourceline", 1) or 1) + 1
    return 180


def check_if_segment_exists(xslt_content: str, segment_name: str) -> bool:
    from lxml import etree  # noqa: PLC0415
    seg = (segment_name or "").upper().strip()
    if not seg:
        return False
    try:
        parser = etree.XMLParser(remove_blank_text=False, recover=True)
        tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    except Exception:
        return (f"<{seg}>" in xslt_content) or (f"<{seg} " in xslt_content) or (f'name="{seg}"' in xslt_content)
    if tree.xpath(f"//xsl:element[translate(@name,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')='{seg}']", namespaces=_XSL_NS):
        return True
    if tree.xpath(f"//*[translate(local-name(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')='{seg}']"):
        return True
    return (f"<{seg}>" in xslt_content) or (f"<{seg} " in xslt_content) or (f'name="{seg}"' in xslt_content)


def check_if_field_exists(xslt_content: str, segment_name: str, field_name: str) -> dict:
    from lxml import etree  # noqa: PLC0415
    seg = (segment_name or "").upper().strip()
    fld = (field_name or "").upper().strip()
    if not seg or not fld:
        return {"exists": False}
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    segments = tree.xpath(f"//xsl:element[translate(@name,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')='{seg}']", namespaces=_XSL_NS)
    if not segments:
        segments = tree.xpath(f"//*[translate(local-name(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')='{seg}']")
    if not segments:
        return {"exists": False}
    segment = segments[0]
    fields = segment.xpath(f".//xsl:element[translate(@name,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')='{fld}']", namespaces=_XSL_NS)
    if not fields:
        fields = segment.xpath(f".//*[translate(local-name(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')='{fld}']")
    if not fields:
        return {"exists": False}
    field = fields[0]
    value_nodes = field.xpath(".//xsl:value-of/@select | .//xsl:sequence/@select", namespaces=_XSL_NS)
    return {"exists": True, "current_mapping": str(value_nodes[0]) if value_nodes else "unknown", "line": int(getattr(field, "sourceline", 1) or 1)}


def find_existing_segment_location(xslt_content: str, segment_name: str) -> dict:
    from lxml import etree  # noqa: PLC0415
    seg = (segment_name or "").upper().strip()
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    lines = xslt_content.splitlines()
    segments = tree.xpath(f"//xsl:element[translate(@name,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')='{seg}']", namespaces=_XSL_NS)
    style = "dynamic"
    if not segments:
        segments = tree.xpath(f"//*[translate(local-name(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')='{seg}']")
        style = "literal"
    if not segments:
        raise ValueError(f"Segment {segment_name} not found")
    node = segments[0]
    start_line = int(getattr(node, "sourceline", 1) or 1)
    children = list(node)
    last_field_line = int(getattr(children[-1], "sourceline", start_line) or start_line) if children else start_line
    closing_tag_line = len(lines)
    start_i = max(last_field_line - 1, start_line - 1)
    if style == "dynamic":
        for i in range(start_i, len(lines)):
            if "</xsl:element>" in lines[i]:
                closing_tag_line = i + 1
                break
    else:
        for i in range(start_i, len(lines)):
            if re.search(rf"</\s*{re.escape(seg)}\s*>", lines[i], re.IGNORECASE):
                closing_tag_line = i + 1
                break
    return {"start_line": start_line, "last_field_line": last_field_line, "closing_tag_line": closing_tag_line, "style": style}


def infer_type_from_name(field_name: str) -> str:
    """Infer data type from field naming conventions."""
    n = (field_name or "").lower()
    if any(x in n for x in ("date", "time", "datetime", "timestamp")):
        return "date"
    if any(x in n for x in ("amount", "price", "total", "quantity", "qty", "cost", "rate")):
        return "numeric"
    return "string"


def similar_field_names(name1: str, name2: str) -> bool:
    """Heuristic semantic similarity for field names."""
    a = (name1 or "").lower()
    b = (name2 or "").lower()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    synonyms = [
        ["amount", "total", "sum", "value"],
        ["date", "datetime", "timestamp"],
        ["id", "number", "identifier", "code"],
        ["quantity", "qty", "count"],
        ["price", "rate", "cost"],
    ]
    return any(any(s in a for s in group) and any(s in b for s in group) for group in synonyms)


def get_transaction_type(xslt_content: str) -> str:
    """Detect EDI transaction type where possible."""
    txt = xslt_content or ""
    for code in EDI_TRANSACTION_SPECS:
        if f"<ST01>{code}</ST01>" in txt or f"X12_00401_{code}" in txt:
            return code
    return "unknown"


def detect_transformation_type(xslt_content: str) -> dict:
    """Detect broad transformation family for universal guidance."""
    txt = xslt_content or ""
    lower = txt.lower()
    if "x12_00401_810" in txt or "<ST01>810</ST01>" in txt:
        return {"type": "edi_810", "confidence": 0.95, "notes": "EDI 810 Invoice"}
    if "x12_00401_850" in txt or "<ST01>850</ST01>" in txt:
        return {"type": "edi_850", "confidence": 0.95, "notes": "EDI 850 Purchase Order"}
    if "x12_00401_856" in txt or "<ST01>856</ST01>" in txt:
        return {"type": "edi_856", "confidence": 0.95, "notes": "EDI 856 Ship Notice"}
    if "soap:envelope" in lower or "soapenv:" in lower:
        return {"type": "soap", "confidence": 0.9, "notes": "SOAP web service transformation"}
    if "json" in lower:
        return {"type": "rest", "confidence": 0.8, "notes": "REST/JSON transformation"}
    return {"type": "custom", "confidence": 0.5, "notes": "Custom XML transformation"}


def infer_segment_structure(segment_name: str, xslt_content: str) -> dict:
    """Infer unknown segment structure from existing XSLT patterns."""
    from lxml import etree  # noqa: PLC0415
    seg = (segment_name or "").strip()
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    segments = tree.xpath(
        f"//*[local-name()='{seg}'] | //xsl:element[@name='{seg}']",
        namespaces=_XSL_NS,
    )
    if not segments:
        return {"name": seg, "required_fields": {}, "optional_fields": {}, "inferred": True}
    segment = segments[0]
    existing_fields: Dict[str, Dict[str, Any]] = {}
    children = segment.xpath("./*[local-name()] | ./xsl:element", namespaces=_XSL_NS)
    for child in children:
        if hasattr(child, "get"):
            fname = child.get("name") or (str(getattr(child, "tag", "")).split("}")[-1])
        else:
            fname = str(getattr(child, "tag", "")).split("}")[-1]
        if not fname:
            continue
        value_nodes = child.xpath(".//xsl:value-of/@select | .//xsl:sequence/@select", namespaces=_XSL_NS)
        ftype = "string"
        if value_nodes:
            mapping = str(value_nodes[0])
            if "format-date" in mapping or "date" in mapping.lower():
                ftype = "date"
            elif "format-number" in mapping or any(k in mapping.lower() for k in ("amount", "price", "total")):
                ftype = "numeric"
        existing_fields[fname] = {"name": fname, "type": ftype, "inferred": True}
    return {
        "name": seg,
        "required_fields": {},
        "optional_fields": existing_fields,
        "inferred": True,
        "note": f"Structure inferred from existing {seg} in XSLT",
    }


def get_segment_spec(segment_name: str, xslt_content: str) -> dict:
    """Get segment spec from known transaction model or infer dynamically."""
    seg = (segment_name or "").upper()
    tx = get_transaction_type(xslt_content)
    if tx in EDI_TRANSACTION_SPECS:
        tx_specs = EDI_TRANSACTION_SPECS[tx].get("segments", {})
        if seg in tx_specs:
            spec = dict(tx_specs[seg])
            spec["inferred"] = False
            return spec
    if seg in EDI_SEGMENT_SPECS:
        spec = dict(EDI_SEGMENT_SPECS[seg])
        spec["inferred"] = False
        return spec
    return infer_segment_structure(seg, xslt_content)


def explain_xslt(xslt_content: str) -> str:
    """Human-readable explanation for any XSLT style family."""
    from lxml import etree  # noqa: PLC0415
    kind = detect_transformation_type(xslt_content)
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    templates = tree.xpath("//xsl:template", namespaces=_XSL_NS)
    variables = tree.xpath("//xsl:variable", namespaces=_XSL_NS)
    lines = [f"Transformation Type: {kind['notes']}", f"Templates Found: {len(templates)}"]
    for i, t in enumerate(templates[:5], start=1):
        match = t.get("match", "unknown")
        mode = t.get("mode")
        line = f"{i}. Template match `{match}`"
        if mode:
            line += f" (mode `{mode}`)"
        lines.append(line)
    lines.append(f"Variables Defined: {len(variables)}")
    for v in variables[:10]:
        lines.append(f"- {v.get('name','unknown')}: {v.get('select','')[:80]}")
    return "\n".join(lines)


def get_smart_recommendation(source_field_name: str, requested_field: str, segment: str, xslt_content: str = "") -> dict:
    seg = (segment or "").upper()
    req = (requested_field or "").upper()
    spec = get_segment_spec(seg, xslt_content or "")
    all_fields = {**spec.get("required_fields", {}), **spec.get("optional_fields", {})}
    field_spec = all_fields.get(req, {})
    src_lower = (source_field_name or "").lower()
    warnings: List[str] = []
    recommendations: List[dict] = []
    if not spec.get("inferred"):
        if field_spec.get("warning") and any(k in src_lower for k in ("amount", "total", "price", "net")):
            warnings.append(field_spec["warning"])
        for f_name, f_def in all_fields.items():
            rec_for = [x.lower() for x in f_def.get("recommended_for", [])]
            if rec_for and any(x in src_lower for x in rec_for):
                recommendations.append({"field": f_name, "name": f_def.get("name", f_name), "reason": f"Standard field for {source_field_name}"})
        for seg_name, seg_spec in EDI_SEGMENT_SPECS.items():
            if seg_name == seg:
                continue
            pool = {**seg_spec.get("required_fields", {}), **seg_spec.get("optional_fields", {})}
            for f_name, f_def in pool.items():
                rec_for = [x.lower() for x in f_def.get("recommended_for", [])]
                if rec_for and any(x in src_lower for x in rec_for):
                    recommendations.append({"field": f_name, "segment": seg_name, "name": f_def.get("name", f_name), "reason": f"Standard location for {source_field_name}"})
    else:
        inferred_fields = spec.get("optional_fields", {})
        for f_name, f_def in inferred_fields.items():
            if similar_field_names(source_field_name, f_name):
                recommendations.append({"field": f_name, "name": f_def.get("name", f_name), "reason": f"Similar to existing field {f_name}"})
            if f_def.get("type") == infer_type_from_name(source_field_name):
                recommendations.append({"field": f_name, "name": f_def.get("name", f_name), "reason": f"Same data type ({f_def.get('type')})"})
    msg = ""
    if recommendations:
        msg = "Based on similar fields in your XSLT, you might want to use:\n"
        for rec in recommendations[:2]:
            seg_part = f" in {rec.get('segment', seg)} segment" if rec.get("segment") else ""
            msg += f"- {rec['field']}{seg_part}: {rec['name']}\n"
        msg += "Or proceed with your requested field."
    elif warnings:
        msg = f"WARNING: {warnings[0]}\nReply with 'proceed' to continue anyway."
    return {
        "is_valid": True if spec.get("inferred") else (len(warnings) == 0),
        "warnings": warnings,
        "recommendations": recommendations,
        "user_message": msg,
        "inferred": bool(spec.get("inferred", False)),
    }


def _generate_field_code(field: str, source_var: str, format_spec: str, style: str) -> str:
    base = field or "BIG01"
    fld = base.upper() if re.match(r"^[A-Za-z]{2,3}\d{2}$", base) else base
    src = source_var or ""
    fmt = (format_spec or "").upper()
    fmt_lower = (format_spec or "").lower()
    if fmt in ("YYYYMMDD", "CCYYMMDD") or "date" in fmt_lower:
        value_expr = f"fn:format-dateTime({src}, '[Y0001][M01][D01]')"
    elif fmt in ("MM/DD/YY", "MM-DD-YY"):
        value_expr = f"fn:format-dateTime({src}, '[M01]/[D01]/[Y01]')"
    elif fmt in ("MM/DD/YYYY", "MM-DD-YYYY"):
        value_expr = f"fn:format-dateTime({src}, '[M01]/[D01]/[Y0001]')"
    elif "decimal" in fmt_lower:
        dm = re.search(r"(\d+)", fmt_lower)
        decimals = int(dm.group(1)) if dm else 2
        pattern = "0" if decimals <= 0 else ("0." + ("0" * decimals))
        value_expr = f"fn:format-number({src}, '{pattern}')"
    else:
        value_expr = src
    if style == "dynamic":
        return f"<xsl:element name=\"{fld}\">\n  <xsl:value-of select=\"{value_expr}\"/>\n</xsl:element>"
    return f"<{fld}>\n  <xsl:value-of select=\"{value_expr}\"/>\n</{fld}>"


def to_pascal_case(text: str) -> str:
    parts = [p for p in re.split(r"[_\s]+", text or "") if p]
    if len(parts) == 1 and re.search(r"[a-z][A-Z]", parts[0]):
        return parts[0][0].upper() + parts[0][1:]
    return "".join(p[:1].upper() + p[1:] for p in parts) or text


def to_camel_case(text: str) -> str:
    p = to_pascal_case(text)
    return (p[:1].lower() + p[1:]) if p else p


def detect_naming_style(segment_name: str, xslt_content: str) -> str:
    from lxml import etree  # noqa: PLC0415
    try:
        parser = etree.XMLParser(remove_blank_text=False, recover=True)
        tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    except Exception:
        return "PascalCase"
    seg = tree.xpath(f"//*[local-name()='{segment_name}'] | //xsl:element[@name='{segment_name}']", namespaces=_XSL_NS)
    if not seg:
        return "PascalCase"
    node = seg[0]
    names: List[str] = []
    for c in list(node)[:3]:
        nm = c.get("name") if hasattr(c, "get") else None
        if not nm and isinstance(getattr(c, "tag", None), str):
            nm = c.tag.split("}")[-1]
        if nm and not nm.lower().startswith("xsl"):
            names.append(nm)
    if not names:
        return "PascalCase"
    if all(n[:1].isupper() and any(ch.islower() for ch in n) for n in names):
        return "PascalCase"
    if all(n[:1].islower() and any(ch.isupper() for ch in n) for n in names):
        return "camelCase"
    if all(n.isupper() for n in names):
        return "UPPERCASE"
    if all(n.islower() for n in names):
        return "lowercase"
    return "PascalCase"


def normalize_field_name(source_field_name: str, target_segment: str, xslt_content: str) -> str:
    style = detect_naming_style(target_segment, xslt_content)
    base = source_field_name or "NewField"
    if base.lower().endswith("email"):
        base = "Email"
    if style == "PascalCase":
        return to_pascal_case(base)
    if style == "camelCase":
        return to_camel_case(base)
    if style == "UPPERCASE":
        return re.sub(r"[^A-Za-z0-9]", "", base).upper()
    if style == "lowercase":
        return re.sub(r"[^A-Za-z0-9]", "", base).lower()
    return to_pascal_case(base)


def _insert_field_into_existing_segment(xslt_content: str, location: dict, field_code: str) -> str:
    lines = xslt_content.splitlines()
    idx = max(0, min(len(lines), int(location.get("closing_tag_line", len(lines))) - 1))
    indent = ""
    if 0 <= idx - 1 < len(lines):
        indent = re.match(r"\s*", lines[idx - 1]).group(0) if lines[idx - 1] else ""
    block = [(indent + ln) if ln.strip() else ln for ln in field_code.splitlines()]
    return "\n".join(lines[:idx] + block + lines[idx:])


def auto_discover_source(xslt_content: str, field_def: dict) -> Optional[str]:
    hints: List[str] = []
    name = field_def.get("name", "")
    if "Invoice Date" in name:
        hints = ["InvoiceDate", "Date", "invoice_date", "InvDate"]
    elif "Invoice Number" in name:
        hints = ["InvoiceId", "InvoiceNumber", "invoice_id", "InvNum"]
    elif "Purchase Order" in name:
        hints = ["PONumber", "PurchaseOrder", "CustomerPurchaseOrder"]
    for hint in hints:
        src = find_source_field_in_xslt(xslt_content, hint)
        if src:
            return src.get("usage")
    return None


def generate_segment_code(segment: str, primary_field: str, source_var: str, format_spec: str, xslt_content: str = "") -> str:
    raw_segment = segment or "BIG"
    segment = raw_segment.upper() if re.match(r"^[A-Za-z]{2,3}$", raw_segment) else raw_segment
    raw_field = primary_field or (f"{segment}01" if re.match(r"^[A-Za-z]{2,3}$", segment) else "NewField")
    primary_field = raw_field.upper() if re.match(r"^[A-Za-z]{2,3}\d{2}$", raw_field) else raw_field
    spec = get_segment_spec(segment, xslt_content or "")
    if spec.get("inferred") or not spec.get("required_fields"):
        field_code = _generate_field_code(primary_field, source_var, format_spec, "dynamic")
        comment = f"<!-- Added {primary_field} field to {segment} segment -->"
        return f"{comment}\n<xsl:element name=\"{segment}\">\n  {field_code}\n</xsl:element>"
    blocks: List[str] = []
    required = spec.get("required_fields", {})
    for fname, fdef in required.items():
        if fname == primary_field:
            blocks.append(_generate_field_code(fname, source_var, format_spec or fdef.get("format", ""), "dynamic"))
            continue
        auto_src = auto_discover_source(xslt_content, fdef) if xslt_content else None
        if auto_src:
            blocks.append(_generate_field_code(fname, auto_src, fdef.get("format", ""), "dynamic"))
        elif fdef.get("default"):
            blocks.append(f"<xsl:element name=\"{fname}\">\n  <xsl:value-of select=\"'{fdef['default']}'\"/>\n</xsl:element>")
        else:
            blocks.append(f"<xsl:element name=\"{fname}\">\n  <xsl:value-of select=\"'REQUIRED_{fname}'\"/>\n</xsl:element>")
    if primary_field not in required:
        blocks.append(_generate_field_code(primary_field, source_var, format_spec, "dynamic"))
    joined = "\n".join("  " + ln if ln else ln for b in blocks for ln in b.splitlines())
    return f"<xsl:element name=\"{segment}\">\n{joined}\n</xsl:element>"


def find_output_template(xslt_tree, target_segment: str):
    templates = xslt_tree.xpath("//xsl:template", namespaces=_XSL_NS)
    for t in templates:
        hits = t.xpath(
            f".//*[local-name()='{target_segment}'] | .//xsl:element[@name='{target_segment}']",
            namespaces=_XSL_NS,
        )
        if hits:
            return t
    root_t = xslt_tree.xpath("//xsl:template[@match='/']", namespaces=_XSL_NS)
    return root_t[0] if root_t else (templates[0] if templates else None)


def find_segment_element(xslt_tree, target_segment: str):
    hits = xslt_tree.xpath(
        f"//*[local-name()='{target_segment}'] | //xsl:element[@name='{target_segment}']",
        namespaces=_XSL_NS,
    )
    return hits[0] if hits else None


def find_dom_insert_position(template, location: dict) -> int:
    target_segment = str(location.get("target_segment", "") or "")
    children = list(template)
    for i, child in enumerate(children):
        if not isinstance(getattr(child, "tag", None), str):
            continue
        cname = child.get("name") if hasattr(child, "get") else None
        if not cname:
            cname = child.tag.split("}")[-1]
        if cname == target_segment:
            return i + 1
    return len(children)


def _apply_add_segment_via_dom(xslt_content: str, location: dict, field_code: str) -> str:
    """True DOM insertion to avoid invalid XML from line-based text splicing."""
    from lxml import etree  # noqa: PLC0415
    parser = etree.XMLParser(remove_blank_text=False, recover=True)
    tree = etree.fromstring(xslt_content.encode("utf-8"), parser=parser)
    mode = location.get("add_mode", "create_new")

    if mode == "add_to_existing":
        segment = find_segment_element(tree, location.get("target_segment", ""))
        if segment is None:
            raise ValueError("Cannot find target segment element for insertion")
        # Replace path: remove existing field first to avoid nested duplicates.
        if location.get("replace_existing"):
            target_field = str(location.get("target_field", "") or "")
            if target_field:
                old_hits = segment.xpath(
                    f".//*[local-name()='{target_field}'] | .//xsl:element[@name='{target_field}']",
                    namespaces=_XSL_NS,
                )
                if old_hits:
                    old = old_hits[0]
                    parent = old.getparent()
                    if parent is not None:
                        parent.remove(old)
        new_field = etree.fromstring(field_code.encode("utf-8"), parser=parser)
        segment.append(new_field)
        return etree.tostring(tree, encoding="unicode")

    parent_template = find_output_template(tree, location.get("target_segment", ""))
    if parent_template is None:
        raise ValueError("Cannot find appropriate template for insertion")
    new_element = etree.fromstring(field_code.encode("utf-8"), parser=parser)
    pos = find_dom_insert_position(parent_template, location)
    parent_template.insert(pos, new_element)
    return etree.tostring(tree, encoding="unicode")


def locate_element_in_xslt(xslt_content: str, user_field_description: str, rag_engine=None) -> dict:
    """
    Multi-strategy element location with robust fallback.
    """
    desc = user_field_description or ""
    is_add_request = any(w in desc.lower() for w in ("add", "insert", "create"))
    if is_add_request:
        src, seg, fld, fmt = parse_add_request(desc)
        if not fld:
            fld = normalize_field_name(src, seg, xslt_content)
        source_location = find_source_field_in_xslt(xslt_content, src)
        if not source_location:
            source_location = {
                "type": "inferred",
                "name": src,
                "line": 1,
                "usage": src,
            }
        recommendation = get_smart_recommendation(src, fld, seg, xslt_content)
        if "proceed" in desc.lower() or "override" in desc.lower():
            recommendation["is_valid"] = True
            recommendation["user_message"] = ""
        segment_exists = check_if_segment_exists(xslt_content, seg)
        if segment_exists:
            field_check = check_if_field_exists(xslt_content, seg, fld)
            if field_check.get("exists"):
                if any(k in desc.lower() for k in ("replace existing", "overwrite", "replace it", "replace mapping")):
                    return {
                        "found": True,
                        "is_add_request": True,
                        "add_mode": "add_to_existing",
                        "replace_existing": True,
                        "source_field": src,
                        "source_location": source_location,
                        "target_segment": seg,
                        "target_field": fld,
                        "format": fmt,
                        "recommendation": recommendation,
                        "user_message": "",
                        "segment_location": find_existing_segment_location(xslt_content, seg),
                    }
                return {
                    "found": True,
                    "is_add_request": True,
                    "conflict": True,
                    "conflict_type": "field_exists",
                    "source_field": src,
                    "source_location": source_location,
                    "target_segment": seg,
                    "target_field": fld,
                    "format": fmt,
                    "recommendation": recommendation,
                    "current_mapping": field_check.get("current_mapping", "unknown"),
                    "line_number": field_check.get("line", 1) or 1,
                    "user_message": (
                        f"{fld} already exists (mapped to {field_check.get('current_mapping', 'unknown')}).\n"
                        + (recommendation.get("user_message") or "Replace existing mapping?")
                    ),
                }
            return {
                "found": True,
                "is_add_request": True,
                "add_mode": "add_to_existing",
                "source_field": src,
                "source_location": source_location,
                "target_segment": seg,
                "target_field": fld,
                "format": fmt,
                "recommendation": recommendation,
                "user_message": recommendation.get("user_message", ""),
                "segment_location": find_existing_segment_location(xslt_content, seg),
            }
        insertion_point = find_segment_insertion_point(xslt_content, seg)
        return {
            "found": True,
            "is_add_request": True,
            "add_mode": "create_new",
            "source_field": src,
            "source_location": source_location,
            "target_segment": seg,
            "target_field": fld,
            "format": fmt,
            "recommendation": recommendation,
            "user_message": recommendation.get("user_message", ""),
            "xpath_location": "",
            "line_number": source_location.get("line", 1),
            "current_value": source_location.get("usage", ""),
            "context_before": "",
            "context_after": "",
            "insertion_point": insertion_point,
            "suggestion": (
                f"Add {seg} segment after ST segment at line {insertion_point}, "
                f"map {src} to {fld} in {fmt or 'requested'} format"
            ),
        }

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


def _build_guidance_response(location: dict, guidance_type: str, message: str) -> str:
    """Embed machine-readable guidance for dispatcher/UI confirmation flow."""
    payload = {
        "status": "needs_confirmation",
        "type": guidance_type,
        "message": message,
        "recommendations": (location.get("recommendation") or {}).get("recommendations", []),
        "target_segment": location.get("target_segment"),
        "target_field": location.get("target_field"),
        "source_field": location.get("source_field"),
        "current_mapping": location.get("current_mapping"),
    }
    return (
        "## ACTION REQUIRED\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=True)}\n"
        "```\n\n"
        f"{message}"
    )


def extract_modify_guidance(response_text: str) -> dict:
    """Parse ACTION REQUIRED json block if present."""
    m = re.search(r"##\s*ACTION REQUIRED\s*```json\s*(\{.*?\})\s*```", response_text, re.DOTALL | re.IGNORECASE)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


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

    if location.get("is_add_request"):
        src_loc = location.get("source_location")
        if not src_loc:
            return (
                f"[modify] Source field '{location.get('source_field', '')}' not found in XSLT.",
                None,
            )
        if location.get("conflict"):
            return _build_guidance_response(location, "conflict", location.get("user_message", "Conflict detected.")), None
        recommendation = location.get("recommendation") or {}
        if location.get("user_message") and not recommendation.get("is_valid", True):
            return _build_guidance_response(location, "recommendation", location.get("user_message", "")), None

        target_segment = location.get("target_segment", "BIG")
        target_field = location.get("target_field", "BIG01")
        format_spec = location.get("format", "YYYYMMDD")
        transform_info = detect_transformation_type(full_raw_xslt)
        guidance_line = ""
        if transform_info.get("type") in {"custom", "unknown"}:
            guidance_line = (
                f"INFO: This appears to be a {transform_info.get('notes','custom transformation')}. "
                "I'm inferring the structure from your existing XSLT. Please verify output matches requirements.\n\n"
            )

        if location.get("add_mode") == "add_to_existing":
            seg_loc = location.get("segment_location") or find_existing_segment_location(full_raw_xslt, target_segment)
            before_snippet = (
                f"<!-- Existing {target_segment} found at line {seg_loc['start_line']} -->\n"
                f"<!-- Insert {target_field} before line {seg_loc['closing_tag_line']} -->"
            )
            field_code = _generate_field_code(
                field=target_field,
                source_var=src_loc.get("usage", location.get("source_field", "")),
                format_spec=format_spec,
                style=seg_loc.get("style", "dynamic"),
            )
            patched_xslt = _apply_add_segment_via_dom(full_raw_xslt, location, field_code)
            is_valid, validation_error = validate_xslt_wellformed(patched_xslt)
            if not is_valid:
                return f"[modify] Generated field-in-segment patch failed validation: {validation_error}", None
            response_text = (
                f"{guidance_line}"
                "## CHANGE SUMMARY\n"
                f"Add {target_field} to existing {target_segment} segment.\n\n"
                "## BEFORE\n```xml\n"
                f"{before_snippet}\n```\n\n"
                "## AFTER\n```xml\n"
                f"{field_code}\n```\n\n"
                "## EXPLANATION\n"
                f"Inserted `{target_field}` into existing `{target_segment}` while avoiding duplicate segment creation."
            )
            return response_text, patched_xslt

        insertion_point = int(location.get("insertion_point", 180) or 180)
        before_snippet = f"<!-- Line {insertion_point} -->\n<!-- No {target_segment} segment currently -->"
        after_snippet = generate_segment_code(
            segment=target_segment,
            primary_field=target_field,
            source_var=src_loc.get("usage", location.get("source_field", "")),
            format_spec=format_spec,
            xslt_content=full_raw_xslt,
        )
        patched_xslt = _apply_add_segment_via_dom(full_raw_xslt, location, after_snippet)
        is_valid, validation_error = validate_xslt_wellformed(patched_xslt)
        if not is_valid:
            return f"[modify] Generated add-segment patch failed validation: {validation_error}", None
        response_text = (
            f"{guidance_line}"
            "## CHANGE SUMMARY\n"
            f"Add complete {target_segment} segment and map {location.get('source_field','')} to {target_field}.\n\n"
            "## BEFORE\n```xml\n"
            f"{before_snippet}\n```\n\n"
            "## AFTER\n```xml\n"
            f"{after_snippet}\n```\n\n"
            "## EXPLANATION\n"
            f"Created `{target_segment}` with required fields and inserted it near line {insertion_point} based on EDI segment order."
        )
        return response_text, patched_xslt

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

    # Try the full-XSLT term-based extraction first — this scans the real file
    # for all occurrences of meaningful tokens from the modification request and
    # returns up to 5 bounded template blocks.  Fall back to the small context
    # window from locate_element_in_xslt only when the term scan finds nothing.
    candidate_blocks, extraction_error = _extract_real_candidate_blocks(
        full_raw_xslt, modification_request
    )
    if extraction_error or not candidate_blocks:
        # Fallback: use the ±10-line window around the located element.
        candidate_block = "\n".join(
            [location.get("context_before", ""), location.get("context_after", "")]
        ).strip()
        if not candidate_block:
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
        engine="modify",
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
