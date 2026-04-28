"""
xslt_index.py
─────────────
Build a per-file in-memory index from an XSLT ingested dict (produced by
file_ingestion.ingest_file) and expose it as OpenAI-compatible tool functions.

The LLM receives a compact table-of-contents (~500 tokens) in the system
prompt and calls the tools below to fetch exactly the data it needs.  Every
tool call is a pure Python dict lookup — no LLM call, no I/O.

Public API
──────────
build_xslt_index(ingested)      → index dict
get_toc_string(index)           → compact TOC for the system prompt
XSLT_TOOLS                      → OpenAI function-calling schema list
execute_xslt_tool(index, name, args) → dict result for a tool call
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# ── OpenAI function-calling schema ────────────────────────────────────────────

XSLT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_template",
            "description": (
                "Get full details of a specific XSLT template by name or match "
                "pattern, including its params, local variables, calls, "
                "apply-templates, conditionals, value-of expressions, and the "
                "raw source lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": (
                            "Template name (e.g. 'build_isa') or match pattern "
                            "(e.g. '/' or 'custInvoiceTrans')."
                        ),
                    }
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_variable",
            "description": (
                "Get the declaration and usage information for a global variable "
                "or parameter (scope, select/value expression, and the names of "
                "templates that reference it)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Variable or parameter name, without the $ prefix.",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_segment_templates",
            "description": (
                "Find all templates that produce a specific EDI segment or output "
                "element (e.g. 'ISA', 'IT1', 'N1', 'BIG').  Returns a list of "
                "template identifiers and their key field mappings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "segment": {
                        "type": "string",
                        "description": (
                            "EDI segment name or output element tag (case-insensitive)."
                        ),
                    }
                },
                "required": ["segment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_xslt",
            "description": (
                "Search the full XSLT source for a keyword, XPath expression, or "
                "literal value.  Returns up to 10 matching line windows (±3 lines "
                "of context around each match) so you can see exact source code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Search term (case-insensitive substring match).",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_call_chain",
            "description": (
                "Walk the template call graph starting from a named template or "
                "match pattern and return the full call chain as an indented tree "
                "showing every callee and what parameters are passed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_point": {
                        "type": "string",
                        "description": (
                            "Template name or match pattern to start from "
                            "(e.g. 'build_envelope' or '/')."
                        ),
                    }
                },
                "required": ["entry_point"],
            },
        },
    },
]


# ── Index builder ──────────────────────────────────────────────────────────────

def build_xslt_index(ingested: dict) -> dict:
    """
    Build a queryable in-memory index from a file_ingestion.ingest_file() dict
    for an XSLT file.

    The index contains:
      toc            — compact table-of-contents (for the system prompt)
      templates      — dict keyed by name AND match pattern
      variables      — global variables and params keyed by name
      segment_map    — {SEGMENT_NAME: [template_identifier, ...]}
      hardcoded_values — list from parsed_content
      raw_xml        — full source text (for search_xslt)

    Returns an empty index (with a toc noting the parse failure) when
    parsed_content is missing or incomplete.
    """
    pc = ingested.get("parsed_content") or {}
    meta = ingested.get("metadata") or {}

    # ── Template index (keyed by name and by match) ────────────────────────
    templates: Dict[str, dict] = {}
    for tmpl in pc.get("template_call_graph", []):
        name  = tmpl.get("name")
        match = tmpl.get("match")
        if name:
            templates[name] = tmpl
        if match:
            templates[match] = tmpl
        # When both exist, store under a combined key as canonical
        if name and match:
            templates[f"name:{name}"] = tmpl
            templates[f"match:{match}"] = tmpl

    # ── Variable index ─────────────────────────────────────────────────────
    variables: Dict[str, dict] = {}
    for v in pc.get("global_variables", []):
        vname = v.get("name")
        if vname:
            variables[vname] = {"scope": "global_variable", **v, "used_in": []}
    for p in pc.get("global_params", []):
        pname = p.get("name")
        if pname:
            variables[pname] = {"scope": "global_param", **p, "used_in": []}

    # Populate used_in by scanning template variable_used lists
    for tmpl in pc.get("template_call_graph", []):
        tmpl_id = tmpl.get("name") or tmpl.get("match") or "?"
        for vref in tmpl.get("variables_used", []):
            if vref in variables:
                variables[vref]["used_in"].append(tmpl_id)

    # ── Segment → templates map ────────────────────────────────────────────
    segment_map: Dict[str, List[str]] = {}
    for tmpl in pc.get("template_call_graph", []):
        tmpl_id = tmpl.get("name") or tmpl.get("match") or "?"
        for oe in tmpl.get("output_elements", []):
            if isinstance(oe, dict):
                seg = (oe.get("tag") or "").upper()
            else:
                seg = str(oe).upper()
            if seg:
                segment_map.setdefault(seg, [])
                if tmpl_id not in segment_map[seg]:
                    segment_map[seg].append(tmpl_id)

    # ── Table of contents (compact, fits in ~600 tokens) ──────────────────
    tcg = pc.get("template_call_graph", [])
    entry_pts = pc.get("entry_points", [])
    toc = {
        "file":            meta.get("filename", "unknown"),
        "xslt_version":    pc.get("version", "1.0"),
        "output_method":   (pc.get("outputs") or {}).get("method", "xml"),
        "template_count":  len(tcg),
        "entry_points":    [
            (ep.get("name") or ep.get("match") or "?") for ep in entry_pts
        ],
        "named_templates": sorted(
            t.get("name") for t in tcg if t.get("name")  # type: ignore[misc]
        ),
        "match_templates": sorted(
            t.get("match") for t in tcg if t.get("match")  # type: ignore[misc]
        ),
        "global_variables": [v.get("name") for v in pc.get("global_variables", [])],
        "global_params":    [p.get("name") for p in pc.get("global_params", [])],
        "segments_produced": sorted(segment_map.keys()),
        "hardcoded_count":  len(pc.get("hardcoded_values", [])),
        "imports_includes": pc.get("imports_includes", []),
        "modes":            list((pc.get("mode_index") or {}).keys()),
    }

    return {
        "toc":              toc,
        "templates":        templates,
        "variables":        variables,
        "segment_map":      segment_map,
        "hardcoded_values": pc.get("hardcoded_values", []),
        "raw_xml":          pc.get("raw_xml", ""),
    }


# ── TOC string builder ─────────────────────────────────────────────────────────

def get_toc_string(index: dict) -> str:
    """
    Render the table-of-contents as a compact text block suitable for embedding
    in the system prompt (~400–700 tokens for a large production stylesheet).
    """
    toc = index.get("toc", {})
    lines = [
        f"File: {toc.get('file', '?')}",
        f"XSLT version: {toc.get('xslt_version', '?')}  |  "
        f"Output method: {toc.get('output_method', '?')}",
        f"Templates: {toc.get('template_count', 0)} total",
        "",
        "Entry points: " + (
            ", ".join(toc.get("entry_points", [])) or "(none detected)"
        ),
    ]

    named = toc.get("named_templates", [])
    if named:
        lines.append(f"\nNamed templates ({len(named)}):")
        for n in named:
            lines.append(f"  - {n}")

    match_ = toc.get("match_templates", [])
    if match_:
        lines.append(f"\nMatch templates ({len(match_)}):")
        for m in match_:
            lines.append(f"  - {m}")

    gv = toc.get("global_variables", [])
    gp = toc.get("global_params", [])
    if gv or gp:
        lines.append(f"\nGlobal variables: {', '.join(v for v in gv if v)}")
        if gp:
            lines.append(f"Global params: {', '.join(p for p in gp if p)}")

    segs = toc.get("segments_produced", [])
    if segs:
        lines.append(f"\nSegments/elements produced: {', '.join(segs)}")

    hc = toc.get("hardcoded_count", 0)
    if hc:
        lines.append(f"Hardcoded literal values: {hc}")

    ii = toc.get("imports_includes", [])
    if ii:
        lines.append("\nImports/includes:")
        for imp in ii:
            lines.append(f"  - {imp.get('type', '?')}: {imp.get('href', '?')}")

    modes = toc.get("modes", [])
    if modes:
        lines.append(f"\nProcessing modes: {', '.join(modes)}")

    return "\n".join(lines)


# ── Tool executor ──────────────────────────────────────────────────────────────

def execute_xslt_tool(index: dict, tool_name: str, args: dict) -> dict:
    """
    Dispatch a single tool call and return a JSON-serialisable result dict.
    All operations are pure Python dict/string operations — no LLM calls.
    """
    try:
        if tool_name == "get_template":
            return _get_template(index, args.get("identifier", ""))
        if tool_name == "get_variable":
            return _get_variable(index, args.get("name", ""))
        if tool_name == "get_segment_templates":
            return _get_segment_templates(index, args.get("segment", ""))
        if tool_name == "search_xslt":
            return _search_xslt(index, args.get("keyword", ""))
        if tool_name == "get_call_chain":
            return _get_call_chain(index, args.get("entry_point", ""))
        return {"error": f"Unknown tool: {tool_name!r}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# ── Individual tool implementations ────────────────────────────────────────────

def _get_template(index: dict, identifier: str) -> dict:
    """Return full template data for the given name or match pattern."""
    templates = index.get("templates", {})
    # Try direct lookup, then case-insensitive scan
    entry = templates.get(identifier)
    if entry is None:
        id_lower = identifier.lower()
        for k, v in templates.items():
            if k.lower() == id_lower:
                entry = v
                break
    if entry is None:
        # fuzzy: substring match on keys
        id_lower = identifier.lower()
        candidates = [k for k in templates if id_lower in k.lower()]
        if len(candidates) == 1:
            entry = templates[candidates[0]]
        elif candidates:
            return {
                "error": f"Ambiguous identifier {identifier!r}.",
                "candidates": candidates[:10],
            }
        else:
            return {
                "error": f"No template found for identifier {identifier!r}.",
                "available_keys": list(templates.keys())[:30],
            }

    # Strip raw_xml from a copy to keep the result focused
    result = {k: v for k, v in entry.items() if k != "raw_xml"}

    # Add a source snippet if available — returns both numbered (for display)
    # and raw (for exact patch construction).
    raw = index.get("raw_xml", "")
    if raw and (entry.get("name") or entry.get("match")):
        snippet = _extract_template_source(raw, entry.get("name"), entry.get("match"))
        if isinstance(snippet, dict):
            result["source_snippet"]     = snippet["numbered"]   # display version
            result["source_snippet_raw"] = snippet["raw"]        # exact copyable text
            result["source_start_line"]  = snippet["start_line"]
            result["source_end_line"]    = snippet["end_line"]
        elif snippet:
            result["source_snippet"] = snippet

    return result


def _get_variable(index: dict, name: str) -> dict:
    """Return declaration and usage for a global variable or param."""
    variables = index.get("variables", {})
    entry = variables.get(name)
    if entry is None:
        # case-insensitive fallback
        name_lower = name.lower()
        for k, v in variables.items():
            if k.lower() == name_lower:
                entry = v
                break
    if entry is None:
        return {
            "error": f"No global variable or param named {name!r}.",
            "available": list(variables.keys()),
        }
    return dict(entry)


def _get_segment_templates(index: dict, segment: str) -> dict:
    """Return templates that produce the given EDI segment / output element."""
    seg_upper = segment.upper()
    segment_map = index.get("segment_map", {})
    tmpl_ids = segment_map.get(seg_upper, [])

    if not tmpl_ids:
        # Try partial match (e.g. "IT1" matches "IT1" in segments like "IT101")
        tmpl_ids = [
            tid
            for seg_key, ids in segment_map.items()
            if seg_upper in seg_key or seg_key in seg_upper
            for tid in ids
        ]
        tmpl_ids = list(dict.fromkeys(tmpl_ids))  # deduplicate

    if not tmpl_ids:
        return {
            "segment": segment,
            "templates": [],
            "note": f"No templates found producing segment {segment!r}.",
            "all_segments": sorted(segment_map.keys()),
        }

    templates = index.get("templates", {})
    details = []
    for tid in tmpl_ids:
        entry = templates.get(tid, {})
        details.append({
            "identifier": tid,
            "value_of":   entry.get("value_of", [])[:20],
            "calls":      entry.get("calls", []),
            "conditionals": entry.get("conditionals", [])[:10],
        })

    return {"segment": segment, "templates": details}


def _search_xslt(index: dict, keyword: str) -> dict:
    """Return up to 10 matching line windows (±3 lines context) from raw_xml."""
    raw = index.get("raw_xml", "")
    if not raw:
        return {"keyword": keyword, "matches": [], "note": "raw_xml not available"}

    kw_lower = keyword.lower()
    lines = raw.splitlines()
    matching_windows = []
    seen_ranges: set = set()

    for i, line in enumerate(lines):
        if kw_lower in line.lower():
            start = max(0, i - 3)
            end   = min(len(lines), i + 4)
            rng   = (start, end)
            if rng not in seen_ranges:
                seen_ranges.add(rng)
                window_lines = lines[start:end]
                matching_windows.append({
                    "line_number": i + 1,
                    # context: human-readable with line-number prefix for orientation
                    "context": "\n".join(
                        f"{start + j + 1:4d}  {ln}"
                        for j, ln in enumerate(window_lines)
                    ),
                    # raw_lines: exact source text WITHOUT line-number prefix.
                    # Use these when building patch "before" blocks — they must
                    # match the XSLT character-for-character.
                    "raw_lines": "\n".join(window_lines),
                    # match_line: the single line that triggered this match (exact)
                    "match_line": lines[i],
                })
            if len(matching_windows) >= 10:
                break

    return {
        "keyword": keyword,
        "match_count": len(matching_windows),
        "matches": matching_windows,
    }


def _get_call_chain(index: dict, entry_point: str, _visited: Optional[set] = None) -> dict:
    """Walk the call graph from entry_point and return an indented tree."""
    if _visited is None:
        _visited = set()

    templates = index.get("templates", {})
    entry = templates.get(entry_point)
    if entry is None:
        ep_lower = entry_point.lower()
        for k in templates:
            if k.lower() == ep_lower:
                entry = templates[k]
                break

    if entry is None:
        return {
            "error": f"No template found for entry point {entry_point!r}.",
            "available_entry_points": [
                (ep.get("name") or ep.get("match") or "?")
                for ep in index.get("toc", {}).get("entry_points", [])
            ]
            or list(templates.keys())[:20],
        }

    tmpl_id = entry.get("name") or entry.get("match") or entry_point
    if tmpl_id in _visited:
        return {"chain": f"  [recursive call to {tmpl_id}]"}
    _visited.add(tmpl_id)

    tree_lines = [f"{tmpl_id}"]
    for call in entry.get("calls", []):
        callee = call.get("callee", "?")
        wp = call.get("with_params", [])
        param_str = (
            "(" + ", ".join(f"{p['name']}={p.get('select') or p.get('value') or '?'}" for p in wp) + ")"
            if wp else ""
        )
        sub = _get_call_chain(index, callee, _visited)
        sub_chain = sub.get("chain", f"{callee}") if isinstance(sub, dict) else callee
        tree_lines.append(
            "  → calls: " + callee + param_str + "\n"
            + "\n".join("    " + ln for ln in sub_chain.splitlines() if ln.strip())
        )
    for apply in entry.get("applies", []):
        mode = apply.get("mode", "default")
        sel  = apply.get("select", "*")
        tree_lines.append(f"  → apply-templates: select={sel} mode={mode}")

    return {"chain": "\n".join(tree_lines)}


# ── Helper: extract raw template source from full XSLT text ───────────────────

def _extract_template_source(raw_xml: str, name: Optional[str], match: Optional[str]) -> str:
    """
    Extract the raw XML lines for one xsl:template from the full stylesheet.
    Returns up to 80 lines so the LLM gets sufficient detail without noise.
    """
    lines = raw_xml.splitlines()
    start_idx = None
    # Find the opening tag of the template
    for i, line in enumerate(lines):
        if "xsl:template" not in line:
            continue
        if name and f'name="{name}"' in line:
            start_idx = i
            break
        if name and f"name='{name}'" in line:
            start_idx = i
            break
        if match and f'match="{match}"' in line:
            start_idx = i
            break
        if match and f"match='{match}'" in line:
            start_idx = i
            break

    if start_idx is None:
        return ""

    # Collect lines until the closing </xsl:template>
    depth = 0
    end_idx = start_idx
    for i in range(start_idx, min(start_idx + 200, len(lines))):
        ln = lines[i]
        if "<xsl:template" in ln:
            depth += 1
        if "</xsl:template>" in ln:
            depth -= 1
            if depth <= 0:
                end_idx = i
                break

    snippet_lines = lines[start_idx : end_idx + 1]
    # Cap at 80 lines to avoid huge tool results
    truncated = False
    if len(snippet_lines) > 80:
        snippet_lines = snippet_lines[:78]
        truncated = True

    numbered = "\n".join(
        f"{start_idx + j + 1:4d}  {ln}" for j, ln in enumerate(snippet_lines)
    )
    if truncated:
        numbered += "\n  ... [truncated — use search_xslt for full content]"

    # Return a dict so callers get both the numbered display version AND the
    # raw text for exact patch construction.
    return {
        "numbered": numbered,
        "raw": "\n".join(snippet_lines),
        "start_line": start_idx + 1,
        "end_line": start_idx + len(snippet_lines),
    }
