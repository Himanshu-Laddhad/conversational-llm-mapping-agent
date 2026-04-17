"""
File Agent Module

Accept an ingested dict from file_ingestion, maintain conversation history,
and use Groq to explain the file and answer follow-up questions with full
context retention.
"""

import json
import os
import time
from typing import Optional, Iterator
from groq import Groq
from dotenv import load_dotenv

from modules.usage_tracker import log_usage


class FileAgent:
    """
    Conversational agent for explaining and analyzing parsed EDI, XML, and XSLT files.
    """
    
    def __init__(
        self,
        groq_api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,       # alias used by groq_agent.py
        provider: Optional[str] = None,      # accepted for compat; Groq only for now
    ):
        """
        Initialize the FileAgent.

        Args:
            groq_api_key: Groq API key (loads from .env if not provided)
            api_key:      Alias for groq_api_key (used by collaborator groq_agent.py)
            model:        Groq model to use. Falls back to GROQ_MODEL env var.
            provider:     LLM provider string (accepted but ignored; Groq only).
        """
        # Load environment variables
        load_dotenv()

        # Get API key from argument or environment (support both param names)
        api_key = api_key or groq_api_key or os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError(
                "GROQ_API_KEY must be provided as argument or set in .env file"
            )
        
        # Initialize Groq client
        self.client = Groq(api_key=api_key)
        self.model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        
        # Initialize conversation state
        self.history = []
        self.file_metadata = None
    
    def load_file(self, ingested: dict) -> str:
        """
        Load a parsed file and generate an initial explanation.
        
        Args:
            ingested: Output dict from file_ingestion.ingest_file()
        
        Returns:
            Initial explanation string
        
        Raises:
            TypeError: If ingested is not a dict.
            ValueError: If ingested is missing required keys.
        """
        if not isinstance(ingested, dict):
            raise TypeError(
                f"ingested must be a dict from ingest_file(), got {type(ingested).__name__}"
            )
        if "parsed_content" not in ingested:
            raise ValueError(
                "ingested dict is missing 'parsed_content' key — "
                "pass the return value of ingest_file() directly"
            )

        # Store metadata
        self.file_metadata = ingested.get("metadata", {})
        
        # Reset conversation history
        self.history = []

        # ── Token-budget enforcement ──────────────────────────────────────────
        # Groq on-demand free tier: 12 000 TPM (tokens per minute).
        # 1 token ≈ 4 chars.  The system prompt + XSLT instructions already
        # consume ~2 000 tokens, leaving ~8 000 tokens for the file JSON.
        # Cap raw_xml/raw_text at 6 000 chars (~1 500 tokens) and the whole
        # serialised JSON at 24 000 chars (~6 000 tokens) to stay safely under
        # the 12 000 TPM limit even with conversation history building up.
        _MAX_RAW_FIELD  = 6_000    # per raw_xml / raw_text field
        _MAX_JSON_TOTAL = 24_000   # entire serialised ingested JSON

        ingested_for_prompt = ingested.copy()
        if isinstance(ingested_for_prompt.get("parsed_content"), dict):
            pc = dict(ingested_for_prompt["parsed_content"])
            for _field in ("raw_xml", "raw_text"):
                if isinstance(pc.get(_field), str) and len(pc[_field]) > _MAX_RAW_FIELD:
                    pc[_field] = (
                        pc[_field][:_MAX_RAW_FIELD]
                        + f"\n... [truncated — {len(pc[_field])} total chars; "
                        "full source available for simulation/modify]"
                    )
            ingested_for_prompt["parsed_content"] = pc

        # Serialize and enforce the overall JSON cap
        json_string = json.dumps(ingested_for_prompt, indent=2, ensure_ascii=False)
        if len(json_string) > _MAX_JSON_TOTAL:
            json_string = (
                json_string[:_MAX_JSON_TOTAL]
                + f'\n  "...": "[truncated at {_MAX_JSON_TOTAL} chars to fit token budget]"'
            )
        
        # Extract metadata for system message
        file_type = self.file_metadata.get("file_type", "unknown")
        detected_version = self.file_metadata.get("detected_version", "unknown")
        parse_status = self.file_metadata.get("parse_status", "unknown")
        parse_error = self.file_metadata.get("parse_error")
        
        # Build system message
        system_content = f"""You are an expert in EDI, XML, and XSLT file formats and B2B integration standards including X12 EDI, EDIFACT, and XML-based APIs. The user has uploaded a {file_type} file (version: {detected_version}). Here is its fully parsed structure as JSON:

<parsed_file>
{json_string}
</parsed_file>"""
        
        if parse_status == "failed" and parse_error:
            system_content += f"""

NOTE: The file parsing failed with error: {parse_error}
The raw text content is included in the parsed_file JSON for your reference."""
        
        system_content += """

When first responding, explain this file clearly in plain English: what type it is, what standard it follows, what its overall purpose appears to be, and a breakdown of each major section or segment with its meaning and notable values. Be specific about segment IDs, element values, and structure where relevant."""

        if file_type == "XSLT":
            system_content += """

Because this is an XSLT mapping stylesheet, structure your explanation to cover ALL of the following sections. Use template_call_graph, entry_points, mode_index, hardcoded_values, and global_variables from the parsed JSON to give specific, accurate answers for each section:

1. TRANSFORMATION SUMMARY
   - One sentence stating what this stylesheet converts (source format → target format).
   - State the XSLT version, output method (XML/text/HTML), and any namespace declarations.
   - List every imported or included stylesheet (imports_includes) and what role each plays.

2. ENTRY POINTS AND EXECUTION FLOW
   - Identify the entry-point templates from the entry_points list (match="/" or named templates not called by others).
   - Walk through the execution flow step by step: which template fires first, what it produces or calls, and how control passes to child templates via xsl:call-template and xsl:apply-templates.
   - Show the complete call chain as an indented tree. Example:
       match="/" → calls: build_envelope
         build_envelope → calls: build_isa, build_gs, build_st
           build_isa → calls: format_date
   - If multiple modes exist (from mode_index), explain what triggers each mode and which templates handle it.

3. TEMPLATE RELATIONSHIP MAP
   - For each template in template_call_graph, state:
     a) Identity: match pattern OR template name
     b) What it receives: params_accepted (name, default, required)
     c) What it calls: calls list (callee names + with_params passed to each)
     d) What it dispatches to: applies list (select path + mode)
     e) What it produces: output_elements (literal XML/EDI elements it creates)
   - Highlight any templates that are defined but never called — potential dead code or orphan templates.

4. FIELD MAPPING TABLE
   - For every xsl:value-of expression in each template (value_of list), create a mapping row:
       Source XPath  |  Template  |  Output Field / EDI Segment+Element
   - Group rows by output EDI segment where possible (e.g., all ISA fields together, all GS fields together).
   - For xsl:for-each loops (for_each list), state what input node set is iterated and what repeating output structure it produces (e.g., one HL loop per line item).

5. VARIABLE AND PARAMETER DEPENDENCY
   - List all global_variables and global_params: name, select expression, and what business value they hold.
   - For each template, note which variables it references (variables_used) and whether they are global or local (local_variables).
   - Identify variables used across multiple templates — these are shared/key business values (e.g., sender ID, date format string).
   - Flag any $variable reference that has no matching declared variable — these are potential bugs.

6. CONDITIONAL AND BUSINESS LOGIC
   - For each template's conditionals (if/when/otherwise), explain in plain English what business rule the test expression implements.
   - Explain any value-translation logic (e.g., "if source type='PO' output qualifier 'NE', else 'RE'").
   - Identify choose/when chains acting as lookup tables (e.g., mapping order type codes to EDI qualifier values).
   - Flag any otherwise branches producing hardcoded default or fallback values.

7. HARDCODED VALUES
   - List every entry in hardcoded_values: the literal value, where it appears, and its business meaning.
   - Group by category: EDI qualifiers, trading-partner/account IDs, currency codes, date formats, version strings, and other constants.
   - Flag any hardcoded sender/receiver IDs or account numbers — these typically need to be parameterized for multi-partner deployments.

8. SEGMENT-LEVEL TRANSFORMATION WALKTHROUGH
   - Walk through each major EDI segment or output section this stylesheet produces.
   - For each segment state: which template produces it, which source XPath fields feed each element, and any business rules applied.
   - Use this format:
       ISA segment → produced by: [template name]
         ISA01 (auth qualifier) = hardcoded '00'
         ISA06 (sender ID)      = $senderID (global param)
         ISA09 (date)           = format-date(current-date(), '[Y0001][M01][D01]')"""

        if file_type == "D365_XML":
            system_content += """

Because this is a Microsoft Dynamics 365 (D365/AX) ERP XML output, structure your explanation to cover ALL of the following:
1. SOURCE SYSTEM IDENTIFICATION — Confirm this is Microsoft Dynamics 365 (AX) ERP data. Identify the document type (Customer Invoice, Sales Order, etc.) and the target EDI transaction it is intended to map to (e.g., X12 810 Invoice, X12 856 Ship Notice/ASN).
2. INVOICE / DOCUMENT HEADER — Extract and explain every header field with its actual value: InvoiceId, SalesId, InvoiceDate, InvoiceAmount, currency, payment terms (PaymnetTermDays, PaymnetTermDescription, PaymnetTermCode), DueDate, ParmId, LedgerVoucher, SalesOriginId, CustomerRef.
3. CUSTOMER AND DELIVERY INFORMATION — Extract with actual values: ExternalLocationID (trading-partner location), LocationId (internal customer account), DeliveryName, and all four address blocks — ShipTo (SalesOrderHeaderAddress), ShipFrom (ShipFromAddress), Vendor (VendorAddress), BillTo (BTAddress/InvoiceAccountAddress). State street, city, state, zip, and phone for each.
4. LINE ITEM DETAILS — For each custInvoiceTrans entry list: ItemId (internal SKU), ExternalItemId (customer's item number / ASIN), Barcode, Name (full product description), Qty, SalesUnit, SalesPrice (unit price), LineAmountMST (extended line total), DiscPercent, OrigCountryRegionId, DlvDate.
5. SHIPMENT AND CARRIER — With actual values: ShipmentID, CarrierName, DlvMode (carrier service code), ShipCarrierTrackingNum, TotalNoOfCartons, TotalShipmentofOrders (weight), ShipmentArrivalUTCDateTime.
6. D365-TO-EDI FIELD MAPPING CONTEXT — For each major D365 field, state which X12 EDI segment and element it maps to. Examples: InvoiceId → BIG02 (810) or BSN02 (856), SalesId → REF*CO, InvoiceAmount → TDS01, SalesPrice → IT104, Qty → IT102, CustomerRef → REF*PO, ShipCarrierTrackingNum → REF*CN, DlvMode → TD504, ExternalLocationID → N104 (ship-to)."""

        if file_type == "X12_XML":
            system_content += """

Because this is an X12 XML file generated by Altova MapForce (an XML representation of an X12 EDI transaction), structure your explanation to cover ALL of the following:
1. TRANSACTION IDENTIFICATION — Identify the transaction type from the root element name (e.g., X12_00401_856 = X12 version 00401, Transaction Set 856 Ship Notice/ASN). State the full business purpose of this transaction type and what business event it communicates.
2. ISA ENVELOPE — Extract and explain with actual values: ISA05/ISA06 (Sender Qualifier and ID — who sent the message), ISA07/ISA08 (Receiver Qualifier and ID — who receives it), ISA09/ISA10 (interchange date and time), ISA12 (Version/Release number), ISA13 (Interchange Control Number), ISA15 (Usage Indicator: P=Production, T=Test).
3. GS FUNCTIONAL GROUP — With actual values: GS01 (Functional ID Code), GS02/GS03 (Sender/Receiver Application IDs), GS04/GS05 (Date/Time), GS06 (Group Control Number), GS08 (Version/Release/Industry Code).
4. TRANSACTION-SPECIFIC SEGMENTS — For 856 (Ship Notice): BSN segment with Shipment ID (BSN02), ship date (BSN03), time (BSN04), and hierarchical structure code (BSN05). For 810 (Invoice): BIG with invoice date, invoice number, PO date, PO number. For 850 (Purchase Order): BEG with PO number, order type, date. Extract all actual field values.
5. HL LOOP HIERARCHY — Describe each Hierarchical Level present (S=Shipment, O=Order, P=Pack, I=Item) and what business entity it represents. Explain the parent-child relationships shown by HL01 (ID) and HL02 (Parent ID).
6. LINE ITEMS AND REFERENCE DATA — With actual values from the file: LIN product IDs (VN=Vendor Part#, SK=SKU/Buyer's Part#), SN1 quantities (quantity shipped, unit of measure, line status); REF segments (CN=Carrier Tracking#, IV=Invoice#, PO=PO#); MAN marks/SSCC-18 carton labels (CP qualifier); PRF purchase order references; TD5 carrier name and routing sequence; CTT transaction line count."""
        
        # Add system message
        self.history.append({
            "role": "system",
            "content": system_content
        })
        
        # Generate initial explanation
        explanation = self.chat("Please explain this file.")
        
        return explanation
    
    def chat(
        self,
        user_message: str,
        stream: bool = False
    ):
        """
        Send a message and get a response from the agent.
        
        Args:
            user_message: User's message
            stream: If True, return a generator that yields raw Groq chunks and
                    automatically appends the full assembled reply to history once
                    the stream is exhausted. If False, return full response string.
        
        Returns:
            If stream=False: response string
            If stream=True: generator yielding Groq stream chunks; history is
                            updated automatically — no need to call
                            append_assistant_message() after consuming the stream.
        """
        # Append user message to history
        self.history.append({
            "role": "user",
            "content": user_message
        })
        
        if stream:
            # Request usage data in the final stream chunk
            t0 = time.perf_counter()
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=self.history,
                stream=True,
                stream_options={"include_usage": True},
            )

            def _stream_and_record():
                chunks = []
                last_usage = None
                call_latency_ms = (time.perf_counter() - t0) * 1000
                for chunk in completion:
                    text = chunk.choices[0].delta.content or "" if chunk.choices else ""
                    if text:
                        chunks.append(text)
                    if getattr(chunk, "usage", None) is not None:
                        last_usage = chunk.usage
                    yield chunk
                self.history.append({
                    "role": "assistant",
                    "content": "".join(chunks),
                })
                if last_usage is not None:
                    log_usage(
                        provider="groq",
                        model=self.model,
                        caller="file_agent",
                        prompt_tokens=last_usage.prompt_tokens,
                        completion_tokens=last_usage.completion_tokens,
                        total_tokens=last_usage.total_tokens,
                        max_tokens=0,
                        temperature=0.0,
                        latency_ms=call_latency_ms,
                    )

            return _stream_and_record()
        else:
            # Non-streaming: usage is available directly on the response object
            t0 = time.perf_counter()
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=self.history,
                stream=False,
            )
            latency_ms = (time.perf_counter() - t0) * 1000

            response = completion.choices[0].message.content

            self.history.append({
                "role": "assistant",
                "content": response,
            })

            usage = getattr(completion, "usage", None)
            if usage is not None:
                log_usage(
                    provider="groq",
                    model=self.model,
                    caller="file_agent",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    max_tokens=0,
                    temperature=0.0,
                    latency_ms=latency_ms,
                )

            return response
    
    def append_assistant_message(self, text: str):
        """
        Manually append an assistant message to history.
        
        Useful when injecting assistant text that was generated outside of
        chat() (e.g. a pre-canned reply or text from another LLM call).
        Streaming responses via chat(stream=True) are recorded automatically
        and do not require this method.
        
        Args:
            text: Full assistant response text
        """
        self.history.append({
            "role": "assistant",
            "content": text
        })
    
    def reset(self):
        """
        Clear conversation history and file metadata.
        """
        self.history = []
        self.file_metadata = None
    
    @property
    def history(self) -> list:
        """
        Get the current conversation history.
        
        Returns:
            List of message dicts with "role" and "content" keys
        """
        return self._history
    
    @history.setter
    def history(self, value: list):
        """Set the conversation history."""
        self._history = value


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path
    
    # Import file_ingestion from same directory
    try:
        from file_ingestion import ingest_file
    except ImportError:
        print("[ERROR] Could not import file_ingestion module")
        print("        Make sure you're running from the project root:")
        print("        python modules/file_agent.py <file_path>")
        sys.exit(1)
    
    print("\n" + "="*80)
    print("  FILE AGENT MODULE - Interactive Test")
    print("="*80 + "\n")
    
    if len(sys.argv) < 2:
        # Demo mode: use file from test_files folder
        test_files_dir = Path(__file__).parent.parent / "test_files"
        
        if not test_files_dir.exists():
            print("[ERROR] test_files directory not found\n")
            print("Expected location:", test_files_dir)
            print("\nPlease create test_files/ folder with sample files or run with a file path:")
            print("   python modules/file_agent.py path/to/your/file\n")
            sys.exit(1)
        
        # Find test files (prefer EDI files first, then XML/XSD/XSLT)
        test_files = []
        for ext in ['*.edi', '*.x12', '*.edifact', '*.xml', '*.xsd', '*.xsl', '*.xslt']:
            test_files.extend(test_files_dir.glob(ext))
        
        if not test_files:
            print("[WARN] No test files found in test_files/\n")
            print("Expected file types: .edi, .x12, .edifact, .xml, .xsd, .xsl, .xslt\n")
            sys.exit(1)
        
        # Use first test file
        test_file = test_files[0]
        
        print(f"[DEMO] Loading test file: {test_file.name}\n")
        print(f"Usage: python modules/file_agent.py <path_to_file>\n")
        print("-" * 80 + "\n")
        
        print(f"[FILE] Processing: {test_file}\n")
        
        # Ingest the test file
        try:
            ingested = ingest_file(file_path=str(test_file))
            
            print("[OK] File ingested successfully")
            print(f"   Type: {ingested['metadata']['file_type']}")
            print(f"   Version: {ingested['metadata']['detected_version']}")
            print(f"   Status: {ingested['metadata']['parse_status']}\n")
            print("-" * 80 + "\n")
            
            # Create agent and load file
            agent = FileAgent()
            print("[AGENT] Initializing File Agent with Groq...\n")
            
            explanation = agent.load_file(ingested)
            print("[EXPLANATION] Initial file analysis:")
            print("-" * 80)
            print(explanation)
            print("\n" + "-" * 80 + "\n")
            
            # Show other available test files
            if len(test_files) > 1:
                print("[INFO] Other test files available:")
                for tf in test_files[1:]:
                    print(f"        - {tf.name}")
                print(f"\n        Test any file with: python modules/file_agent.py test_files/<filename>\n")
                print("-" * 80 + "\n")
            
            # Interactive Q&A
            print("[CHAT] You can now ask questions about the file.")
            print("       Type 'quit' or 'exit' to stop.\n")
            print("="*80 + "\n")
            
            while True:
                try:
                    user_input = input("You: ").strip()
                    if not user_input:
                        continue
                    if user_input.lower() in ['quit', 'exit', 'q']:
                        print("\nGoodbye!\n")
                        break
                    
                    response = agent.chat(user_input)
                    print(f"\nAgent: {response}\n")
                    print("-" * 80 + "\n")
                    
                except KeyboardInterrupt:
                    print("\n\nGoodbye!\n")
                    break
                except Exception as e:
                    print(f"\n[ERROR] {e}\n")
            
        except Exception as e:
            print(f"[ERROR] {e}\n")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    
    else:
        # User provided a file path
        file_path = sys.argv[1]
        
        if not Path(file_path).exists():
            print(f"[ERROR] File not found: {file_path}\n")
            sys.exit(1)
        
        print(f"[FILE] Loading: {file_path}\n")
        print("-" * 80 + "\n")
        
        try:
            # Ingest the file
            ingested = ingest_file(file_path=file_path)
            
            print("[OK] File ingested successfully")
            print(f"     Type: {ingested['metadata']['file_type']}")
            print(f"     Version: {ingested['metadata']['detected_version']}")
            print(f"     Status: {ingested['metadata']['parse_status']}")
            
            if ingested['metadata']['parse_error']:
                print(f"     [WARN] Parse Error: {ingested['metadata']['parse_error']}")
            
            print("\n" + "-" * 80 + "\n")
            
            # Create agent and load file
            agent = FileAgent()
            print("[AGENT] Initializing File Agent with Groq...\n")
            
            explanation = agent.load_file(ingested)
            print("[EXPLANATION] Initial file analysis:")
            print("-" * 80)
            print(explanation)
            print("\n" + "-" * 80 + "\n")
            
            # Interactive Q&A
            print("[CHAT] You can now ask questions about the file.")
            print("       Type 'quit' or 'exit' to stop.\n")
            print("="*80 + "\n")
            
            while True:
                try:
                    user_input = input("You: ").strip()
                    if not user_input:
                        continue
                    if user_input.lower() in ['quit', 'exit', 'q']:
                        print("\nGoodbye!\n")
                        break
                    
                    response = agent.chat(user_input)
                    print(f"\nAgent: {response}\n")
                    print("-" * 80 + "\n")
                    
                except KeyboardInterrupt:
                    print("\n\nGoodbye!\n")
                    break
                except Exception as e:
                    print(f"\n[ERROR] {e}\n")
        
        except Exception as e:
            print(f"[ERROR] {e}\n")
            import traceback
            traceback.print_exc()
            sys.exit(1)
