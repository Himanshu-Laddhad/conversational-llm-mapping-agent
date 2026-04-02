"""
app.py
──────
Streamlit frontend for the Conversational Mapping Intelligence Agent.

Unified multi-file chat: upload any number of mapping files at any point in
the conversation. The agent maintains session memory, auto-injects RAG context
from the indexed data/ folder, and selects the most relevant file per turn.

Run with:
    streamlit run app.py
"""

from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

# Load .env so GROQ_API_KEY is available
_env = Path(__file__).resolve().parent / ".env"
if _env.exists():
    load_dotenv(_env)

from modules.dispatcher import dispatch
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
  .block-container { padding-top: 1.5rem; }

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

  .file-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 0.78rem;
    margin: 2px 0;
  }
</style>
""", unsafe_allow_html=True)


# ── Session state init (runs once per browser session) ────────────────────────

def _init_state() -> None:
    if "session" not in st.session_state:
        st.session_state.session = Session()
    if "messages" not in st.session_state:
        st.session_state.messages = []          # [{role, content, intent?, file_used?}]
    if "active_files" not in st.session_state:
        st.session_state.active_files = []      # [{"name": str, "path": str}]
    if "pending_paths" not in st.session_state:
        st.session_state.pending_paths = []     # new file paths to send on next dispatch
    if "audit_dict" not in st.session_state:
        st.session_state.audit_dict = None
    if "audit_ingested" not in st.session_state:
        st.session_state.audit_ingested = None
    if "last_route" not in st.session_state:
        st.session_state.last_route = None
    if "patched_xslt" not in st.session_state:
        st.session_state.patched_xslt = None
    if "patched_xslt_filename" not in st.session_state:
        st.session_state.patched_xslt_filename = "modified.xml"


_init_state()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_upload(uploaded_file) -> str:
    """Persist an UploadedFile to data/uploads/ and return its path."""
    uploads_dir = Path(__file__).resolve().parent / "data" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / uploaded_file.name
    dest.write_bytes(uploaded_file.getbuffer())
    return str(dest)


def _badge(intent: str) -> str:
    cls = f"badge badge-{intent.lower()}"
    return f'<span class="{cls}">{intent}</span>'


def _active_file_names() -> set:
    return {f["name"] for f in st.session_state.active_files}


# ── Sidebar ────────────────────────────────────────────────────────────────────

_data_dir  = Path(__file__).resolve().parent / "data"
_index_dir = Path(__file__).resolve().parent / ".rag_index"

with st.sidebar:
    st.title("🔗 PartnerLinQ")
    st.caption("Conversational Mapping Intelligence Agent")
    st.divider()

    # ── Multi-file uploader ────────────────────────────────────────────────────
    st.subheader("📂 Upload Mapping Files")
    uploaded_files = st.file_uploader(
        "Supported: .xml .xsl .xslt .xsd .edi .txt",
        type=["xml", "xsl", "xslt", "xsd", "edi", "txt"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key="file_uploader_widget",
    )

    # Detect newly added files (not already in active_files)
    known_names = _active_file_names()
    new_uploads = [f for f in (uploaded_files or []) if f.name not in known_names]
    if new_uploads:
        for uf in new_uploads:
            saved = _save_upload(uf)
            st.session_state.active_files.append({"name": uf.name, "path": saved})
            st.session_state.pending_paths.append(saved)
        st.rerun()

    # ── Active file list ───────────────────────────────────────────────────────
    if st.session_state.active_files:
        st.caption(f"**{len(st.session_state.active_files)} file(s) in session:**")
        to_remove = None
        for i, af in enumerate(st.session_state.active_files):
            col_name, col_btn = st.columns([5, 1])
            with col_name:
                st.markdown(
                    f'<div class="file-chip">📄 {af["name"]}</div>',
                    unsafe_allow_html=True,
                )
            with col_btn:
                if st.button("✕", key=f"rm_{i}", help=f"Remove {af['name']}"):
                    to_remove = i
        if to_remove is not None:
            removed = st.session_state.active_files.pop(to_remove)
            # Also remove from session's ingested_files (match by filename)
            session = st.session_state.session
            session.ingested_files = [
                f for f in session.ingested_files
                if f.get("metadata", {}).get("filename", "") != removed["name"]
            ]
            # Update primary alias
            session.ingested = session.ingested_files[-1] if session.ingested_files else None
            session.agent    = None   # reset agent — primary file may have changed
            st.rerun()
    else:
        st.caption("No files loaded — upload files or ask a general question.")

    st.divider()

    # ── RAG index widget ───────────────────────────────────────────────────────
    st.subheader("🗂 RAG Index")
    _file_count = max(len(list(_data_dir.glob("*"))) - 1, 0)  # subtract .gitkeep
    _indexed    = _index_dir.exists()

    rag_col1, rag_col2 = st.columns(2)
    rag_col1.metric("Files in data/", _file_count)
    rag_col2.metric("Index", "Built ✅" if _indexed else "None ❌")

    if not _indexed:
        st.caption("Add files to `data/` then run:\n`python scripts/index_data.py`")

    if st.button("🔄 Re-index data/", use_container_width=True, disabled=(_file_count == 0)):
        with st.spinner("Indexing…"):
            try:
                from modules.rag_engine import index_folder
                idx_result = index_folder(
                    folder_path=str(_data_dir),
                    persist_dir=str(_index_dir),
                    force_reindex=True,
                )
                st.success(
                    f"Done — indexed {idx_result.get('indexed', 0)}, "
                    f"skipped {idx_result.get('skipped', 0)}"
                )
            except Exception as ex:
                st.error(f"Index failed: {ex}")

    st.divider()

    # ── Session controls ───────────────────────────────────────────────────────
    st.subheader("⚙️ Session")
    s_col1, s_col2 = st.columns(2)
    with s_col1:
        if st.button("🔄 New Session", use_container_width=True):
            st.session_state.session.reset()
            st.session_state.messages       = []
            st.session_state.active_files   = []
            st.session_state.pending_paths  = []
            st.session_state.audit_dict     = None
            st.session_state.audit_ingested = None
            st.session_state.last_route     = None
            st.rerun()
    with s_col2:
        st.metric("Turns", len(st.session_state.session.history))

    st.divider()

    # ── Debug expander ─────────────────────────────────────────────────────────
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
            session_files = st.session_state.session.ingested_files
            st.write(f"**Session files ({len(session_files)}):**")
            for f in session_files:
                st.caption(f"• {f.get('metadata', {}).get('filename', '?')}")
        else:
            st.caption("No message sent yet.")

    st.divider()
    st.caption("PartnerLinQ × Industry Practicum · 2026")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN CHAT AREA
# ══════════════════════════════════════════════════════════════════════════════

st.subheader("Mapping Intelligence Chat")
st.caption(
    "Upload one or more mapping files in the sidebar, then ask anything — "
    "explain, modify, generate, simulate, or audit. "
    "The agent remembers the full conversation and all uploaded files."
)

# ── Render conversation history ────────────────────────────────────────────────
for msg in st.session_state.messages:
    role = msg["role"]
    with st.chat_message(role):
        if role == "assistant":
            intent = msg.get("intent", "")
            file_used = msg.get("file_used", "")
            if intent:
                header = _badge(intent)
                if file_used:
                    header += f'&nbsp;<span style="font-size:0.72rem;color:#64748b;">using <b>{file_used}</b></span>'
                st.markdown(header, unsafe_allow_html=True)
        st.markdown(msg["content"])


# ── Download Modified XSLT button ─────────────────────────────────────────────
# Shown after a successful modify patch so the user can grab the edited file.
if st.session_state.get("patched_xslt"):
    st.divider()
    dl_col1, dl_col2 = st.columns([7, 3])
    with dl_col1:
        fname = st.session_state.get("patched_xslt_filename", "modified.xml")
        st.markdown(
            f"**Modified XSLT ready to download:** `{fname}`  \n"
            "The patch was applied and validated successfully. "
            "Download the file and drop it into MapForce or your XSLT processor."
        )
    with dl_col2:
        st.download_button(
            label="Download Modified XSLT",
            data=st.session_state.patched_xslt.encode("utf-8"),
            file_name=st.session_state.get("patched_xslt_filename", "modified.xml"),
            mime="application/xml",
            use_container_width=True,
            type="primary",
        )


# ── Inline audit form ──────────────────────────────────────────────────────────
if st.session_state.audit_dict is not None:
    audit_dict = st.session_state.audit_dict
    questions  = audit_dict.get("questions", [])
    summary    = audit_dict.get("summary", "")

    st.divider()
    st.markdown("### 📋 Audit Verification Form")

    if summary:
        if "CRITICAL" in summary and not summary.startswith("0 CRITICAL"):
            st.error(f"**Audit Summary:** {summary}")
        elif "WARNING" in summary and not summary.startswith("0 CRITICAL, 0 WARNING"):
            st.warning(f"**Audit Summary:** {summary}")
        else:
            st.success(f"**Audit Summary:** {summary}")

    if questions:
        st.caption(
            "Answer the questions below so the agent can verify your mapping "
            "is production-ready, then click **Submit Answers**."
        )
        SEV_ICON = {"FAIL": "🔴", "WARNING": "🟡", "INFO": "🔵"}

        with st.form("audit_followup_form"):
            answers = []
            for q in questions:
                sev  = q.get("severity", "INFO")
                cat  = q.get("category", "")
                cv   = q.get("current_value")
                icon = SEV_ICON.get(sev, "⚪")
                lbl  = f"{icon} **[{sev}]** [{cat}] {q['question']}"
                if cv:
                    lbl += f"  *(current value: `{cv}`)*"
                st.markdown(lbl)
                ans = st.text_input(
                    "Your answer",
                    key=f"aq_{q['id']}",
                    label_visibility="collapsed",
                    placeholder="Type your answer here…",
                )
                answers.append({"id": q["id"], "question": q["question"], "answer": ans})
                st.markdown("---")

            submitted = st.form_submit_button(
                "✅ Submit Answers for Verification",
                use_container_width=True,
                type="primary",
            )

        if submitted:
            ingested_ref = (
                st.session_state.audit_ingested
                or st.session_state.session.ingested
            )
            if ingested_ref is None:
                st.error("No ingested file context available for followup.")
            else:
                with st.spinner("Running second-pass verification…"):
                    try:
                        followup, _ = audit_followup(ingested_ref, answers)
                    except Exception as ex:
                        followup = f"[ERROR] {ex}"

                if "DO NOT DEPLOY" in followup.upper():
                    st.error("### Verification Result")
                    st.error(followup)
                elif "REVIEW REQUIRED" in followup.upper():
                    st.warning("### Verification Result")
                    st.warning(followup)
                else:
                    st.success("### Verification Result")
                    st.success(followup)

                st.session_state.messages.append({
                    "role":    "assistant",
                    "content": f"**Audit Verification Result**\n\n{followup}",
                    "intent":  "audit",
                })
                st.session_state.audit_dict     = None
                st.session_state.audit_ingested = None
                st.rerun()
    else:
        st.info("The audit found no specific questions. Review the report above.")
        if st.button("Clear Audit Form"):
            st.session_state.audit_dict     = None
            st.session_state.audit_ingested = None
            st.rerun()


# ── Chat input ─────────────────────────────────────────────────────────────────
user_input = st.chat_input("Ask anything about your mapping files…")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})

    with st.spinner("Thinking…"):
        try:
            result = dispatch(
                user_message=user_input,
                file_paths=st.session_state.pending_paths,   # new uploads this turn
                session=st.session_state.session,
            )
            dispatch_error = None
        except Exception as ex:
            result        = None
            dispatch_error = str(ex)

    # Clear pending paths — session now owns the ingested dicts
    st.session_state.pending_paths = []

    if result is None:
        response_text = f"⚠️ Error: {dispatch_error}"
        intent        = "error"
        file_used     = ""
    else:
        response_text = result["primary_response"] or "_No response generated._"
        intent        = result["route"].get("primary", "unknown")
        file_used     = result.get("primary_file_name", "")
        st.session_state.last_route = result["route"]

        if result.get("audit_dict") is not None:
            st.session_state.audit_dict     = result["audit_dict"]
            st.session_state.audit_ingested = result.get("ingested")

        # Store patched XSLT for download button (modify intent)
        patched = result.get("patched_xslt")
        if patched and intent == "modify":
            st.session_state.patched_xslt = patched
            # Build a filename: original name + _patched suffix
            orig_name = result.get("primary_file_name", "mapping.xml")
            stem = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
            st.session_state.patched_xslt_filename = f"{stem}_patched.xml"
        else:
            st.session_state.patched_xslt = None

    st.session_state.messages.append({
        "role":      "assistant",
        "content":   response_text,
        "intent":    intent,
        "file_used": file_used,
    })
    st.rerun()
