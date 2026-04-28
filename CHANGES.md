# PartnerLinQ Mapping Intelligence Agent — Code Changes

---

## Chapter 1 — File Type Detection & XSLT Template Analysis

> **Branch:** `fix/file-type-detection`  
> **Author:** Preetham Thirunavakkarasu  
> **Date:** April 2026  
> **Files changed:** `modules/file_ingestion.py` · `modules/file_agent.py`

### Summary

Three gaps were identified during user acceptance testing with real PartnerLinQ mapping data files. The agent was producing incorrect and misleading descriptions for two critical file types used in every integration project, and had no ability to explain relationships between templates in XSLT mapping stylesheets.

---

### Change 1 — D365 XML File Detection and Parsing

**Before:** Uploading a Microsoft Dynamics 365 ERP invoice XML file (SourceFile.txt) produced:

```
File type detected: XML
"This appears to be a custom or proprietary standard — possibly a non-standard EDI format.
The structure does not match any known B2B integration standard."
```

No business fields were extracted. The agent could not identify the customer, invoice number, line items, carrier, or amounts.

**After:**
```
File type detected: D365_XML (version: D365:AX | Inv:SOCI-214540 | SO:S0248567)

Microsoft Dynamics 365 Customer Invoice
  Invoice:       SOCI-214540
  Sales Order:   S0248567
  Customer:      Amazon (location 8001774)
  Amount:        $77.14 USD
  Payment Terms: Net 90 days
  Item:          Yagi Antenna 700/800/900 MHz — Qty 2 EA @ $38.57
  Carrier:       UPS Ground
  Tracking:      1ZE844690399592095
  Ship From:     Wilson Electronics, Saint George UT
  Ship To:       21 Roadway Dr, Carlisle PA 17015
  D365→EDI mapping: InvoiceId→BIG02, SalesId→REF*CO, Amount→TDS01,
                     SalesPrice→IT104, Qty→IT102, Tracking→REF*CN
```

| # | Root Cause | Fix |
|---|-----------|-----|
| 1 | `detect_file_type()` had no D365 detection path — fell into generic XML | Added D365_XML detection checking for `<saleCustInvoice>` / `<custInvoiceTrans>` in content |
| 2 | `.txt` files with XML content were hitting the X12 EDI detector first | Added XML-content guard: skip X12 EDI path when `.txt` file starts with `<` |
| 3 | `parse_xml()` returned a raw XML tree with no field labelling | Added `parse_d365_xml()` extracting all business fields into a labelled dict |
| 4 | No domain-specific LLM prompt for D365 files | Added 6-section D365_XML system prompt in `file_agent.py` |

---

### Change 2 — X12 MapForce XML File Detection and Parsing

**Before:** Uploading a MapForce-generated X12 856 Ship Notice XML file (TargetFile.txt) produced:

```
File type detected: XML
"This file appears to be a purchase order or invoice — possibly an X12 850 or 810.
The ISA sender and receiver cannot be determined. Business content appears empty."
```

The agent misidentified an 856 Ship Notice as an 850 Purchase Order, reported sender/receiver as unknown, and said the content was empty — all incorrect.

**After:**
```
File type detected: X12_XML (version: ISA:00401 | TS:856 | Root:X12_00401_856)

X12 856 — Ship Notice / Advance Shipment Notice (ASN)
  Sender:     Wilson Electronics (ISA ID: 4356735021)
  Receiver:   Costco (ISA ID: 4253138601CH)
  Control#:   255720301  |  Environment: Production
  Shipment:   WHSHP-213678  |  Ship Date: 2025-12-19
  UPS Tracking: 1ZY20J840344148347
  PO Reference: 00847006432917
  Item:       Vendor Part 477061 / SKU 1884550 — Qty 1 EA (status: AC)
  SSCC-18:    00008100059602597475
  HL Loops:   Shipment(1) → Order(2) → Pack(3) → Item(4)
  Carrier:    Costco UPS (routing: B)
```

| # | Root Cause | Fix |
|---|-----------|-----|
| 1 | No X12_XML detection path — MapForce XML fell into generic XML or X12 EDI | Added X12_XML detection checking for `<X12_` pattern in first 500 chars |
| 2 | `<ISA>` XML tags accidentally triggered the flat-file X12 EDI parser | Same `.txt` XML-content guard as Change 1 |
| 3 | No parser for MapForce X12 XML structure | Added `parse_x12_xml()` extracting ISA/GS envelope, HL loops, segments, line items, SSCC labels |
| 4 | No domain-specific LLM prompt for X12 XML files | Added 6-section X12_XML system prompt in `file_agent.py` |

---

### Change 3 — XSLT Template Relationship Analysis

`parse_xslt()` in `file_ingestion.py` now extracts per template:

| New Field | What It Contains |
|-----------|-----------------|
| `calls` | Every `xsl:call-template` with callee name and `with-param` values passed |
| `applies` | Every `xsl:apply-templates` with `select` path, mode, and sort key |
| `conditionals` | Every `xsl:if` / `xsl:when` / `xsl:otherwise` with test expression |
| `value_of` | Every `xsl:value-of select=` expression (the field mappings) |
| `for_each` | Every `xsl:for-each` loop with its node-set select |
| `variables_used` | All `$varname` references in that template's XPath |
| `local_variables` | Variables declared inside the template |
| `params_accepted` | Parameters the template accepts (name, default, required) |
| `output_elements` | Literal XML/EDI elements the template produces |

Plus three new top-level structures:

| New Structure | What It Contains |
|--------------|-----------------|
| `entry_points` | Templates that fire first (match="/" or not called by any other template) |
| `mode_index` | Templates grouped by `xsl:apply-templates` mode |
| `hardcoded_values` | Every string literal with its location and context |

---

## Chapter 2 — Tool-Calling Architecture for Explain & Modify

> **Date:** April 2026  
> **Files changed:** `modules/xslt_index.py` (new) · `modules/modification_engine.py` · `modules/file_agent.py` · `modules/groq_agent.py` · `modules/dispatcher.py` · `modules/session.py` · `modules/llm_client.py` · `modules/intent_router.py` · `app.py` · `.env` · `.env.example`

### Summary

`explain` and `modify` were failing silently on large production XSLT files (400–1,200 lines) because the full file content exceeded the working context of the LLM. The fix introduces a structured XSLT index and OpenAI function calling, so the LLM fetches only what it needs rather than receiving the entire file.

---

### Change 4 — New module: `xslt_index.py`

A new module builds a queryable in-memory index from any `ingest_file()` output dict for an XSLT file. The index is built once at upload and stored in `Session.xslt_indices`.

**Index contents:**
- `templates` — dict keyed by name and match pattern; each entry contains `calls`, `applies`, `value_of`, `conditionals`, `output_elements`, etc.
- `variables` — global variables and params, with usage tracking across templates
- `segment_map` — `{SEGMENT_NAME: [template_id, ...]}` for quick segment lookup
- `hardcoded_values` — list of all literal strings in the file
- `raw_xml` — full source text (used by `search_xslt`)

**Tool functions exposed to the LLM (OpenAI function-calling schema):**

| Tool | What it returns |
|------|----------------|
| `get_template(identifier)` | Full template data including `source_snippet` (numbered, for display) and `source_snippet_raw` (exact copyable text for patches) |
| `get_variable(name)` | Declaration, select expression, and list of templates that reference the variable |
| `get_segment_templates(segment)` | All templates producing a given EDI segment/output element |
| `search_xslt(keyword)` | Up to 10 matching line windows (±3 lines context), each with `context` (numbered), `raw_lines` (exact text), and `match_line` (the single triggering line) |
| `get_call_chain(entry_point)` | Full call tree from a named template or match pattern |

---

### Change 5 — `modification_engine.py`: multi-patch tool-calling primary path

The `modify()` function now has two execution paths:

**Primary path (requires `xslt_index`):**
1. System prompt includes the XSLT table-of-contents.
2. LLM uses `search_xslt` / `get_template` / `get_call_chain` to explore all affected locations (including cascading effects in other templates).
3. LLM calls `submit_patches` once with a list of `{description, before, after, line_hint}` dicts.
4. `apply_patches_sequential()` verifies all `before` blocks exist, sorts patches bottom-to-top by `line_hint`, then applies each using `_replace_at_line_hint()`.
5. `verify_patches_applied()` confirms every `after` is present and no `before` remains.
6. `validate_xslt_wellformed()` runs lxml on the result.
7. `_build_slim_response()` returns a numbered change list + `difflib.unified_diff` — no full XSLT in the UI response.

**Fallback path (no `xslt_index` — legacy):**
- Used when no pre-built index is available (e.g. non-XSLT files).
- Extracts candidate blocks by keyword search, sends to LLM, parses BEFORE/AFTER sections, applies single patch.

**Key new helpers:**

```python
def _replace_at_line_hint(text, before, after, line_hint):
    """Replace the occurrence of 'before' nearest to line_hint (not always the first)."""

def apply_patches_sequential(raw_xslt, patches):
    """All-or-nothing: verify all BEFOREs exist, sort bottom-to-top, apply."""

def verify_patches_applied(patched_xslt, patches):
    """Confirm every AFTER is present and no BEFORE remains."""

def _build_slim_response(patches, verification, original_xslt, patched_xslt):
    """Compact UI response: numbered change list + unified diff only."""
```

---

### Change 6 — `session.py`: XSLT index storage

Added `xslt_indices` field (dict keyed by filename) with `set_xslt_index()` / `get_xslt_index()` methods. Cleared on session `reset()`.

---

### Change 7 — `dispatcher.py`: engine-aware model resolution + Groq intent routing

**Model resolution:**
```python
def _engine_model(engine: str) -> str:
    """Return EXPLAIN_MODEL, MODIFY_MODEL etc. from env, or fall back to provider default."""
    if model:          # caller-forced model always wins
        return model
    return _gdm(_prov, engine=engine)
```

Each engine now passes the appropriate model:
- `explain()` → `EXPLAIN_MODEL` (default `gpt-4.1-mini`)
- `modify()` → `MODIFY_MODEL` (default `gpt-4.1`)
- `simulate()` → `SIMULATE_MODEL` (default `gpt-4.1-mini`)
- `audit()` → `AUDIT_MODEL` (default `gpt-4.1`)
- `generate()` → `GENERATE_MODEL` (default `gpt-4.1`)

**Intent routing via Groq:**
```python
_groq_key = os.getenv("GROQ_API_KEY")
if _groq_key:
    route_result = route(user_message, api_key=_groq_key, provider="groq",
                         model=os.getenv("INTENT_ROUTER_MODEL", "llama-3.1-8b-instant"))
else:
    route_result = route(user_message, api_key=api_key, provider=provider or "openai")
```

**XSLT index lifecycle:**
- At file upload: dispatcher calls `build_xslt_index(ingested)` for any XSLT file and stores it via `session.set_xslt_index(filename, index)`.
- At explain/modify: dispatcher retrieves the index via `session.get_xslt_index(filename)` and passes it to the engine.

---

### Change 8 — `llm_client.py`: engine-aware model resolution + `chat_complete_with_tools`

**`get_default_model(provider, engine=None)`** now checks `{ENGINE}_MODEL` env var first:
```python
def get_default_model(provider: str, engine: Optional[str] = None) -> str:
    if engine:
        engine_val = os.getenv(f"{engine.upper()}_MODEL", "")
        if engine_val:
            return engine_val
    provider_val = os.getenv(_MODEL_ENV_VARS.get(provider, ""), "")
    return provider_val or DEFAULT_MODELS.get(provider, "gpt-4.1-mini")
```

**`chat_complete_with_tools()`** runs an OpenAI-style tool-calling loop:
- Sends messages + tool schemas to the LLM.
- On each `tool_calls` response, dispatches to the caller-supplied `tool_executor` function.
- Appends tool results as `tool` role messages and calls the LLM again.
- Stops when no tool calls are made or `max_tool_rounds` is reached.
- Returns `(final_text, all_messages)` so callers can extract structured tool outputs from the message thread.

---

### Change 9 — `app.py`: remove API key input, filter providers by env

- Removed the sidebar text input for API key override.
- Provider dropdown now only lists providers whose `<PROVIDER>_API_KEY` environment variable is set.
- All `dispatch()` calls resolve API keys exclusively from environment variables.

---

### Change 10 — `.env` / `.env.example`: per-engine model overrides

New environment variables for task-specific model selection:

```ini
EXPLAIN_MODEL=gpt-4.1-mini
MODIFY_MODEL=gpt-4.1
SIMULATE_MODEL=gpt-4.1-mini
AUDIT_MODEL=gpt-4.1
GENERATE_MODEL=gpt-4.1
INTENT_ROUTER_MODEL=llama-3.1-8b-instant
INTENT_ROUTER_THRESHOLD=0.45
```

If any variable is unset, `get_default_model()` falls back to the provider-level default.

---

## Backward Compatibility

All changes are additive or narrowly scoped:

| File Type | Detection | Parser | Status |
|-----------|-----------|--------|--------|
| X12 EDI (flat file) | `content_start.startswith("ISA")` | `parse_x12_edi()` | Unchanged |
| EDIFACT | `UNA` / `UNB` prefix | `parse_edifact()` | Unchanged |
| XSLT / XSL | extension or `xsl:stylesheet` in content | `parse_xslt()` | Enhanced (backward compatible) |
| XSD | extension or `xs:schema` | `parse_xsd()` | Unchanged |
| XML (generic) | extension or `<?xml` declaration | `parse_xml()` | Unchanged |
| D365_XML | `<saleCustInvoice>` in content | `parse_d365_xml()` | Added Ch. 1 |
| X12_XML | `<X12_` root element | `parse_x12_xml()` | Added Ch. 1 |

No database schema changes, no new required dependencies for existing functionality, no breaking API changes. The tool-calling path in `modify()` activates only when `xslt_index` is provided; all callers that do not pass it continue using the legacy path unchanged.
