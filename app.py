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
  .block-container { padding-top: 2.5rem; }

  /* prevent tab labels from being clipped by the container edge */
  .stTabs [data-baseweb="tab-list"] {
    margin-top: 0.25rem;
    overflow: visible;
  }
  .stTabs [data-baseweb="tab"] {
    overflow: visible;
  }

  /* ── Compact sidebar ── */
  section[data-testid="stSidebar"] > div:first-child {
    padding-top: 0.75rem !important;
    padding-bottom: 0.5rem !important;
  }
  section[data-testid="stSidebar"] .stButton > button {
    padding: 0.25rem 0.5rem !important;
    font-size: 0.8rem !important;
    min-height: 1.8rem !important;
  }
  section[data-testid="stSidebar"] .stSelectbox > div,
  section[data-testid="stSidebar"] .stTextInput > div {
    margin-bottom: 0 !important;
  }
  section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] > div {
    gap: 0.25rem !important;
  }
  section[data-testid="stSidebar"] hr {
    margin: 0.4rem 0 !important;
  }
  section[data-testid="stSidebar"] .stCaption,
  section[data-testid="stSidebar"] p {
    margin-bottom: 0.15rem !important;
    font-size: 0.8rem !important;
  }
  section[data-testid="stSidebar"] [data-testid="stMetric"] {
    padding: 0.2rem 0 !important;
  }
  section[data-testid="stSidebar"] [data-testid="stMetricValue"] {
    font-size: 1rem !important;
  }
  section[data-testid="stSidebar"] [data-testid="stExpander"] {
    margin-bottom: 0.2rem !important;
  }

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
    if "active_xslt_file" not in st.session_state:
        st.session_state.active_xslt_file = None
    if "active_source_file" not in st.session_state:
        st.session_state.active_source_file = None
    if "active_target_file" not in st.session_state:
        st.session_state.active_target_file = None
    if "audit_dict" not in st.session_state:
        st.session_state.audit_dict = None
    if "audit_ingested" not in st.session_state:
        st.session_state.audit_ingested = None
    if "last_route" not in st.session_state:
        st.session_state.last_route = None
    if "llm_provider" not in st.session_state:
        st.session_state.llm_provider = "openai"
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
    if "queued_user_prompt" not in st.session_state:
        st.session_state.queued_user_prompt = None
    if "chat_agent" not in st.session_state:
        st.session_state.chat_agent = None      # live FileAgent for streaming follow-ups
    if "token_stats" not in st.session_state:
        try:
            from modules.token_tracker import empty_session_stats
            st.session_state.token_stats = empty_session_stats()
        except Exception:
            st.session_state.token_stats = {}


_init_state()

# ── Login Gate ────────────────────────────────────────────────────────────────

DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "partnerlinq2026")

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
      /* ── Full-page dark canvas ── */
      .stApp {
        background: linear-gradient(135deg, #05050f 0%, #0d0522 40%, #060f1f 100%) !important;
      }
      .block-container {
        max-width: 460px !important;
        padding-top: 7vh !important;
        padding-bottom: 0 !important;
      }
      section[data-testid="stSidebar"] { display: none; }
      header[data-testid="stHeader"]   { background: transparent !important; }
      #MainMenu, footer                { visibility: hidden; }

      /* ── Keyframe animations ── */
      @keyframes fadeSlideUp {
        from { opacity: 0; transform: translateY(28px); }
        to   { opacity: 1; transform: translateY(0);    }
      }
      @keyframes shimmer {
        0%   { background-position: -300% center; }
        100% { background-position:  300% center; }
      }
      @keyframes softPulse {
        0%, 100% { box-shadow: 0 0 0px rgba(206,184,136,0); }
        50%      { box-shadow: 0 0 32px rgba(206,184,136,0.18); }
      }
      @keyframes orbFloat {
        0%, 100% { transform: translateY(0px)   scale(1);    opacity: 0.18; }
        50%      { transform: translateY(-18px) scale(1.04); opacity: 0.28; }
      }

      /* ── Decorative orbs ── */
      .login-orb1, .login-orb2, .login-orb3 {
        position: fixed; border-radius: 50%;
        pointer-events: none; z-index: 0;
        filter: blur(60px);
      }
      .login-orb1 {
        width: 320px; height: 320px;
        background: radial-gradient(circle, #CEB888 0%, transparent 70%);
        top: -80px; right: -80px;
        animation: orbFloat 8s ease-in-out infinite;
      }
      .login-orb2 {
        width: 240px; height: 240px;
        background: radial-gradient(circle, #4a90d9 0%, transparent 70%);
        bottom: 10%; left: -60px;
        animation: orbFloat 11s ease-in-out infinite reverse;
      }
      .login-orb3 {
        width: 180px; height: 180px;
        background: radial-gradient(circle, #9b59b6 0%, transparent 70%);
        bottom: 30%; right: 5%;
        animation: orbFloat 9s ease-in-out infinite 2s;
      }

      /* ── Login card ── */
      .login-card {
        position: relative; z-index: 1;
        background: rgba(255,255,255,0.038);
        border: 1px solid rgba(206,184,136,0.22);
        border-radius: 22px;
        padding: 2.8rem 2.5rem 2.2rem;
        backdrop-filter: blur(18px);
        -webkit-backdrop-filter: blur(18px);
        animation: fadeSlideUp 0.75s cubic-bezier(0.22, 1, 0.36, 1) both,
                   softPulse 4s ease-in-out 0.75s infinite;
        margin-bottom: 1rem;
      }

      /* ── Heading ── */
      .login-heading {
        font-size: 1.85rem;
        font-weight: 800;
        letter-spacing: -0.03em;
        background: linear-gradient(90deg, #CEB888 0%, #fffbe6 40%, #CEB888 70%, #e8d5a3 100%);
        background-size: 300% auto;
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        animation: shimmer 4s linear infinite;
        text-align: center;
        margin: 0 0 0.45rem;
        line-height: 1.15;
      }
      .login-sub {
        color: #ffffff;
        font-size: 8.75rem;
        font-weight: 700;
        text-align: center;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        margin: 0 0 2.2rem;
      }

      /* ── Input fields ── */
      .stTextInput > label { color: rgba(220,220,255,0.7) !important; font-size: 0.8rem !important; }
      .stTextInput > div > div > input {
        background: rgba(255,255,255,0.06) !important;
        border: 1px solid rgba(206,184,136,0.25) !important;
        border-radius: 10px !important;
        color: #f0eadc !important;
        padding: 0.6rem 1rem !important;
        transition: border-color 0.2s, box-shadow 0.2s;
      }
      .stTextInput > div > div > input:focus {
        border-color: rgba(206,184,136,0.65) !important;
        box-shadow: 0 0 0 3px rgba(206,184,136,0.12) !important;
        outline: none !important;
      }
      .stTextInput > div > div > input::placeholder { color: rgba(200,190,170,0.35) !important; }

      /* ── Sign-in button ── */
      .stFormSubmitButton > button, div[data-testid="stFormSubmitButton"] > button {
        background: linear-gradient(135deg, #CEB888 0%, #a8904f 100%) !important;
        color: #0a0a0a !important;
        font-weight: 700 !important;
        font-size: 0.92rem !important;
        letter-spacing: 0.06em !important;
        border: none !important;
        border-radius: 11px !important;
        padding: 0.7rem 1.5rem !important;
        transition: opacity 0.2s, transform 0.15s, box-shadow 0.2s !important;
        box-shadow: 0 4px 18px rgba(206,184,136,0.3) !important;
        width: 100% !important;
        margin-top: 0.5rem !important;
      }
      .stFormSubmitButton > button:hover, div[data-testid="stFormSubmitButton"] > button:hover {
        opacity: 0.88 !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 6px 24px rgba(206,184,136,0.45) !important;
      }

      /* ── Error ── */
      div[data-testid="stAlert"] {
        background: rgba(220,50,50,0.12) !important;
        border: 1px solid rgba(220,80,80,0.3) !important;
        border-radius: 10px !important;
        color: #ffaaaa !important;
      }
    </style>

    <!-- decorative ambient orbs -->
    <div class="login-orb1"></div>
    <div class="login-orb2"></div>
    <div class="login-orb3"></div>
    <div class="login-card">
      <p class="login-heading">PurdueXPartnerLinQ</p>
      <p class="login-sub">Conversational Mapping Agent</p>
    </div>
    """, unsafe_allow_html=True)

    with st.form("login_form"):
        name     = st.text_input("Name", placeholder="Your name")
        password = st.text_input("Access key", type="password", placeholder="••••••••")
        submitted = st.form_submit_button("Sign In →", use_container_width=True, type="primary")

        if submitted:
            if password != DEMO_PASSWORD:
                st.error("Incorrect access key.")
            elif not name.strip():
                st.error("Please enter your name.")
            else:
                st.session_state.logged_in     = True
                st.session_state.reviewer_name = name.strip()
                st.session_state.current_user  = {
                    "name": name.strip(),
                    "email": f"{name.strip().lower().replace(' ','.')}@partnerlinq.com",
                    "role": "EDI Analyst",
                }
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


def _active_file_by_path(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    for af in st.session_state.active_files:
        if af.get("path") == path:
            return af
    return None


def _role_display_name(path: Optional[str]) -> str:
    af = _active_file_by_path(path)
    if af:
        return af.get("name", "")
    if path:
        return Path(path).name
    return ""


def _sync_role_paths_to_session() -> None:
    s = st.session_state.session
    s.set_role_file("xslt", st.session_state.get("active_xslt_file"))
    s.set_role_file("source", st.session_state.get("active_source_file"))
    s.set_role_file("target", st.session_state.get("active_target_file"))


def _sync_role_paths_from_session() -> None:
    s = st.session_state.session
    st.session_state.active_xslt_file = s.get_role_file("xslt")
    st.session_state.active_source_file = s.get_role_file("source")
    st.session_state.active_target_file = s.get_role_file("target")

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
    _sync_role_paths_from_session()


def _pick_sample_input_path() -> Optional[str]:
    selected = st.session_state.get("active_source_file")
    if selected and Path(selected).exists():
        return selected
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

    from modules.llm_client import PROVIDERS as _PROV
    _sim_provider = st.session_state.get("llm_provider", "openai")
    _sim_env_key  = _PROV.get(_sim_provider, {}).get("env_key", "OPENAI_API_KEY")
    _sim_api_key  = os.getenv(_sim_env_key) or os.getenv("OPENAI_API_KEY") or None

    latest_ingested = ingest_file(file_path=latest_path)
    response_text, output_xml = simulate(
        latest_ingested,
        source_file=sample_path,
        api_key=_sim_api_key,
        model=None,
    )
    return response_text, output_xml


# ── Sidebar ────────────────────────────────────────────────────────────────────

_data_dir  = Path(__file__).resolve().parent / "data"
_index_dir = Path(__file__).resolve().parent / ".rag_index"

with st.sidebar:
    st.markdown("""
    <style>
      @keyframes sb-shimmer {
        0%   { background-position: -300% center; }
        100% { background-position:  300% center; }
      }
      .sb-card {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(206,184,136,0.22);
        border-radius: 12px;
        padding: 0.75rem 1rem 0.65rem;
        margin-bottom: 0.5rem;
      }
      .sb-heading {
        font-size: 1rem;
        font-weight: 800;
        letter-spacing: -0.01em;
        background: linear-gradient(90deg, #CEB888 0%, #fffbe6 40%, #CEB888 70%, #e8d5a3 100%);
        background-size: 300% auto;
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        animation: sb-shimmer 4s linear infinite;
        margin: 0 0 0.15rem;
        line-height: 1.2;
      }
      .sb-sub {
        font-size: 0.72rem;
        font-weight: 700;
        color: #ffffff;
        letter-spacing: 0.06em;
        margin: 0;
        line-height: 1.3;
      }
    </style>
    <div class="sb-card">
      <p class="sb-heading">PartnerLinQ × Purdue</p>
      <p class="sb-sub">Conversational Mapping Agent</p>
    </div>
    """, unsafe_allow_html=True)
    # ── card CSS (shared for all sidebar cards) ──────────────────────────────
    st.markdown("""
    <style>
      .sb-section-card {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 10px;
        padding: 0.6rem 0.8rem 0.5rem;
        margin-bottom: 0.45rem;
      }
      .sb-section-title {
        font-size: 0.7rem;
        font-weight: 700;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: rgba(206,184,136,0.85);
        margin: 0 0 0.35rem;
      }
    </style>
    """, unsafe_allow_html=True)

    # ── USER card ─────────────────────────────────────────────────────────────
    user = st.session_state.current_user
    if user:
        with st.container():
            st.markdown('<div class="sb-section-card"><p class="sb-section-title">Signed in</p></div>', unsafe_allow_html=True)
            st.caption(f"👤 **{user['name']}** · {user['role']}")
            if st.button("Sign out", use_container_width=True):
                from modules.usage_tracker import reset_session_stats
                reset_session_stats()
                for key in ["logged_in", "current_user", "session", "messages",
                            "active_files", "pending_paths", "audit_dict",
                            "audit_ingested", "last_route", "llm_provider",
                            "active_xslt_file", "active_source_file", "active_target_file"]:
                    st.session_state.pop(key, None)
                st.rerun()

    # ── LLM PROVIDER card ─────────────────────────────────────────────────────
    from modules.llm_client import PROVIDERS, DEFAULT_MODELS

    st.markdown('<div class="sb-section-card"><p class="sb-section-title">🤖 LLM Provider</p></div>', unsafe_allow_html=True)
    _provider_options = [
        p for p in PROVIDERS
        if os.getenv(PROVIDERS[p].get("env_key", ""))
    ]
    if not _provider_options:
        st.warning("No API keys found in `.env`.")
    else:
        _provider_labels = [PROVIDERS[p]["label"] for p in _provider_options]
        if st.session_state.llm_provider not in _provider_options:
            st.session_state.llm_provider = _provider_options[0]
        _current_idx = _provider_options.index(st.session_state.llm_provider)
        _selected_label = st.selectbox(
            "Provider",
            options=_provider_labels,
            index=_current_idx,
            label_visibility="collapsed",
            key="provider_selectbox",
        )
        _selected_provider = _provider_options[_provider_labels.index(_selected_label)]
        if _selected_provider != st.session_state.llm_provider:
            st.session_state.llm_provider = _selected_provider
            st.rerun()
        _default_model = DEFAULT_MODELS.get(_selected_provider, "—")
        st.caption(f"Model: `{_default_model}`")

    # ── FILES card ────────────────────────────────────────────────────────────
    st.markdown('<div class="sb-section-card"><p class="sb-section-title">📂 Files in session</p></div>', unsafe_allow_html=True)
    if st.session_state.active_files:
        to_remove = None
        for i, af in enumerate(st.session_state.active_files):
            col_name, col_btn = st.columns([5, 1])
            with col_name:
                st.markdown(f'<div class="file-chip">{af["name"]}</div>', unsafe_allow_html=True)
            with col_btn:
                if st.button("x", key=f"rm_{i}", help=f"Remove {af['name']}"):
                    to_remove = i
        if to_remove is not None:
            removed = st.session_state.active_files.pop(to_remove)
            session = st.session_state.session
            removed_path = removed.get("path", "")
            session.ingested_files = [
                f for f in session.ingested_files
                if f.get("metadata", {}).get("source_path", "") != removed_path
            ]
            session.ingested = session.ingested_files[-1] if session.ingested_files else None
            session.agent    = None
            if st.session_state.active_xslt_file == removed_path:
                st.session_state.active_xslt_file = None
            if st.session_state.active_source_file == removed_path:
                st.session_state.active_source_file = None
            if st.session_state.active_target_file == removed_path:
                st.session_state.active_target_file = None
            _sync_role_paths_to_session()
            st.rerun()
    else:
        st.caption("No files — attach via 📎 in chat.")

    # ── FILE ROLES card ───────────────────────────────────────────────────────
    _all_files = st.session_state.active_files
    _all_opts  = ["(none)"] + [af["path"] for af in _all_files]
    _all_labels = {"(none)": "(none)"}
    for af in _all_files:
        _all_labels[af["path"]] = af["name"]

    _xslt_opts = ["(none)"]
    for ing in st.session_state.session.ingested_files:
        _meta = ing.get("metadata", {})
        if _meta.get("file_type") == "XSLT":
            _p = _meta.get("source_path", "")
            if _p:
                _xslt_opts.append(_p)
                _all_labels.setdefault(_p, Path(_p).name)

    def _select_index(options: list, current: Optional[str]) -> int:
        if current and current in options:
            return options.index(current)
        return 0

    with st.expander("📁 File Roles", expanded=False):
        _selected_xslt = st.selectbox(
            "XSLT",
            options=_xslt_opts,
            format_func=lambda v: _all_labels.get(v, v),
            index=_select_index(_xslt_opts, st.session_state.get("active_xslt_file")),
            key="role_select_xslt",
        )
        _selected_source = st.selectbox(
            "Source XML",
            options=_all_opts,
            format_func=lambda v: _all_labels.get(v, v),
            index=_select_index(_all_opts, st.session_state.get("active_source_file")),
            key="role_select_source",
        )
        _selected_target = st.selectbox(
            "Target XML",
            options=_all_opts,
            format_func=lambda v: _all_labels.get(v, v),
            index=_select_index(_all_opts, st.session_state.get("active_target_file")),
            key="role_select_target",
        )
        st.caption(
            f"XSLT: `{_role_display_name(st.session_state.active_xslt_file) or 'none'}` · "
            f"Src: `{_role_display_name(st.session_state.active_source_file) or 'none'}` · "
            f"Tgt: `{_role_display_name(st.session_state.active_target_file) or 'none'}`"
        )

    st.session_state.active_xslt_file   = None if _selected_xslt   == "(none)" else _selected_xslt
    st.session_state.active_source_file = None if _selected_source == "(none)" else _selected_source
    st.session_state.active_target_file = None if _selected_target == "(none)" else _selected_target
    _sync_role_paths_to_session()

    # ── SESSION card ──────────────────────────────────────────────────────────
    st.markdown('<div class="sb-section-card"><p class="sb-section-title">⚙️ Session</p></div>', unsafe_allow_html=True)
    if st.button("New Session", use_container_width=True):
        from modules.usage_tracker import reset_session_stats
        reset_session_stats()
        st.session_state.session.reset()
        st.session_state.messages            = []
        st.session_state.active_files        = []
        st.session_state.pending_paths       = []
        st.session_state.active_xslt_file    = None
        st.session_state.active_source_file  = None
        st.session_state.active_target_file  = None
        st.session_state.audit_dict          = None
        st.session_state.audit_ingested      = None
        st.session_state.last_route          = None
        st.session_state.chat_agent          = None
        from modules.token_tracker import empty_session_stats
        st.session_state.token_stats = empty_session_stats()
        st.rerun()

    # ── COST card ─────────────────────────────────────────────────────────────
    from modules.usage_tracker import get_session_stats as _get_stats
    _qs = _get_stats()
    st.markdown('<div class="sb-section-card"><p class="sb-section-title">💰 Est. Session Cost</p></div>', unsafe_allow_html=True)
    if _qs["calls"] == 0:
        st.caption("No API calls yet.")
    else:
        _qs_c1, _qs_c2 = st.columns(2)
        _qs_c1.metric("Tokens",    f"{_qs['total_tokens']:,}")
        _qs_c2.metric("Est. cost", f"${_qs['estimated_cost_usd']:.4f}")
        st.caption("→ **📈 Analytics** tab for full breakdown")

    st.caption("PartnerLinQ · Industry Practicum · 2026")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA — TABBED LAYOUT
# ══════════════════════════════════════════════════════════════════════════════

tab_chat, tab_review, tab_history, tab_analytics = st.tabs(["💬 Chat", "🧾 Review & Diff", "📋 Revision History", "📈 Analytics"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — CHAT
# ══════════════════════════════════════════════════════════════════════════════

with tab_chat:
    st.subheader("Mapping Intelligence Chat")
    st.caption(
        "Attach mapping files using the paperclip button below, then ask anything — "
        "explain, modify, generate, simulate, or audit. "
        "The agent remembers the full conversation and all uploaded files. "
        "Roles are strict: XSLT for explain/modify/review, source for simulation input, "
        "target for output validation only."
    )

    # ── Render conversation history ────────────────────────────────────────────
    for _msg_idx, msg in enumerate(st.session_state.messages):
        role = msg["role"]
        with st.chat_message(role):
            if role == "assistant":
                intent = msg.get("intent", "")
                file_used = msg.get("file_used", "")
                source_used = msg.get("source_file_used", "")
                target_used = msg.get("target_file_used", "")
                if intent:
                    header = _badge(intent)
                    if file_used:
                        header += f'&nbsp;<span style="font-size:0.72rem;color:#64748b;">using <b>{file_used}</b></span>'
                    if source_used:
                        header += f'&nbsp;<span style="font-size:0.72rem;color:#64748b;">source <b>{source_used}</b></span>'
                    if target_used:
                        header += f'&nbsp;<span style="font-size:0.72rem;color:#64748b;">target <b>{target_used}</b></span>'
                    st.markdown(header, unsafe_allow_html=True)
            st.markdown(msg["content"])
            if role == "assistant" and msg.get("intent") == "compare":
                _comp = msg.get("xslt_compare_data") or {}
                if _comp:
                    st.markdown("#### Compare Details")
                    st.caption(f"Risk level: `{_comp.get('risk_level', 'unknown')}`")
                    _added = _comp.get("added_segments_in_revised", []) or []
                    _missing = _comp.get("missing_segments_in_revised", []) or []
                    _div = _comp.get("mapping_divergence", []) or []
                    if _added:
                        st.write(f"Added segments: `{', '.join(_added[:20])}`")
                    if _missing:
                        st.write(f"Removed segments: `{', '.join(_missing[:20])}`")
                    if _div:
                        st.write(f"Mapping divergence points: `{len(_div)}`")
                    _diff_preview = _comp.get("diff_preview", "")
                    if _diff_preview:
                        st.markdown("Diff preview:")
                        st.code(_diff_preview, language="diff")
            if role == "assistant" and msg.get("intent") == "simulate":
                if msg.get("status") == "validation_only":
                    st.info("XSLT Validation Mode — Saxon transform unavailable")
                _status = msg.get("target_match_status", "")
                _summary = msg.get("target_match_summary", "")
                _extra = msg.get("extra_output_segments", []) or []
                if _status:
                    st.markdown("#### Target Comparison")
                    if _status == "matches_target":
                        st.success(_summary or "Output matches target.")
                    elif _status == "partial_match":
                        st.warning(_summary or "Output partially matches target.")
                    elif _status == "does_not_match":
                        st.error(_summary or "Output does not match target.")
                    elif _status != "no_target":
                        st.info(_summary or "Target comparison unavailable.")
                    if _extra:
                        st.caption(f"Extra output segments (not in target): `{', '.join(_extra)}`")

                # ── Compact actionable findings table ──────────────────────────
                _findings = msg.get("simulate_audit_findings", []) or []
                if _findings:
                    _crits = [f for f in _findings if f.get("severity") == "CRITICAL"]
                    _warns = [f for f in _findings if f.get("severity") != "CRITICAL"]
                    st.markdown(
                        f"#### Issues Found &nbsp; "
                        f"{'🔴 ' + str(len(_crits)) + ' critical' if _crits else ''}"
                        f"{'  🟡 ' + str(len(_warns)) + ' warning' if _warns else ''}"
                    )
                    for i, fx in enumerate(_findings[:12]):
                        sev_icon = "🔴" if fx.get("severity") == "CRITICAL" else "🟡"
                        _c1, _c2, _c3 = st.columns([2, 4, 2])
                        with _c1:
                            st.markdown(
                                f"{sev_icon} **{fx.get('field', '?')}**  \n"
                                f"<span style='font-size:0.8em;color:gray'>"
                                f"line {fx.get('xslt_line','?')} · {fx.get('issue_type','').replace('_',' ')}"
                                f"</span>",
                                unsafe_allow_html=True,
                            )
                        with _c2:
                            out_v = fx.get("output_val", "")
                            exp_v = fx.get("expected_val", "")
                            st.markdown(
                                f"`{out_v}` → `{exp_v}`",
                                unsafe_allow_html=False,
                            )
                        with _c3:
                            if st.button("Apply Fix", key=f"sim_fix_{_msg_idx}_{i}"):
                                st.session_state.queued_user_prompt = fx.get("apply_prompt", "")
                                st.rerun()
                        st.divider()
            if role == "assistant" and msg.get("intent") == "modify":
                _m_status = msg.get("modify_status", "")
                _m_guidance = msg.get("modify_guidance", {}) or {}
                if _m_status == "needs_confirmation" and _m_guidance:
                    st.warning("Action required before applying this modification.")
                    st.write(_m_guidance.get("message", "Please confirm how to proceed."))
                    _recs = _m_guidance.get("recommendations", []) or []
                    if _recs:
                        st.write("Recommended alternatives:")
                        for rec in _recs[:3]:
                            seg_part = f" in {rec.get('segment')} segment" if rec.get("segment") else ""
                            st.info(f"{rec.get('field','')} {seg_part} - {rec.get('name','')} ({rec.get('reason','')})")
                    _a, _b, _c = st.columns(3)
                    with _a:
                        if st.button("Proceed as requested", key=f"mod_proceed_{_msg_idx}"):
                            srcf = _m_guidance.get("source_field") or "InvoiceNetAmount"
                            tgtf = _m_guidance.get("target_field") or "BIG04"
                            seg = _m_guidance.get("target_segment") or "BIG"
                            st.session_state.queued_user_prompt = f"Add {srcf} to {tgtf} in {seg} segment and proceed anyway"
                            st.rerun()
                    with _b:
                        if st.button("Use recommended", key=f"mod_reco_{_msg_idx}") and _recs:
                            first = _recs[0]
                            srcf = _m_guidance.get("source_field") or "source field"
                            seg = first.get("segment", _m_guidance.get("target_segment", ""))
                            st.session_state.queued_user_prompt = f"Add {srcf} to {first.get('field','')} in {seg} segment"
                            st.rerun()
                    with _c:
                        st.button("Cancel", key=f"mod_cancel_{_msg_idx}", disabled=True)
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

    # ── Attachment popover (compact file upload near the chat input) ─────────
    # Capture the uploader return value OUTSIDE the popover context so that
    # st.rerun() is never called from inside the popover block.  Calling rerun
    # from inside a with-st.popover() raises StopException before the context
    # manager exits cleanly, which silently discards any session_state changes
    # made inside the block.
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

    # ── Chat input ────────────────────────────────────────────────────────────
    user_input = st.chat_input("Ask anything about your mapping files…")

    if user_input:
        _active_provider = st.session_state.get("llm_provider", "openai")
        from modules.llm_client import PROVIDERS as _PROVIDERS
        _env_key_name    = _PROVIDERS.get(_active_provider, {}).get("env_key", "OPENAI_API_KEY")
        _active_api_key  = os.getenv(_env_key_name) or os.getenv("OPENAI_API_KEY") or ""

        _streaming_providers = {"openai", "groq"}
        _non_explain_actions = {"modify", "simulate", "audit", "generate", "compare"}
        _agent_for_stream    = st.session_state.get("chat_agent")

        def _is_non_explain(msg: str) -> bool:
            from modules.dispatcher import _classify_action
            return _classify_action(msg) in _non_explain_actions

        if (
            _agent_for_stream is not None
            and _active_provider in _streaming_providers
            and not st.session_state.pending_paths
            and not _is_non_explain(user_input)
        ):
            with st.chat_message("user"):
                st.markdown(user_input)

            with st.chat_message("assistant"):
                try:
                    _stream_gen    = _agent_for_stream.chat(user_input, stream=True)
                    _streamed_text = st.write_stream(_stream_gen)
                except Exception as _se:
                    _streamed_text = f"⚠️ Streaming error: {_se}"
                    st.markdown(_streamed_text)

            st.session_state.messages.append({"role": "user", "content": user_input})
            st.session_state.messages.append({
                "role":     "assistant",
                "content":  _streamed_text,
                "intent":   "explain",
                "file_used": "",
            })
            st.rerun()

        st.session_state.messages.append({"role": "user", "content": user_input})

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

            _turn_usage = result.get("token_usage")
            if _turn_usage:
                from modules.token_tracker import merge_into_session
                merge_into_session(st.session_state.token_stats, _turn_usage)

            if result.get("audit_dict") is not None:
                st.session_state.audit_dict     = result["audit_dict"]
                st.session_state.audit_ingested = result.get("ingested")

            _result_agent = result.get("agent")
            if _result_agent is not None and intent == "explain":
                st.session_state.chat_agent = _result_agent
            elif intent not in ("explain", "unknown"):
                st.session_state.chat_agent = None

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

                _orig_ing = result.get("ingested")
                if _orig_ing:
                    _before_raw = (_orig_ing.get("parsed_content") or {}).get("raw_xml", "")
                    st.session_state.review_before_xslt = _before_raw or None
                st.session_state.review_after_xslt = patched
                st.session_state.review_rule_key   = download_filename

            elif simulate_out and intent == "simulate":
                download_filename = f"{_sid}_transform_output.xml"
                download_label    = "Download transform output (XML)"
                download_xslt     = simulate_out

            elif generated and intent == "generate":
                download_filename = f"{_sid}_generated.xml"
                download_label    = "Download generated XSLT"
                download_xslt     = generated
                response_text = _re.sub(
                    r"```xml[\s\S]*?```",
                    "_Full XSLT is available via the download button below._",
                    response_text,
                    count=1,
                )
                try:
                    _ingest_and_update_session(generated, download_filename, original_name=None)
                except Exception:
                    pass

        _msg: dict = {
            "role":              "assistant",
            "content":           response_text,
            "intent":            intent,
            "file_used":         file_used,
            "download_xslt":     download_xslt,
            "download_filename": download_filename,
            "download_label":    download_label,
        }
        if result is not None and intent == "simulate":
            _msg["status"]                  = result.get("status", "")
            _msg["target_match_status"]     = result.get("target_match_status", "")
            _msg["target_match_summary"]    = result.get("target_match_summary", "")
            _msg["missing_target_segments"] = result.get("missing_target_segments", [])
            _msg["extra_output_segments"]   = result.get("extra_output_segments", [])
            _msg["mismatched_fields"]       = result.get("mismatched_fields", [])
            _msg["autofix_suggestions"]     = result.get("autofix_suggestions", [])
            _msg["simulate_audit_findings"] = result.get("simulate_audit_findings", [])
        st.session_state.messages.append(_msg)
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
            _revs = st.session_state.session.xslt_revisions
            if _revs:
                _rev_labels = [
                    f"Revision {i+1} ({r.timestamp}) — {r.description[:48]}"
                    for i, r in enumerate(_revs)
                ]
                _sel_idx = st.selectbox(
                    "Revision selector",
                    options=list(range(len(_revs))),
                    format_func=lambda idx: _rev_labels[idx],
                    index=len(_revs) - 1,
                    key="rev_selector_idx",
                )
                _sel_rev = _revs[_sel_idx]
                st.caption(f"Viewing revision {_sel_idx + 1}: `{_sel_rev.timestamp}`")
                if _sel_idx >= 0:
                    st.session_state.review_after_xslt = _sel_rev.content
                if _sel_idx > 0:
                    _prev = _revs[_sel_idx - 1]
                    _cmp = st.session_state.session.compare_revisions(_prev.id, _sel_rev.id)
                    if _cmp.get("summary"):
                        st.info(_cmp["summary"])
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
                            _af_provider = st.session_state.get("llm_provider", "openai")
                            from modules.llm_client import PROVIDERS as _AFPROV
                            _af_env_key  = _AFPROV.get(_af_provider, {}).get("env_key", "OPENAI_API_KEY")
                            _af_api_key  = os.getenv(_af_env_key) or os.getenv("OPENAI_API_KEY") or None
                            followup, _ = audit_followup(
                                ingested_ref,
                                answers,
                                api_key=_af_api_key,
                                provider=_af_provider,
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


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — REVISION HISTORY
# ══════════════════════════════════════════════════════════════════════════════

with tab_history:
    st.markdown("### 📋 Revision History")
    _revs = st.session_state.session.xslt_revisions if st.session_state.session else []

    if not _revs:
        st.info("No XSLT revisions yet. Modify an XSLT in the chat to start tracking changes.")
    else:
        st.caption(f"{len(_revs)} revision(s) in this session")

        for _ri, _rev in enumerate(reversed(_revs)):
            _rev_num = len(_revs) - _ri
            _ts = getattr(_rev, "timestamp", "?")
            _desc = getattr(_rev, "description", "") or "Modification"
            _content = getattr(_rev, "content", "") or ""
            _diff = getattr(_rev, "diff_text", None)

            with st.expander(f"Rev {_rev_num} — {_ts}  ·  {_desc[:72]}"):
                col_dl, col_apply = st.columns([2, 1])
                with col_dl:
                    st.download_button(
                        label=f"Download Rev {_rev_num}",
                        data=_content.encode("utf-8"),
                        file_name=f"revision_{_rev_num}_{_ts.replace(':', '-')}.xml",
                        mime="application/xml",
                        key=f"dl_rev_{_rev_num}",
                        use_container_width=True,
                    )
                with col_apply:
                    if st.button(
                        "Restore as active",
                        key=f"restore_rev_{_rev_num}",
                        help="Load this revision into the Review tab",
                    ):
                        st.session_state.review_after_xslt = _content
                        st.session_state.review_before_xslt = (
                            getattr(_revs[len(_revs) - _rev_num - 1], "content", "")
                            if len(_revs) - _rev_num - 1 >= 0 else ""
                        )
                        st.session_state.review_rule_key = f"rev_{_rev_num}"
                        st.success(f"Rev {_rev_num} loaded into Review & Diff tab.")
                        st.rerun()

                if _diff:
                    st.markdown("**Diff vs previous revision:**")
                    st.code(_diff[:3_000] + ("\n... [truncated]" if len(_diff) > 3_000 else ""), language="diff")
                else:
                    st.caption("(No diff available — this is the first revision)")

                if _content:
                    st.markdown("**Preview (first 30 lines):**")
                    _preview_lines = _content.splitlines()[:30]
                    st.code("\n".join(_preview_lines), language="xml")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — ANALYTICS  (dynamic token usage × cost table)
# ══════════════════════════════════════════════════════════════════════════════

with tab_analytics:
    st.markdown("### 📈 Session Analytics")
    st.caption("Live token usage and estimated cost for this session, broken down by engine and model.")

    from modules.usage_tracker import get_session_stats, PRICING

    _stats = get_session_stats()
    _ts    = st.session_state.get("token_stats", {})

    # ── Summary KPI row ──────────────────────────────────────────────────────
    _kpi1, _kpi2, _kpi3, _kpi4 = st.columns(4)
    _kpi1.metric("API Calls",       _stats.get("calls", 0))
    _kpi2.metric("Total Tokens",    f"{_stats.get('total_tokens', 0):,}")
    _kpi3.metric("Prompt Tokens",   f"{_stats.get('prompt_tokens', 0):,}")
    _kpi4.metric("Output Tokens",   f"{_stats.get('completion_tokens', 0):,}")

    _total_cost = _stats.get("estimated_cost_usd", 0.0)
    if _stats.get("calls", 0) == 0:
        st.info("No API calls made yet this session. Start a chat to see live usage data.")
    else:
        st.metric("Estimated session cost", f"${_total_cost:.5f}", help="Based on public pricing at time of release")

        # ── Per-engine breakdown table ─────────────────────────────────────
        st.markdown("#### Breakdown by Engine")
        _by_engine = _ts.get("by_engine", {})

        if _by_engine:
            import pandas as _pd

            _engine_icons = {
                "explain":       "🔍",
                "simulate":      "⚙️",
                "modify":        "✏️",
                "audit":         "🛡️",
                "generate":      "🏗️",
                "intent_router": "🧭",
                "rag":           "📚",
                "unknown":       "❓",
            }

            _rows = []
            for _eng, _estats in sorted(_by_engine.items()):
                _model       = _estats.get("model", "—")
                _p_in        = _estats.get("prompt_tokens", 0)
                _p_out       = _estats.get("completion_tokens", 0)
                _p_tot       = _estats.get("total_tokens", 0)
                _calls       = _estats.get("calls", 0)
                _price_in, _price_out = PRICING.get(_model, (0.0, 0.0))
                _cost_in     = _p_in  * _price_in  / 1_000_000
                _cost_out    = _p_out * _price_out / 1_000_000
                _eng_cost    = round(_cost_in + _cost_out, 6)
                _icon        = _engine_icons.get(_eng, "•")
                _rows.append({
                    "Engine":         f"{_icon} {_eng.capitalize()}",
                    "Model":          _model,
                    "Calls":          _calls,
                    "Input Tokens":   _p_in,
                    "Output Tokens":  _p_out,
                    "Total Tokens":   _p_tot,
                    "Input $/1M":     f"${_price_in:.2f}",
                    "Output $/1M":    f"${_price_out:.2f}",
                    "Est. Cost (USD)": f"${_eng_cost:.5f}",
                })

            _df = _pd.DataFrame(_rows)
            st.dataframe(_df, use_container_width=True, hide_index=True)

            # ── Totals row ─────────────────────────────────────────────────
            _tot_in  = sum(r["Input Tokens"]  for r in _rows)
            _tot_out = sum(r["Output Tokens"] for r in _rows)
            _tot_tok = sum(r["Total Tokens"]  for r in _rows)
            st.markdown(
                f"**Totals — Input: `{_tot_in:,}` · Output: `{_tot_out:,}` · "
                f"Total: `{_tot_tok:,}` · Est. cost: `${_total_cost:.5f}`**"
            )
        else:
            st.caption("Engine breakdown not yet available.")

        # ── Last call detail ───────────────────────────────────────────────
        _lc = _stats.get("last_call")
        if _lc:
            st.divider()
            st.markdown("#### Last API Call")
            _lc1, _lc2, _lc3 = st.columns(3)
            _lc1.metric("Input",  f"{_lc['prompt_tokens']:,}")
            _lc2.metric("Output", f"{_lc['completion_tokens']:,}")
            _lc3.metric("Cost",   f"${_lc['cost_usd']:.5f}")
            st.caption(
                f"`{_lc['model']}` via **{_lc['provider']}** · "
                f"{_lc['latency_ms']:.0f} ms · caller: `{_lc['caller']}`"
            )

    # ── Pricing reference table ────────────────────────────────────────────
    st.divider()
    st.markdown("#### Model Pricing Reference")
    st.caption("Rates used to compute cost estimates (USD per 1 million tokens)")

    from modules.usage_tracker import PRICING_COMPARISON as _PCOMP
    import pandas as _pd2

    _ref_rows = [
        {
            "Model":          r["model"],
            "Provider":       r["provider"],
            "Input $/1M":     f"${r['input_per_1M']:.2f}",
            "Output $/1M":    f"${r['output_per_1M']:.2f}",
            "Session Input $":  f"${_stats.get('prompt_tokens', 0) * r['input_per_1M'] / 1_000_000:.5f}",
            "Session Output $": f"${_stats.get('completion_tokens', 0) * r['output_per_1M'] / 1_000_000:.5f}",
        }
        for r in _PCOMP
    ]
    st.dataframe(_pd2.DataFrame(_ref_rows), use_container_width=True, hide_index=True)
    st.caption("'Session Input $' / 'Session Output $' shows what this session would cost if all tokens had used that model.")


