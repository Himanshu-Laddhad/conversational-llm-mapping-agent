"""
app.py
──────
Streamlit frontend for the Conversational Mapping Intelligence Agent.

Run with:
    streamlit run app.py

Tabs:
  Tab 1 — Single-File Chat: upload a mapping file, ask anything, get audit forms
  Tab 2 — RAG Search: cross-file questions over everything in the data/ folder
"""

import os
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

# Load .env so GROQ_API_KEY is available
_env = Path(__file__).resolve().parent / ".env"
if _env.exists():
    load_dotenv(_env)

from modules.dispatcher import dispatch, dispatch_folder
from modules.session import Session
from modules.audit_engine import audit_followup

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PartnerLinQ — Mapping Intelligence",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* Tone down the default Streamlit header padding */
  .block-container { padding-top: 1.5rem; }

  /* Intent badge pill */
  .badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .badge-explain    { background: #d1fae5; color: #065f46; }
  .badge-simulate   { background: #dbeafe; color: #1e40af; }
  .badge-modify     { background: #fef3c7; color: #92400e; }
  .badge-generate   { background: #ede9fe; color: #5b21b6; }
  .badge-audit      { background: #fee2e2; color: #991b1b; }
  .badge-rag        { background: #e0f2fe; color: #0369a1; }

  /* Audit severity pills */
  .sev-fail    { color: #dc2626; font-weight: 700; }
  .sev-warning { color: #d97706; font-weight: 700; }
  .sev-info    { color: #2563eb; font-weight: 700; }
</style>
""", unsafe_allow_html=True)


# ── Session state init (runs once per browser session) ────────────────────────

def _init_state() -> None:
    if "session" not in st.session_state:
        st.session_state.session = Session()
    if "messages" not in st.session_state:
        st.session_state.messages = []          # [{role, content, intent?}]
    if "file_path" not in st.session_state:
        st.session_state.file_path = None       # path of the last uploaded file
    if "file_name" not in st.session_state:
        st.session_state.file_name = None
    if "audit_dict" not in st.session_state:
        st.session_state.audit_dict = None      # set when audit intent fires
    if "audit_ingested" not in st.session_state:
        st.session_state.audit_ingested = None  # ingested dict for followup
    if "last_route" not in st.session_state:
        st.session_state.last_route = None      # for debug expander


_init_state()

# ── Helper: upload → temp file ────────────────────────────────────────────────

def _save_upload(uploaded_file) -> str:
    """Save a Streamlit UploadedFile to a temp path and return the path."""
    uploads_dir = Path(__file__).resolve().parent / "data" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / uploaded_file.name
    dest.write_bytes(uploaded_file.getbuffer())
    return str(dest)


# ── Helper: intent badge HTML ──────────────────────────────────────────────────

def _badge(intent: str) -> str:
    cls = f"badge badge-{intent.lower()}"
    return f'<span class="{cls}">{intent}</span>'


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🔗 PartnerLinQ")
    st.caption("Conversational Mapping Intelligence Agent")
    st.divider()

    # File uploader
    st.subheader("📂 Upload Mapping File")
    uploaded = st.file_uploader(
        "Supported: .xml .xsl .xslt .xsd .edi .txt",
        type=["xml", "xsl", "xslt", "xsd", "edi", "txt"],
        label_visibility="collapsed",
    )
    if uploaded is not None:
        saved_path = _save_upload(uploaded)
        if saved_path != st.session_state.file_path:
            # New file — store path and clear stale audit state
            st.session_state.file_path = saved_path
            st.session_state.file_name = uploaded.name
            st.session_state.audit_dict = None
            st.session_state.audit_ingested = None
            st.success(f"Loaded: **{uploaded.name}**")

    if st.session_state.file_name:
        st.info(f"Active file: **{st.session_state.file_name}**")
    else:
        st.caption("No file loaded — ask a general question or use the RAG tab.")

    st.divider()

    # Session controls
    st.subheader("⚙️ Session")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔄 New Session", use_container_width=True):
            st.session_state.session.reset()
            st.session_state.messages = []
            st.session_state.file_path = None
            st.session_state.file_name = None
            st.session_state.audit_dict = None
            st.session_state.audit_ingested = None
            st.session_state.last_route = None
            st.rerun()
    with col2:
        turns = len(st.session_state.session.history)
        st.metric("Turns", turns)

    st.divider()

    # Debug expander
    with st.expander("🔬 Debug — last route", expanded=False):
        if st.session_state.last_route:
            r = st.session_state.last_route
            st.write(f"**Primary:** `{r.get('primary', '—')}`")
            st.write(f"**Multi-intent:** `{r.get('is_multi', False)}`")
            st.write(f"**Active:** `{r.get('active_intents', [])}`")
            scores = r.get("scores", {})
            if scores:
                st.write("**Scores:**")
                for k, v in scores.items():
                    st.progress(float(v), text=f"{k}: {v:.2f}")
        else:
            st.caption("No message sent yet.")

    st.divider()
    st.caption("PartnerLinQ × Industry Practicum · 2026")


# ── Main layout: two tabs ──────────────────────────────────────────────────────

tab1, tab2 = st.tabs(["💬 Chat (Single-File)", "🗂️ RAG Search (Multi-File)"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Single-File Chat
# ══════════════════════════════════════════════════════════════════════════════

with tab1:
    st.subheader("Single-File Mapping Chat")
    st.caption(
        "Ask anything about the uploaded mapping — explain, modify, generate, "
        "simulate, or audit. The agent remembers the whole conversation."
    )

    # ── Render conversation history ────────────────────────────────────────────
    for msg in st.session_state.messages:
        role = msg["role"]
        with st.chat_message(role):
            intent = msg.get("intent")
            if intent and role == "assistant":
                st.markdown(_badge(intent), unsafe_allow_html=True)
            st.markdown(msg["content"])

    # ── Inline audit form ──────────────────────────────────────────────────────
    if st.session_state.audit_dict is not None:
        audit_dict = st.session_state.audit_dict
        questions  = audit_dict.get("questions", [])
        summary    = audit_dict.get("summary", "")

        st.divider()
        st.markdown("### 📋 Audit Verification Form")
        if summary:
            # Colour-code the summary badge
            if "CRITICAL" in summary and not summary.startswith("0 CRITICAL"):
                st.error(f"**Audit Summary:** {summary}")
            elif "WARNING" in summary and not summary.startswith("0 CRITICAL, 0 WARNING"):
                st.warning(f"**Audit Summary:** {summary}")
            else:
                st.success(f"**Audit Summary:** {summary}")

        if questions:
            st.caption(
                "Please answer the questions below so the agent can verify "
                "your mapping is production-ready, then click **Submit Answers**."
            )

            SEV_ICON = {"FAIL": "🔴", "WARNING": "🟡", "INFO": "🔵"}

            with st.form("audit_followup_form"):
                answers = []
                for q in questions:
                    sev   = q.get("severity", "INFO")
                    cat   = q.get("category", "")
                    cv    = q.get("current_value")
                    icon  = SEV_ICON.get(sev, "⚪")
                    label = f"{icon} **[{sev}]** [{cat}] {q['question']}"
                    if cv:
                        label += f"  *(current value: `{cv}`)*"

                    st.markdown(label)
                    ans = st.text_input(
                        "Your answer",
                        key=f"aq_{q['id']}",
                        label_visibility="collapsed",
                        placeholder="Type your answer here…",
                    )
                    answers.append({
                        "id":       q["id"],
                        "question": q["question"],
                        "answer":   ans,
                    })
                    st.markdown("---")

                submitted = st.form_submit_button(
                    "✅ Submit Answers for Verification",
                    use_container_width=True,
                    type="primary",
                )

            if submitted:
                ingested_ref = st.session_state.audit_ingested
                if ingested_ref is None:
                    ingested_ref = st.session_state.session.ingested

                if ingested_ref is None:
                    st.error("No ingested file context available for followup.")
                else:
                    with st.spinner("Running second-pass verification…"):
                        try:
                            followup, _ = audit_followup(ingested_ref, answers)
                        except Exception as ex:
                            followup = f"[ERROR] {ex}"

                    # Determine verdict colour
                    if "DO NOT DEPLOY" in followup.upper():
                        st.error("### Verification Result")
                        st.error(followup)
                    elif "REVIEW REQUIRED" in followup.upper():
                        st.warning("### Verification Result")
                        st.warning(followup)
                    else:
                        st.success("### Verification Result")
                        st.success(followup)

                    # Add to chat history and clear the form
                    st.session_state.messages.append({
                        "role":    "assistant",
                        "content": f"**Audit Verification Result**\n\n{followup}",
                        "intent":  "audit",
                    })
                    st.session_state.audit_dict     = None
                    st.session_state.audit_ingested = None
                    st.rerun()
        else:
            st.info(
                "The audit found no specific questions to ask. "
                "Review the audit report above for any critical findings."
            )
            if st.button("Clear Audit Form"):
                st.session_state.audit_dict     = None
                st.session_state.audit_ingested = None
                st.rerun()

    # ── Chat input ─────────────────────────────────────────────────────────────
    user_input = st.chat_input("Ask anything about the mapping…")

    if user_input:
        # Append user message to history
        st.session_state.messages.append({"role": "user", "content": user_input})

        # Call dispatch
        with st.spinner("Thinking…"):
            try:
                result = dispatch(
                    user_message=user_input,
                    file_path=st.session_state.file_path,
                    session=st.session_state.session,
                )
            except Exception as ex:
                result = None
                err_msg = str(ex)

        if result is None:
            response_text = f"⚠️ Error: {err_msg}"
            intent = "error"
        else:
            response_text = result["primary_response"] or "_No response generated._"
            intent = result["route"].get("primary", "unknown")
            st.session_state.last_route = result["route"]

            # Store audit_dict if the audit intent ran
            if result.get("audit_dict") is not None:
                st.session_state.audit_dict     = result["audit_dict"]
                st.session_state.audit_ingested = result.get("ingested")

            # After first dispatch with file, no need to re-send file_path
            # (session.ingested will carry it forward)
            if result.get("ingested") is not None:
                st.session_state.file_path = None   # session remembers it now

        st.session_state.messages.append({
            "role":    "assistant",
            "content": response_text,
            "intent":  intent,
        })
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — RAG Multi-File Search
# ══════════════════════════════════════════════════════════════════════════════

with tab2:
    st.subheader("Multi-File RAG Search")
    st.caption(
        "Ask cross-file questions about all mappings indexed from the `data/` folder. "
        "Run `python scripts/index_data.py` after adding files to build the index."
    )

    data_dir = Path(__file__).resolve().parent / "data"
    index_dir = Path(__file__).resolve().parent / ".rag_index"

    # Quick stats
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        file_count = len(list(data_dir.glob("*"))) - 1  # subtract .gitkeep
        st.metric("Files in data/", max(file_count, 0))
    with col_b:
        indexed = index_dir.exists()
        st.metric("Index built", "Yes ✅" if indexed else "No ❌")
    with col_c:
        st.metric("Index path", ".rag_index/")

    if not indexed:
        st.info(
            "No RAG index found yet. Add mapping files to the `data/` folder, "
            "then run:\n```bash\npython scripts/index_data.py\n```"
        )

    st.divider()

    # Search form
    with st.form("rag_search_form"):
        rag_question = st.text_area(
            "Question about your mapping files",
            placeholder="e.g. Which mappings hardcode the ISA06 sender ID? "
                        "Which files map the unit price field?",
            height=100,
        )
        col_left, col_right = st.columns([3, 1])
        with col_left:
            top_k = st.slider("Chunks to retrieve (top-k)", 1, 10, 5)
        with col_right:
            force_reindex = st.checkbox("Force re-index", value=False)

        rag_submitted = st.form_submit_button(
            "🔍 Search", use_container_width=True, type="primary"
        )

    if rag_submitted:
        if not rag_question.strip():
            st.warning("Please enter a question before searching.")
        else:
            with st.spinner("Searching across all indexed mapping files…"):
                try:
                    rag_result = dispatch_folder(
                        user_message=rag_question,
                        folder_path=str(data_dir),
                        force_reindex=force_reindex,
                        top_k=top_k,
                    )
                    rag_error = None
                except Exception as ex:
                    rag_result = None
                    rag_error  = str(ex)

            if rag_error:
                st.error(f"Search failed: {rag_error}")
            else:
                st.markdown("### Answer")
                st.markdown(rag_result["primary_response"])

                # Index stats expander
                idx_stats = rag_result.get("index_result", {})
                if idx_stats:
                    with st.expander("📊 Index stats", expanded=False):
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Indexed",  idx_stats.get("indexed",  0))
                        c2.metric("Skipped",  idx_stats.get("skipped",  0))
                        c3.metric("Errors",   len(idx_stats.get("errors", [])))
                        if idx_stats.get("errors"):
                            st.write("**Errors:**")
                            for e in idx_stats["errors"]:
                                st.caption(f"- {e}")

    st.divider()

    # Instructions card
    with st.expander("ℹ️ How to add files to the RAG index", expanded=False):
        st.markdown("""
**Step 1 — Drop files into the `data/` folder:**
```
data/
  Graybar_850_XSLT.xml
  810_NordStrom_Xslt.xml
  ASN_856_Mapping.xslt
  ...
```

**Step 2 — Rebuild the index:**
```bash
python scripts/index_data.py
```
Use `--force` to re-index files that were already indexed:
```bash
python scripts/index_data.py --force
```

**Step 3 — Search here.**  
The index is stored in `.rag_index/` (git-ignored) and persists between runs.
""")
