# PartnerLinQ — Conversational Mapping Intelligence Agent

A Streamlit-based AI agent that lets EDI analysts interact with XSLT/EDI mapping files in plain English. Upload a mapping file, ask questions, simulate transformations, modify mappings, audit for issues, or generate new XSLTs — all through a conversational chat interface.

---

## Features

| Intent | What it does |
|---|---|
| **Explain** | Describes what an XSLT/EDI mapping does in plain English |
| **Simulate** | Runs the XSLT transformation against a real source file using Saxon-HE (XSLT 2.0/3.0) or lxml (XSLT 1.0), with LLM fallback |
| **Modify** | Adds fields, changes values, inserts segments, returns a downloadable patched XSLT |
| **Audit** | Checks a mapping for misconfigurations and production-readiness issues |
| **Generate** | Creates a new XSLT mapping from plain-English requirements |

---

## Project Structure

```
PartnerLinQ/
├── app.py                        # Streamlit UI — multi-file upload, chat, sidebar
├── modules/
│   ├── dispatcher.py             # Central router — intent → engine
│   ├── intent_router.py          # Classifies user intent via LLM scoring
│   ├── file_ingestion.py         # Parses XSLT, X12 EDI, D365 XML, X12 XML
│   ├── file_agent.py             # Conversational FileAgent (Groq-backed)
│   ├── groq_agent.py             # explain() engine
│   ├── simulation_engine.py      # simulate() — Saxon-HE → lxml → LLM cascade
│   ├── modification_engine.py    # modify() — returns patched XSLT
│   ├── audit_engine.py           # audit() — structured issue detection
│   ├── xslt_generator.py         # generate() — XSLT from requirements
│   ├── rag_engine.py             # RAG folder indexing and querying
│   ├── llm_client.py             # Multi-provider LLM client (Groq / OpenAI / Anthropic)
│   └── session.py                # Session state: file history, conversation memory
├── requirements.txt
├── test_changes.py               # Verification test suite (26 checks)
└── .env                          # API keys (never commit this file)
```

---

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/Preetham33/Industry-Practicum_PartnerLinQ.git
cd Industry-Practicum_PartnerLinQ
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

> **Note:** `saxonche` (Saxon-HE 12.x) is required for XSLT 2.0/3.0 simulation. It is included in `requirements.txt`.

### 3. Configure your API key
Create a `.env` file in the project root:
```
GROQ_API_KEY=gsk_your_key_here
GROQ_MODEL=llama-3.3-70b-versatile
INTENT_ROUTER_THRESHOLD=0.45
```
Get a free Groq API key at [console.groq.com/keys](https://console.groq.com/keys).

### 4. Run the app
```bash
streamlit run app.py
```

---

## How to Use

1. **Upload files** using the paperclip / Attach files button
   - Upload your **XSLT mapping file** (`.xml` or `.xsl`)
   - For simulation, also upload the **source data file** (D365 XML, X12 EDI, etc.)
2. **Ask anything** in the chat box:
   - `"Explain what this mapping does"`
   - `"Simulate this XSLT transformation on the uploaded source file"`
   - `"Add a DTM segment with today's date after the BIG segment"`
   - `"Audit this mapping for production issues"`
   - `"Generate an XSLT that maps D365 XML to X12 810"`
3. For **Modify** results, a **Download patched XSLT** button appears automatically.

---

## Simulation Engine — Execution Hierarchy

The simulation engine tries processors in this order:

```
1. Altova extension check
   └─ If actual altova:funcName() calls detected → skip to LLM (not executable)

2. Saxon-HE (saxonche)
   └─ XSLT 2.0 / 3.0 — uses the real source file from disk
   └─ On success → result passed to Groq for analysis

3. lxml
   └─ XSLT 1.0 fallback
   └─ On success → result passed to Groq for analysis

4. LLM simulation (dry-run)
   └─ Used only when no processor can execute the XSLT
   └─ Banner clearly labels this as "⚠️ Dry-run"
```

The response always includes a processor banner at the top:
- `Saxon-HE ✅` — real execution with source data
- `lxml ✅` — real execution (XSLT 1.0 only)
- `⚠️ Altova extensions detected` — proprietary functions, LLM fallback used
- `⚠️ Dry-run mode` — no source file provided, LLM simulation only

---

## Recent Fixes & Improvements

### 1. Saxon-HE Integration (`simulation_engine.py`)
**Problem:** The simulation engine always fell back to LLM dry-run simulation, never actually executing the XSLT.  
**Fix:** Rewrote the engine with a Saxon-HE → lxml → LLM cascade. Saxon-HE (`saxonche` package) now executes XSLT 2.0/3.0 files against the real source data, producing actual transformed output.

### 2. Source File Path Plumbing (`dispatcher.py`, `file_ingestion.py`)
**Problem:** The source file's disk path was never passed through to the simulation engine, so Saxon had no file to process.  
**Fix:**
- `file_ingestion.py` now stores `source_path` (original disk path) in the ingested metadata dict.
- `dispatcher.py` simulate block now auto-scans `session.ingested_files` for a non-XSLT file (D365_XML, X12_EDI, X12_XML, etc.) and passes its `source_path` to `simulate()` automatically — no manual wiring required from the UI.

### 3. Altova Extension Detection (`simulation_engine.py`)
**Problem:** Detection was too broad — XSLTs that merely *declare* the Altova namespace (`xmlns:altova="..."`) were flagged even when they never call any Altova functions. Saxon can execute these fine.  
**Fix:** Detection now uses a regex that matches actual function *call* patterns (`altova:funcName(` / `altovaext:funcName(`) instead of namespace declarations.

### 4. Out-of-Scope Guardrail (`dispatcher.py`)
**Problem:** The agent answered completely off-topic questions (e.g. "What is the capital of France?"), wasting LLM tokens.  
**Fix:** Added `_is_in_scope()` — a fast keyword check (no LLM calls) that runs before intent routing. Messages with no EDI/XSLT domain signal receive a friendly redirect. Uses substring matching for long keywords and whole-word `\b` regex for short EDI codes (ISA, GS, ST…) to avoid false positives.

### 5. `patched_xslt` Download Bug (`dispatcher.py`)
**Problem:** The modified XSLT returned by `modify()` was silently discarded (assigned to `_mod_agent`), so the Download button in the UI never received the patched file.  
**Fix:** Changed `response, _mod_agent = modify(...)` to `response, patched_xslt = modify(...)` so the patched content is correctly captured and returned to `app.py`.

### 6. TPM Rate Limit Fix (`file_agent.py`)
**Problem:** Uploading large XSLT files (e.g. 38KB Nordstrom 810) caused a `413 Request too large` error from Groq's API (12,000 token/minute limit on free tier).  
**Fix:** Added per-field cap of 6,000 characters and a 24,000 character total JSON cap on the serialized ingested file sent to the LLM.

### 7. Multi-Provider Compatibility (`file_agent.py`, `dispatcher.py`)
**Problem:** Collaborator branches added multi-provider LLM support (`provider=` parameter). This caused `__init__() got an unexpected keyword argument 'api_key'` and `dispatch() got an unexpected keyword argument 'provider'` errors on startup.  
**Fix:** Added `api_key` and `provider` as accepted backward-compatible parameters to `FileAgent.__init__()` and `dispatch()`.

---

## Running the Test Suite

```bash
python test_changes.py
```

Runs 26 checks covering:
- Saxon-HE XSLT execution
- Altova function call detection
- Out-of-scope guardrail (in-scope and out-of-scope messages)
- `patched_xslt` capture from `modify()`
- Source file auto-resolution from session

---

## Supported File Types

| File Type | Description |
|---|---|
| XSLT (`.xml`, `.xsl`) | MapForce-generated XSLT 1.0/2.0/3.0 mappings |
| X12 EDI (`.edi`, `.txt`) | Flat-file ISA/GS envelope EDI transactions |
| D365 XML (`.xml`, `.txt`) | Microsoft Dynamics 365 ERP output (saleCustInvoice) |
| X12 XML (`.xml`) | Altova MapForce XML representation of X12 EDI |
| XSD (`.xsd`) | XML Schema definitions |

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | Groq API key from console.groq.com |
| `GROQ_MODEL` | No | Model name (default: `llama-3.3-70b-versatile`) |
| `INTENT_ROUTER_THRESHOLD` | No | Confidence threshold 0.0–1.0 (default: `0.45`) |

---

## Contributing

1. Fork the repo and create a feature branch
2. Make your changes
3. Run `python test_changes.py` — all checks must pass
4. Open a pull request against `main`

---

## Team

Industry Practicum — PartnerLinQ Integration Project  
Built for EDI/XSLT mapping intelligence using Groq LLMs + Saxon-HE + Altova MapForce.
