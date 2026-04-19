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

import os
import re as _re
import difflib
from pathlib import Path
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

# Load .env so GROQ_API_KEY is available
_env = Path(__file__).resolve().parent / ".env"
if _env.exists():
    load_dotenv(_env)

from modules.dispatcher import dispatch
from modules.file_ingestion import ingest_file
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
  .badge-error      { background: #fef2f2; color: #dc2626; }

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
    color: #1e293b;   /* explicit dark text — stays readable in Streamlit dark mode */
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
    if "llm_provider" not in st.session_state:
        st.session_state.llm_provider = "groq"
    if "llm_api_key" not in st.session_state:
        st.session_state.llm_api_key = os.getenv("GROQ_API_KEY", "")
    if "review_before_xslt" not in st.session_state:
        st.session_state.review_before_xslt = None
    if "review_after_xslt" not in st.session_state:
        st.session_state.review_after_xslt = None
    if "review_rule_key" not in st.session_state:
        st.session_state.review_rule_key = None
    if "review_status" not in st.session_state:
        st.session_state.review_status = None
    if "comparison_summary" not in st.session_state:
        st.session_state.comparison_summary = ""
    if "latest_version_path" not in st.session_state:
        st.session_state.latest_version_path = ""
    if "latest_test_result" not in st.session_state:
        st.session_state.latest_test_result = None
    if "latest_test_output" not in st.session_state:
        st.session_state.latest_test_output = None
    if "test_readiness_status" not in st.session_state:
        st.session_state.test_readiness_status = ""


_init_state()

# ── Login Gate ────────────────────────────────────────────────────────────────

DEMO_PASSWORD = "partnerlinq2026"

PRESET_USERS = {
    "burhan@partnerlinq.com":   {"name": "Burhan Rasool", "role": "EDI Analyst"},
    "abdullah@partnerlinq.com": {"name": "Abdullah",      "role": "EDI Analyst"},
    "farhan@partnerlinq.com":   {"name": "Farhan",        "role": "EDI Analyst"},
    "tom@partnerlinq.com":      {"name": "Tom",           "role": "EDI Analyst"},
}

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "current_user" not in st.session_state:
    st.session_state.current_user = None

if not st.session_state.logged_in:
    st.markdown("""
    <style>
      .block-container { max-width: 480px !important; padding-top: 4rem; }
      section[data-testid="stSidebar"] { display: none; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("## 🔗 PartnerLinQ")
    st.markdown("**Conversational Mapping Intelligence Agent**")
    st.divider()

    st.markdown("##### Quick sign in")
    cols = st.columns(2)
    presets = list(PRESET_USERS.items())
    for i, (email, info) in enumerate(presets):
        col = cols[i % 2]
        if col.button(f"{info['name']} — {info['role']}", key=f"preset_{i}", use_container_width=True):
            st.session_state.logged_in = True
            st.session_state.current_user = {"email": email, **info}
            st.rerun()

    st.divider()
    st.markdown("##### Or sign in manually")

    with st.form("login_form"):
        name      = st.text_input("Full name", placeholder="Your name")
        email     = st.text_input("Email address", placeholder="you@example.com")
        password  = st.text_input("Password", type="password", placeholder="••••••••")
        submitted = st.form_submit_button("Sign in →", use_container_width=True, type="primary")

        if submitted:
            if password != DEMO_PASSWORD:
                st.error("Incorrect password.")
            elif not name or not email:
                st.error("Please enter your name and email.")
            else:
                role = PRESET_USERS.get(email, {}).get("role", "Guest User")
                st.session_state.logged_in = True
                st.session_state.current_user = {"name": name, "email": email, "role": role}
                st.rerun()

    st.stop()
# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_upload(uploaded_file) -> str:
    """Persist an UploadedFile to data/uploads/ and return its path.

    A session-ID prefix is prepended so two users uploading files with the
    same name do not overwrite each other on disk.
    """
    uploads_dir = Path(__file__).resolve().parent / "data" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    session_id = st.session_state.session.session_id
    dest = uploads_dir / f"{session_id}_{uploaded_file.name}"
    dest.write_bytes(uploaded_file.getbuffer())
    return str(dest)


def _badge(intent: str) -> str:
    cls = f"badge badge-{intent.lower()}"
    return f'<span class="{cls}">{intent}</span>'


def _active_file_names() -> set:
    return {f["name"] for f in st.session_state.active_files}

def _extract_xml_fence(text: str) -> Optional[str]:
    """
    Extract the first ```xml ...``` fenced block.
    Returns None if not found.
    """
    m = _re.search(r"```xml\\s*(.*?)\\s*```", text, flags=_re.DOTALL | _re.IGNORECASE)
    return m.group(1).strip() if m else None


def _extract_modify_after_block(text: str) -> Optional[str]:
    """
    Extract the AFTER block from the modify engine format:
    ## AFTER
    ```xml
    ...
    ```
    """
    m = _re.search(r"##\\s+AFTER\\s*```xml\\s*(.*?)\\s*```", text, flags=_re.DOTALL | _re.IGNORECASE)
    return m.group(1).strip() if m else None


def _copy_button(label: str, text: str, key: str) -> None:
    escaped = (
        text.replace("\\\\", "\\\\\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )
    components.html(
        f"""
        <button id="{key}" style="padding:0.25rem 0.6rem;border:1px solid #e2e8f0;border-radius:6px;background:#f8fafc;cursor:pointer;">
          {label}
        </button>
        <script>
          const btn = document.getElementById("{key}");
          btn.addEventListener("click", async () => {{
            try {{
              await navigator.clipboard.writeText(`{escaped}`);
              btn.innerText = "Copied!";
              setTimeout(() => btn.innerText = "{label}", 1200);
            }} catch (e) {{
              btn.innerText = "Copy failed";
              setTimeout(() => btn.innerText = "{label}", 1200);
            }}
          }});
        </script>
        """,
        height=40,
    )


def _ingest_and_update_session(
    xslt_str: str,
    filename: str,
    original_name: Optional[str] = None,
    chip_name: Optional[str] = None,
) -> None:
    """
    Write xslt_str to data/uploads/, ingest it, then update the session and
    the active_files chip list.

    Args:
        xslt_str:      Full XSLT content to save.
        filename:      Disk filename (may include session-id prefix).
        original_name: metadata["filename"] of the file being replaced
                       (used by session.replace_file; disk name with prefix).
        chip_name:     Display name shown in the sidebar chip (no prefix).
                       Falls back to original_name if not provided.

    - original_name set  → replace that file in session (modify flow).
    - original_name None → add as a new file (generate flow).
    """
    uploads_dir = Path(__file__).resolve().parent / "data" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / filename
    dest.write_text(xslt_str, encoding="utf-8")

    new_ing = ingest_file(file_path=str(dest))
    session = st.session_state.session

    # Derive the clean display name for the chip (strip session-id prefix if present)
    _sid = session.session_id
    _new_display = filename
    if _new_display.startswith(_sid + "_"):
        _new_display = _new_display[len(_sid) + 1:]

    if original_name:
        session.replace_file(original_name, new_ing)
        # Find the chip using chip_name (display name) or fall back to original_name
        _find = chip_name or original_name
        for af in st.session_state.active_files:
            if af["name"] == _find:
                af["name"] = _new_display
                af["path"] = str(dest)
                break
        else:
            # Chip not found under expected name — append as new entry
            st.session_state.active_files.append({"name": _new_display, "path": str(dest)})
    else:
        session.add_file(new_ing)
        st.session_state.active_files.append({"name": _new_display, "path": str(dest)})


def _pick_sample_input_path() -> Optional[str]:
    for ing in reversed(st.session_state.session.ingested_files):
        meta = ing.get("metadata", {})
        file_type = meta.get("file_type", "")
        source_path = meta.get("source_path", "")
        if file_type != "XSLT" and source_path and Path(source_path).exists():
            return source_path
    return None


def _test_latest_xslt() -> tuple[Optional[str], Optional[str]]:
    latest_path = st.session_state.get("latest_version_path") or ""
    if not latest_path or not Path(latest_path).exists():
        return None, "No latest revised XSLT is available yet."

    sample_path = _pick_sample_input_path()
    if not sample_path:
        return None, (
            "No sample XML/input file is loaded. Upload a source XML or EDI sample, "
            "then click **Test latest XSLT** again."
        )

    from modules.file_ingestion import ingest_file
    from modules.simulation_engine import simulate

    latest_ingested = ingest_file(file_path=latest_path)
    response_text, output_xml = simulate(
        latest_ingested,
        source_file=sample_path,
        api_key=st.session_state.get("llm_api_key") or None,
        model=None,
    )
    return response_text, output_xml


# ── Sidebar ────────────────────────────────────────────────────────────────────

_data_dir  = Path(__file__).resolve().parent / "data"
_index_dir = Path(__file__).resolve().parent / ".rag_index"

with st.sidebar:
    st.markdown("## PartnerLinQ")
    st.caption("Conversational Mapping Intelligence Agent")
    st.divider()

    # ── Logged-in user ────────────────────────────────────────────────────────
    user = st.session_state.current_user
    if user:
        st.markdown(f"**{user['name']}**  \n*{user['role']}*")
        if st.button("Sign out", use_container_width=True):
            from modules.usage_tracker import reset_session_stats
            reset_session_stats()
            for key in ["logged_in", "current_user", "session", "messages",
                        "active_files", "pending_paths", "audit_dict",
                        "audit_ingested", "last_route", "llm_provider", "llm_api_key"]:
                st.session_state.pop(key, None)
            st.rerun()
    st.divider()

    # ── LLM Provider ──────────────────────────────────────────────────────────
    from modules.llm_client import PROVIDERS, DEFAULT_MODELS

    st.markdown("**LLM Provider**")
    _provider_options = list(PROVIDERS.keys())
    _provider_labels  = [PROVIDERS[p]["label"] for p in _provider_options]
    _current_idx = _provider_options.index(st.session_state.llm_provider) \
                   if st.session_state.llm_provider in _provider_options else 0

    _selected_label = st.selectbox(
        "Provider",
        options=_provider_labels,
        index=_current_idx,
        label_visibility="collapsed",
        key="provider_selectbox",
    )
    _selected_provider = _provider_options[_provider_labels.index(_selected_label)]
    if _selected_provider != st.session_state.llm_provider:
        # Reset API key when switching providers so user enters the right key
        import os as _os
        st.session_state.llm_provider = _selected_provider
        _env_key = PROVIDERS[_selected_provider].get("env_key", "GROQ_API_KEY")
        st.session_state.llm_api_key  = _os.getenv(_env_key, "")
        st.rerun()

    _env_key_name = PROVIDERS[_selected_provider].get("env_key", "GROQ_API_KEY")
    _api_key_input = st.text_input(
        "API Key",
        value=st.session_state.llm_api_key,
        type="password",
        placeholder=f"Paste your {_selected_label} key…",
        label_visibility="collapsed",
        key="api_key_input",
    )
    if _api_key_input != st.session_state.llm_api_key:
        st.session_state.llm_api_key = _api_key_input

    _default_model = DEFAULT_MODELS.get(_selected_provider, "—")
    st.caption(f"Model: `{_default_model}`")
    st.divider()

    # ── Active file list ───────────────────────────────────────────────────────
    st.markdown("**Files in session**")
    if st.session_state.active_files:
        to_remove = None
        for i, af in enumerate(st.session_state.active_files):
            col_name, col_btn = st.columns([5, 1])
            with col_name:
                st.markdown(
                    f'<div class="file-chip">{af["name"]}</div>',
                    unsafe_allow_html=True,
                )
            with col_btn:
                if st.button("x", key=f"rm_{i}", help=f"Remove {af['name']}"):
                    to_remove = i
        if to_remove is not None:
            removed = st.session_state.active_files.pop(to_remove)
            session = st.session_state.session
            session.ingested_files = [
                f for f in session.ingested_files
                if f.get("metadata", {}).get("filename", "") != removed["name"]
            ]
            session.ingested = session.ingested_files[-1] if session.ingested_files else None
            session.agent    = None
            st.rerun()
    else:
        st.caption("No files — attach via the paperclip in chat.")
    st.divider()

    # ── RAG index ─────────────────────────────────────────────────────────────
    st.markdown("**RAG Index**")
    _RAG_EXTS   = {".xml", ".xsl", ".xslt", ".xsd", ".edi", ".txt"}
    _file_count = sum(
        1 for f in _data_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in _RAG_EXTS
    )
    _indexed = _index_dir.exists()

    rag_col1, rag_col2 = st.columns(2)
    rag_col1.metric("data/ files", _file_count)
    rag_col2.metric("Status", "Ready" if _indexed else "None")

    if not _indexed:
        st.caption("Run `python scripts/index_data.py` after adding files to `data/`.")

    if st.button("Re-index data/", use_container_width=True, disabled=(_file_count == 0)):
        with st.spinner("Indexing…"):
            try:
                from modules.rag_engine import index_folder
                from modules.rules_store import RulesStore, utc_now
                db_path = Path(__file__).resolve().parent / "rules_store.db"
                idx_result = index_folder(
                    folder_path=str(_data_dir),
                    persist_dir=str(_index_dir),
                    force_reindex=True,
                )
                try:
                    with RulesStore(db_path) as store:
                        now = utc_now()
                        store.log_event(
                            actor=str(st.session_state.get("reviewer_name") or "system"),
                            action="reindex_data",
                            target=str(_data_dir),
                            status="success",
                            started_at=now,
                            finished_at=now,
                            duration_ms=0,
                            why="user_clicked_reindex",
                            error=None,
                            metadata=idx_result,
                        )
                except Exception:
                    pass
                st.success(
                    f"Indexed {idx_result.get('indexed', 0)}, "
                    f"skipped {idx_result.get('skipped', 0)}"
                )
            except Exception as ex:
                st.error(f"Index failed: {ex}")
    st.divider()

    # ── Session controls ───────────────────────────────────────────────────────
    st.markdown("**Session**")
    if st.button("New Session", use_container_width=True):
        from modules.usage_tracker import reset_session_stats
        reset_session_stats()
        st.session_state.session.reset()
        st.session_state.messages       = []
        st.session_state.active_files   = []
        st.session_state.pending_paths  = []
        st.session_state.audit_dict     = None
        st.session_state.audit_ingested = None
        st.session_state.last_route     = None
        st.rerun()
    st.divider()

    # ── Reviewer identity (for audit log) ─────────────────────────────────────
    st.subheader("✅ Review")
    st.text_input(
        "Your name (for approvals/audit)",
        key="reviewer_name",
        placeholder="e.g. Annabelle",
    )

    # ── Debug expander ─────────────────────────────────────────────────────────
    with st.expander("Debug — last route", expanded=False):
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
                st.caption(f"- {f.get('metadata', {}).get('filename', '?')}")
        else:
            st.caption("No message sent yet.")

    st.divider()

    # ── Token Usage & Cost ─────────────────────────────────────────────────────
    from modules.usage_tracker import get_session_stats, PRICING_COMPARISON

    st.markdown("**Token Usage & Cost**")
    _stats = get_session_stats()

    if _stats["calls"] == 0:
        st.caption("No API calls made yet this session.")
    else:
        # Last call
        lc = _stats["last_call"]
        st.caption("**Last call**")
        lc_cols = st.columns(3)
        lc_cols[0].metric("Input", f"{lc['prompt_tokens']:,}")
        lc_cols[1].metric("Output", f"{lc['completion_tokens']:,}")
        lc_cols[2].metric("Cost", f"${lc['cost_usd']:.5f}")
        st.caption(f"`{lc['model']}` via {lc['provider']} · {lc['latency_ms']:.0f} ms · called by `{lc['caller']}`")

        # Session totals
        st.caption("**Session total**")
        st_cols = st.columns(3)
        st_cols[0].metric("Calls", _stats["calls"])
        st_cols[1].metric("Tokens", f"{_stats['total_tokens']:,}")
        st_cols[2].metric("Est. cost", f"${_stats['estimated_cost_usd']:.4f}")

    # Pricing comparison table
    with st.expander("Model pricing comparison", expanded=False):
        st.caption("Prices in USD per 1 million tokens")
        _hdr = "| Model | Provider | Input/1M | Output/1M |"
        _sep = "|---|---|---|---|"
        _rows = "\n".join(
            f"| `{r['model']}` | {r['provider']} | ${r['input_per_1M']:.2f} | ${r['output_per_1M']:.2f} |"
            for r in PRICING_COMPARISON
        )
        st.markdown(f"{_hdr}\n{_sep}\n{_rows}")

    st.divider()
    st.caption("PartnerLinQ · Industry Practicum · 2026")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA — TABBED LAYOUT
# ══════════════════════════════════════════════════════════════════════════════

tab_chat, tab_review = st.tabs(["💬 Chat", "🧾 Review & Diff"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — CHAT
# ══════════════════════════════════════════════════════════════════════════════

with tab_chat:
    st.subheader("Mapping Intelligence Chat")
    st.caption(
        "Attach mapping files using the paperclip button below, then ask anything — "
        "explain, modify, generate, simulate, or audit. "
        "The agent remembers the full conversation and all uploaded files."
    )

    # ── Render conversation history ────────────────────────────────────────────
    for _msg_idx, msg in enumerate(st.session_state.messages):
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
            # Inline download button — only on assistant messages that produced an XSLT
            if msg.get("download_xslt"):
                dl_fname = msg.get("download_filename", "output.xml")
                _parts = dl_fname.split("_", 2)
                _fallback_label = _parts[-1] if len(_parts) >= 3 else dl_fname
                dl_label = msg.get("download_label") or f"Download {_fallback_label}"
                st.download_button(
                    label=dl_label,
                    data=msg["download_xslt"].encode("utf-8"),
                    file_name=dl_fname,
                    mime="application/xml",
                    type="primary",
                    use_container_width=False,
                    key=f"dl_{_msg_idx}",
                )

    st.divider()

    # ── Attachment popover (compact file upload near the chat input) ───────────
    # Capture the uploader return value OUTSIDE the popover context so that
    # st.rerun() is never called from inside the popover block.
    _inline_uploads = None
    with st.popover("📎 Attach files", use_container_width=False):
        st.caption("Upload mapping files to use in the chat.")
        _inline_uploads = st.file_uploader(
            "Attach files",
            type=["xml", "xsl", "xslt", "xsd", "edi", "txt"],
            accept_multiple_files=True,
            key="inline_uploader",
            label_visibility="collapsed",
        )

    # Process new inline uploads AFTER the popover context has closed.
    if _inline_uploads:
        _known = _active_file_names()
        _new_inline = [f for f in _inline_uploads if f.name not in _known]
        if _new_inline:
            for _uf in _new_inline:
                _saved = _save_upload(_uf)
                st.session_state.active_files.append({"name": _uf.name, "path": _saved})
                try:
                    _ing = ingest_file(file_path=_saved)
                    st.session_state.session.add_file(_ing)
                except Exception:
                    st.session_state.pending_paths.append(_saved)
            st.rerun()

    # ── Chat input ─────────────────────────────────────────────────────────────
    user_input = st.chat_input("Ask anything about your mapping files…")

    if user_input:
        st.session_state.messages.append({"role": "user", "content": user_input})

        _active_provider = st.session_state.get("llm_provider", "groq")
        _active_api_key  = st.session_state.get("llm_api_key", "")

        with st.spinner("Thinking…"):
            try:
                result = dispatch(
                    user_message=user_input,
                    file_paths=st.session_state.pending_paths,
                    session=st.session_state.session,
                    provider=_active_provider,
                    api_key=_active_api_key or None,
                )
                dispatch_error = None
            except Exception as ex:
                result        = None
                dispatch_error = str(ex)

        # Clear pending paths — session now owns the ingested dicts
        st.session_state.pending_paths = []

        download_xslt     = None   # type: Optional[str]
        download_filename = None   # type: Optional[str]
        download_label    = None   # type: Optional[str]

        if result is None:
            response_text = f"⚠️ Error: {dispatch_error}"
            intent        = "error"
            file_used     = ""
        else:
            response_text = result["primary_response"] or "_No response generated._"
            intent        = result["route"].get("primary", "unknown")
            file_used     = result.get("primary_file_name", "")
            st.session_state.last_route = result["route"]

            # ── Capture XSLT for review tab (generate / modify) ────────────────
            ing = result.get("ingested") or {}
            before = None
            try:
                before = (ing.get("parsed_content") or {}).get("raw_xml")
            except Exception:
                before = None
            extracted = None
            if intent == "generate":
                extracted = _extract_xml_fence(response_text)
            elif intent == "modify":
                extracted = _extract_modify_after_block(response_text) or _extract_xml_fence(response_text)
            if extracted:
                st.session_state.review_before_xslt = before
                st.session_state.review_after_xslt  = extracted
                st.session_state.review_rule_key    = file_used or "generated_xslt"
                st.session_state.latest_test_result = None
                st.session_state.latest_test_output = None

            # Structured comparison + revision metadata (modify intent)
            if result.get("comparison_data"):
                comp = result["comparison_data"]
                st.session_state.review_before_xslt = comp.get("old_xslt") or st.session_state.review_before_xslt
                st.session_state.review_after_xslt  = comp.get("new_xslt") or st.session_state.review_after_xslt
                summary_parts = []
                if result.get("change_summary"):
                    summary_parts.append(str(result["change_summary"]))
                if comp.get("summary"):
                    summary_parts.append(str(comp["summary"]))
                st.session_state.comparison_summary = " | ".join(summary_parts)
            else:
                st.session_state.comparison_summary = result.get("change_summary", "") or ""

            st.session_state.latest_version_path   = result.get("latest_version_path", "") or ""
            st.session_state.test_readiness_status = result.get("test_readiness_status", "") or ""

            if result.get("audit_dict") is not None:
                st.session_state.audit_dict     = result["audit_dict"]
                st.session_state.audit_ingested = result.get("ingested")

            # ── Auto-ingest patched/generated XSLT back into session ──────────
            # dispatcher already updated the session for generate; for modify
            # we sync here so that the sidebar chip and next-turn ingested both
            # point to the new file.
            patched      = result.get("patched_xslt")
            generated    = result.get("generated_xslt")
            simulate_out = result.get("simulate_output")
            _sid         = st.session_state.session.session_id

            if patched and intent == "modify":
                _raw_orig = result.get("primary_file_name", "mapping.xml")
                if _raw_orig.startswith(_sid + "_"):
                    _orig_display = _raw_orig[len(_sid) + 1:]
                else:
                    _orig_display = _raw_orig
                _stem             = _orig_display.rsplit(".", 1)[0] if "." in _orig_display else _orig_display
                _new_display      = f"{_stem}_patched.xml"
                download_filename = f"{_sid}_{_new_display}"
                download_label    = f"Download {_new_display}"
                download_xslt     = patched
                try:
                    _ingest_and_update_session(
                        patched,
                        download_filename,
                        original_name=_raw_orig,
                        chip_name=_orig_display,
                    )
                except Exception:
                    pass

            elif simulate_out and intent == "simulate":
                download_filename = f"{_sid}_transform_output.xml"
                download_label    = "Download transform output (XML)"
                download_xslt     = simulate_out

            elif generated and intent == "generate":
                download_filename = f"{_sid}_generated.xml"
                download_label    = "Download generated XSLT"
                download_xslt     = generated
                # Strip the ```xml ... ``` fence from chat — it goes into the
                # download. dispatcher already ingested the file into session.
                response_text = _re.sub(
                    r"```xml[\s\S]*?```",
                    "_Full XSLT is available via the download button below._",
                    response_text,
                    count=1,
                )
                # Sync sidebar chip — dispatcher wrote the file; just add chip.
                _gen_display = "generated.xml"
                if not any(af["name"] == _gen_display for af in st.session_state.active_files):
                    _gen_path = (
                        Path(__file__).resolve().parent / "data" / "uploads"
                        / f"{_sid}_generated.xml"
                    )
                    st.session_state.active_files.append(
                        {"name": _gen_display, "path": str(_gen_path)}
                    )

        st.session_state.messages.append({
            "role":              "assistant",
            "content":           response_text,
            "intent":            intent,
            "file_used":         file_used,
            "download_xslt":     download_xslt,
            "download_filename": download_filename,
            "download_label":    download_label,
        })
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — REVIEW & DIFF
# ══════════════════════════════════════════════════════════════════════════════

with tab_review:
    st.markdown("### 🧾 Review Generated XSLT")

    if not st.session_state.review_after_xslt:
        st.info("No generated XSLT to review yet. Ask the agent to **generate** or **modify** an XSLT, then switch back here.")
    else:
        rule_key    = st.session_state.review_rule_key or "unspecified_rule"
        before_xslt = st.session_state.review_before_xslt or ""
        after_xslt  = st.session_state.review_after_xslt or ""

        top_l, top_r = st.columns([3, 2])
        with top_l:
            st.caption(f"**Rule key:** `{rule_key}`")
            if st.session_state.latest_version_path:
                st.caption(f"**Latest active revision:** `{st.session_state.latest_version_path}`")
            if st.session_state.comparison_summary:
                st.info(st.session_state.comparison_summary)
        with top_r:
            st.text_input("Reason (required)", key="review_reason", placeholder="Why approve/reject/rollback?")

        action_cols = st.columns([1, 1, 1, 2])
        with action_cols[0]:
            _copy_button("Copy XSLT", after_xslt, key="copy_xslt_btn")
        with action_cols[1]:
            st.download_button(
                "Download .xslt",
                data=after_xslt,
                file_name=f"{rule_key}.xslt",
                mime="application/xml",
                use_container_width=True,
            )
        with action_cols[2]:
            show_diff = st.checkbox("Show diff", value=True)

        old_col, new_col = st.columns(2)
        with old_col:
            st.markdown("#### Original / Previous XSLT")
            if before_xslt:
                st.code(before_xslt, language="xml")
            else:
                st.info("No previous XSLT available for comparison yet.")
        with new_col:
            st.markdown("#### Latest Revised XSLT")
            if after_xslt:
                st.code(after_xslt, language="xml")
            else:
                st.info("No revised XSLT has been generated yet.")

        if show_diff:
            if not before_xslt:
                st.warning("No 'before' XSLT available for diff (upload an XSLT or modify an existing mapping).")
            else:
                diff = difflib.HtmlDiff(wrapcolumn=80).make_table(
                    before_xslt.splitlines(),
                    after_xslt.splitlines(),
                    fromdesc="Before",
                    todesc="After",
                    context=True,
                    numlines=3,
                )
                st.markdown(diff, unsafe_allow_html=True)

        btn_l, btn_r, btn_rb, btn_test = st.columns([1, 1, 2, 2])
        reviewer = (st.session_state.get("reviewer_name") or "unknown").strip()
        reason   = (st.session_state.get("review_reason") or "").strip()

        with btn_l:
            if st.button("✅ Approve", type="primary", use_container_width=True):
                if not reason:
                    st.error("Reason is required to approve.")
                else:
                    try:
                        import approval_gate
                        res = approval_gate.approve(rule_key=rule_key, xslt=after_xslt, actor=reviewer, why=reason)
                        st.session_state.review_status = f"Approved {rule_key} as v{res.get('version')}"
                    except Exception as ex:
                        st.session_state.review_status = f"Approve failed: {ex}"
                    st.rerun()

        with btn_r:
            if st.button("❌ Reject", use_container_width=True):
                if not reason:
                    st.error("Reason is required to reject.")
                else:
                    try:
                        import approval_gate
                        approval_gate.reject(rule_key=rule_key, xslt=after_xslt, actor=reviewer, why=reason)
                        st.session_state.review_status = f"Rejected {rule_key}"
                    except Exception as ex:
                        st.session_state.review_status = f"Reject failed: {ex}"
                    st.rerun()

        with btn_rb:
            try:
                from modules.rules_store import RulesStore
                db_path = Path(__file__).resolve().parent / "rules_store.db"
                with RulesStore(db_path) as store:
                    versions = store.list_rule_versions(rule_key)
            except Exception:
                versions = []

            if versions:
                choices  = [f"v{v.version} — {v.approved_at.date()} by {v.approved_by}" for v in versions]
                selected = st.selectbox("Rollback to approved version", choices, index=0)
                sel_ver  = versions[choices.index(selected)].version
                if st.button("⏪ Rollback", use_container_width=True):
                    if not reason:
                        st.error("Reason is required to rollback.")
                    else:
                        try:
                            import approval_gate
                            res = approval_gate.rollback(rule_key=rule_key, version=sel_ver, actor=reviewer, why=reason)
                            st.session_state.review_after_xslt = res.get("xslt")
                            st.session_state.review_status = f"Rolled back {rule_key} to v{sel_ver}"
                        except Exception as ex:
                            st.session_state.review_status = f"Rollback failed: {ex}"
                        st.rerun()
            else:
                st.caption("No approved versions stored yet (approve one to enable rollback).")

        with btn_test:
            if st.button("🧪 Test latest XSLT", use_container_width=True):
                with st.spinner("Testing latest revised XSLT…"):
                    try:
                        test_result, test_output = _test_latest_xslt()
                        st.session_state.latest_test_result = test_result
                        st.session_state.latest_test_output = test_output
                        if not test_result:
                            st.session_state.review_status = (
                                st.session_state.test_readiness_status
                                or "No latest test result was produced."
                            )
                    except Exception as ex:
                        st.session_state.latest_test_result = None
                        st.session_state.latest_test_output = None
                        st.session_state.review_status = f"Latest test failed: {ex}"
                st.rerun()

        if st.session_state.review_status:
            if "failed" in str(st.session_state.review_status).lower():
                st.error(st.session_state.review_status)
            else:
                st.success(st.session_state.review_status)

        if st.session_state.test_readiness_status:
            if "ready" in str(st.session_state.test_readiness_status).lower():
                st.caption(f"Test readiness: {st.session_state.test_readiness_status}")
            else:
                st.warning(st.session_state.test_readiness_status)

        if st.session_state.latest_test_result:
            st.markdown("#### Latest Test Result")
            st.markdown(st.session_state.latest_test_result)
            if st.session_state.latest_test_output:
                st.markdown("#### Latest Transform Output")
                st.code(st.session_state.latest_test_output, language="xml")
                st.download_button(
                    label="⬇ Download transform output (XML)",
                    data=st.session_state.latest_test_output.encode("utf-8"),
                    file_name=f"{st.session_state.session.session_id}_test_output.xml",
                    mime="application/xml",
                    use_container_width=False,
                    key="dl_test_output",
                )
            else:
                st.info("No real XML transform output was produced; the test used LLM simulation.")

    # ── Inline audit form ──────────────────────────────────────────────────────
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
                    if cv is not None:
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
                            followup, _ = audit_followup(
                                ingested_ref,
                                answers,
                                api_key=st.session_state.get("llm_api_key") or None,
                                provider=st.session_state.get("llm_provider", "groq"),
                            )
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
