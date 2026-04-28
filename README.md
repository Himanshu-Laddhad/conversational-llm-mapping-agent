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

Every message you send goes through this pipeline:

```
User Message
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  Step 1: File Ingestion  (file_ingestion.py)         │
│  Parses uploaded XSLT/EDI/XML into a structured     │
│  dict. On first XSLT upload, builds an in-memory    │
│  XSLT index (xslt_index.py) stored in the session.  │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 2: Out-of-Scope Guardrail  (dispatcher.py)     │
│  Keyword-checks the message for EDI/XSLT signals.   │
│  Off-topic → returns redirect. Zero LLM tokens.     │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 3: Intent Classification  (intent_router.py)   │
│  Groq llama-3.1-8b-instant scores all 5 intents     │
│  independently. Any score ≥ 0.45 is "active".       │
│  LLM CALL #1 — ~200 tokens, ~50 ms on Groq          │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 4: RAG Context Injection  (rag_engine.py)      │
│  Top-3 relevant snippets from .rag_index/ prepended  │
│  to the engine prompt when the index exists.         │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 5: Engine Dispatch  (dispatcher.py)            │
│                                                     │
│  explain  → gpt-4.1-mini  tool-calling loop         │
│             LLM uses search/get_template/call_chain  │
│             to explore XSLT before answering         │
│                                                     │
│  modify   → gpt-4.1  tool-calling loop              │
│             LLM explores XSLT, calls submit_patches  │
│             with all changes; applied bottom-to-top  │
│             + verified + diff shown                  │
│                                                     │
│  simulate → gpt-4.1-mini  + Saxon-HE / lxml         │
│  generate → gpt-4.1  XSLT from scratch              │
│  audit    → gpt-4.1  structured quality review      │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Step 6: Usage Logging  (usage_tracker.py)           │
│  Every LLM call writes to logs/llm_usage.jsonl.     │
│  Token counts + cost shown live in the sidebar.     │
└─────────────────────────────────────────────────────┘
```

### LLM provider split

| Task | Provider | Model | Why |
|---|---|---|---|
| Intent routing | **Groq** | `llama-3.1-8b-instant` | 877 tok/s, $0.05/M — classification only |
| Explain (tool-calling) | **OpenAI** | `gpt-4.1-mini` | 1 M-token context, strong function calling |
| Modify (tool-calling) | **OpenAI** | `gpt-4.1` | Best for code diffs, SWE-bench 54.6% |
| Simulate | **OpenAI** | `gpt-4.1-mini` | Code reasoning, cost-efficient |
| Audit | **OpenAI** | `gpt-4.1` | Thorough structured analysis |
| Generate | **OpenAI** | `gpt-4.1` | Top code generation quality |

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

## Modify Pipeline — How Changes Are Applied

```
User: "Change all occurrences of sender ID 'ACME' to 'PARTNERLINQ'"
         │
         ▼  gpt-4.1 with XSLT tools
  search_xslt("ACME")          ← finds all 4 locations across templates
  get_call_chain("/")           ← checks what else is downstream
  get_template("build_isa")     ← fetches exact source lines
         │
         ▼  LLM calls submit_patches()
  patches = [
    { "before": "...", "after": "...", "line_hint": 47 },
    { "before": "...", "after": "...", "line_hint": 122 },
    ...
  ]
         │
         ▼  Python (no LLM)
  verify all BEFOREs exist → abort if any missing  (all-or-nothing)
  apply patches bottom-to-top (sorted by line_hint desc)
  verify_patches_applied() → confirm every AFTER is present
  validate_xslt_wellformed() → lxml XML parse check
         │
         ▼  UI response
  ## Changes Made — 4/4 patches applied
  1. ✓ Update sender ID in ISA template — line 47
  2. ✓ Update sender ID in GS header — line 122
  ...
  ```diff
  - <xsl:value-of select="'ACME'"/>
  + <xsl:value-of select="'PARTNERLINQ'"/>
  ```
  --- AUTO-AUDIT appended by dispatcher ---
  [Download Modified XSLT] button
```

**Key design decisions:**
- Patches use plain `str.replace` (not DOM) — untouched lines stay byte-for-byte identical
- Bottom-to-top application prevents line-number drift between patches
- All-or-nothing: if any BEFORE is not found, nothing is changed
- Fallback: if no XSLT index is built yet, the legacy single-patch path is used

---

## Model Pricing Reference

| Model | Provider | Input / 1M tokens | Output / 1M tokens |
|---|---|---|---|
| `gpt-4.1` | OpenAI | $2.00 | $8.00 |
| `gpt-4.1-mini` | OpenAI | $0.40 | $1.60 |
| `gpt-4.1-nano` | OpenAI | $0.10 | $0.40 |
| `llama-3.3-70b-versatile` | Groq | $0.59 | $0.79 |
| `llama-3.1-8b-instant` | Groq | $0.05 | $0.08 |
| `claude-3-5-haiku-20241022` | Anthropic | $0.80 | $4.00 |
| `llama-3.3-70b-instruct` | Meta (NVIDIA NIM) | $0.77 | $0.77 |

> **Intent routing** uses `llama-3.1-8b-instant` on Groq (fastest + cheapest for classification).  
> **All other tasks** use OpenAI GPT-4.1 family, selected per task for best quality/cost balance.

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

Copy `.env.example` to `.env` and fill in your keys. API keys are never shown in the UI.

### API Keys

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | **Yes** | OpenAI API key — powers explain, modify, simulate, audit, generate |
| `GROQ_API_KEY` | **Yes** | Groq API key — powers intent routing (llama-3.1-8b-instant) |
| `NVIDIA_API_KEY` | No | NVIDIA NIM key (optional fallback provider) |
| `ANTHROPIC_API_KEY` | No | Anthropic key (legacy path, no tool calling) |

### Provider-level model defaults

| Variable | Default | Description |
|---|---|---|
| `OPENAI_MODEL` | `gpt-4.1-mini` | Fallback when no per-engine override is set |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq fallback model |
| `NVIDIA_MODEL` | `meta/llama-3.3-70b-instruct` | NVIDIA NIM model |
| `ANTHROPIC_MODEL` | `claude-3-5-haiku-20241022` | Anthropic model |

### Per-engine model overrides (take priority over provider defaults)

| Variable | Default | Task |
|---|---|---|
| `EXPLAIN_MODEL` | `gpt-4.1-mini` | XSLT explain + tool-calling explore |
| `MODIFY_MODEL` | `gpt-4.1` | XSLT patch generation |
| `SIMULATE_MODEL` | `gpt-4.1-mini` | XSLT simulation analysis |
| `AUDIT_MODEL` | `gpt-4.1` | XSLT quality audit |
| `GENERATE_MODEL` | `gpt-4.1` | XSLT generation from scratch |
| `INTENT_ROUTER_MODEL` | `llama-3.1-8b-instant` | Intent classification (Groq) |
| `INTENT_ROUTER_THRESHOLD` | `0.45` | Min confidence score for active intent |

---

## Team

Industry Practicum — PartnerLinQ Integration Project
Built for EDI/XSLT mapping intelligence using Groq LLMs + Saxon-HE + Altova MapForce.
