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
├── approval_gate.py              # Approve / Reject / Rollback integration layer
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
│   ├── rules_store.py            # SQLite store — approved rule versions + audit log
│   └── session.py                # Session state: file history, conversation memory
├── scripts/
│   └── index_data.py             # CLI tool — indexes data/ folder into ChromaDB RAG index
├── requirements.txt
├── test_changes.py               # Verification test suite (31 checks)
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

> See [`BUGFIX.md`](BUGFIX.md) for full root-cause analysis and code diffs.

### B1 — `patched_xslt` Download Bug (`dispatcher.py`) — HIGH
The modified XSLT returned by `modify()` was silently discarded (`_mod_agent`), so the Download button never received the patched file. Fixed by unpacking into `patched_xslt`.

### B2/B12 — Dead variable in simulate branch (`dispatcher.py`) — MEDIUM/LOW
`_ctx_prefix` was computed inside the simulate branch but never forwarded (simulate takes no user-message parameter). Removed the dead assignment.

### B3 — `IF_NO_ELSE` false-positives on all XSLT files (`audit_engine.py`) — HIGH
Every valid stylesheet triggered a "no xsl:otherwise fallback" warning because the rule checked `xsl:if` instead of `xsl:choose`. Fixed to check `choose_count > 0 and otherwise_count == 0`.

### B4 — Groq API `content=None` crash (all engines) — HIGH
`AttributeError: 'NoneType' object has no attribute 'strip'` on any empty or tool-call response. Added `or ""` guard in all five engine files.

### B5 — lxml warnings discard real transform output (`simulation_engine.py`) — HIGH
Informational `error_log` entries caused the successful lxml result to be thrown away. Fixed to only discard on `ERROR` or `FATAL_ERROR` level.

### B6 — Stale `patched_xslt` survives session reset (`app.py`) — MEDIUM
After "New Session" or "Sign out", the previous session's Download button could reappear. Added `patched_xslt` and `patched_xslt_filename` to both reset paths.

### B7 — Audit form hides falsy current values (`app.py`) — MEDIUM
Current values of `0`, `0.0`, or `False` were hidden by a truthiness check. Fixed with `if cv is not None`.

### B8 — RAG file count metric wrong (`app.py`) — MEDIUM
Sidebar count used `len(glob("*")) - 1`, counting directories and assuming exactly one `.gitkeep`. Replaced with a recursive count filtered to supported extensions.

### B9 — Wrong file selected on keyword score tie (`session.py`) — MEDIUM
`if score > best_score` kept the oldest file on ties. Changed to `>=` so the most recently uploaded file wins.

### B10 — Demo password exposed in error message (`app.py`) — MEDIUM
Incorrect password showed "Use: partnerlinq2026", leaking the credential. Changed to a generic "Incorrect password." message.

### B13 — Error intent badge unstyled (`app.py`) — LOW
Exception responses showed an unstyled chip because `.badge-error` CSS was missing. Added the rule.

### B14 — File uploads overwrite on same filename (`app.py`) — LOW
Two uploads with identical names silently overwrote each other on disk. Prefixed saved filenames with the session ID (`{session_id}_{filename}`).

---

## Approval Workflow

The agent includes a lightweight approval/rollback layer for XSLT rule changes, backed by a local SQLite database (`rules_store.db`).

| Component | File | Purpose |
|---|---|---|
| `RulesStore` | `modules/rules_store.py` | SQLite store — versioned rule approvals + full audit event log |
| `approval_gate` | `approval_gate.py` | Thin API — `approve()`, `reject()`, `rollback()` |

### Usage

```python
from approval_gate import approve, reject, rollback

# Approve a mapping version
approve(rule_key="nordstrom_810", xslt=xslt_content, actor="alice", why="Verified output matches spec")

# Reject without storing a version (still logged for audit)
reject(rule_key="nordstrom_810", xslt=xslt_content, actor="bob", why="Missing DTM segment")

# Roll back to a prior approved version
rollback(rule_key="nordstrom_810", version=2, actor="alice", why="v3 caused mapping failures")
```

The `rules_store.db` file is created automatically at the project root on first use. Every approve/reject/rollback action is written to the `audit_events` table with actor, timestamp, duration, and reason.

---

## RAG Index

To pre-index your mapping files for cross-file RAG queries, place them in the `data/` folder and run:

```bash
python scripts/index_data.py          # incremental — skips already-indexed files
python scripts/index_data.py --force  # wipe and rebuild from scratch
```

Supported file types: `.xml`, `.xsl`, `.xslt`, `.xsd`, `.edi`, `.txt`

---

## Running the Test Suite

```bash
python test_changes.py
```

Runs 31 checks covering:
- Saxon-HE XSLT execution
- Altova function call detection
- Out-of-scope guardrail (13 in-scope and out-of-scope messages)
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
