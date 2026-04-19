# PartnerLinQ — Conversational Mapping Intelligence Agent

A Streamlit-based AI agent that lets EDI analysts interact with XSLT/EDI mapping files in plain English. Upload a mapping file, ask questions, simulate transformations, modify mappings, audit for issues, or generate new XSLTs — all through a conversational chat interface.

---

## Table of Contents

1. [Features](#features)
2. [Project Structure](#project-structure)
3. [Setup & Installation](#setup--installation)
4. [How to Run](#how-to-run)
5. [How the Code Works — Full Flow](#how-the-code-works--full-flow)
6. [How Tokens Are Counted](#how-tokens-are-counted)
7. [How Cost Is Calculated](#how-cost-is-calculated)
8. [Real Example — Token Breakdown](#real-example--token-breakdown)
9. [Model Pricing Reference](#model-pricing-reference)
10. [Simulation Engine](#simulation-engine)
11. [Approval Workflow](#approval-workflow)
12. [RAG Index](#rag-index)
13. [Running the Test Suite](#running-the-test-suite)
14. [Supported File Types](#supported-file-types)
15. [Environment Variables](#environment-variables)

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
│   ├── usage_tracker.py          # Token usage logger + cost calculator
│   ├── rules_store.py            # SQLite store — approved rule versions + audit log
│   └── session.py                # Session state: file history, conversation memory
├── scripts/
│   └── index_data.py             # CLI tool — indexes data/ folder into ChromaDB RAG index
├── requirements.txt
├── test_changes.py               # Verification test suite (31 checks)
└── .env                          # API keys (never commit this file)
```

---

## Setup & Installation

### 1. Clone the repo
```bash
git clone https://github.com/Preetham33/Industry-Practicum_PartnerLinQ.git
cd Industry-Practicum_PartnerLinQ
```

### 2. Install dependencies
```bash
pip3 install -r requirements.txt
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
python3 -m streamlit run app.py
```

---

## How to Run

```bash
# Standard run
python3 -m streamlit run app.py

# App opens at http://localhost:8501
```

**Using the app:**
1. Sign in using a preset user or enter your name/email with password `partnerlinq2026`
2. Upload a mapping file (`.xml` or `.xsl`) using the paperclip button
3. For simulation, also upload a source data file (D365 XML, X12 EDI, etc.)
4. Type your question in the chat box

---

## How the Code Works — Full Flow

Every message you send goes through a 7-step pipeline:

```
User Message
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  Step 1: File Ingestion (file_ingestion.py)          │
│  Reads and parses the uploaded XSLT/EDI file into   │
│  a structured dict with metadata and content        │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 2: Out-of-Scope Guardrail (dispatcher.py)      │
│  Keyword-checks the message for EDI/XSLT signals.   │
│  If none found → returns a redirect message.        │
│  Zero LLM tokens spent on off-topic questions.      │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 3: Intent Classification (intent_router.py)    │
│  Sends message to LLM → gets confidence scores      │
│  for all 5 intents (explain/modify/simulate/        │
│  generate/audit). Any score ≥ 0.45 is "active".    │
│  LLM CALL #1 — uses ~200–400 tokens                 │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 4: RAG Context Injection (rag_engine.py)       │
│  If .rag_index/ exists, retrieves top-3 relevant    │
│  snippets from indexed data/ files and prepends     │
│  them to the engine prompt.                         │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 5: Engine Dispatch (dispatcher.py)             │
│  Calls one engine per active intent:                │
│                                                     │
│  explain  → groq_agent.explain()   LLM CALL #2     │
│  simulate → simulation_engine.simulate()            │
│  modify   → modification_engine.modify() LLM CALL  │
│  generate → xslt_generator.generate()   LLM CALL   │
│  audit    → audit_engine.audit()        LLM CALL   │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 6: File Agent (file_agent.py)                  │
│  Manages conversational memory. For explain intent, │
│  the FileAgent wraps the LLM call and maintains    │
│  full conversation history across turns.            │
│  LLM CALL #3 (for explain/chat turns)              │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 7: Usage Logging (usage_tracker.py)            │
│  Every LLM call writes to logs/llm_usage.jsonl      │
│  and updates the in-memory session accumulator.     │
│  Token counts + cost shown live in the sidebar.    │
└─────────────────────────────────────────────────────┘
```

### Why multiple LLM calls per message?

A single user message triggers **2–3 LLM calls** internally:

| Call | Module | Purpose | Typical tokens |
|---|---|---|---|
| #1 | `intent_router.py` | Classify what the user wants | ~300–500 |
| #2 | `groq_agent.py` / engine | Generate the actual answer | ~5,000–15,000 |
| #3 | `file_agent.py` | Conversational memory management | ~2,000–5,000 |

This is why the **Session total** in the sidebar is always higher than a single call.

---

## How Tokens Are Counted

### What is a token?

A **token** is the smallest unit of text that a language model processes. Roughly:
- **1 token ≈ 4 characters** of English text
- **1 token ≈ ¾ of a word**
- 100 tokens ≈ 75 words

Examples:
```
"Hello world"        → 2 tokens
"XSLT stylesheet"    → 3 tokens
"<xsl:template>"     → 6 tokens (XML tags tokenize into more pieces)
```

### Where do the token counts come from?

The token counts shown in the UI are **not estimated by our code** — they come directly from the Groq API response:

```python
# In llm_client.py — after every API call:
response = client.chat.completions.create(...)

usage = response.usage          # Groq counts tokens server-side
log_usage(
    prompt_tokens     = usage.prompt_tokens,      # exact input count
    completion_tokens = usage.completion_tokens,  # exact output count
    total_tokens      = usage.total_tokens,
    ...
)
```

Groq uses its own tokenizer (same family as OpenAI's tiktoken for Llama models) to count tokens server-side. The counts are **exact**, not estimates.

### What counts as "Input tokens"?

Input tokens = everything sent **to** the LLM in a single API call:

```
Input = system_prompt + conversation_history + your_question + file_contents
```

For an "Explain" request on a large XSLT file:

| Component | Approximate tokens |
|---|---|
| System instructions | ~200 |
| Conversation history (prior turns) | ~500–2,000 |
| Your question ("Explain what this mapping does") | ~10 |
| Full XSLT file contents | ~5,000–12,000 |
| **Total input** | **~8,000–15,000** |

The XSLT file dominates because it gets embedded in full inside the prompt.

### What counts as "Output tokens"?

Output tokens = the text the LLM generates in its response. For a detailed explanation of an XSLT file, that's typically **500–1,500 tokens**.

### Where is this tracked in the code?

`modules/usage_tracker.py` maintains two records:

**1. Persistent log file** (`logs/llm_usage.jsonl`) — one JSON record per API call:
```json
{
  "timestamp":         "2026-04-19T10:23:00.123456+00:00",
  "provider":          "groq",
  "model":             "llama-3.3-70b-versatile",
  "caller":            "groq_agent",
  "prompt_tokens":     8054,
  "completion_tokens": 785,
  "total_tokens":      8839,
  "max_tokens_cap":    1000,
  "temperature":       0.1,
  "latency_ms":        24766.0,
  "cost_usd":          0.005372
}
```

**2. In-memory session accumulator** — updated live after every call:
```python
_session_stats = {
    "calls":              3,
    "prompt_tokens":      17025,
    "completion_tokens":  785,
    "total_tokens":       17810,
    "estimated_cost_usd": 0.0109,
    "last_call": { ... }   # details of most recent call
}
```

---

## How Cost Is Calculated

### The formula

```
cost ($) = (input_tokens × input_price_per_1M + output_tokens × output_price_per_1M)
           ─────────────────────────────────────────────────────────────────────────
                                      1,000,000
```

### Where it happens in the code

In `modules/usage_tracker.py`:

```python
def _compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p_in, p_out = PRICING.get(model, (0.0, 0.0))
    return (prompt_tokens * p_in + completion_tokens * p_out) / 1_000_000
```

This is called inside `log_usage()` on every API call, before writing to the log file and before updating the session accumulator.

### Pricing table (hardcoded in usage_tracker.py)

```python
PRICING = {
    "llama-3.3-70b-versatile":       (0.59,  0.79),   # Groq — current model
    "gpt-4o-mini":                   (0.15,  0.60),   # OpenAI
    "gpt-4o":                        (2.50, 10.00),   # OpenAI
    "claude-3-5-haiku-20241022":     (0.80,  4.00),   # Anthropic
    "claude-3-5-sonnet-20241022":    (3.00, 15.00),   # Anthropic
    "meta/llama-3.3-70b-instruct":   (0.77,  0.77),   # NVIDIA NIM (Meta)
    "qwen2.5-72b-instruct":          (0.90,  0.90),   # Qwen (Together AI)
}
# Format: model_id → (input_price_per_1M_tokens, output_price_per_1M_tokens)
```

> **Important:** Token counts are always exact (from the API). The cost is an estimate based on our hardcoded pricing table. If a provider changes their prices, update this table in `usage_tracker.py`.

---

## Real Example — Token Breakdown

**Action:** Upload `856GrayBarXSLT - FRM.xml` and ask *"Explain what this mapping does"*

**File:** 53,482 bytes, 872 lines of XSLT 2.0

### Call #1 — Intent Router
```
Input:  system_prompt (~400 tokens) + user_message (~10 tokens)  = ~410 tokens
Output: JSON scores for 5 intents                                  = ~100 tokens
Cost:   (410 × $0.59 + 100 × $0.79) / 1,000,000                  ≈ $0.000321
```

### Call #2 — Groq Agent (Explain)
```
Input:  system_prompt + full XSLT file contents + question        = ~8,054 tokens
Output: plain-English explanation                                  =   785 tokens
Cost:   (8,054 × $0.59 + 785 × $0.79) / 1,000,000

      = ($4,751.86 + $620.15) / 1,000,000
      = $5,372.01 / 1,000,000
      = $0.00537  ✅  (matches what the UI showed)
```

### Call #3 — File Agent (session memory)
```
Input:  conversation context + file summary                       = ~9,346 tokens
Output: session memory update                                      =   215 tokens
Cost:   ≈ $0.0057
```

### Session Total
```
Calls:  3
Tokens: 410 + 8,839 + 9,561 = 17,810 tokens
Cost:   $0.000321 + $0.00537 + $0.0057 ≈ $0.0109  ✅  (matches UI)
```

---

## Model Pricing Reference

| Model | Provider | Input / 1M tokens | Output / 1M tokens |
|---|---|---|---|
| `llama-3.3-70b-versatile` | **Groq (current)** | $0.59 | $0.79 |
| `gpt-4o-mini` | OpenAI | $0.15 | $0.60 |
| `claude-3-5-haiku` | Anthropic | $0.80 | $4.00 |
| `qwen2.5-72b-instruct` | Qwen (Together AI) | $0.90 | $0.90 |
| `llama-3.3-70b-instruct` | Meta (NVIDIA NIM) | $0.77 | $0.77 |

### Cost comparison for a typical session (17,810 tokens)

Assuming a 80/20 input-to-output split (14,248 input / 3,562 output):

| Model | Provider | Estimated cost |
|---|---|---|
| `gpt-4o-mini` | OpenAI | **$0.0049** (cheapest) |
| `llama-3.3-70b-instruct` | Meta (NIM) | $0.0137 |
| `llama-3.3-70b-versatile` | Groq (current) | $0.0112 |
| `qwen2.5-72b-instruct` | Qwen | $0.0160 |
| `claude-3-5-haiku` | Anthropic | $0.0256 (most expensive for high output) |

> Groq offers the best balance of speed, cost, and quality for this use case.

---

## Simulation Engine — Execution Hierarchy

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

The response always includes a processor banner:
- `Saxon-HE ✅` — real execution with source data
- `lxml ✅` — real execution (XSLT 1.0 only)
- `⚠️ Altova extensions detected` — proprietary functions, LLM fallback used
- `⚠️ Dry-run mode` — no source file provided, LLM simulation only

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

approve(rule_key="nordstrom_810", xslt=xslt_content, actor="alice", why="Verified output matches spec")
reject(rule_key="nordstrom_810", xslt=xslt_content, actor="bob", why="Missing DTM segment")
rollback(rule_key="nordstrom_810", version=2, actor="alice", why="v3 caused mapping failures")
```

---

## RAG Index

To pre-index your mapping files for cross-file queries, place them in `data/` and run:

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

## Team

Industry Practicum — PartnerLinQ Integration Project
Built for EDI/XSLT mapping intelligence using Groq LLMs + Saxon-HE + Altova MapForce.
