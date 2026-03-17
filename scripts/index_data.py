"""
scripts/index_data.py
─────────────────────
One-command RAG indexer for the Conversational Mapping Intelligence Agent.

Walks the `data/` folder at the repo root, ingests every supported mapping
file (XSLT, XML, XSD, EDI), creates embeddings, and stores them in the local
ChromaDB index at `.rag_index/`.

After indexing, the RAG engine can answer cross-file questions like:
  "Which mappings handle 810 invoices?"
  "Which XSLTs have hardcoded sender IDs?"

Usage
─────
    # Index new files only (incremental — skips already-indexed files):
    python scripts/index_data.py

    # Wipe the existing index and rebuild from scratch:
    python scripts/index_data.py --force

Supported file types
────────────────────
    .xml   .xsl   .xslt   .xsd   .edi   .txt (EDI content)

Files that fail to parse are logged as errors but do not stop the process.

How to add new mapping files
────────────────────────────
    1. Copy your XSLT / XML / XSD / EDI files into the data/ folder
       (any level of sub-folders is fine — the indexer recurses).
    2. Run:  python scripts/index_data.py
    3. The new files are indexed incrementally without re-processing files
       that were already indexed.
"""

import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `modules` is importable regardless
# of where this script is called from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from modules.rag_engine import index_folder  # noqa: E402  (after sys.path fix)

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_DIR  = _REPO_ROOT / "data"
INDEX_DIR = _REPO_ROOT / ".rag_index"

# ── CLI flags ──────────────────────────────────────────────────────────────────
force_rebuild = "--force" in sys.argv

# ── Pre-flight checks ──────────────────────────────────────────────────────────
print()
print("=" * 60)
print("  MAPPING DATA INDEXER")
print("=" * 60)
print(f"  Data folder  : {DATA_DIR}")
print(f"  Index folder : {INDEX_DIR}")
print(f"  Force rebuild: {force_rebuild}")
print()

if not DATA_DIR.exists():
    print("[ERROR] data/ folder not found.")
    print("        This should not happen if you cloned the repo correctly.")
    print("        Create it manually:  mkdir data")
    sys.exit(1)

# Check for any non-.gitkeep files
data_files = [
    p for p in DATA_DIR.rglob("*")
    if p.is_file() and p.name != ".gitkeep"
]

if not data_files:
    print("[INFO] data/ folder is empty.")
    print()
    print("  To get started:")
    print("    1. Copy your XSLT / XML / XSD / EDI mapping files into data/")
    print("    2. Re-run:  python scripts/index_data.py")
    print()
    sys.exit(0)

print(f"[INFO] Found {len(data_files)} file(s) in data/")
print()

# ── Index ──────────────────────────────────────────────────────────────────────
print("[INDEX] Running...")
print()

result = index_folder(
    folder_path=str(DATA_DIR),
    persist_dir=str(INDEX_DIR),
    force_reindex=force_rebuild,
)

# ── Summary ────────────────────────────────────────────────────────────────────
print("=" * 60)
print(f"  Indexed  : {result.get('indexed', 0):>4}  (new files added to index)")
print(f"  Skipped  : {result.get('skipped', 0):>4}  (already in index)")
errors = result.get("errors") or []
print(f"  Errors   : {len(errors):>4}  (parse failures — see below)")
print("=" * 60)
print()

if errors:
    print("[ERRORS] The following files could not be parsed:")
    for err in errors:
        print(f"  {err}")
    print()
    print("  These files are skipped — the index is still usable.")
    print("  Common causes: unsupported encoding, binary content, unknown format.")
    print()

if result.get("indexed", 0) > 0 or result.get("skipped", 0) > 0:
    print("[DONE] Index is ready. You can now query it with:")
    print()
    print("  from modules.dispatcher import dispatch_folder")
    print("  result = dispatch_folder(")
    print('      user_message="Which mappings handle 810 invoices?",')
    print('      folder_path="data",')
    print("  )")
    print("  print(result['primary_response'])")
    print()
