"""
File processing modules for EDI, XML, and XSLT parsing and analysis.
Includes intent routing, all five single-file engines (explain, simulate,
modify, generate, audit), and the RAG multi-file engine (index_folder, query_folder).
"""

from .file_ingestion import ingest_file, UnsupportedFileTypeError
from .file_agent import FileAgent
from .intent_router import route, get_meta, INTENT_META, ALL_INTENTS
from .groq_agent import explain
from .simulation_engine import simulate
from .modification_engine import modify
from .xslt_generator import generate
from .audit_engine import audit
from .rag_engine import index_folder, query_folder
from .dispatcher import dispatch, dispatch_folder

__all__ = [
    "ingest_file",
    "UnsupportedFileTypeError",
    "FileAgent",
    "route",
    "get_meta",
    "INTENT_META",
    "ALL_INTENTS",
    "explain",
    "simulate",
    "modify",
    "generate",
    "audit",
    "index_folder",
    "query_folder",
    "dispatch",
    "dispatch_folder",
]
