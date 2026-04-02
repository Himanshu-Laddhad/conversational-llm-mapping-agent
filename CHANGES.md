# PartnerLinQ Mapping Intelligence Agent — Code Changes

> **Branch:** `fix/file-type-detection`
> **Author:** Preetham Thirunavakkarasu
> **Date:** April 2026
> **Files changed:** `modules/file_ingestion.py` · `modules/file_agent.py`

---

## What This Branch Fixes

Three gaps were identified during user acceptance testing with real PartnerLinQ mapping data files. The agent was producing incorrect and misleading descriptions for two critical file types used in every integration project, and had no ability to explain relationships between templates in XSLT mapping stylesheets.

---

## Change 1 — D365 XML File Detection and Parsing

### Before
Uploading a Microsoft Dynamics 365 ERP invoice XML file (SourceFile.txt) produced this:

```
File type detected: XML
"This appears to be a custom or proprietary standard — possibly a non-standard EDI format.
The structure does not match any known B2B integration standard."
```

No business fields were extracted. The agent could not identify the customer, invoice number, line items, carrier, or amounts.

### After
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

### Root Causes Fixed
| # | Root Cause | Fix |
|---|-----------|-----|
| 1 | `detect_file_type()` had no D365 detection path — fell into generic XML | Added D365_XML detection checking for `<saleCustInvoice>` / `<custInvoiceTrans>` in content |
| 2 | `.txt` files with XML content were hitting the X12 EDI detector first | Added XML-content guard: skip X12 EDI path when `.txt` file starts with `<` |
| 3 | `parse_xml()` returned a raw XML tree with no field labelling | Added `parse_d365_xml()` extracting all business fields into a labelled dict |
| 4 | No domain-specific LLM prompt for D365 files | Added 6-section D365_XML system prompt in `file_agent.py` |

### Where in the Code
**`modules/file_ingestion.py`**
- Line ~31: Added `_txt_is_xml` guard to X12 EDI detection block
- Before the generic XML block: added D365_XML detection (checks `<saleCustInvoice>`, `<custInvoiceTrans>`, `<SalesTable>`)
- New function: `parse_d365_xml()` — extracts invoice header, line items, four address blocks, shipment/carrier info, business summary string
- `ingest_file()` dispatcher: added `elif file_type == "D365_XML": parse_d365_xml()`

**`modules/file_agent.py`**
- Added `if file_type == "D365_XML":` system prompt block with 6 structured sections covering source system ID, header fields, addresses, line items, shipment, and D365-to-EDI field mapping guide

---

## Change 2 — X12 MapForce XML File Detection and Parsing

### Before
Uploading a MapForce-generated X12 856 Ship Notice XML file (TargetFile.txt) produced this:

```
File type detected: XML
"This file appears to be a purchase order or invoice — possibly an X12 850 or 810.
The ISA sender and receiver cannot be determined. Business content appears empty."
```

The agent misidentified an 856 Ship Notice as an 850 Purchase Order, reported sender/receiver as unknown, and said the content was empty — all incorrect.

### After
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

### Root Causes Fixed
| # | Root Cause | Fix |
|---|-----------|-----|
| 1 | No X12_XML detection path — MapForce XML fell into generic XML or X12 EDI | Added X12_XML detection checking for `<X12_` pattern in first 500 chars |
| 2 | `<ISA>` XML tags accidentally triggered the flat-file X12 EDI parser | Same `.txt` XML-content guard as Change 1 |
| 3 | No parser for MapForce X12 XML structure | Added `parse_x12_xml()` extracting ISA/GS envelope, HL loops, segments, line items, SSCC labels |
| 4 | No domain-specific LLM prompt for X12 XML files | Added 6-section X12_XML system prompt in `file_agent.py` |

### Where in the Code
**`modules/file_ingestion.py`**
- Before the generic XML block: added X12_XML detection (checks root element starts with `X12_`, parses ISA version and TS number from root element name)
- Fallback block: added X12_XML check as safety net
- New function: `parse_x12_xml()` — extracts ISA envelope, GS group, BSN/BIG/BEG transaction segments, HL loops (S/O/P/I), REF, TD5, PRF, LIN/SN1, MAN/SSCC, CTT totals
- `ingest_file()` dispatcher: added `elif file_type == "X12_XML": parse_x12_xml()`

**`modules/file_agent.py`**
- Added `if file_type == "X12_XML":` system prompt block with 6 structured sections covering transaction ID, ISA envelope, GS group, transaction segments, HL hierarchy, and line-item/reference data

---

## Change 3 — XSLT Template Relationship Analysis

### Before
When an XSLT mapping stylesheet was uploaded and the user asked "how do templates relate to each other?", the agent could only give a flat list of template names. It could not:
- Show which templates call which other templates
- Explain the execution flow from entry point to output
- Map `xsl:value-of` expressions to their output EDI fields
- Identify shared variables across templates
- Explain conditional business logic per template

### After
The agent now produces:

```
Entry Point: match="/" template fires first.

Execution Flow:
  match="/" → calls: build_envelope
    build_envelope → calls: build_isa, build_gs, build_st
      build_isa → applies: ISA/* (default mode) → calls: format_date
      build_gs  → value-of: $groupControlNum, $senderAppID
      build_st  → for-each: /Order/LineItem → calls: build_hl_item

Field Mapping (ISA segment):
  ISA01  = hardcoded '00'          (no authorization info qualifier)
  ISA06  = $senderID               (global param — Wilson Electronics ID)
  ISA08  = $receiverID             (global param — trading partner ID)
  ISA09  = format-date(...)        (today's date in YYMMDD format)
  ISA12  = hardcoded '00401'       (X12 version 4010)

Business Logic — pricingTable template:
  xsl:choose: if @minQty exists → output bulk tier row
              otherwise → output standard price row

Hardcoded Values flagged for parameterization:
  '00401'  — X12 version string (ISA12) — hardcoded, should be a param
  '4356735021' — sender EDI ID (ISA06) — hardcoded, multi-partner risk
```

### What Was Added to `parse_xslt()`
**`modules/file_ingestion.py`** — `parse_xslt()` now extracts per template:

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

**`modules/file_agent.py`** — XSLT system prompt expanded from 6 to 8 sections:
1. Transformation Summary
2. **Entry Points and Execution Flow** *(new — call tree with indentation)*
3. **Template Relationship Map** *(new — per-template calls/applies/outputs)*
4. Field Mapping Table
5. **Variable and Parameter Dependency** *(new — cross-template variable tracing)*
6. Conditional and Business Logic
7. Hardcoded Values
8. **Segment-Level Transformation Walkthrough** *(new — segment-by-segment breakdown)*

---

## Testing

### Quick Python smoke test
```bash
cd ~/Downloads/Industry-Practicum_PartnerLinQ-main
python3 -c "
from modules.file_ingestion import ingest_file

# Test D365 XML
r = ingest_file(file_path='/path/to/SourceFile.txt')
print(r['metadata']['file_type'])          # expect: D365_XML
print(r['parsed_content']['business_summary'])

# Test X12 XML
r = ingest_file(file_path='/path/to/TargetFile.txt')
print(r['metadata']['file_type'])          # expect: X12_XML
print(r['parsed_content']['business_summary'])

# Test XSLT
r = ingest_file(file_path='test_files/sample_catalog_transform.xslt')
print(r['metadata']['file_type'])          # expect: XSLT
print([t['name'] or t['match'] for t in r['parsed_content']['template_call_graph']])
print(r['parsed_content']['entry_points'])
"
```

### Streamlit UI test
```bash
python3 -m streamlit run app.py
```

Upload files from `PartnerlinQ MappingData/856_4010_OUT_COSTCO/856/` and ask:

**For TargetFile.txt (X12 XML):**
- "What type of file is this and what business transaction does it represent?"
- "Who is the sender and receiver? What are their EDI IDs?"
- "Explain the HL loop hierarchy."
- "Run an audit on this file."

**For SourceFile.txt (D365 XML):**
- "What ERP system generated this file and what EDI transaction does it map to?"
- "List all line items with quantities and prices."
- "Map the D365 fields to X12 EDI segments."

**For any XSLT file:**
- "Walk me through the execution flow of this stylesheet."
- "Which templates call which other templates?"
- "Show me the field mapping table."
- "Are there any hardcoded values that should be parameterized?"

---

## Backward Compatibility

All existing file types are unaffected:

| File Type | Detection | Parser | Status |
|-----------|-----------|--------|--------|
| X12 EDI (flat file) | `content_start.startswith("ISA")` | `parse_x12_edi()` | ✅ Unchanged |
| EDIFACT | `UNA` / `UNB` prefix | `parse_edifact()` | ✅ Unchanged |
| XSLT | `.xsl`/`.xslt` extension or `xsl:stylesheet` in content | `parse_xslt()` | ✅ Enhanced (backward compatible) |
| XSD | `.xsd` extension or `xs:schema` in content | `parse_xsd()` | ✅ Unchanged |
| XML (generic) | `.xml` extension or `<?XML` declaration | `parse_xml()` | ✅ Unchanged |
| **D365_XML** | `<saleCustInvoice>` / `<custInvoiceTrans>` in content | `parse_d365_xml()` | 🆕 New |
| **X12_XML** | `<X12_` root element pattern in content | `parse_x12_xml()` | 🆕 New |

No database changes, no new dependencies, no configuration changes, no breaking API changes.
