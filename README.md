# Conversational Mapping Intelligence Agent

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![Streamlit](https://img.shields.io/badge/Streamlit-UI-red?logo=streamlit)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4.1-green?logo=openai)
![Groq](https://img.shields.io/badge/Groq-LLaMA--3-orange)
![LangChain](https://img.shields.io/badge/Agentic-Tool--Calling-yellow)
![ChromaDB](https://img.shields.io/badge/RAG-ChromaDB-purple)

> **Industry project** — A production-grade
> conversational AI agent that reduces EDI/XSLT mapping turnaround from days to
> minutes. Built with GPT-4.1 tool-calling, RAG retrieval, and real XSLT execution.

---

## Business Impact

| Metric | Before | After |
|---|---|---|
| Mapping turnaround | 2–3 days (specialist queue) | Minutes (self-serve) |
| Pre-production misconfiguration detection | Manual review | Automated audit on every change |
| Change traceability | None | Full versioned rollback trail |

This directly addresses a bottleneck that costs retail and supply chain organizations
real money: every stalled shipment notice (856), failed payment reconciliation (820),
or misconfigured PO (850) traces back to an XSLT mapping that only one person on the
team can touch.

---

## What It Does

Any analyst — not just an EDI specialist — can interact with XSLT/EDI mapping files
in plain English through a chat interface:

| Intent | Example prompt | What happens |
|---|---|---|
| **Explain** | "What does this mapping do?" | Plain-English breakdown of the XSLT logic |
| **Simulate** | "Run this against my D365 XML" | Real Saxon-HE / lxml execution, not a guess |
| **Modify** | "Change sender ID ACME to NEWCORP everywhere" | Verified patches, diff shown, downloadable |
| **Audit** | "Check this for production issues" | 31-point structured quality review |
| **Generate** | "Create a new 810 mapping for this schema" | XSLT from scratch via GPT-4.1 |

---

## Skills Demonstrated

Relevant for **Data Analyst**, **Data Scientist**, and **AI/ML Engineer** roles in
tech, retail, and supply chain:

| Area | What's built |
|---|---|
| **Agentic AI / LLM Systems** | Multi-provider orchestration (GPT-4.1 + Groq LLaMA-3), tool-calling loop, intent classification with confidence scoring |
| **RAG & Vector Search** | ChromaDB index over domain documents, top-k retrieval injected into engine prompts |
| **ML System Design** | Cost-aware model routing (latency vs. quality), fallback cascade (Saxon-HE → lxml → LLM), all-or-nothing patch verification |
| **Data Engineering** | Multi-format ingestion (XSLT, X12 EDI, D365 XML, XSD), token + cost telemetry pipeline, JSONL usage logs |
| **Analytics & Observability** | Per-call token accounting, cost attribution by model and task, live sidebar metrics |
| **Production Practices** | SQLite-backed approval/rollback store, 31-check automated test suite, guardrail layer (zero-token out-of-scope rejection) |

---

## Architecture

```
User Message
    │
    ├─ Out-of-scope guardrail (zero LLM tokens for off-topic queries)
    │
    ├─ Intent classification  ── Groq llama-3.1-8b-instant  (~50ms, $0.05/M)
    │
    ├─ RAG context injection  ── ChromaDB top-3 retrieval
    │
    └─ Engine dispatch ─────────┬─ explain   → GPT-4.1-mini  (tool-calling)
                                ├─ modify    → GPT-4.1       (code diffs)
                                ├─ simulate  → Saxon-HE → lxml → LLM
                                ├─ audit     → GPT-4.1       (structured review)
                                └─ generate  → GPT-4.1       (XSLT from scratch)
```

**Model selection rationale:**

| Task | Model | Why |
|---|---|---|
| Intent routing | Groq `llama-3.1-8b-instant` | 877 tok/s, cheapest for classification |
| Explain, Simulate | OpenAI `gpt-4.1-mini` | 1M context, strong function calling, cost-efficient |
| Modify, Audit, Generate | OpenAI `gpt-4.1` | Best code generation quality (SWE-bench 54.6%) |

---

## Retail & Supply Chain Relevance

| Document type | Where this matters |
|---|---|
| 850 Purchase Order | Retailer → supplier onboarding mapping errors cause delayed fulfillment |
| 856 Advance Ship Notice | Misconfigured ASN mappings stall warehouse receiving |
| 810 Invoice / 820 Remittance | Payment reconciliation failures in financial EDI rails |
| 835 ERA | Healthcare payer remittance — audit trail maps to compliance requirements |

---

## Tech Stack

- **LLM Orchestration:** OpenAI GPT-4.1 / GPT-4.1-mini, Groq LLaMA-3
- **Agent Framework:** Custom tool-calling loop (search_xslt, get_template, get_call_chain, submit_patches)
- **Vector DB:** ChromaDB
- **XSLT Execution:** Saxon-HE 12.x (XSLT 2.0/3.0), lxml (XSLT 1.0)
- **UI:** Streamlit
- **Storage:** SQLite (approval/rollback store), JSONL (usage telemetry)
- **Testing:** 31-check automated test suite

---

## Setup

```bash
git clone https://github.com/Himanshu-Laddhad/conversational-llm-mapping-agent.git
cd conversational-llm-mapping-agent
pip install -r requirements.txt
```

Create `.env`:
```
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.3-70b-versatile
INTENT_ROUTER_THRESHOLD=0.45
```

```bash
python -m streamlit run app.py
# Opens at http://localhost:8501
```

---

## How to Use

1. Sign in with your name/email
2. Upload a mapping file (`.xml` or `.xsl`) via the paperclip button
3. For simulation, also upload a source data file (D365 XML, X12 EDI, etc.)
4. Ask anything in plain English

**Supported file types:** `.xml`, `.xsl`, `.xslt`, `.xsd`, `.edi`, `.txt`

---

## Approval & Rollback Workflow

Every XSLT modification goes through a versioned approval layer backed by SQLite:

```python
approve(rule_key="nordstrom_810", xslt=content, actor="alice", why="Matches spec")
rollback(rule_key="nordstrom_810", version=2,   actor="alice", why="v3 caused failures")
```

All changes are actor-stamped, timestamped, and reversible — maps to SOX/change
management requirements in financial services and retail.

---

## Modify Pipeline (How Patches Work)

- LLM uses tools to explore the XSLT before generating any changes
- Patches are applied **bottom-to-top** (prevents line-number drift)
- **All-or-nothing:** if any expected string isn't found, nothing is changed
- Every patch is verified present post-apply + XML well-formedness checked
- Auto-audit runs on every successful modify

---

## Test Suite

```bash
python test_changes.py
```

31 checks covering Saxon-HE execution, Altova function detection, out-of-scope
guardrail (13 messages), patch capture, and source file resolution.

---

## Built During

Industry practicum — [PartnerLinQ](https://www.partnerlinq.com/) (B2B Supply Chain
Integration SaaS) · Purdue University MS Business Analytics · Spring 2026