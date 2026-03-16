"""
File processing modules for EDI, XML, and XSLT parsing and analysis.
Includes intent routing, explain engine, simulate engine, and modify engine.
"""

from .file_ingestion import ingest_file, UnsupportedFileTypeError
from .file_agent import FileAgent
from .intent_router import route, get_meta, INTENT_META, ALL_INTENTS
from .groq_agent import explain
from .simulation_engine import simulate
from .modification_engine import modify
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
    "modify",
    "dispatch",
]
