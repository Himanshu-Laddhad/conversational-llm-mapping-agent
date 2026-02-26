"""
File Agent Module

Accept an ingested dict from file_ingestion, maintain conversation history,
and use Groq to explain the file and answer follow-up questions with full
context retention.
"""

import json
import os
from typing import Optional, Iterator
from groq import Groq
from dotenv import load_dotenv


class FileAgent:
    """
    Conversational agent for explaining and analyzing parsed EDI, XML, and XSLT files.
    """
    
    def __init__(
        self,
        groq_api_key: Optional[str] = None,
        model: str = "llama-3.3-70b-versatile"
    ):
        """
        Initialize the FileAgent.
        
        Args:
            groq_api_key: Groq API key (loads from .env if not provided)
            model: Groq model to use for chat completions
        """
        # Load environment variables
        load_dotenv()
        
        # Get API key from argument or environment
        api_key = groq_api_key or os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError(
                "GROQ_API_KEY must be provided as argument or set in .env file"
            )
        
        # Initialize Groq client
        self.client = Groq(api_key=api_key)
        self.model = model
        
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
        """
        # Store metadata
        self.file_metadata = ingested.get("metadata", {})
        
        # Reset conversation history
        self.history = []
        
        # Serialize the full ingested dict to JSON
        json_string = json.dumps(ingested, indent=2, ensure_ascii=False)
        
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
            stream: If True, return streaming generator; if False, return full response
        
        Returns:
            If stream=False: response string
            If stream=True: generator yielding response chunks (caller must call
                           append_assistant_message after consuming stream)
        """
        # Append user message to history
        self.history.append({
            "role": "user",
            "content": user_message
        })
        
        # Call Groq API
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            stream=stream
        )
        
        if stream:
            # Return generator for streaming
            return completion
        else:
            # Extract full response
            response = completion.choices[0].message.content
            
            # Append assistant response to history
            self.history.append({
                "role": "assistant",
                "content": response
            })
            
            return response
    
    def append_assistant_message(self, text: str):
        """
        Manually append an assistant message to history.
        
        Use this after consuming a streaming response to persist it to history.
        
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
