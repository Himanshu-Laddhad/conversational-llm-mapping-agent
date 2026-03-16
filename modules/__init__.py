"""
File processing modules for EDI, XML, and XSLT parsing and analysis.
Includes intent routing, explain engine, and simulate engine.
"""

from .file_ingestion import ingest_file, UnsupportedFileTypeError
from .file_agent import FileAgent
from .intent_router import route, get_meta, INTENT_META, ALL_INTENTS
from .groq_agent import explain
from .simulation_engine import simulate
from .dispatcher import dispatch

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
    "dispatch",
]
