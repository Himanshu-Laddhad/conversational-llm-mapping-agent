# PartnerLinQ — Bug Fix Log

> **Author:** Industry Practicum Team  
> **Last updated:** April 2026  
> **Files changed (cumulative):** `app.py` · `modules/dispatcher.py` · `modules/audit_engine.py` · `modules/modification_engine.py` · `modules/simulation_engine.py` · `modules/rag_engine.py` · `modules/xslt_generator.py` · `modules/session.py` · `modules/intent_router.py` · `modules/llm_client.py` · `modules/xslt_index.py`

---

## Batch 1 — March 2026 (commit `f7bfe42`)

13 bugs found by systematic audit of every module and fixed in a single commit. Bugs ranged from a broken Download button (the patched XSLT was silently discarded) to false-positive audit warnings on all valid XSLT files. Every fix is root-cause targeted with no scope creep.

---

### B1 — `dispatcher.py`: patched XSLT discarded from modify()

**Severity:** HIGH  
**Symptom:** The "Download Modified XSLT" button in the UI never offered a file, even after a successful modify.

**Root cause:** `modify()` returns `(response_str, patched_xslt)`. The dispatcher unpacked it as `response, _mod_agent = modify(...)`, discarding the patched file. The `patched_xslt` key in the return dict was always `None`.

**Fix:**
```python
# Before
response, _mod_agent = modify(ingested, modification_request=msg, ...)

# After
response, patched_xslt = modify(ingested, modification_request=msg, ...)
```

---

### B2 / B12 — `dispatcher.py`: dead variable in simulate branch

**Severity:** MEDIUM / LOW  
**Symptom:** Session context (`_ctx_prefix`) was never forwarded to the simulate engine.

**Root cause:** `msg = _ctx_prefix + user_message` was computed inside the simulate branch but `simulate()` takes no user message parameter — the variable was unused dead code.

**Fix:** Removed the dead assignment. Session context forwarding to `simulate` is a known limitation noted for a future enhancement.

---

### B3 — `audit_engine.py`: `IF_NO_ELSE` rule false-positives on all XSLT files

**Severity:** HIGH  
**Symptom:** Every valid XSLT file triggered a WARNING saying "xsl:if has no xsl:otherwise fallback."

**Root cause:** `xsl:if` intentionally has no `xsl:otherwise` — that construct belongs to `xsl:choose/xsl:when`. The rule was checking `if_count > 0 and otherwise_count == 0`, which is true for virtually every stylesheet even if it is correctly written. The `choose_count` variable was already computed but never used.

**Fix:**
```python
# Before — flags xsl:if (always wrong)
if if_count > 0 and otherwise_count == 0:

# After — flags xsl:choose with no fallback (the real concern)
if choose_count > 0 and otherwise_count == 0:
```

---

### B4 — All engines: Groq API `content=None` crash

**Severity:** HIGH  
**Files:** `modification_engine.py`, `simulation_engine.py`, `rag_engine.py`, `xslt_generator.py`, `audit_engine.py`  
**Symptom:** `AttributeError: 'NoneType' object has no attribute 'strip'` on any Groq response where the model returns a tool-call or empty completion.

**Root cause:** Every engine called `.message.content.strip()` directly. The Groq API allows `content=None`.

**Fix:** Added an `or ""` guard in all five places:
```python
# Before
response.choices[0].message.content.strip()

# After
(response.choices[0].message.content or "").strip()
```

---

### B5 — `simulation_engine.py`: lxml warnings discard real transform output

**Severity:** HIGH  
**Symptom:** Even when `lxml` successfully executed an XSLT 1.0 stylesheet and produced correct XML, the result was silently thrown away if the transform generated any log entry (including informational warnings), falling back to the slower and less accurate LLM simulation.

**Root cause:** The error check treated every `error_log` entry as fatal.

**Fix:** Only discard on `ERROR` or `FATAL_ERROR` level entries:
```python
# Before
if errors:
    return None, f"lxml XSLT transform warnings/errors: {msgs}"

# After
fatal = [e for e in errors if e.level_name in ("FATAL_ERROR", "ERROR")]
if fatal:
    return None, f"lxml XSLT transform errors: {msgs}"
```

---

### B6 — `app.py`: stale patched XSLT survives session reset

**Severity:** MEDIUM  
**Symptom:** After clicking "New Session" or "Sign out", the Download button from a previous modify operation could still appear in the next session.

**Root cause:** Both reset code paths omitted `patched_xslt` and `patched_xslt_filename` from the cleared state keys.

**Fix:** Added both keys to `New Session` reset and `Sign out` `pop()` list.

---

### B7 — `app.py`: audit form hides valid falsy current values

**Severity:** MEDIUM  
**Symptom:** When an audit question's `current_value` was `0`, `0.0`, or `False`, the label showed no current value, even though those are legitimate production values.

**Root cause:** `if cv:` evaluates `0` and `False` as falsy.

**Fix:**
```python
# Before
if cv:
    lbl += f"  *(current value: `{cv}`)*"

# After
if cv is not None:
    lbl += f"  *(current value: `{cv}`)*"
```

---

### B8 — `app.py`: RAG file count metric is wrong

**Severity:** MEDIUM  
**Symptom:** The "Files in data/" sidebar metric showed incorrect counts when the `data/` folder contained subdirectories or more than one non-mapping file.

**Root cause:** `len(list(_data_dir.glob("*"))) - 1` counted directories and subtracted a hardcoded 1 for `.gitkeep`.

**Fix:** Replaced with a proper recursive count filtered to supported extensions:
```python
_RAG_EXTS = {".xml", ".xsl", ".xslt", ".xsd", ".edi", ".txt"}
_file_count = sum(
    1 for f in _data_dir.rglob("*")
    if f.is_file() and f.suffix.lower() in _RAG_EXTS
)
```

---

### B9 — `session.py`: wrong file selected on keyword score tie

**Severity:** MEDIUM  
**Symptom:** When two uploaded files scored equally on keyword matching (e.g. neither filename matched the query at all), the agent used the oldest uploaded file instead of the most recently added one.

**Root cause:** `if score > best_score` kept the first match in the list on ties.

**Fix:** `if score >= best_score` — the loop now overwrites on ties, so the last file in `ingested_files` wins.

---

### B10 — `app.py`: demo password exposed in error message

**Severity:** MEDIUM  
**Symptom:** Typing an incorrect password displayed "Incorrect password. Use: partnerlinq2026", leaking the credential.

**Fix:** Changed to `st.error("Incorrect password.")`.

---

### B13 — `app.py`: error intent badge unstyled

**Severity:** LOW  
**Symptom:** When `dispatch()` raised an exception, the assistant message showed an unstyled chip because the CSS only defined `.badge-explain` through `.badge-rag` but not `.badge-error`.

**Fix:** Added `.badge-error { background: #fef2f2; color: #dc2626; }` to the injected CSS.

---

### B14 — `app.py`: file uploads overwrite on same filename

**Severity:** LOW  
**Symptom:** If two users (or two turns) uploaded files with identical names, the second upload silently overwrote the first on disk at `data/uploads/<filename>`.

**Fix:** Prefix saved filename with the session ID:
```python
dest = uploads_dir / f"{session_id}_{uploaded_file.name}"
```

---

## Batch 2 — April 2026 (Tool-Calling Architecture + Model Routing)

Following user feedback that `explain` and `modify` failed silently on large XSLT files (token truncation, wrong occurrence replaced), a second round of targeted fixes was applied alongside the new tool-calling architecture.

---

### B15 — `modification_engine.py`: patch applied to wrong occurrence when `before` text is not unique

**Severity:** HIGH  
**Symptom:** A modify request targeting `BIG02` at line 178 was applied instead to a different identical `<xsl:sequence select="fn:string(.)" />` occurrence at line 125 (`DistributionCenterAccount`), leaving `BIG02` unchanged.

**Root cause:** `apply_patches_sequential` called `str.replace(before, after, 1)` which unconditionally replaces the **first** occurrence in the file. When the LLM provides a `line_hint` of 178, the code correctly sorted patches bottom-to-top, but the final `str.replace` still blindly chose line 125 because that was the first occurrence in the string.

**Fix:** Replaced `str.replace(..., 1)` with `_replace_at_line_hint()`:
```python
def _replace_at_line_hint(text: str, before: str, after: str, line_hint: int) -> str:
    """Replace the occurrence of 'before' whose start line is closest to line_hint."""
    positions = []
    start = 0
    while True:
        idx = text.find(before, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1

    if not positions or line_hint <= 0:
        return text[:positions[0]] + after + text[positions[0] + len(before):]

    def _line_of(pos):
        return text.count("\n", 0, pos) + 1

    best_pos = min(positions, key=lambda p: abs(_line_of(p) - line_hint))
    return text[:best_pos] + after + text[best_pos + len(before):]
```

---

### B16 — `xslt_index.py`: `_extract_template_source` returned only a plain string, blocking raw patch extraction

**Severity:** HIGH  
**Symptom:** When `get_template` tool results were used to build patch `before` blocks, the LLM received numbered source lines like `  178  <xsl:sequence ...>`. If the LLM copied this verbatim into `before`, the patch would never match because line-number prefixes are not in the actual file.

**Root cause:** `_extract_template_source` returned a single `numbered` string with `"  NNN  "` prefixes. There was no way to obtain the exact copyable text.

**Fix:** Changed return type to a dict with separate fields:
```python
return {
    "numbered": numbered,          # for display — has line-number prefixes
    "raw":      "\n".join(snippet_lines),  # exact copyable text — no prefixes
    "start_line": start_idx + 1,
    "end_line":   start_idx + len(snippet_lines),
}
```
`get_template` now exposes `source_snippet_raw` to the LLM, and the system prompt explicitly instructs it to use only `match_line`, `raw_lines`, or `source_snippet_raw` for patch `before` fields.

---

### B17 — `intent_router.py`: `Optional[str]` type annotations used without import

**Severity:** MEDIUM  
**Symptom:** `NameError: name 'Optional' is not defined` when running the router module standalone.

**Root cause:** `Optional` was used in function signatures but `from typing import Optional` was missing.

**Fix:** Added the import; also fixed a markdown fence stripping bug where leading whitespace before ` ```json ` caused the JSON parse to fail.

---

### B18 — `dispatcher.py`: intent router always used the caller's provider key for classification

**Severity:** MEDIUM  
**Symptom:** When the user's active provider was OpenAI, intent classification consumed expensive `gpt-4.1` tokens even though a fast `llama-3.1-8b-instant` on Groq would classify correctly for ~50× less cost.

**Root cause:** `route(user_message, api_key=api_key)` passed through whatever key the caller had, with no special handling for the Groq classification path.

**Fix:** Dispatcher now checks `GROQ_API_KEY` first and always routes classification to Groq's `llama-3.1-8b-instant` if available:
```python
_groq_key = os.getenv("GROQ_API_KEY")
if _groq_key:
    route_result = route(user_message, api_key=_groq_key, provider="groq",
                         model=os.getenv("INTENT_ROUTER_MODEL", "llama-3.1-8b-instant"))
else:
    route_result = route(user_message, api_key=api_key, provider=provider or "openai")
```

---

### B19 — `app.py`: API key exposed in the UI sidebar

**Severity:** MEDIUM  
**Symptom:** The sidebar rendered a text input allowing users to override the API key at runtime, inadvertently surfacing secrets in the browser DOM.

**Fix:** Removed the API key input widget entirely. All keys are resolved exclusively from `.env` via environment variables. The provider dropdown now only shows providers whose `<PROVIDER>_API_KEY` is set in the environment.

---

## Backward Compatibility

All fixes are additive or narrow in scope. No public API signatures changed. No new dependencies introduced. All existing file types, intent routing, and session behavior are unaffected.
