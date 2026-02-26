# Modules Directory

This directory contains the core modules for file processing, intent routing, and conversational AI.

## Structure

```
modules/
├── __init__.py           # Package exports
├── file_ingestion.py     # File parsing (X12, EDIFACT, XML, XSLT)
├── file_agent.py         # Conversational AI agent with Groq
└── intent_router.py      # Multi-intent classification
```

## Testing Modules from Terminal

All modules can be run standalone for testing. When run without arguments, they automatically use test files from the `test_files/` folder.

### 1. File Ingestion Module

**Test with files from `test_files/` folder (demo mode):**
```bash
python modules/file_ingestion.py
```
This will automatically load and parse the first test file found (e.g., `sample_850.edi`, `sample_catalog.xml`).

**Process a specific file:**
```bash
python modules/file_ingestion.py path/to/your/file.x12
python modules/file_ingestion.py test_files/sample_catalog.xml
python modules/file_ingestion.py test_files/sample_catalog.xsd
python modules/file_ingestion.py test_files/sample_orders.edifact
```

**Enhanced Version Extraction:**
- **X12 EDI**: ISA12 version + GS08 functional group version + ST transaction type
- **EDIFACT**: Syntax identifier (UNOC:3) + message type and version (ORDERS:96A)
- **XML**: XML version + root element + document version + schema info + namespace standards
- **XSD**: XSD version + target namespace + element count + complex type count + simple type count
- **XSLT**: XSLT version + template count + function count + import/include count

### 2. File Agent Module

**Test with files from `test_files/` folder (interactive demo):**
```bash
python modules/file_agent.py
```
This will automatically load the first test file, generate an AI explanation, and start an interactive chat.

**Load and analyze a specific file:**
```bash
python modules/file_agent.py path/to/your/file.edi
python modules/file_agent.py test_files/sample_850.edi
python modules/file_agent.py test_files/sample_catalog.xml
python modules/file_agent.py test_files/sample_catalog.xsd
```

Then ask questions interactively. Type `quit` or `exit` to stop.

### 3. Intent Router Module

Run the multi-intent test suite:
```bash
python modules/intent_router.py
```

This will classify 8 test messages across 4 intent categories:
- **explain**: Understand/describe a mapping or file
- **generate**: Create new XSLT/EDI mapping from scratch
- **modify**: Edit/update an existing mapping
- **simulate**: Run/test/validate mapping against data

## Supported File Types

The modules support parsing and analysis of the following file formats:

| Format | Extension(s) | Parser | Status |
|--------|-------------|--------|--------|
| **X12 EDI** | `.edi`, `.x12`, `.txt` | `pyx12` | ⚠️ Python 3.12 or earlier |
| **EDIFACT** | `.edifact` | `pydifact` | ✅ Working |
| **XML** | `.xml` | `lxml` | ✅ Working |
| **XSD** | `.xsd` | `lxml` | ✅ Working |
| **XSLT** | `.xsl`, `.xslt` | `lxml` | ✅ Working |

## Importing in Python

```python
from modules import ingest_file, FileAgent, route

# Parse a file
ingested = ingest_file(file_path="sample.x12")

# Create conversational agent
agent = FileAgent()
explanation = agent.load_file(ingested)
response = agent.chat("What is the sender ID?")

# Route user intent
result = route("Explain the BEG segment, then modify it")
print(result["active_intents"])  # ['explain', 'modify']
```

## Requirements

All dependencies are installed in `.venv`:
- `groq` - LLM API for conversational AI
- `python-dotenv` - Environment variable management
- `pydifact` - EDIFACT parsing ✅ Working
- `pyx12` - X12 EDI parsing ⚠️ Requires Python 3.12 or earlier (pkg_resources issue)
- `lxml` - XML/XSLT parsing ✅ Working
- `setuptools` - Required by pyx12

API keys are loaded from `.env` file automatically.

### Known Issues

**X12 EDI Parsing:** The `pyx12` library depends on `pkg_resources` which was deprecated in Python 3.12+ and removed in Python 3.13. If X12 parsing fails, the module will still return the raw file content for LLM processing. Alternative: Use Python 3.12 or earlier, or wait for pyx12 update.

**Workaround:** XML and EDIFACT parsing work perfectly. X12 files can still be analyzed using the raw text output sent to the LLM.
