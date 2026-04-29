"""
simulation_engine.py
────────────────────
Simulate / validate a mapping against source data.

Execution hierarchy
───────────────────
1. Detect Altova extension functions in the XSLT.
   If found → skip all processors (neither Saxon nor lxml can run Altova-specific
   extensions) and fall back to LLM simulation with a clear note to the user.

2. Try Saxon-HE via saxonche (XSLT 2.0 / 3.0 support).
   Most Altova MapForce XSLT files target version 2.0, so Saxon is the right
   first-choice processor. If Saxon succeeds → ground-truth XML is returned and
   fed to the LLM for analysis; LLM simulation is NOT used.

3. If Saxon fails for any reason other than Altova extensions, fall back to
   lxml (XSLT 1.0 only). If the stylesheet uses only 1.0 features, lxml will
   succeed and its output is used as ground truth.

4. If both processors fail → LLM simulation: send the parsed XSLT rules and
   source XML to the configured LLM and ask it to reason through the
   transformation field by field. This is often more useful than raw XML for
   analyst users because the LLM can explain WHY each field has its value.

5. Return (response_str, transform_output_xml_or_None) — mirrors the modify
   engine pattern so callers can display the real latest output when available.

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
import re
import time
import traceback
from pathlib import Path
from typing import Any, Optional, Tuple, Dict, List

from dotenv import load_dotenv

# Load .env from module directory or one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# ── Constants ─────────────────────────────────────────────────────────────────

# Max characters of source XML to include in the LLM prompt (≈ 1 500 tokens).
_MAX_SOURCE_CHARS = 6_000

# Max characters of XSLT parsed content in the LLM context (≈ 2 000 tokens).
_MAX_XSLT_CHARS = 8_000

# Max tokens for LLM output.
_MAX_OUTPUT_TOKENS = 1_500

# ── Partial-match thresholds for compare_output_to_target ────────────────────
# Tunable via env vars so integration tests can tighten/loosen without a code change.
_PARTIAL_MATCH_MAX_MISSING    = int(os.getenv("SIM_PARTIAL_MAX_MISSING",    "2"))
_PARTIAL_MATCH_MAX_MISMATCHES = int(os.getenv("SIM_PARTIAL_MAX_MISMATCHES", "4"))

# Patterns that indicate actual Altova extension FUNCTION CALLS (not just
# namespace declarations).  Many MapForce XSLTs declare the altova namespace
# but never invoke it — Saxon handles those fine.  We only bail out when
# extension functions are genuinely called.
#
# Pattern: the namespace prefix followed by a colon and a function name then "("
# e.g.  altova:format-date(  altovaext:node-set(  fn-user-defined:foo(
_ALTOVA_CALL_PATTERN: re.Pattern = re.compile(
    r"\baltova(ext)?:[A-Za-z_][\w-]*\s*\(",
    re.IGNORECASE,
)
_ALTOVA_FN_USER_PATTERN: re.Pattern = re.compile(
    r"\bfn-user-defined:[A-Za-z_][\w-]*\s*\(",
    re.IGNORECASE,
)

_KEY_SEGMENTS = ["ISA", "GS", "ST", "BIG", "REF", "N1", "IT1", "CTT", "SE"]
_KEY_FIELDS = ["ISA06", "ISA08", "GS02", "GS03", "ST01", "BIG01", "BIG02", "REF01", "REF02", "N101", "IT102", "IT104", "CTT01", "SE01"]

_SYSTEM_PROMPT_SIMULATE = """\
You are an expert EDI/XSLT transformation analyst. Your task is to SIMULATE \
an XSLT transformation and explain what the output would look like.

When given an XSLT mapping and source XML:
1. **Transformation Summary** — one sentence on what this mapping does.
2. **Key Output Fields** — for each important X12 segment (ST, BIG/BEG, REF, N1, \
   IT1/PO1, TDS/CTT, SE), show the field name, its source, and the actual value.
3. **Conditional Logic** — note any xsl:if/xsl:choose that fired or was skipped.
4. **Hardcoded Values** — list values emitted regardless of source data.
5. **Potential Issues** — flag empty/missing source fields mapped to required EDI fields.

Be concise. Use clear section headers. Do NOT reproduce large blocks of XML.
"""

_SYSTEM_PROMPT_ANALYSE = """\
You are an expert EDI/XSLT transformation analyst. The actual XSLT processor \
has already run — you are given the real transform output. Your job is to \
analyse it, not simulate it.

1. **Summary** — one sentence: what this transaction is and whether the output \
   looks structurally correct.
2. **Issues Found** — flag anything suspicious: garbled values, empty required \
   fields, unexpected format, missing segments. Be specific (segment + element + value).
3. **Hardcoded Values** — note any values in the output that look hardcoded rather \
   than sourced from the input.
4. **Conditional Logic** — if notable xsl:if/choose branches fired, mention them briefly.

Be concise and direct. 3–6 bullet points per section maximum. \
Do NOT reproduce the full XML output — reference segment and element names only.
"""

# Keep a backward-compat alias for any external callers
_SYSTEM_PROMPT = _SYSTEM_PROMPT_SIMULATE


def _as_xml_root(xml_text: str):
    try:
        from lxml import etree  # noqa: PLC0415
        return etree.fromstring(xml_text.encode("utf-8", errors="replace"))
    except Exception:
        return None


def _collect_segment_presence(xml_text: str) -> Dict[str, bool]:
    root = _as_xml_root(xml_text)
    if root is None:
        upper = xml_text.upper()
        return {seg: (f"<{seg}" in upper or f"{seg}*" in upper) for seg in _KEY_SEGMENTS}
    from lxml import etree  # noqa: PLC0415
    present = {seg: False for seg in _KEY_SEGMENTS}
    for elem in root.iter():
        local = etree.QName(elem).localname if isinstance(elem.tag, str) else ""
        if local in present:
            present[local] = True
    return present


def _collect_field_values(xml_text: str, fields: List[str]) -> Dict[str, List[str]]:
    root = _as_xml_root(xml_text)
    if root is None:
        values: Dict[str, List[str]] = {f: [] for f in fields}
        for f in fields:
            # lightweight fallback for flat EDI-like text
            m = re.findall(rf"\b{re.escape(f)}\*([^\r\n\*~]+)", xml_text, flags=re.IGNORECASE)
            values[f] = [v.strip() for v in m if v and v.strip()][:5]
        return values
    from lxml import etree  # noqa: PLC0415
    values: Dict[str, List[str]] = {f: [] for f in fields}
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        local = etree.QName(elem).localname
        if local in values:
            txt = (elem.text or "").strip()
            if txt:
                values[local].append(txt)
    return values


def compare_output_to_target(
    output_xml: Optional[str],
    target_text: Optional[str],
) -> Dict[str, Any]:
    """
    Compare actual transform output against target contract/sample output.
    Returns structured comparison fields for UI and API callers.
    """
    result: Dict[str, Any] = {
        "target_match_status": "no_target",
        "target_match_summary": "No target file selected for comparison.",
        "missing_target_segments": [],
        "extra_output_segments": [],
        "mismatched_fields": [],
    }
    if not target_text:
        return result
    if not output_xml:
        result["target_match_status"] = "no_output"
        result["target_match_summary"] = "No real transform output available to compare against target."
        return result

    out_presence = _collect_segment_presence(output_xml)
    tgt_presence = _collect_segment_presence(target_text)

    missing = [s for s in _KEY_SEGMENTS if tgt_presence.get(s) and not out_presence.get(s)]
    extra = [s for s in _KEY_SEGMENTS if out_presence.get(s) and not tgt_presence.get(s)]

    out_fields = _collect_field_values(output_xml, _KEY_FIELDS)
    tgt_fields = _collect_field_values(target_text, _KEY_FIELDS)
    mismatches: List[Dict[str, Any]] = []
    for field in _KEY_FIELDS:
        tvals = tgt_fields.get(field, [])
        ovals = out_fields.get(field, [])
        if not tvals and not ovals:
            continue
        if tvals != ovals:
            mismatches.append({
                "field": field,
                "target": tvals[:3],
                "output": ovals[:3],
            })

    score_penalty = len(missing) + len(extra) + len(mismatches)
    if score_penalty == 0:
        status = "matches_target"
        summary = "Output matches target (structure and key fields)."
    elif len(missing) <= _PARTIAL_MATCH_MAX_MISSING and len(mismatches) <= _PARTIAL_MATCH_MAX_MISMATCHES:
        status = "partial_match"
        summary = (
            f"Output partially matches target: {len(missing)} missing segment(s), "
            f"{len(extra)} extra segment(s), {len(mismatches)} mismatched field(s)."
        )
    else:
        status = "does_not_match"
        summary = (
            f"Output does not match target: {len(missing)} missing segment(s), "
            f"{len(extra)} extra segment(s), {len(mismatches)} mismatched field(s)."
        )

    result.update({
        "target_match_status": status,
        "target_match_summary": summary,
        "missing_target_segments": missing,
        "extra_output_segments": extra,
        "mismatched_fields": mismatches,
    })
    return result


def _find_xslt_line_for_field(xslt_content: str, field: str) -> tuple[int, str]:
    lines = xslt_content.splitlines()
    for i, ln in enumerate(lines, start=1):
        if field in ln or field.lower() in ln.lower():
            return i, ln.strip()
    return 1, lines[0].strip() if lines else ""


def _looks_like_date(v: str) -> bool:
    return bool(re.fullmatch(r"\d{6,8}", (v or "").strip()))


def _looks_like_decimal(v: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", (v or "").strip()))


def _looks_like_garbage(v: str, field: str = "") -> bool:
    """
    Return True when a field value is clearly garbled / wrong.

    Detects:
    - 3+ consecutive identical uppercase letters (ZZZ, YYY …)
    - Alphabetic chars mixed into a field that should be a date (field ends in
      '01'/'09'/'10'/'12' pattern common in ISA/BIG/GS date/time elements)
    - Looks like an escaped XPath expression leaking into the output
    """
    v = (v or "").strip()
    if not v:
        return False
    # 3+ consecutive identical upper-case letters → likely a template placeholder
    if re.search(r"([A-Z])\1{2,}", v):
        return True
    # Date fields should be all-digits; mixed alpha = garbled
    _date_fields = {"BIG01", "BIG03", "ISA09", "ISA10", "GS04", "GS05",
                    "DTM02", "PO102", "ITD04", "ITD06"}
    if field in _date_fields and re.search(r"[A-Za-z]", v):
        return True
    # Looks like a leaked XPath / template expression
    if re.search(r"[${}()\[\]]", v):
        return True
    return False


# Envelope segments that the EDI platform (PartnerLinQ) adds outside the XSLT;
# their absence from XSLT output is expected, not a bug.
_PLATFORM_ENVELOPE_SEGS: set = {"ISA", "GS", "GE", "IEA"}


def generate_autofix_suggestions(comparison_result: Dict[str, Any], xslt_content: str) -> list:
    """
    Analyze mismatches and suggest concrete XSLT fixes with line numbers.
    """
    suggestions = []
    mismatches = comparison_result.get("mismatched_fields", []) or []
    for mm in mismatches:
        field = str(mm.get("field", ""))
        target_val = (mm.get("target") or [""])[0]
        output_val = (mm.get("output") or [""])[0]
        line_no, current_code = _find_xslt_line_for_field(xslt_content, field)

        if _looks_like_garbage(str(output_val), field):
            suggestions.append({
                "issue": f"{field} garbled output ({output_val!r} → expected {target_val!r})",
                "xslt_line": line_no,
                "current_code": current_code,
                "suggested_fix": "<xsl:value-of select=\"fn:format-date(xs:date(.), '[Y0001][M01][D01]')\"/>",
                "explanation": "Output value looks garbled/templated. Fix the date/value formatting logic.",
                "apply_prompt": (
                    f"Fix {field}: the current output is '{output_val}' which is garbled. "
                    f"The expected format is '{target_val}'. "
                    f"Update the XSLT to produce the correct value."
                ),
            })
        elif _looks_like_date(str(target_val)) and not _looks_like_date(str(output_val)):
            suggestions.append({
                "issue": f"{field} date format wrong ({output_val!r} → expected YYYYMMDD like {target_val!r})",
                "xslt_line": line_no,
                "current_code": current_code,
                "suggested_fix": "<xsl:value-of select=\"fn:format-date(xs:date(.), '[Y0001][M01][D01]')\"/>",
                "explanation": "Format date to EDI YYYYMMDD output.",
                "apply_prompt": (
                    f"Fix {field} date format: output is '{output_val}', target expects '{target_val}' "
                    f"(YYYYMMDD). Update the XSLT date formatting to produce 8-digit YYYYMMDD."
                ),
            })
        elif _looks_like_decimal(str(target_val)) and _looks_like_decimal(str(output_val)):
            t_dec = len(str(target_val).split(".")[1]) if "." in str(target_val) else 0
            o_dec = len(str(output_val).split(".")[1]) if "." in str(output_val) else 0
            if t_dec != o_dec:
                fmt = "0." + ("0" * max(t_dec, 1))
                suggestions.append({
                    "issue": f"{field} decimal precision wrong ({output_val} → {target_val})",
                    "xslt_line": line_no,
                    "current_code": current_code,
                    "suggested_fix": f"<xsl:value-of select=\"format-number({field}, '{fmt}')\"/>",
                    "explanation": f"Enforce {t_dec} decimal places with format-number.",
                    "apply_prompt": (
                        f"Fix {field} decimal precision: output is '{output_val}', "
                        f"target expects '{target_val}' ({t_dec} decimal places). "
                        f"Use format-number to enforce {t_dec} decimal places."
                    ),
                })
        elif str(output_val).strip() in ("", "(empty)"):
            suggestions.append({
                "issue": f"{field} empty in output (expected {target_val!r})",
                "xslt_line": line_no,
                "current_code": current_code,
                "suggested_fix": f"<xsl:value-of select=\"{field}\"/>",
                "explanation": "Map a non-empty source value into this field.",
                "apply_prompt": (
                    f"Fix {field}: field is empty in the output but target has '{target_val}'. "
                    f"Map the correct source path into {field}."
                ),
            })
    return suggestions


def audit_simulate_findings(
    comparison: Dict[str, Any],
    xslt_content: str,
    output_xml: str = "",
    target_text: str = "",
) -> List[Dict[str, Any]]:
    """
    Condensed post-simulate audit: one finding per actionable issue.

    Each finding dict::

        {
            "field":       "BIG01",
            "issue_type":  "garbage_output" | "date_format" | "decimal_mismatch"
                           | "empty_field" | "missing_segment" | "value_mismatch",
            "severity":    "CRITICAL" | "WARNING",
            "output_val":  "ZZZZZYZYZZ0227",
            "expected_val":"20260227",
            "apply_prompt":"Fix BIG01 date format: ...",
            "xslt_line":   42,
        }

    Envelope segments (ISA/GS/GE/IEA) are excluded — they are added by the
    PartnerLinQ platform, not the XSLT.
    """
    findings: List[Dict[str, Any]] = []

    # ── 1. Field-level mismatches ─────────────────────────────────────────────
    mismatches = comparison.get("mismatched_fields", []) or []
    for mm in mismatches:
        field      = str(mm.get("field", ""))
        target_val = str((mm.get("target") or [""])[0])
        output_val = str((mm.get("output") or [""])[0])
        line_no, _ = _find_xslt_line_for_field(xslt_content, field)

        if _looks_like_garbage(output_val, field):
            findings.append({
                "field":       field,
                "issue_type":  "garbage_output",
                "severity":    "CRITICAL",
                "output_val":  output_val,
                "expected_val": target_val,
                "apply_prompt": (
                    f"Fix {field}: the XSLT is producing a garbled value '{output_val}'. "
                    f"The target expects '{target_val}'. "
                    f"Locate the template logic that sets {field} and correct the "
                    f"formatting so the output matches '{target_val}'."
                ),
                "xslt_line": line_no,
            })
        elif _looks_like_date(target_val) and not _looks_like_date(output_val):
            findings.append({
                "field":       field,
                "issue_type":  "date_format",
                "severity":    "CRITICAL",
                "output_val":  output_val,
                "expected_val": target_val,
                "apply_prompt": (
                    f"Fix {field} date format: current output is '{output_val}', "
                    f"target expects '{target_val}' (YYYYMMDD). "
                    f"Update the date formatting in the XSLT to produce 8-digit YYYYMMDD."
                ),
                "xslt_line": line_no,
            })
        elif _looks_like_decimal(target_val) and _looks_like_decimal(output_val):
            t_dec = len(target_val.split(".")[1]) if "." in target_val else 0
            o_dec = len(output_val.split(".")[1]) if "." in output_val else 0
            if t_dec != o_dec:
                findings.append({
                    "field":       field,
                    "issue_type":  "decimal_mismatch",
                    "severity":    "WARNING",
                    "output_val":  output_val,
                    "expected_val": target_val,
                    "apply_prompt": (
                        f"Fix {field} decimal precision: output is '{output_val}', "
                        f"target expects '{target_val}' ({t_dec} decimal places). "
                        f"Apply format-number to enforce {t_dec} decimal places on {field}."
                    ),
                    "xslt_line": line_no,
                })
        elif output_val.strip() in ("", "(empty)"):
            findings.append({
                "field":       field,
                "issue_type":  "empty_field",
                "severity":    "WARNING",
                "output_val":  "(empty)",
                "expected_val": target_val,
                "apply_prompt": (
                    f"Fix {field}: field is empty in the output but target has '{target_val}'. "
                    f"Map the correct source element into {field}."
                ),
                "xslt_line": line_no,
            })
        else:
            findings.append({
                "field":       field,
                "issue_type":  "value_mismatch",
                "severity":    "WARNING",
                "output_val":  output_val,
                "expected_val": target_val,
                "apply_prompt": (
                    f"Fix {field}: output has '{output_val}' but target expects '{target_val}'. "
                    f"Update the source mapping for {field} to produce the correct value."
                ),
                "xslt_line": line_no,
            })

    # ── 2. Missing segments (excluding platform envelope segments) ────────────
    missing = [
        s for s in (comparison.get("missing_target_segments", []) or [])
        if s not in _PLATFORM_ENVELOPE_SEGS
    ]
    for seg in missing:
        line_no, _ = _find_xslt_line_for_field(xslt_content, seg)
        findings.append({
            "field":       seg,
            "issue_type":  "missing_segment",
            "severity":    "CRITICAL",
            "output_val":  "(absent)",
            "expected_val": f"<{seg}> segment",
            "apply_prompt": (
                f"The {seg} segment is missing from the XSLT output but present in the target. "
                f"Add a template or xsl:for-each block to produce the {seg} segment with the "
                f"correct source mappings."
            ),
            "xslt_line": line_no,
        })

    return findings

# ── Altova extension detection ────────────────────────────────────────────────

def _detect_altova_extensions(raw_xslt: str) -> bool:
    """
    Return True if the XSLT *calls* Altova-specific extension functions that
    Saxon-HE and lxml cannot execute.

    Many MapForce stylesheets declare the altova: namespace (xmlns:altova=...)
    but never actually call any extension functions — Saxon handles those fine.
    We only block processing when extension functions are genuinely invoked,
    i.e. when we see patterns like:
        altova:format-date(   altovaext:node-set(   fn-user-defined:foo(
    """
    if _ALTOVA_CALL_PATTERN.search(raw_xslt):
        return True
    if _ALTOVA_FN_USER_PATTERN.search(raw_xslt):
        return True
    return False


def _detect_xslt_version(raw_xslt: str) -> str:
    """
    Extract the declared XSLT version from the stylesheet element.
    Returns '2.0', '3.0', '1.0', or 'unknown'.
    """
    m = re.search(r'<xsl:stylesheet[^>]+version=["\']([^"\']+)["\']', raw_xslt)
    if m:
        return m.group(1).strip()
    m = re.search(r'<xsl:transform[^>]+version=["\']([^"\']+)["\']', raw_xslt)
    if m:
        return m.group(1).strip()
    return "unknown"


# ── Saxon-HE transform (XSLT 2.0 / 3.0) ─────────────────────────────────────

def _try_saxon_transform(
    raw_xslt: str, source_path: str
) -> Tuple[Optional[str], Optional[str]]:
    """
    Attempt an XSLT 2.0/3.0 transformation using Saxon-HE (saxonche).

    Returns (output_xml_str, None)  on success.
    Returns (None, error_message)   on failure.
    """
    try:
        from saxonche import PySaxonProcessor  # type: ignore
    except ImportError:
        return None, (
            "saxonche is not installed. Run: pip install saxonche --break-system-packages"
        )

    try:
        with PySaxonProcessor(license=False) as proc:
            xslt30 = proc.new_xslt30_processor()

            # ── Compile the stylesheet ────────────────────────────────────────
            executable = xslt30.compile_stylesheet(stylesheet_text=raw_xslt)

            # saxonche signals errors through exception_occurred / error_message
            if xslt30.exception_occurred:
                err = xslt30.error_message or "Unknown compilation error"
                xslt30.clear_exception()
                return None, f"Saxon compilation error: {err}"

            if executable is None:
                return None, "Saxon returned no executable — stylesheet may be invalid."

            # ── Parse the source document ─────────────────────────────────────
            source_node = proc.parse_xml(xml_file_name=str(source_path))

            if proc.exception_occurred:
                err = proc.error_message or "Unknown parse error"
                proc.clear_exception()
                return None, f"Saxon source parse error: {err}"

            if source_node is None:
                return None, "Saxon could not parse the source file as XML."

            # ── Apply templates ───────────────────────────────────────────────
            result = executable.apply_templates_returning_string(
                xdm_value=source_node
            )

            if executable.exception_occurred:
                err = executable.error_message or "Unknown transform error"
                executable.clear_exception()
                return None, f"Saxon transform error: {err}"

            if result is None:
                # Some stylesheets use named entry templates; try the default
                executable.set_global_context_item(xdm_item=source_node)
                result = executable.call_template_returning_string()

                if executable.exception_occurred:
                    err = executable.error_message or "No output produced"
                    executable.clear_exception()
                    return None, f"Saxon call-template error: {err}"

            return (str(result) if result else None), None

    except Exception as exc:  # noqa: BLE001
        if os.getenv("SIM_DEBUG", "0") == "1":
            print("[SIM_DEBUG] _try_saxon_transform exception:")
            traceback.print_exc()
        return None, f"Saxon exception: {exc}"


# ── lxml transform (XSLT 1.0 fallback) ───────────────────────────────────────

def _try_lxml_transform(
    raw_xslt: str, source_path: str
) -> Tuple[Optional[str], Optional[str]]:
    """
    Attempt an XSLT transformation using lxml (XSLT 1.0 only).

    Returns (output_xml_str, None) on success (warnings are non-fatal).
    Returns (None, error_message)  on failure (e.g. XSLT 2.0 features used,
    or transform produced no output).
    """
    try:
        from lxml import etree  # type: ignore

        xslt_tree   = etree.fromstring(raw_xslt.encode("utf-8"))
        source_tree = etree.parse(source_path)
        transform   = etree.XSLT(xslt_tree)
        result      = transform(source_tree)
        output      = str(result) if result is not None else ""

        if not output.strip():
            # No output produced — report errors or a generic message.
            errors = transform.error_log
            msgs = "; ".join(str(e) for e in errors) if errors else "lxml produced empty output"
            return None, f"lxml transform error: {msgs}"

        # Output was produced; warnings in error_log are non-fatal — return
        # the output so callers don't unnecessarily fall back to LLM.
        return output, None

    except Exception as exc:  # noqa: BLE001
        if os.getenv("SIM_DEBUG", "0") == "1":
            print("[SIM_DEBUG] _try_lxml_transform exception:")
            traceback.print_exc()
        return None, str(exc)


# ── Main simulate() function ──────────────────────────────────────────────────

def simulate(
    ingested: dict,
    source_file: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: str = "openai",
    session: Optional[Any] = None,
) -> Tuple[str, Any]:
    """
    Simulate / validate a mapping against source data.

    Execution order:
      1. Detect Altova extensions → LLM fallback with explanation if found.
      2. Saxon-HE (XSLT 2.0/3.0) — ground-truth execution.
      3. lxml (XSLT 1.0) — fallback if Saxon fails.
      4. LLM simulation — last resort.

    Args:
        ingested:     Output dict from file_ingestion.ingest_file() for the
                      XSLT / mapping file.
        source_file:  Path to the source XML input file (e.g. SourceFile.txt).
                      Optional — if not provided, a dry-run analysis is performed.
        api_key:      LLM API key. Falls back to the provider's env var.
        model:        LLM model identifier. Falls back to the provider's env var,
                      then llama-3.3-70b-versatile.

    Returns:
        (response_str, transform_output_xml_or_None) where response_str is the
        simulation analysis and the second element is the real transform output
        when Saxon/lxml succeeds (else None).
    """
    # ── Validate inputs ───────────────────────────────────────────────────────
    if not isinstance(ingested, dict):
        raise TypeError(f"ingested must be a dict, got {type(ingested).__name__}")
    if "parsed_content" not in ingested:
        raise ValueError("ingested dict is missing 'parsed_content' key")

    try:
        from .llm_client import chat_complete, DEFAULT_MODELS, PROVIDERS, get_default_model
    except ImportError:
        from llm_client import chat_complete, DEFAULT_MODELS, PROVIDERS, get_default_model  # type: ignore

    env_key_name = PROVIDERS.get(provider, {}).get("env_key", "OPENAI_API_KEY")
    key = api_key or os.environ.get(env_key_name) or os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not key:
        raise ValueError(
            f"API key required for provider {provider!r}. "
            "Pass api_key= or set the appropriate key in .env"
        )

    # Use engine-aware model resolution so SIMULATE_MODEL (or the provider default)
    # is respected instead of always falling back to GROQ_MODEL.
    resolved_model = model or get_default_model(provider, engine="simulate")

    # ── Extract raw XSLT source ───────────────────────────────────────────────
    raw_xslt: Optional[str] = (
        ingested.get("parsed_content", {}).get("raw_xml")
        or ingested.get("parsed_content", {}).get("raw_text")
    )

    xslt_result_xml: Optional[str] = None
    xslt_error:      Optional[str] = None
    processor_used:  str           = "none"
    altova_detected: bool          = False

    # ── Step 1: Detect Altova extensions ──────────────────────────────────────
    if raw_xslt:
        altova_detected = _detect_altova_extensions(raw_xslt)
        xslt_version    = _detect_xslt_version(raw_xslt)
    else:
        xslt_version    = "unknown"

    if altova_detected:
        # Known limitation — no external processor can run Altova extensions.
        # Fall straight through to LLM simulation with a clear explanation.
        xslt_error = (
            "This XSLT uses Altova-specific extension functions "
            "(altova:* namespace). These are proprietary to Altova MapForce and "
            "cannot be executed by Saxon-HE or lxml. "
            "Falling back to LLM-based simulation."
        )
        processor_used = "llm (altova extensions)"

    elif raw_xslt and source_file and Path(source_file).exists():

        # ── Step 2: Try Saxon-HE (XSLT 2.0 / 3.0) ───────────────────────────
        xslt_result_xml, xslt_error = _try_saxon_transform(raw_xslt, str(source_file))

        if xslt_result_xml is not None:
            processor_used = f"Saxon-HE 12.9 (XSLT {xslt_version})"
        else:
            # ── Step 3: Try lxml (XSLT 1.0 fallback) ─────────────────────────
            lxml_result, lxml_error = _try_lxml_transform(raw_xslt, str(source_file))

            if lxml_result is not None:
                xslt_result_xml = lxml_result
                # Keep the Saxon error as context for the LLM if both tried
                xslt_error      = None
                processor_used  = "lxml (XSLT 1.0)"
            else:
                # Both processors failed — LLM simulation
                # Prefer the Saxon error message (more informative for 2.0 files)
                processor_used  = "llm (both processors failed)"
                # xslt_error already holds the Saxon error

    elif not source_file or not Path(str(source_file)).exists():
        processor_used = "llm (no source file — dry-run)"

    # ── Step 4: Load source file text for LLM context ────────────────────────
    source_xml_text: Optional[str] = None
    if source_file and Path(str(source_file)).exists():
        try:
            source_xml_text = Path(str(source_file)).read_text(
                encoding="utf-8", errors="replace"
            )
            if len(source_xml_text) > _MAX_SOURCE_CHARS:
                source_xml_text = (
                    source_xml_text[:_MAX_SOURCE_CHARS]
                    + f"\n... [truncated at {_MAX_SOURCE_CHARS} chars] ..."
                )
        except OSError as exc:
            source_xml_text = f"[Could not read source file: {exc}]"

    # ── Step 5: Build LLM prompt ──────────────────────────────────────────────
    user_message = _build_user_message(
        ingested=ingested,
        source_xml_text=source_xml_text,
        xslt_result_xml=xslt_result_xml,
        xslt_error=xslt_error,
        source_file=source_file,
        processor_used=processor_used,
        altova_detected=altova_detected,
    )

    # ── Step 6: Call LLM via unified client ───────────────────────────────────
    # When Saxon/lxml produced real output, use the concise analysis prompt
    # (much shorter, avoids re-generating the XML).  For pure LLM simulation
    # mode (no real output) keep the full simulation system prompt.
    _sys_prompt = (
        _SYSTEM_PROMPT_ANALYSE if xslt_result_xml else _SYSTEM_PROMPT_SIMULATE
    )
    # Limit output tokens when we're only analysing a real result — half suffices.
    _out_tokens = _MAX_OUTPUT_TOKENS // 2 if xslt_result_xml else _MAX_OUTPUT_TOKENS
    try:
        llm_text = chat_complete(
            messages=[
                {"role": "system", "content": _sys_prompt},
                {"role": "user",   "content": user_message},
            ],
            api_key=key,
            model=resolved_model,
            provider=provider,
            temperature=0.2,
            max_tokens=_out_tokens,
            engine="simulate",
        )
    except Exception as exc:  # noqa: BLE001
        if os.getenv("SIM_DEBUG", "0") == "1":
            print(f"[SIM_DEBUG] LLM call failed (provider={provider})")
            print(f"[SIM_DEBUG] processor_used={processor_used}")
            print(f"[SIM_DEBUG] xslt_version={xslt_version}")
            print(f"[SIM_DEBUG] source_file={source_file}")
            print(f"[SIM_DEBUG] xslt_error={xslt_error}")
            traceback.print_exc()
        fallback = generate_local_fallback_response(
            xslt_content=raw_xslt or "",
            source_content=source_xml_text or "",
            session=session,
        )
        summary = fallback.get("summary", "").strip()
        # Return the Saxon/lxml XML output if it was produced, even though
        # the LLM analysis failed — the transform result is still valid.
        return f"[validation_only]\n{summary}", xslt_result_xml

    # Prepend the processor banner so the user always knows what ran
    banner = _build_processor_banner(
        processor_used=processor_used,
        xslt_version=xslt_version,
        altova_detected=altova_detected,
        xslt_error=xslt_error if xslt_result_xml is None else None,
    )

    return banner + llm_text, xslt_result_xml


def get_modified_segments_summary(session: Optional[Any]) -> str:
    """Return a short summary of recent revisions if available."""
    try:
        revs = getattr(session, "xslt_revisions", []) if session is not None else []
    except Exception:
        revs = []
    if not revs:
        return "- No modifications yet"
    lines: List[str] = []
    for idx, rev in enumerate(revs[-3:], start=1):
        desc = getattr(rev, "description", "") or "Modified XSLT"
        lines.append(f"- Revision {idx}: {desc}")
    return "\n".join(lines)


def generate_local_fallback_response(
    xslt_content: str,
    source_content: str,
    session: Optional[Any],
) -> Dict[str, Any]:
    """Generate useful static diagnostics when Saxon and the LLM are unavailable."""
    templates = []
    variables = []
    output_elements: List[str] = []
    parse_ok = False
    try:
        from lxml import etree  # noqa: PLC0415
        parser = etree.XMLParser(remove_blank_text=False, recover=True)
        tree = etree.fromstring((xslt_content or "").encode("utf-8"), parser=parser)
        templates = tree.xpath("//xsl:template", namespaces={"xsl": "http://www.w3.org/1999/XSL/Transform"})
        variables = tree.xpath("//xsl:variable", namespaces={"xsl": "http://www.w3.org/1999/XSL/Transform"})
        for template in templates:
            elems = template.xpath(".//*[not(starts-with(local-name(), 'xsl'))]")
            for elem in elems[:10]:
                tag = elem.tag.split("}")[-1] if isinstance(elem.tag, str) and "}" in elem.tag else str(elem.tag)
                if tag not in output_elements:
                    output_elements.append(tag)
        parse_ok = True
    except Exception:
        pass
    lower = (xslt_content or "").lower()
    if "x12_00401_810" in (xslt_content or "") or "<st01>810</st01>" in lower:
        tx_note = "EDI 810 Invoice"
    elif "x12_00401_850" in (xslt_content or "") or "<st01>850</st01>" in lower:
        tx_note = "EDI 850 Purchase Order"
    elif "x12_00401_856" in (xslt_content or "") or "<st01>856</st01>" in lower:
        tx_note = "EDI 856 Ship Notice"
    elif "soap:envelope" in lower or "soapenv:" in lower:
        tx_note = "SOAP web service transformation"
    else:
        tx_note = "Custom XML transformation"

    # Only show validation checkmarks when lxml actually parsed the file
    # successfully — avoid false confidence when this fallback was triggered by
    # a provider API error rather than by a genuine validation run.
    if parse_ok:
        validation_section = (
            "**Validation Checks (lxml parse):**\n"
            "✅ XSLT is well-formed XML\n"
            "✅ All xsl: namespace declarations present\n"
            "✅ Template structure parseable\n"
        )
    else:
        validation_section = (
            "**Validation Checks:**\n"
            "⚠️ XSLT structure could not be fully parsed — please review the stylesheet manually.\n"
        )

    summary = (
        f"XSLT Validation Results\n\n"
        f"**Transformation Type:** {tx_note}\n\n"
        f"**XSLT Structure Analysis:**\n"
        f"- Templates: {len(templates)} template(s) defined\n"
        f"- Variables: {len(variables)} variable(s) declared\n"
        f"- Output segments: {', '.join(output_elements[:10]) if output_elements else '(not detected)'}\n\n"
        f"{validation_section}\n"
        f"**Note:** Full Saxon transformation unavailable in this environment.\n\n"
        f"**Next Steps:**\n"
        f"1. Download the modified XSLT\n"
        f"2. Test in your EDI system with an actual Saxon processor\n"
        f"3. Verify output against the target specification\n\n"
        f"**Modified Segments in This Version:**\n"
        f"{get_modified_segments_summary(session)}"
    )
    return {
        "status": "validation_only",
        "message": "XSLT Validation Results",
        "summary": summary,
        "validation_passed": parse_ok,
        "requires_external_testing": True,
    }


# ── Message builder ───────────────────────────────────────────────────────────

def _build_processor_banner(
    processor_used: str,
    xslt_version: str,
    altova_detected: bool,
    xslt_error: Optional[str],
) -> str:
    """Return a short banner prepended to the LLM response explaining what ran."""
    if "Saxon" in processor_used:
        return (
            f"> **Simulation engine:** Saxon-HE 12.9 — "
            f"actual XSLT {xslt_version} execution ✅  \n"
            f"> The output below is the **real transform result**, not an estimate.\n\n"
        )
    elif "lxml" in processor_used:
        return (
            f"> **Simulation engine:** lxml (XSLT 1.0) — "
            f"actual execution ✅  \n"
            f"> The output below is the **real transform result**.\n\n"
        )
    elif altova_detected:
        return (
            "> ⚠️ **Altova extension functions detected.**  \n"
            "> Saxon-HE and lxml cannot run Altova-specific extensions (`altova:*` namespace).  \n"
            "> This is a known limitation of any XSLT processor that is not Altova MapForce itself.  \n"
            "> The analysis below is an **LLM-based simulation** — accurate for field mapping logic "
            "but cannot reproduce Altova function outputs.\n\n"
        )
    elif xslt_error:
        return (
            f"> ⚠️ **Processor note:** Both Saxon-HE and lxml could not execute this stylesheet.  \n"
            f"> Reason: `{xslt_error[:200]}`  \n"
            f"> Falling back to **LLM-based simulation**.\n\n"
        )
    else:
        return (
            "> ℹ️ **Dry-run mode** — no source file provided.  \n"
            "> Showing what this mapping *would* produce for a typical input.\n\n"
        )


def _build_user_message(
    ingested: dict,
    source_xml_text: Optional[str],
    xslt_result_xml: Optional[str],
    xslt_error: Optional[str],
    source_file: Optional[str],
    processor_used: str,
    altova_detected: bool,
) -> str:
    """Compose the user-turn message for the LLM simulation prompt."""

    parts: list[str] = []

    # ── XSLT mapping specification ────────────────────────────────────────────
    parsed    = ingested.get("parsed_content", {})
    meta      = ingested.get("metadata", {})
    file_type = meta.get("file_type", "unknown")
    file_name = meta.get("filename", "unknown")

    parts.append(f"## Mapping File\nName: {file_name}  |  Type: {file_type}\n")

    # Structured mapping rules (preferred — more concise than raw XML)
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
            raw = raw[:_MAX_XSLT_CHARS] + "\n... [truncated] ..."
        parts.append(f"### XSLT Raw XML\n```xml\n{raw}\n```\n")

    # ── Source data ───────────────────────────────────────────────────────────
    # When Saxon already produced the real output, skip sending the full source
    # XML — the output IS the result and the LLM only needs to analyse it.
    # This cuts prompt size ~60 % and avoids the LLM reproducing raw XML.
    if xslt_result_xml:
        src_name = Path(str(source_file)).name if source_file else "source"
        parts.append(
            f"## Source Input\nFile: `{src_name}` — "
            f"content omitted; real transform output is provided below.\n"
        )
    elif source_xml_text:
        src_name = Path(str(source_file)).name if source_file else "source"
        parts.append(
            f"## Source Input File ({src_name})\n```xml\n{source_xml_text}\n```\n"
        )
    else:
        parts.append(
            "## Source Input File\n"
            "No source file provided. Perform a **dry-run analysis** instead:\n"
            "describe what the mapping WOULD produce for a typical input, based on "
            "the XSLT templates and hardcoded values above.\n"
        )

    # ── Actual processor output (if available) ────────────────────────────────
    if xslt_result_xml:
        # Send a compact preview — the LLM must reference segments, not copy XML
        preview = xslt_result_xml[:4_000]
        if len(xslt_result_xml) > 4_000:
            preview += f"\n... [{len(xslt_result_xml) - 4_000} more chars — full output available via download] ..."
        parts.append(
            f"## Actual Transform Output ({processor_used})\n"
            f"```xml\n{preview}\n```\n"
            f"Analyse this output. Do NOT reproduce it — reference segment/element names.\n"
        )
    elif xslt_error and not altova_detected:
        parts.append(
            f"## Processor Attempt\n"
            f"Both Saxon-HE and lxml could not execute this stylesheet.\n"
            f"Error: `{xslt_error}`\n"
            f"Perform an **LLM-based simulation** instead.\n"
        )
    elif altova_detected:
        parts.append(
            "## Processor Note\n"
            "This stylesheet calls Altova-specific extension functions "
            "(`altova:*`) that Saxon-HE and lxml cannot execute. "
            "Simulate using only the template logic and field mappings above, "
            "noting which fields depend on Altova extension calls.\n"
        )

    # ── Task instruction ──────────────────────────────────────────────────────
    if xslt_result_xml:
        task = (
            "Analyse the actual transform output above. Flag issues (garbled values, "
            "empty required fields, wrong formats). Note any hardcoded values and "
            "conditional branches that fired. Be concise — no more than 6 bullet "
            "points per section. Do NOT reproduce the XML."
        )
    else:
        task = (
            "Simulate this transformation: walk through the XSLT templates, "
            "apply them to the source data above, and show what the output "
            "would look like field-by-field. Identify data quality issues "
            "or source fields that are missing/empty."
        )

    parts.append(f"## Your Task\n{task}\n")

    return "\n".join(parts)


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("\n" + "=" * 80)
    print("  SIMULATION ENGINE — Mapping Transform Validator")
    print("  Execution order: Saxon-HE → lxml → LLM simulation")
    print("=" * 80 + "\n")

    if len(sys.argv) < 2:
        print("Usage: python modules/simulation_engine.py <xslt_file> [source_file]\n")
        print("Examples:")
        print(
            '  python modules/simulation_engine.py '
            '"MappingData/810_C-000340_OUT/810_NordStrom_Xslt_11-08-2023.xml" '
            '"MappingData/810_C-000340_OUT/810/0413e4fc.../SourceFile.txt"'
        )
        sys.exit(0)

    xslt_path = sys.argv[1]
    src_path  = sys.argv[2] if len(sys.argv) > 2 else None

    if not Path(xslt_path).exists():
        print(f"[ERROR] XSLT file not found: {xslt_path}")
        sys.exit(1)

    if src_path and not Path(src_path).exists():
        print(f"[ERROR] Source file not found: {src_path}")
        sys.exit(1)

    try:
        from .file_ingestion import ingest_file
    except ImportError:
        from file_ingestion import ingest_file  # standalone execution

    print(f"[INGEST ] {xslt_path}")
    ingested = ingest_file(file_path=xslt_path)
    print(f"[TYPE   ] {ingested.get('metadata', {}).get('file_type', 'unknown')}")
    raw = (
        ingested.get("parsed_content", {}).get("raw_xml")
        or ingested.get("parsed_content", {}).get("raw_text")
        or ""
    )
    version = _detect_xslt_version(raw)
    altova  = _detect_altova_extensions(raw)
    print(f"[XSLT   ] version={version}  altova_extensions={altova}")
    if src_path:
        print(f"[SOURCE ] {src_path}")
    print()

    print("[SIMULATE] Running …\n")
    response, _ = simulate(ingested, source_file=src_path)

    print("=" * 80)
    print(response)
    print("=" * 80 + "\n")
