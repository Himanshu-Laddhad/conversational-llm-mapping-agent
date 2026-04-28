# PartnerLinQ — Design Decisions

This document explains the key architectural and product decisions made during development, along with the tradeoffs that led to each choice.

---

## 1. Dual-provider strategy — Groq for classification, OpenAI for execution

**Decision:** Use two LLM providers for different tasks. Groq (llama-3.1-8b-instant) handles intent classification only; OpenAI (gpt-4.1-mini / gpt-4.1) handles explain, modify, simulate, audit, and generate.

**Reasons:**
- Intent classification is a narrow JSON-output task. `llama-3.1-8b-instant` runs at ~877 tok/s on Groq with a cost of $0.05/M tokens — approximately 50× cheaper than running it on OpenAI `gpt-4.1`.
- `gpt-4.1` has a 1M-token context window, native function/tool calling, and significantly stronger code reasoning than any open-weight model at the time, making it the right choice for deep XSLT analysis.
- Keeping the classification path on Groq means the system can still route correctly even when the user has only a Groq key — it will fall back gracefully to OpenAI for execution only when both keys are present.
- Per-engine model overrides are configurable in `.env` (`EXPLAIN_MODEL`, `MODIFY_MODEL`, `AUDIT_MODEL`, etc.) so the balance can be tuned without code changes.

**Tradeoff:** Two API keys are required for full functionality. If only `OPENAI_API_KEY` is set, classification falls back to OpenAI which works correctly but costs more per turn.

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

**Tradeoff:** Doubles the API calls on modify/generate turns, increasing latency and token usage. Acceptable because modify and generate are the least frequent intents.

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

---

## 11. XSLT tool-calling index for explain and modify (instead of full-file context)

**Decision:** For XSLT files, build an in-memory index (`xslt_index.py`) at upload time and expose it to the LLM as OpenAI function-calling tools (`search_xslt`, `get_template`, `get_variable`, `get_segment_templates`, `get_call_chain`). The LLM fetches only the parts it needs rather than receiving the full XSLT in its context window.

**Reasons:**
- Production XSLT mapping files at PartnerLinQ are 400–1,200 lines. Sending them in full on every explain or modify turn is expensive (30–100K tokens per file) and unreliable — the model loses track of structure in very long context windows.
- A tool-calling approach allows the LLM to explore the file like a developer would: search for a keyword, open a specific template, trace a call chain. This produces more accurate answers and patches than reading everything at once.
- The index is built once at upload from `file_ingestion` output (which already contains the parsed template graph, variables, segments, and raw XML), so there is zero extra I/O cost at query time.
- Tool execution is pure Python dictionary lookup — no network call, no embedding, no latency beyond the LLM turns themselves.

**Tradeoff:** Requires OpenAI as the LLM provider (function calling). Non-OpenAI providers fall back gracefully to the legacy single-context path. Adds 1–3 extra tool-call round trips per explain/modify turn but produces substantially better results on large files.

---

## 12. Multi-patch tool-calling for modify with line-hint-aware replacement

**Decision:** `modify()` uses a `submit_patches` tool that accepts a structured list of `{description, before, after, line_hint}` dicts. Patches are applied bottom-to-top using `_replace_at_line_hint()` which selects the occurrence nearest to `line_hint` when `before` appears multiple times.

**Reasons:**
- The legacy single-patch approach used `str.replace(before, after, 1)` which always replaced the first occurrence. In a 400-line XSLT, many lines like `<xsl:sequence select="fn:string(.)" />` appear dozens of times — the first one is almost never the right one.
- Submitting all patches in a single tool call (rather than iteratively) means the LLM must explore and commit at once, reducing token usage and eliminating the risk of conflicting incremental patches.
- Bottom-to-top application (sorted by `line_hint` descending) means earlier patches do not shift line numbers for later ones, making the final character-offset arithmetic correct regardless of multi-line `after` blocks.
- An all-or-nothing pre-flight check (verify every `before` exists before touching anything) prevents partial application that could produce a broken XSLT with no recovery path.

**Tradeoff:** If the LLM provides a wrong `line_hint`, the nearest-occurrence selection may still pick the wrong line when two identical snippets are close together. Mitigated by instructing the LLM to use multi-line `raw_lines` from `search_xslt` as the `before` block instead of a single-line `match_line` when the single line is not unique.

---

## 13. API keys backend-only (no UI override)

**Decision:** API keys are read exclusively from `.env` environment variables. There is no text input in the sidebar to override them at runtime.

**Reasons:**
- Exposing a key input in the UI puts secrets in the browser DOM, where they can be captured by browser extensions, screenshots, or screen-share sessions.
- In a shared deployment (multiple analysts using the same app instance), a per-turn key override would allow one user to accidentally or intentionally substitute another user's key.
- The provider selector in the sidebar already shows only providers whose `<PROVIDER>_API_KEY` is present in the environment, making the set of available providers clear without exposing the keys themselves.

**Tradeoff:** Changing providers requires a server restart to reload `.env`. Acceptable for a practicum-scale deployment where the operator controls the environment.
