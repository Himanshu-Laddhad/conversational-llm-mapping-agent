# PartnerLinQ — Design Decisions

This document explains the key architectural and product decisions made during development, along with the tradeoffs that led to each choice.

---

## 1. Groq + LLaMA 3.3 70B instead of OpenAI GPT-4

**Decision:** Use Groq's inference API with the `llama-3.3-70b-versatile` model as the LLM backbone.

**Reasons:**
- Groq's hardware accelerator delivers sub-second latency on 70B-parameter models, which is critical for a conversational UI that must feel responsive.
- The free tier provides sufficient tokens for a practicum project without a billing relationship.
- LLaMA 3.3 70B performs comparably to GPT-4-turbo on structured-output tasks (XSLT generation, JSON audit questions) while remaining fully open-weight, reducing vendor lock-in.

**Tradeoff:** Groq daily token quotas can be exhausted under heavy testing. The fix is to rotate API keys or introduce request queuing — not a code change.

---

## 2. Five discrete intents (explain / simulate / modify / generate / audit) rather than a single chat loop

**Decision:** Every user message is first classified into one of five intents before calling any engine.

**Reasons:**
- Each intent requires a fundamentally different system prompt and return shape. A single generic chat loop would produce unfocused responses for tasks like "simulate this XSLT on this XML" or "audit for hardcoded sender IDs."
- Intent classification gives the UI a strongly-typed result it can render differently (download button for modify/generate, checklist form for audit, code block for simulate).
- Adding a sixth intent in the future requires touching only `intent_router.py` and writing one new engine — no changes to any existing path.

**Tradeoff:** Misclassification by the LLM router causes the wrong engine to run. Mitigated with a detailed few-shot system prompt in `intent_router.py` and a fallback to `explain` on any ambiguous result.

---

## 3. Auto-audit after every modify and generate operation

**Decision:** After `modify()` or `generate()` returns a new XSLT string, `audit()` is called automatically before the dispatcher returns to the UI.

**Reasons:**
- In EDI integrations, a single misconfigured segment (wrong qualifier, transposed ID, missing null-check) can cause partner rejections or financial errors worth thousands of dollars.
- Users who generate XSLT and immediately download it without reading the audit report are the highest-risk scenario.
- Making the audit invisible and automatic means it runs even when the user does not think to ask for it.

**Tradeoff:** Doubles the Groq API calls on modify/generate turns, increasing latency and token usage. Acceptable because modify and generate are the least frequent intents.

---

## 4. ChromaDB + sentence-transformers for RAG instead of a managed vector service

**Decision:** Store the RAG vector index on disk as a local ChromaDB collection, embedded with `all-MiniLM-L6-v2`.

**Reasons:**
- The project runs in a local or single-server environment with no persistent cloud infrastructure.
- ChromaDB persists to a directory (`.rag_index/`) and requires zero configuration or network access at query time.
- `all-MiniLM-L6-v2` is a 22 MB model that runs on CPU in under 100 ms per query, which is fast enough for interactive use.
- A user who clones the repo can rebuild the index with one command: `python scripts/index_data.py`.

**Tradeoff:** The index is not shareable across machines by default. Mitigated by keeping the `data/` folder under `.gitignore` and documenting the rebuild workflow in `README.md`.

---

## 5. `data/` folder for RAG source files (git-ignored) rather than indexing `MappingData/`

**Decision:** A dedicated `data/` directory is the only source for RAG indexing. It is git-ignored. Real mapping files are placed there by the user after cloning.

**Reasons:**
- Real PartnerLinQ mapping files contain trading partner EDI IDs, customer names, and contract terms. Committing them to a public GitHub repository would be a data security incident.
- The `data/.gitkeep` sentinel ensures the folder exists in the cloned repo so the UI's file-count metric always renders correctly.
- Separating "code" from "data" is standard ML practice — it makes the repo portable and the data lifecycle independent.

**Tradeoff:** A freshly cloned repo has an empty index and RAG queries return nothing until `index_data.py` is run. The README documents this clearly and the UI shows a live file count to make the empty state visible.

---

## 6. Session object with scored file selection, not per-turn file argument

**Decision:** All ingested files are stored in a `Session.ingested_files` list. The dispatcher calls `session.get_primary_ingested(user_message)` to score and pick the most relevant file per turn.

**Reasons:**
- Users frequently upload an XSLT and its source/target XML in the same conversation. Subsequent questions like "why does the ISA segment have the wrong sender ID?" should automatically match the XSLT, not the XML.
- Requiring the user to re-specify which file they mean on every message breaks conversational flow.
- The scoring function is simple (keyword overlap between filename + file-type and the message) but works well in practice because file types and segment names appear naturally in queries.

**Tradeoff:** Score ties used to fall back to the oldest file. Fixed in B9 by changing `>` to `>=` so the most recently added file wins ties — matching the natural user expectation that "the last file I uploaded is the active one."

---

## 7. Two-layer audit (hardcoded rules + LLM) rather than LLM-only

**Decision:** The audit engine first runs a deterministic rule pass (null-check gaps, hardcoded IDs, IF_NO_ELSE, namespace issues, etc.) before calling the LLM for open-ended analysis.

**Reasons:**
- Hardcoded rules are 100% reproducible and testable. An audit of the same XSLT always flags the same issues regardless of LLM sampling temperature or API availability.
- The rules catch the most common and highest-stakes mistakes (missing null checks, hardcoded sender IDs) with zero token cost.
- The LLM layer adds value for things rules cannot express: "this conditional logic looks inverted given the business context" or "this variable shadows a global with the same name."
- The structured `questions_json` return from the LLM is stable enough to drive a Streamlit form because the rule-layer output constrains the range of issues and reduces hallucination.

**Tradeoff:** Maintaining the rule list requires EDI domain knowledge. Rules can lag behind new standards. The LLM fallback partially covers this gap.

---

## 8. Streamlit frontend over React

**Decision:** The UI is implemented as a single `app.py` Streamlit file.

**Reasons:**
- The primary users are integration engineers and business analysts, not end consumers. Fast iteration and correct behaviour matter more than pixel-perfect design.
- Streamlit's `st.chat_message`, `st.file_uploader`, and `st.form` components map directly onto the required UI primitives without writing JavaScript.
- A single-file Python frontend shares the same process and import path as the backend modules, eliminating the need for an API layer, serialisation contracts, and CORS configuration during development.

**Tradeoff:** Streamlit's execution model reruns the entire script on every interaction, which can cause performance issues at scale. The current state-management approach (`st.session_state`) handles the practicum-scale user load without issues.

---

## 9. lxml for XSLT 1.0 simulation with LLM fallback for XSLT 2.0

**Decision:** `simulate()` attempts an actual `lxml` XSLT 1.0 transformation first. If `lxml` raises a fatal error (not just a warning), or if the stylesheet uses XSLT 2.0 syntax, the engine falls back to asking the LLM to predict the output.

**Reasons:**
- Real output from `lxml` is always more accurate than LLM prediction. When it works, it should be used.
- XSLT 2.0 is common in PartnerLinQ mappings but not supported by `lxml`. The LLM fallback covers this without failing.
- Treating all `error_log` entries as fatal (the original bug, B5) was discarding valid output for stylesheets that use `xsl:message` or generate non-fatal warnings. The fix restricts fallback to genuine `ERROR`/`FATAL_ERROR` levels only.

**Tradeoff:** LLM-predicted transformation output is approximate. The UI should (and does) label it as "predicted" when the LLM path is taken.

---

## 10. Uploaded files prefixed with session ID on disk

**Decision:** Files saved to `data/uploads/` are named `{session_id}_{original_filename}` rather than `{original_filename}`.

**Reasons:**
- If two users (or the same user in two browser tabs) upload files with identical names, a flat name would cause a silent overwrite on disk. The second upload would replace the first, and the first session's queries would silently return results from the wrong file.
- The session ID is a UUID generated at login, making collisions practically impossible.

**Tradeoff:** The `data/uploads/` directory fills up with prefixed files over time. A cleanup sweep keyed on session ID age is a natural future improvement.
