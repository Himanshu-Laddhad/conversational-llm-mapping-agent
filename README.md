# Industry-Practicum_PartnerLinQ

Conversational Mapping Intelligence Agent for PartnerLinQ EDI/XML integration projects. Upload any mapping file, ask questions in plain English, and get explanations, simulations, modifications, generated stubs, and pre-production audits — all in a single unified chat interface.

---

## Repository Documentation

| File | Contents |
|------|----------|
| `DESIGN_DECISIONS.md` | 10 key architectural and product decisions with tradeoffs |
| `BUGFIX.md` | Root-cause analysis and fixes for all 13 bugs found in the full project audit |
| `CHANGES.md` | Detailed changelog for the file-type detection and XSLT parsing branch |
| `agent_logic.mmd` | Mermaid flowchart of the full agent pipeline (open in any Mermaid viewer) |
| `architecture.mmd` | High-level system architecture and workflow diagram |
| `scripts/index_data.py` | One-command RAG index builder |

---

## Git Workflow — Branch Rules

**Never push directly to `main`.** Every contribution must go through a feature branch and pull request.

### First-time setup

```bash
git clone https://github.com/Preetham33/Industry-Practicum_PartnerLinQ.git
cd Industry-Practicum_PartnerLinQ
```

### Daily workflow

```bash
# 1. Sync with main
git checkout main
git pull origin main

# 2. Create your branch
git checkout -b yourname/task-description

# 3. Make changes, then commit
git add .
git commit -m "Brief description of what you changed"

# 4. Push to GitHub
git push origin yourname/task-description
```

### Opening a pull request

Go to the repository on GitHub. Click **Compare & pull request** next to your branch, add a description, request a review from one teammate, and merge once approved. Delete the branch after merging and run `git pull origin main` locally to sync.

---

## Agent Setup

### Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Configure your API key

```bash
cp .env.example .env
```

Edit `.env` and set:

```
GROQ_API_KEY=your_groq_api_key_here
```

Get a free key at https://console.groq.com

### Step 3 — Add your mapping files

Copy XSLT / XML / XSD / EDI mapping files into `data/` (any subfolder depth is fine):

```
data/
├── 810_Invoice/
│   └── 810_NordStrom_Xslt.xml
├── 856_ShipNotice/
│   └── Costco_856_XSLT.xml
└── your_mapping.xslt
```

The `data/` folder is git-ignored so trading partner files are never committed to the repository.

### Step 4 — Build the RAG index

Run once after adding files. Re-run whenever the `data/` folder changes.

```bash
python scripts/index_data.py
```

Force a full rebuild from scratch:

```bash
python scripts/index_data.py --force
```

### Step 5 — Launch the web UI

```bash
streamlit run app.py
```

Open **http://localhost:8501** in your browser. Log in with the credentials in `.env`.

---

## Web UI Overview

The UI is a single unified chat interface. All five agent intents work from the same chat box.

| Area | What it does |
|------|-------------|
| **Sidebar — Upload Files** | Upload one or more mapping files at any point during the conversation. All uploaded files stay active for the session; remove individual files with the X button. Supported formats: `.xml .xsl .xslt .xsd .edi .txt` |
| **Sidebar — RAG Index** | Shows how many files are indexed in `data/` and lets you trigger a re-index without leaving the app. |
| **Chat** | Ask anything in plain English. The agent classifies each message into one of five intents (explain / simulate / modify / generate / audit), picks the most relevant uploaded file, and auto-injects context from the RAG index. |
| **Audit Form** | Appears automatically after an `audit` intent. Fill in the verification checklist and submit for a second-pass SAFE / REVIEW / DO NOT DEPLOY verdict. |
| **Download Button** | Appears after `modify` or `generate` intents. Downloads the patched or newly generated XSLT file. |
| **New Session** | Clears conversation history, all uploaded files, the audit form, and the download state. |

---

## Agent Intents

| Intent | Triggered by | What the agent does |
|--------|-------------|---------------------|
| `explain` | "What does this do?", "Explain the ISA segment" | Multi-turn explanation using file structure context |
| `simulate` | "Run this XSLT on this XML", "What output does this produce?" | Executes XSLT 1.0 via `lxml`; falls back to LLM prediction for XSLT 2.0 |
| `modify` | "Change the sender ID to PROD001", "Add a null check" | Proposes and applies a targeted patch; auto-audits the result |
| `generate` | "Generate an 810 invoice mapping skeleton" | Produces a new XSLT 2.0 stub from a natural language spec; auto-audits the result |
| `audit` | "Audit this before go-live", "Check for hardcoded values" | Runs hardcoded rule checks + LLM dynamic analysis; returns a structured question form for verification |

---

## Supported File Types

| Format | Detected as | Parser |
|--------|-------------|--------|
| Altova MapForce XSLT (`.xsl`, `.xslt`, `.xml` with `xsl:stylesheet`) | `XSLT` | `parse_xslt()` — extracts call graph, entry points, field mappings, conditionals |
| XML Schema (`.xsd`) | `XSD` | `parse_xsd()` |
| Microsoft Dynamics 365 invoice XML | `D365_XML` | `parse_d365_xml()` — extracts invoice header, line items, addresses, D365-to-EDI field map |
| MapForce X12 XML (root element starts with `X12_`) | `X12_XML` | `parse_x12_xml()` — extracts ISA/GS envelope, HL loops, segment data, SSCC labels |
| X12 EDI flat file (starts with `ISA`) | `X12_EDI` | `parse_x12_edi()` |
| EDIFACT (starts with `UNA`/`UNB`) | `EDIFACT` | `parse_edifact()` |
| Generic XML | `XML` | `parse_xml()` |

---

## Using the Agent Programmatically

**Single-file question:**

```python
from modules.dispatcher import dispatch

result = dispatch(
    user_message="Explain what this XSLT does.",
    file_path="data/810_Invoice/810_NordStrom_Xslt.xml",
)
print(result["primary_response"])
```

**Audit a mapping:**

```python
result = dispatch(
    user_message="Audit this mapping and flag any issues before go-live.",
    file_path="data/810_Invoice/810_NordStrom_Xslt.xml",
)
print(result["primary_response"])
questions = result["audit_dict"]["questions"]   # list of structured form questions
```

**Multi-turn session (all intents share memory and file context):**

```python
from modules.session import Session
from modules.dispatcher import dispatch

session = Session()

r1 = dispatch("Explain this mapping.", file_path="data/810_Invoice/810_NordStrom_Xslt.xml", session=session)
r2 = dispatch("Now audit it.", session=session)              # file context carried from r1
r3 = dispatch("Modify the sender ID to PROD001.", session=session)
# r3["patched_xslt"] contains the modified XSLT string ready for download
```

---

## Project Structure

```
.
├── app.py                        # Streamlit web UI
├── modules/
│   ├── dispatcher.py             # Central intent routing and orchestration
│   ├── intent_router.py          # LLM-based intent classifier
│   ├── file_ingestion.py         # File type detection and parsing
│   ├── session.py                # Multi-turn session and file context
│   ├── groq_agent.py             # Explain engine
│   ├── simulation_engine.py      # Simulate engine (lxml + LLM fallback)
│   ├── modification_engine.py    # Modify engine
│   ├── xslt_generator.py         # Generate engine
│   ├── audit_engine.py           # Audit engine (rules + LLM)
│   └── rag_engine.py             # RAG query engine (ChromaDB)
├── scripts/
│   └── index_data.py             # Offline RAG index builder
├── data/                         # Your mapping files go here (git-ignored)
│   └── .gitkeep
├── .rag_index/                   # ChromaDB vector index (git-ignored, auto-created)
├── BUGFIX.md                     # Bug fix log with root-cause analysis
├── DESIGN_DECISIONS.md           # Architecture and product decision rationale
├── CHANGES.md                    # File-type detection changelog
├── agent_logic.mmd               # Agent pipeline Mermaid diagram
├── architecture.mmd              # System architecture Mermaid diagram
├── requirements.txt
└── .env.example
```
