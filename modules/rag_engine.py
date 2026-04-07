"""
rag_engine.py
─────────────
Multi-file ingestion and RAG-based cross-file querying.

Pipeline
────────
INDEX (run once per folder, or when files change):
  1. Walk folder_path, find all supported mapping files.
  2. Call file_ingestion.ingest_file() on each.
  3. Extract a text summary from the parsed content (type-specific).
  4. Embed each summary using sentence-transformers (all-MiniLM-L6-v2).
  5. Store document + embedding + metadata in ChromaDB (persisted to disk).

QUERY (run any number of times after indexing):
  1. Embed the user question using the same model.
  2. Retrieve the top-K most similar documents from ChromaDB.
  3. Build a prompt: system + retrieved chunks + user question.
  4. Call Groq and return the answer.

Supported file extensions (auto-filtered):
  .xml  .xsl  .xslt  .xsd  .edi  .x12  .txt  .edifact

Token budget (llama-3.3-70b-versatile at ~12 000 TPM):
  System prompt         :   ~300 tokens
  Top-5 chunks × 300 t :  ~1 500 tokens
  User question         :   ~100 tokens
  Output budget         :  1 000 tokens
  Total                 :  ~2 900 tokens  ← well within 12 000 TPM

Usage (as module)::

    from modules.rag_engine import index_folder, query_folder

    # Index once
    result = index_folder("MappingData/MappingData/")
    print(result)   # {"indexed": 12, "skipped": 0, "errors": []}

    # Query any number of times
    response, _ = query_folder("Which mappings use Nordstrom as receiver?")
    print(response)

Usage (standalone CLI)::

    python modules/rag_engine.py index  MappingData/MappingData/
    python modules/rag_engine.py query  "Which mappings use Nordstrom as receiver?"
    python modules/rag_engine.py query  "List all 810 invoice mappings" --top-k 8
"""

import json
import os
from pathlib import Path
from typing import Any, Optional, Tuple

from dotenv import load_dotenv
from groq import Groq

# Load .env from module directory or one level up
_here = Path(__file__).resolve().parent
for _candidate in [_here / ".env", _here.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# ── Constants ─────────────────────────────────────────────────────────────────

# File extensions that will be ingested when walking a folder
_SUPPORTED_EXTENSIONS = {".xml", ".xsl", ".xslt", ".xsd", ".edi", ".x12", ".edifact", ".txt"}

# Max characters extracted from each file's parsed content before embedding.
# ~2 000 chars ≈ 500 tokens — keeps embeddings focused and ChromaDB fast.
_MAX_CHUNK_CHARS = 2_000

# Sentence-transformers model — small (80 MB), fast, no API key needed.
_EMBED_MODEL = "all-MiniLM-L6-v2"

# Max output tokens for the LLM cross-file answer.
_MAX_OUTPUT_TOKENS = 1_000

# Default number of chunks to retrieve at query time.
_DEFAULT_TOP_K = 5

_SYSTEM_PROMPT = """\
You are an expert EDI/XSLT integration analyst. You have been given excerpts from \
multiple mapping files (XSLT stylesheets, EDI transactions, XSD schemas, etc.) \
retrieved from a knowledge base.

For each excerpt the source filename is shown. Use this information to answer the \
user's question accurately.

Rules:
- Always cite the specific file(s) your answer comes from.
- If the answer is not present in the provided excerpts, say so clearly — do not \
  hallucinate.
- Be concise but complete.
- When listing multiple files, use a bullet list.
"""


# ── Text extraction helpers ───────────────────────────────────────────────────

def _extract_text(ingested: dict) -> str:
    """
    Extract a compact text summary from an ingested file dict.
    Used as the document text stored in ChromaDB.
    """
    meta = ingested.get("metadata", {})
    parsed = ingested.get("parsed_content", {})
    file_type = meta.get("file_type", "UNKNOWN")
    filename = meta.get("filename", "unknown")

    parts = [f"File: {filename}", f"Type: {file_type}"]

    if file_type == "XSLT":
        # Include field_mappings, hardcoded_values, templates — same slice as simulate
        relevant = {
            k: parsed[k]
            for k in ("field_mappings", "hardcoded_values", "templates")
            if k in parsed and parsed[k]
        }
        if relevant:
            parts.append(json.dumps(relevant, default=str))
        elif parsed.get("raw_xml"):
            parts.append(parsed["raw_xml"][:1_000])

    elif file_type in ("XML", "XSD"):
        # Namespaces + root element summary
        if parsed.get("namespaces"):
            parts.append("Namespaces: " + json.dumps(parsed["namespaces"], default=str))
        if parsed.get("root"):
            root = parsed["root"]
            parts.append(f"Root tag: {root.get('tag', '')}")
            children = root.get("children", [])
            child_tags = [c.get("tag", "") for c in children[:10]]
            if child_tags:
                parts.append("Top-level elements: " + ", ".join(child_tags))
        if parsed.get("raw_xml"):
            parts.append(parsed["raw_xml"][:500])

    elif file_type == "X12_EDI":
        interchanges = parsed.get("interchanges", [])
        for ix in interchanges[:2]:
            isa = ix.get("isa", {})
            parts.append("ISA sender: " + str(isa.get("ISA06", "")))
            parts.append("ISA receiver: " + str(isa.get("ISA08", "")))
            for fg in ix.get("functional_groups", [])[:2]:
                for ts in fg.get("transaction_sets", [])[:2]:
                    segs = ts.get("segments", [])
                    parts.append("Segments: " + ", ".join(
                        str(s.get("id", "")) for s in segs[:20]
                    ))

    elif file_type == "EDIFACT":
        segs = parsed.get("segments", [])
        parts.append("Segments: " + ", ".join(str(s) for s in segs[:20]))

    else:
        # Fallback: raw text
        raw = parsed.get("raw_text", "") or parsed.get("raw_xml", "")
        if raw:
            parts.append(raw[:500])

    text = "\n".join(parts)
    if len(text) > _MAX_CHUNK_CHARS:
        text = text[:_MAX_CHUNK_CHARS] + "\n... [truncated]"
    return text


# ── Public API ────────────────────────────────────────────────────────────────

def index_folder(
    folder_path: str,
    persist_dir: str = ".rag_index",
    collection_name: str = "mappings",
    force_reindex: bool = False,
) -> dict:
    """
    Walk folder_path, ingest each supported file, embed its content, and
    store in a persistent ChromaDB collection.

    Args:
        folder_path:       Path to the folder containing mapping files.
        persist_dir:       Directory where ChromaDB persists its data.
                           Relative to CWD or absolute.
        collection_name:   ChromaDB collection name.
        force_reindex:     If True, re-index files already in the collection.

    Returns:
        {"indexed": int, "skipped": int, "errors": list[str]}

    Raises:
        ValueError: If folder_path does not exist or is not a directory.
    """
    folder = Path(folder_path).resolve()
    if not folder.exists():
        raise ValueError(f"folder_path does not exist: {folder}")
    if not folder.is_dir():
        raise ValueError(f"folder_path is not a directory: {folder}")

    # Lazy imports — avoid import-time cost when rag_engine is not used
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ImportError(
            f"RAG dependencies missing: {exc}. "
            "Run: pip install chromadb sentence-transformers"
        ) from exc

    try:
        from .file_ingestion import ingest_file, UnsupportedFileTypeError
    except ImportError:
        from file_ingestion import ingest_file, UnsupportedFileTypeError  # type: ignore

    persist_path = str(Path(persist_dir).resolve())
    client = chromadb.PersistentClient(path=persist_path)
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

    embed_model = SentenceTransformer(_EMBED_MODEL)

    # Collect existing IDs to support skip-if-already-indexed
    existing_ids: set = set()
    if not force_reindex:
        try:
            existing = collection.get(include=[])
            existing_ids = set(existing.get("ids", []))
        except Exception:
            existing_ids = set()

    indexed = 0
    skipped = 0
    errors: list = []

    for file_path in sorted(folder.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            continue

        doc_id = str(file_path.resolve())

        if doc_id in existing_ids:
            skipped += 1
            continue

        try:
            ingested = ingest_file(file_path=str(file_path))
        except UnsupportedFileTypeError:
            skipped += 1
            continue
        except Exception as exc:
            errors.append(f"{file_path.name}: ingest error — {exc}")
            continue

        if ingested["metadata"].get("parse_status") == "failed":
            errors.append(
                f"{file_path.name}: parse failed — "
                f"{ingested['metadata'].get('parse_error', 'unknown')}"
            )
            continue

        text = _extract_text(ingested)
        if not text.strip():
            skipped += 1
            continue

        embedding = embed_model.encode(text).tolist()

        collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{
                "filename":  ingested["metadata"].get("filename", file_path.name),
                "file_type": ingested["metadata"].get("file_type", "UNKNOWN"),
                "file_path": str(file_path),
                "folder":    str(folder),
            }],
        )
        indexed += 1

    return {"indexed": indexed, "skipped": skipped, "errors": errors}


def query_folder(
    question: str,
    persist_dir: str = ".rag_index",
    collection_name: str = "mappings",
    top_k: int = _DEFAULT_TOP_K,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[str, Any]:
    """
    Embed a question, retrieve the top-K most relevant mapping file chunks
    from ChromaDB, and ask Groq to answer based on those chunks.

    Args:
        question:          The user's question about the indexed mapping files.
        persist_dir:       ChromaDB persist directory (must match index_folder).
        collection_name:   ChromaDB collection name (must match index_folder).
        top_k:             Number of chunks to retrieve (default 5).
        api_key:           Groq API key. Falls back to GROQ_API_KEY env var.
        model:             Groq model. Falls back to GROQ_MODEL env var,
                           then llama-3.3-70b-versatile.

    Returns:
        (response_str, None) — same shape as all other engines.

    Raises:
        ValueError: If question is empty, no API key found, or index is empty.
    """
    if not question or not question.strip():
        raise ValueError("question must be a non-empty string")

    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        raise ValueError(
            "Groq API key required. Pass api_key= or set GROQ_API_KEY in .env"
        )

    resolved_model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ImportError(
            f"RAG dependencies missing: {exc}. "
            "Run: pip install chromadb sentence-transformers"
        ) from exc

    persist_path = str(Path(persist_dir).resolve())
    client = chromadb.PersistentClient(path=persist_path)

    try:
        collection = client.get_collection(name=collection_name)
    except Exception:
        raise ValueError(
            f"Collection '{collection_name}' not found in '{persist_dir}'. "
            "Run index_folder() first."
        )

    count = collection.count()
    if count == 0:
        raise ValueError(
            f"Collection '{collection_name}' is empty. "
            "Run index_folder() first."
        )

    embed_model = SentenceTransformer(_EMBED_MODEL)
    query_embedding = embed_model.encode(question).tolist()

    n_results = min(top_k, count)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )

    docs      = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]

    if not docs:
        return "No relevant mapping files found in the index for this question.", None

    # Build context from retrieved chunks
    context_parts = []
    for doc, meta in zip(docs, metadatas):
        fname = meta.get("filename", "unknown")
        ftype = meta.get("file_type", "")
        context_parts.append(
            f"### [{fname}] (type: {ftype})\n{doc}"
        )
    context = "\n\n".join(context_parts)

    user_message = (
        f"## Retrieved Mapping File Excerpts\n\n"
        f"{context}\n\n"
        f"## Question\n{question.strip()}"
    )

    groq_client = Groq(api_key=key)
    response = groq_client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        temperature=0.2,
        max_tokens=_MAX_OUTPUT_TOKENS,
    )

    return (response.choices[0].message.content or "").strip(), None


# ── CLI harness ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    def _usage() -> None:
        print("\nUsage:")
        print('  python modules/rag_engine.py index  <folder_path>')
        print('  python modules/rag_engine.py index  <folder_path> --force')
        print('  python modules/rag_engine.py query  "<question>"')
        print('  python modules/rag_engine.py query  "<question>" --top-k <N>')
        print()
        print("Examples:")
        print('  python modules/rag_engine.py index  MappingData/MappingData/')
        print('  python modules/rag_engine.py query  "Which mappings use Nordstrom?"')

    if len(sys.argv) < 3:
        _usage()
        sys.exit(0)

    command = sys.argv[1].lower()

    print("\n" + "=" * 80)
    print("  RAG ENGINE — Multi-File Mapping Knowledge Base")
    print("=" * 80 + "\n")

    if command == "index":
        folder_arg   = sys.argv[2]
        force_arg    = "--force" in sys.argv

        if not Path(folder_arg).exists():
            print(f"[ERROR] Folder not found: {folder_arg}")
            sys.exit(1)

        print(f"[INDEX] Folder : {folder_arg}")
        print(f"[INDEX] Force  : {force_arg}")
        print("[INDEX] Running...\n")

        result = index_folder(folder_arg, force_reindex=force_arg)

        print(f"[DONE ] Indexed : {result['indexed']}")
        print(f"[DONE ] Skipped : {result['skipped']}")
        if result["errors"]:
            print(f"[WARN ] Errors  : {len(result['errors'])}")
            for err in result["errors"]:
                print(f"        {err}")

    elif command == "query":
        question_arg = sys.argv[2]
        top_k_arg    = _DEFAULT_TOP_K
        if "--top-k" in sys.argv:
            try:
                top_k_arg = int(sys.argv[sys.argv.index("--top-k") + 1])
            except (IndexError, ValueError):
                print("[WARN] Invalid --top-k value, using default 5")

        print(f"[QUERY] {question_arg}")
        print(f"[TOP-K] {top_k_arg}\n")

        response, _ = query_folder(question_arg, top_k=top_k_arg)

        print("=" * 80)
        print(response)
        print("=" * 80 + "\n")

    else:
        print(f"[ERROR] Unknown command: {command}. Use 'index' or 'query'.")
        _usage()
        sys.exit(1)
