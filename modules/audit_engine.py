"""
audit_engine.py
───────────────
Audit / validate an XSLT or EDI mapping for misconfigurations, risky patterns,
and potential production issues.

Two-layer pipeline
──────────────────
Layer 1 — Hardcoded rule checks (pure Python, no API call, instant):
  Scans the parsed XSLT structure for known high-risk patterns:
    ISA_IDS        Sender/receiver IDs hardcoded
    SE_COUNT       Segment count hardcoded (almost always wrong)
    GS_DATETIME    Functional group date/time hardcoded
    ISA_DATETIME   Interchange date/time hardcoded
    CONTROL_NUM    Control numbers ISA13/GS06 hardcoded
    TEST_DATA      Test values left in (000001, TESTID, TEST, etc.)
    CURRENCY       Currency code hardcoded
    NO_UOM         Quantity mapped without unit-of-measure element
    IF_NO_ELSE     xsl:if with no xsl:otherwise fallback
    ISA_QUALIFIER  ISA05/ISA07 qualifiers not in valid set

Layer 2 — LLM dynamic checks (Groq, one API call):
  Sends the XSLT content + Layer 1 findings to the LLM. Instructs it to find:
    - Subtle XPath logic errors (e.g. price / 100 unintentionally)
    - Conditions that silently drop required segments
    - Placeholder data Layer 1 missed
    - Cross-field inconsistencies (extended amount ≠ price × qty)
    - Non-obvious mapping decisions a new analyst should verify

Output format
─────────────
## AUDIT REPORT

### CRITICAL — must fix before production
- [SE_COUNT] ...

### WARNINGS — review recommended
- [ISA_IDS] ...

### INFO — for awareness
- [CURRENCY] ...

### DYNAMIC FINDINGS (LLM)
- [LOGIC] ...

### QUESTIONS FOR YOU
1. ...

Token budget (llama-3.3-70b-versatile ~12 000 TPM):
  System prompt  : ~400 tokens
  Layer 1 summary: ~200 tokens
  XSLT content   : ≤ 6 000 chars ≈ 1 500 tokens
  Output budget  : 1 200 tokens
  Total          : ~3 300 tokens  ← within 12 000 TPM

Usage (as module)::

    from modules.audit_engine import audit
    from modules.file_ingestion import ingest_file

    ingested = ingest_file("MappingData/.../810_NordStrom_Xslt_11-08-2023.xml")
    response, _ = audit(ingested)
    print(response)

    # With extra context (used by auto-audit after modify/generate):
    response, _ = audit(ingested, context=modify_response)

Usage (standalone CLI)::

    python modules/audit_engine.py MappingData/.../810_NordStrom_Xslt_11-08-2023.xml
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

from dotenv import load_dotenv
from groq import Groq

# Load .env from module directory or one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# ── Constants ─────────────────────────────────────────────────────────────────

# Max XSLT chars sent to LLM for dynamic analysis
_MAX_XSLT_CHARS = 6_000

# Max output tokens for the LLM audit response
_MAX_OUTPUT_TOKENS = 1_200

# Valid ISA qualifier codes (ISA05 / ISA07)
_VALID_ISA_QUALIFIERS = {
    "ZZ", "01", "02", "03", "04", "07", "08", "09",
    "10", "11", "12", "13", "14", "15", "16", "17",
    "18", "19", "20", "21", "22", "23", "24", "25",
    "26", "27", "28", "29", "30", "31", "32", "33",
    "34", "35", "36", "37", "38", "AM", "NR", "SA",
    "SN", "X",
}

# Patterns that suggest test/placeholder data
_TEST_PATTERNS = re.compile(
    r"(TEST|DUMMY|PLACEHOLDER|SAMPLE|EXAMPLE|000001|00000001|SENDER|RECEIVER|"
    r"ACME|MYCOMPANY|YOURCOMPANY|XXXXXX|999999|123456789)",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """\
You are a senior EDI/XSLT integration analyst performing a production-readiness audit \
on an Altova MapForce XSLT 2.0 stylesheet.

You will receive:
  1. The XSLT source (possibly truncated).
  2. A list of findings already detected by automated rule checks (Layer 1).

Your task: find ADDITIONAL issues that the automated rules missed. Focus on:
  - Subtle XPath expressions that may cause wrong values (wrong field, division/multiplication errors)
  - xsl:if / xsl:choose conditions that could silently drop required EDI segments
  - Placeholder or leftover test data the automated rules did not catch
  - Cross-field inconsistencies (e.g. extended amount does not equal unit price × quantity)
  - Non-obvious mapping decisions a new analyst should verify before production use
  - Any D365 source field paths that look suspicious or are likely incorrect

Return EXACTLY this format (no preamble, no extra commentary):

### DYNAMIC FINDINGS (LLM)
- [RULE_ID] <one-sentence description of the finding and why it matters>
(repeat for each finding, max 8)

### QUESTIONS FOR YOU
1. <specific yes/no or value-confirmation question for the user>
(max 6 questions, ordered by severity — most critical first)

If you find no additional issues, write:
### DYNAMIC FINDINGS (LLM)
No additional issues found beyond the automated checks.

### QUESTIONS FOR YOU
No additional questions.
"""


# ── Finding dataclass ─────────────────────────────────────────────────────────

@dataclass
class Finding:
    rule_id:  str    # e.g. "SE_COUNT"
    severity: str    # "FAIL" | "WARNING" | "INFO"
    message:  str    # human-readable description
    layer:    str    # "rule" | "llm"


# ── Layer 1: hardcoded rule checks ────────────────────────────────────────────

def _run_rule_checks(ingested: dict) -> List[Finding]:
    """Run all hardcoded rule checks and return a list of Findings."""
    findings: List[Finding] = []
    parsed = ingested.get("parsed_content", {})
    raw_xml: str = parsed.get("raw_xml", "") or parsed.get("raw_text", "") or ""
    hardcoded: list = parsed.get("hardcoded_values", []) or []
    templates: list = parsed.get("templates", []) or []

    # Build a flat lookup: element_name → value for all hardcoded entries
    hc_map: dict = {}
    for item in hardcoded:
        if isinstance(item, dict):
            name = str(item.get("element", item.get("name", ""))).upper()
            val  = str(item.get("value", ""))
            if name:
                hc_map[name] = val

    # Supplement hc_map by scanning raw_xml for EDI output elements with
    # literal string content (e.g. <ISA06>ACME001</ISA06>).
    # MapForce XSLTs may output these as fixed XML element values.
    if raw_xml:
        for m in re.finditer(
            r"<([A-Z]{2,4}[0-9]{2,3})>([^<\n]{1,40})</\1>",
            raw_xml,
        ):
            elem = m.group(1).upper()
            val  = m.group(2).strip()
            if val and elem not in hc_map:
                hc_map[elem] = val

    # ── ISA_IDS: ISA06 / ISA08 hardcoded ─────────────────────────────────────
    for field_name in ("ISA06", "ISA08"):
        if field_name in hc_map:
            findings.append(Finding(
                rule_id="ISA_IDS",
                severity="WARNING",
                message=(
                    f"{field_name} (sender/receiver ID) is hardcoded as "
                    f'"{hc_map[field_name]}" — confirm this is correct for '
                    f"this trading partner in production."
                ),
                layer="rule",
            ))

    # ── SE_COUNT: SE01 hardcoded or empty ────────────────────────────────────
    if "SE01" in hc_map:
        findings.append(Finding(
            rule_id="SE_COUNT",
            severity="FAIL",
            message=(
                f'Segment count SE01 is hardcoded as "{hc_map["SE01"]}" — '
                f"this value will be wrong for transactions with a different "
                f"number of segments. SE01 should be computed dynamically."
            ),
            layer="rule",
        ))
    elif raw_xml and re.search(r"<SE01>[^<]*</SE01>", raw_xml):
        m = re.search(r"<SE01>([^<]*)</SE01>", raw_xml)
        if m:
            val = m.group(1).strip()
            if val.isdigit():
                findings.append(Finding(
                    rule_id="SE_COUNT",
                    severity="FAIL",
                    message=(
                        f'Segment count SE01 is hardcoded as "{val}" in raw XML — '
                        f"should be computed dynamically."
                    ),
                    layer="rule",
                ))
            elif val == "":
                findings.append(Finding(
                    rule_id="SE_COUNT",
                    severity="FAIL",
                    message=(
                        "Segment count SE01 is empty — it must be set to the actual "
                        "number of segments in the transaction set before production use."
                    ),
                    layer="rule",
                ))

    # ── GS_DATETIME: GS04 / GS05 hardcoded ───────────────────────────────────
    for field_name in ("GS04", "GS05"):
        if field_name in hc_map:
            findings.append(Finding(
                rule_id="GS_DATETIME",
                severity="WARNING",
                message=(
                    f"{field_name} (functional group date/time) is hardcoded as "
                    f'"{hc_map[field_name]}" — this will send a stale date/time '
                    f"for every transaction. It should be generated dynamically."
                ),
                layer="rule",
            ))

    # ── ISA_DATETIME: ISA09 / ISA10 hardcoded ────────────────────────────────
    for field_name in ("ISA09", "ISA10"):
        if field_name in hc_map:
            findings.append(Finding(
                rule_id="ISA_DATETIME",
                severity="WARNING",
                message=(
                    f"{field_name} (interchange date/time) is hardcoded as "
                    f'"{hc_map[field_name]}" — should be dynamic.'
                ),
                layer="rule",
            ))

    # ── CONTROL_NUM: ISA13 / GS06 hardcoded ──────────────────────────────────
    for field_name in ("ISA13", "GS06"):
        if field_name in hc_map:
            findings.append(Finding(
                rule_id="CONTROL_NUM",
                severity="WARNING",
                message=(
                    f"{field_name} (control number) is hardcoded as "
                    f'"{hc_map[field_name]}" — control numbers must be unique '
                    f"per transmission. A static value will cause duplicate "
                    f"control number rejections."
                ),
                layer="rule",
            ))

    # ── TEST_DATA: test-like values in ISA06/ISA08/ISA13 ─────────────────────
    for field_name in ("ISA06", "ISA08", "ISA13", "GS02", "GS03"):
        val = hc_map.get(field_name, "")
        if val and _TEST_PATTERNS.search(val):
            findings.append(Finding(
                rule_id="TEST_DATA",
                severity="FAIL",
                message=(
                    f'{field_name} value "{val}" looks like test/placeholder '
                    f"data — replace with the correct production value before "
                    f"going live."
                ),
                layer="rule",
            ))

    # ── CURRENCY: hardcoded currency code ────────────────────────────────────
    for field_name in ("CUR02", "CUR01", "AMT01"):
        if field_name in hc_map:
            findings.append(Finding(
                rule_id="CURRENCY",
                severity="INFO",
                message=(
                    f"Currency field {field_name} is hardcoded as "
                    f'"{hc_map[field_name]}" — acceptable for single-currency '
                    f"partners, but verify if multi-currency transactions are expected."
                ),
                layer="rule",
            ))

    # ── ISA_QUALIFIER: ISA05 / ISA07 validity ────────────────────────────────
    for field_name in ("ISA05", "ISA07"):
        val = hc_map.get(field_name, "").strip()
        if val and val.upper() not in _VALID_ISA_QUALIFIERS:
            findings.append(Finding(
                rule_id="ISA_QUALIFIER",
                severity="FAIL",
                message=(
                    f'{field_name} qualifier "{val}" is not a recognised X12 '
                    f"ISA qualifier code. Valid values include ZZ, 01, 12, 14, "
                    f"20. This will likely cause rejection at the trading partner."
                ),
                layer="rule",
            ))

    # ── IF_NO_ELSE: xsl:if without xsl:otherwise in raw XML ──────────────────
    if raw_xml:
        # Count xsl:if vs xsl:otherwise blocks
        if_count        = len(re.findall(r"<xsl:if\b", raw_xml, re.IGNORECASE))
        otherwise_count = len(re.findall(r"<xsl:otherwise\b", raw_xml, re.IGNORECASE))
        choose_count    = len(re.findall(r"<xsl:choose\b", raw_xml, re.IGNORECASE))

        # xsl:if without any xsl:otherwise at all (basic check)
        if if_count > 0 and otherwise_count == 0:
            findings.append(Finding(
                rule_id="IF_NO_ELSE",
                severity="WARNING",
                message=(
                    f"Found {if_count} xsl:if block(s) but no xsl:otherwise "
                    f"fallback anywhere in the stylesheet. If a condition is "
                    f"false, the corresponding output segment will be silently "
                    f"omitted — verify this is intentional for all cases."
                ),
                layer="rule",
            ))

    # ── NO_UOM: quantity mapped without unit-of-measure ──────────────────────
    if raw_xml:
        # Look for quantity-related XPath without UOM nearby
        qty_refs = re.findall(
            r"(?:InventQty|QtyOrdered|Quantity|QTY)[^<]{0,50}",
            raw_xml, re.IGNORECASE
        )
        uom_refs = re.findall(
            r"(?:UnitId|UOM|UoM|C001|IT107|SHP07)[^<]{0,50}",
            raw_xml, re.IGNORECASE
        )
        if qty_refs and not uom_refs:
            findings.append(Finding(
                rule_id="NO_UOM",
                severity="WARNING",
                message=(
                    "Quantity field(s) are mapped but no unit-of-measure (UOM) "
                    "element was found. Missing UOM can cause quantity "
                    "interpretation errors (e.g. EA vs. CS vs. DOZ)."
                ),
                layer="rule",
            ))

    return findings


# ── Format Layer 1 findings for both the report and the LLM prompt ────────────

def _format_layer1_for_report(findings: List[Finding]) -> str:
    """Format Layer 1 findings into the CRITICAL / WARNINGS / INFO sections."""
    fails    = [f for f in findings if f.severity == "FAIL"    and f.layer == "rule"]
    warnings = [f for f in findings if f.severity == "WARNING" and f.layer == "rule"]
    infos    = [f for f in findings if f.severity == "INFO"    and f.layer == "rule"]

    parts = []

    if fails:
        parts.append("### CRITICAL — must fix before production")
        for f in fails:
            parts.append(f"- [{f.rule_id}] {f.message}")

    if warnings:
        parts.append("### WARNINGS — review recommended")
        for f in warnings:
            parts.append(f"- [{f.rule_id}] {f.message}")

    if infos:
        parts.append("### INFO — for awareness")
        for f in infos:
            parts.append(f"- [{f.rule_id}] {f.message}")

    if not parts:
        parts.append("### No automated rule violations detected")

    return "\n".join(parts)


def _format_layer1_for_llm(findings: List[Finding]) -> str:
    """Compact summary of Layer 1 findings to include in LLM prompt."""
    if not findings:
        return "Layer 1 automated checks: No violations detected."
    lines = ["Layer 1 automated checks found the following:"]
    for f in findings:
        lines.append(f"  [{f.severity}] [{f.rule_id}] {f.message}")
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def audit(
    ingested: dict,
    context: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[str, Any]:
    """
    Audit an ingested mapping file for misconfigurations and risky patterns.

    Args:
        ingested:  Output dict from file_ingestion.ingest_file().
        context:   Optional extra context string (e.g. output from modify or
                   generate engine) to give the LLM additional information.
        api_key:   Groq API key. Falls back to GROQ_API_KEY env var.
        model:     Groq model. Falls back to GROQ_MODEL env var,
                   then llama-3.3-70b-versatile.

    Returns:
        (response_str, None) where response_str is the full structured audit
        report (CRITICAL / WARNINGS / INFO / DYNAMIC FINDINGS / QUESTIONS).
        The second element is always None (audit is stateless).

    Raises:
        TypeError:  If ingested is not a dict.
        ValueError: If ingested is missing required keys or no API key found.
    """
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

    meta      = ingested.get("metadata", {})
    parsed    = ingested.get("parsed_content", {})
    file_name = meta.get("filename", "unknown")
    file_type = meta.get("file_type", "unknown")

    # ── Layer 1: hardcoded rule checks ───────────────────────────────────────
    layer1_findings = _run_rule_checks(ingested)
    layer1_report   = _format_layer1_for_report(layer1_findings)
    layer1_for_llm  = _format_layer1_for_llm(layer1_findings)

    # ── Layer 2: LLM dynamic checks ──────────────────────────────────────────
    raw_xslt = parsed.get("raw_xml", "") or parsed.get("raw_text", "") or ""
    if len(raw_xslt) > _MAX_XSLT_CHARS:
        raw_xslt = raw_xslt[:_MAX_XSLT_CHARS] + "\n... [truncated]"

    user_parts = [
        f"## Mapping File\nName: {file_name}  |  Type: {file_type}\n",
        f"## XSLT Source\n```xml\n{raw_xslt}\n```\n" if raw_xslt else
        f"## Parsed Structure\n{parsed}\n",
        f"## Automated Rule Check Results\n{layer1_for_llm}\n",
    ]

    if context:
        ctx_preview = context[:1_000]
        if len(context) > 1_000:
            ctx_preview += "\n... [truncated]"
        user_parts.append(
            f"## Additional Context\n"
            f"(Output from a preceding modify or generate operation)\n"
            f"{ctx_preview}\n"
        )

    user_parts.append(
        "Please perform your dynamic analysis now. "
        "Return only the DYNAMIC FINDINGS and QUESTIONS sections as specified."
    )

    user_message = "\n".join(user_parts)

    client = Groq(api_key=key)
    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        temperature=0.1,
        max_tokens=_MAX_OUTPUT_TOKENS,
    )

    llm_section = response.choices[0].message.content.strip()

    # ── Assemble final report ─────────────────────────────────────────────────
    report_parts = [
        f"## AUDIT REPORT\nFile: {file_name}  |  Type: {file_type}",
        "",
        layer1_report,
        "",
        llm_section,
    ]

    return "\n".join(report_parts).strip(), None


# ── CLI harness ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("\n" + "=" * 80)
    print("  AUDIT ENGINE — Mapping Production-Readiness Check")
    print("=" * 80 + "\n")

    if len(sys.argv) < 2:
        print("Usage: python modules/audit_engine.py <mapping_file>\n")
        print("Example:")
        print('  python modules/audit_engine.py '
              '"MappingData/MappingData/810_C-000340_OUT/810_NordStrom_Xslt_11-08-2023.xml"')
        sys.exit(0)

    file_arg = sys.argv[1]
    if not Path(file_arg).exists():
        print(f"[ERROR] File not found: {file_arg}")
        sys.exit(1)

    try:
        from .file_ingestion import ingest_file
    except ImportError:
        from file_ingestion import ingest_file  # type: ignore

    print(f"[INGEST] {file_arg}")
    ingested = ingest_file(file_path=file_arg)
    print(f"[TYPE  ] {ingested['metadata']['file_type']}")
    print()
    print("[AUDIT ] Running...\n")

    response, _ = audit(ingested)

    print("=" * 80)
    print(response)
    print("=" * 80 + "\n")
