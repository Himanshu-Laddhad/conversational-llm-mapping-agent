"""
File Ingestion Module

Accept a file path or raw bytes + filename, detect file type and version,
parse it, and return a standardized JSON-serializable Python dict ready
for LLM consumption.
"""

import os
from typing import Optional, Tuple
from lxml import etree


class UnsupportedFileTypeError(Exception):
    """Raised when file type cannot be detected or is not supported."""
    pass


def detect_file_type(filename: str, raw_text: str) -> Tuple[str, str]:
    """
    Detect file type and version from filename and content.
    
    Returns:
        (file_type, version) tuple where file_type is one of:
        "X12_EDI", "EDIFACT", "XSLT", "XSD", "XML"
    """
    ext = os.path.splitext(filename)[1].lower() if filename else ""
    content_start = raw_text.strip()[:200].upper() if raw_text else ""
    
    # X12 EDI detection
    if content_start.startswith("ISA") or ext in [".x12", ".edi", ".txt"]:
        if raw_text and "ISA" in raw_text[:500]:
            try:
                # ISA segment is 106 characters fixed width
                isa_start = raw_text.find("ISA")
                if isa_start >= 0:
                    isa_line = raw_text[isa_start:isa_start+106]
                    
                    # Element separator is position 3 (ISA*...)
                    elem_sep = isa_line[3] if len(isa_line) > 3 else "*"
                    
                    # Parse ISA elements
                    elements = isa_line.split(elem_sep)
                    if len(elements) >= 13:
                        # ISA12 is the version/standards identifier
                        version = elements[12].strip()
                        
                        # Also extract transaction set info for more context
                        # Look for GS and ST segments
                        additional_info = []
                        
                        # Find GS segment for functional group version
                        gs_match = raw_text.find("GS" + elem_sep)
                        if gs_match >= 0:
                            gs_line = raw_text[gs_match:gs_match+200].split(elem_sep)
                            if len(gs_line) >= 9:
                                # GS08 contains version/release/industry code
                                gs_version = gs_line[8].strip()
                                additional_info.append(f"GS08:{gs_version}")
                        
                        # Find ST segment for transaction set type
                        st_match = raw_text.find("ST" + elem_sep)
                        if st_match >= 0:
                            st_line = raw_text[st_match:st_match+100].split(elem_sep)
                            if len(st_line) >= 2:
                                transaction_type = st_line[1].strip()
                                additional_info.append(f"TS:{transaction_type}")
                        
                        version_str = version if version else "unknown"
                        if additional_info:
                            version_str += " (" + ", ".join(additional_info) + ")"
                        
                        return ("X12_EDI", version_str)
            except Exception:
                pass
        # Only hard-classify as X12 for dedicated EDI extensions.
        # .txt files without a valid ISA header fall through to other detectors
        # (e.g. D365 XML files that happen to have a .txt extension).
        if ext in [".x12", ".edi"]:
            return ("X12_EDI", "unknown")
    
    # EDIFACT detection
    if content_start.startswith("UNA") or content_start.startswith("UNB") or ext == ".edifact":
        try:
            # Detect component/element/segment separators
            if raw_text.startswith("UNA"):
                # UNA defines separators: UNA:+.? ' (component:element.decimal?segment'release)
                comp_sep = raw_text[3] if len(raw_text) > 3 else ":"
                elem_sep = raw_text[4] if len(raw_text) > 4 else "+"
                seg_term = raw_text[8] if len(raw_text) > 8 else "'"
            else:
                comp_sep, elem_sep, seg_term = ":", "+", "'"
            
            # Find UNB segment
            unb_start = raw_text.find("UNB")
            if unb_start >= 0:
                unb_segment = raw_text[unb_start:unb_start+300].split(seg_term)[0]
                
                # UNB structure: UNB+SYNTAXID:SYNTAXVER+SENDER+RECEIVER+DATE:TIME+CONTROLREF...
                parts = unb_segment.split(elem_sep)
                
                version_info = []
                
                # UNB[1]: Syntax identifier (e.g., UNOC:3)
                if len(parts) > 1:
                    syntax = parts[1].split(comp_sep)
                    if len(syntax) >= 2:
                        version_info.append(f"{syntax[0]}:{syntax[1]}")
                    elif len(syntax) == 1:
                        version_info.append(syntax[0])
                
                # Look for UNH for message type and version
                unh_start = raw_text.find("UNH")
                if unh_start >= 0:
                    unh_segment = raw_text[unh_start:unh_start+300].split(seg_term)[0]
                    unh_parts = unh_segment.split(elem_sep)
                    # UNH[2] contains message type (e.g., ORDERS:D:96A:UN)
                    if len(unh_parts) > 2:
                        msg_type = unh_parts[2].split(comp_sep)
                        if len(msg_type) >= 3:
                            version_info.append(f"{msg_type[0]}:{msg_type[2]}")
                
                version_str = " | ".join(version_info) if version_info else "unknown"
                return ("EDIFACT", version_str)
        except Exception:
            pass
        return ("EDIFACT", "unknown")
    
    # XSLT detection
    if ext in [".xsl", ".xslt"] or "xsl:stylesheet" in raw_text[:500] or "xsl:transform" in raw_text[:500]:
        try:
            tree = etree.fromstring(raw_text.encode('utf-8'))
            version_info = []
            
            # Look for xsl:stylesheet or xsl:transform root element
            if tree.tag.endswith("}stylesheet") or tree.tag.endswith("}transform"):
                xsl_version = tree.get("version", "1.0")
                version_info.append(f"XSLT:{xsl_version}")
                
                # Check for XSLT 2.0/3.0 specific elements
                xsl_ns = "http://www.w3.org/1999/XSL/Transform"
                
                # Count key XSLT constructs for additional context
                templates = len(tree.findall(".//{*}template"))
                if templates > 0:
                    version_info.append(f"{templates}T")  # T = templates
                
                functions = len(tree.findall(".//{*}function"))
                if functions > 0:
                    version_info.append(f"{functions}F")  # F = functions
                
                imports = len(tree.findall(".//{*}import")) + len(tree.findall(".//{*}include"))
                if imports > 0:
                    version_info.append(f"{imports}I")  # I = imports/includes
                
                version_str = " | ".join(version_info)
                return ("XSLT", version_str)
        except Exception:
            pass
        return ("XSLT", "unknown")
    
    # XSD (XML Schema Definition) detection
    if ext == ".xsd" or "xs:schema" in raw_text[:500] or "xsd:schema" in raw_text[:500]:
        try:
            tree = etree.fromstring(raw_text.encode('utf-8'))
            version_info = []
            
            # Check if root element is xs:schema or xsd:schema
            if tree.tag.endswith("}schema") or tree.tag == "schema":
                # Extract schema version/namespace
                target_ns = tree.get("targetNamespace", "")
                if target_ns:
                    # Extract meaningful part from namespace
                    ns_parts = target_ns.rstrip("/").split("/")
                    if ns_parts:
                        version_info.append(f"NS:{ns_parts[-1]}")
                
                # Get schema version attribute if present
                schema_version = tree.get("version")
                if schema_version:
                    version_info.append(f"Ver:{schema_version}")
                
                # Count key schema elements
                element_count = len(tree.findall(".//{*}element"))
                if element_count > 0:
                    version_info.append(f"{element_count}E")  # E = elements
                
                complex_type_count = len(tree.findall(".//{*}complexType"))
                if complex_type_count > 0:
                    version_info.append(f"{complex_type_count}CT")  # CT = complex types
                
                simple_type_count = len(tree.findall(".//{*}simpleType"))
                if simple_type_count > 0:
                    version_info.append(f"{simple_type_count}ST")  # ST = simple types
                
                # Extract XML Schema version from namespace
                nsmap = tree.nsmap if hasattr(tree, 'nsmap') else {}
                for prefix, uri in nsmap.items():
                    if "XMLSchema" in uri:
                        if "2001" in uri:
                            version_info.insert(0, "XSD:1.0")
                        elif "1999" in uri:
                            version_info.insert(0, "XSD:1999")
                        break
                
                version_str = " | ".join(version_info) if version_info else "1.0"
                return ("XSD", version_str)
        except Exception:
            pass
        return ("XSD", "unknown")
    
    # XML detection
    if ext == ".xml" or content_start.startswith("<?XML"):
        try:
            tree = etree.fromstring(raw_text.encode('utf-8'))
            version_info = []
            
            # Extract XML declaration version
            xml_version = tree.getroottree().docinfo.xml_version or "1.0"
            encoding = tree.getroottree().docinfo.encoding or "UTF-8"
            version_info.append(f"XML:{xml_version}")
            
            # Get root element info
            root_tag = etree.QName(tree).localname
            version_info.append(f"Root:{root_tag}")
            
            # Check for schema information
            for key, value in tree.attrib.items():
                if "schemaLocation" in key:
                    # Extract schema version if present in URL
                    if "/" in value:
                        schema_parts = value.split("/")
                        for part in schema_parts:
                            if any(c.isdigit() for c in part) and len(part) < 20:
                                version_info.append(f"Schema:{part}")
                                break
                elif "version" in key.lower():
                    version_info.append(f"DocVer:{value}")
            
            # Check namespaces for standards
            nsmap = tree.nsmap if hasattr(tree, 'nsmap') else {}
            for prefix, uri in nsmap.items():
                if uri and ("xmlsoap.org" in uri or "w3.org" in uri):
                    if "/" in uri:
                        std_parts = uri.rstrip("/").split("/")
                        if std_parts:
                            version_info.append(f"NS:{std_parts[-1]}")
                            break
            
            version_str = " | ".join(version_info)
            return ("XML", version_str)
        except Exception:
            pass
        return ("XML", "unknown")
    
    # Fallback: try parsing as XML
    try:
        tree = etree.fromstring(raw_text.encode('utf-8'))
        version = tree.getroottree().docinfo.xml_version or "1.0"
        root_tag = etree.QName(tree).localname
        return ("XML", f"XML:{version} | Root:{root_tag}")
    except Exception:
        raise UnsupportedFileTypeError(
            f"Unable to detect supported file type for: {filename}"
        )


def parse_x12_edi(raw_text: str) -> dict:
    """
    Parse X12 EDI using pure Python — no external library required.

    X12 is a delimiter-based format. The ISA envelope segment is always
    exactly 106 characters and encodes all separator characters at fixed
    byte positions, so no library is needed to parse it correctly.

    Returns nested structure with segments grouped by functional groups
    and transaction sets (identical output shape to the previous implementation).
    """
    try:
        isa_start = raw_text.find("ISA")
        if isa_start < 0:
            raise ValueError("No ISA segment found — not a valid X12 EDI file")

        isa_segment = raw_text[isa_start: isa_start + 106]
        if len(isa_segment) < 106:
            raise ValueError(
                f"ISA segment too short ({len(isa_segment)} chars, expected 106)"
            )

        elem_sep = isa_segment[3]    # position 3:   element separator  (e.g. '*')
        seg_term = isa_segment[105]  # position 105: segment terminator (e.g. '~' or '\\')
        # ISA16 (component separator) is the 17th token when split by elem_sep
        isa_elements = isa_segment.split(elem_sep)
        comp_sep = isa_elements[16][0] if len(isa_elements) > 16 and isa_elements[16] else ":"

        # Split into individual segment strings; strip surrounding whitespace; drop empties
        raw_segments = [s.strip() for s in raw_text[isa_start:].split(seg_term)]
        raw_segments = [s for s in raw_segments if s]

        def _make_seg(elements: list) -> dict:
            return {
                "segment_id": elements[0].strip() if elements else "",
                "elements": elements[1:],
            }

        result = {"interchanges": []}
        current_interchange = None
        current_group = None
        current_transaction = None

        for raw_seg in raw_segments:
            elements = raw_seg.split(elem_sep)
            seg_id = elements[0].strip()
            if not seg_id:
                continue
            seg_dict = _make_seg(elements)

            if seg_id == "ISA":
                current_interchange = {"isa": seg_dict, "functional_groups": []}
                result["interchanges"].append(current_interchange)

            elif seg_id == "GS":
                current_group = {"gs": seg_dict, "transaction_sets": []}
                if current_interchange is not None:
                    current_interchange["functional_groups"].append(current_group)

            elif seg_id == "ST":
                current_transaction = {"st": seg_dict, "segments": []}
                if current_group is not None:
                    current_group["transaction_sets"].append(current_transaction)

            elif seg_id == "SE":
                if current_transaction is not None:
                    current_transaction["se"] = seg_dict

            elif seg_id == "GE":
                if current_group is not None:
                    current_group["ge"] = seg_dict

            elif seg_id == "IEA":
                if current_interchange is not None:
                    current_interchange["iea"] = seg_dict

            else:
                if current_transaction is not None:
                    current_transaction["segments"].append(seg_dict)
                elif current_group is not None:
                    if "pre_transaction_segments" not in current_group:
                        current_group["pre_transaction_segments"] = []
                    current_group["pre_transaction_segments"].append(seg_dict)

        return result

    except Exception as e:
        raise Exception(f"X12 parsing error: {str(e)}")


def parse_edifact(raw_text: str) -> dict:
    """
    Parse EDIFACT file using pydifact.
    
    Returns structure with segments grouped by message groups.
    """
    try:
        from pydifact.segmentcollection import Interchange
        
        interchange = Interchange.from_str(raw_text)
        
        result = {
            "segments": [],
            "message_groups": []
        }
        
        current_group = None
        current_message = None
        
        for segment in interchange.segments:
            seg_dict = {
                "tag": segment.tag,
                "elements": []
            }
            
            # Extract all elements (including composite elements)
            for element in segment.elements:
                if isinstance(element, list):
                    # Composite element
                    seg_dict["elements"].append([str(e) if e else "" for e in element])
                else:
                    seg_dict["elements"].append(str(element) if element else "")
            
            # Structure based on envelope segments
            if segment.tag == "UNB":
                result["unb"] = seg_dict
                
            elif segment.tag == "UNG":
                current_group = {
                    "ung": seg_dict,
                    "messages": []
                }
                result["message_groups"].append(current_group)
                
            elif segment.tag == "UNH":
                current_message = {
                    "unh": seg_dict,
                    "segments": []
                }
                if current_group:
                    current_group["messages"].append(current_message)
                else:
                    # Direct message without group
                    if "messages" not in result:
                        result["messages"] = []
                    result["messages"].append(current_message)
                    
            elif segment.tag == "UNT":
                if current_message:
                    current_message["unt"] = seg_dict
                    
            elif segment.tag == "UNE":
                if current_group:
                    current_group["une"] = seg_dict
                    
            elif segment.tag == "UNZ":
                result["unz"] = seg_dict
                
            else:
                # Add to current message if we're in one
                if current_message:
                    current_message["segments"].append(seg_dict)
                else:
                    result["segments"].append(seg_dict)
        
        return result
        
    except Exception as e:
        raise Exception(f"EDIFACT parsing error: {str(e)}")


def parse_xml(raw_text: str) -> dict:
    """
    Parse XML file using lxml.
    
    Returns nested dict representation of the XML tree.
    """
    try:
        tree = etree.fromstring(raw_text.encode('utf-8'))
        
        def element_to_dict(element):
            """Recursively convert ElementTree to nested dict."""
            result = {
                "tag": etree.QName(element).localname,
                "full_tag": element.tag,
                "attributes": dict(element.attrib),
                "text": element.text.strip() if element.text and element.text.strip() else None,
                "tail": element.tail.strip() if element.tail and element.tail.strip() else None,
                "children": []
            }
            
            for child in element:
                result["children"].append(element_to_dict(child))
            
            return result
        
        # Extract namespaces
        nsmap = tree.nsmap if hasattr(tree, 'nsmap') else {}
        
        # Extract schema location if present
        schema_location = None
        for key, value in tree.attrib.items():
            if key.endswith("schemaLocation"):
                schema_location = value
                break
        
        return {
            "namespaces": {k if k else "default": v for k, v in nsmap.items()},
            "schema_location": schema_location,
            "root": element_to_dict(tree)
        }
        
    except Exception as e:
        raise Exception(f"XML parsing error: {str(e)}")


def parse_xslt(raw_text: str) -> dict:
    """
    Parse XSLT file using lxml.
    
    Returns structured information about the XSLT stylesheet.
    """
    try:
        tree = etree.fromstring(raw_text.encode('utf-8'))
        
        # XSL namespace
        xsl_ns = "http://www.w3.org/1999/XSL/Transform"
        nsmap = {"xsl": xsl_ns}
        
        result = {
            "version": tree.get("version", "1.0"),
            "templates": [],
            "params": [],
            "variables": [],
            "outputs": {},
            "imports_includes": [],
            "raw_xml": raw_text
        }
        
        # Extract templates
        for template in tree.findall(".//xsl:template", namespaces=nsmap):
            result["templates"].append({
                "match": template.get("match"),
                "name": template.get("name"),
                "mode": template.get("mode")
            })
        
        # Extract top-level params
        for param in tree.findall("./xsl:param", namespaces=nsmap):
            result["params"].append({
                "name": param.get("name"),
                "select": param.get("select")
            })
        
        # Extract top-level variables
        for var in tree.findall("./xsl:variable", namespaces=nsmap):
            result["variables"].append({
                "name": var.get("name"),
                "select": var.get("select")
            })
        
        # Extract output
        output_elem = tree.find("./xsl:output", namespaces=nsmap)
        if output_elem is not None:
            result["outputs"] = dict(output_elem.attrib)
        
        # Extract imports and includes
        for imp in tree.findall("./xsl:import", namespaces=nsmap):
            result["imports_includes"].append({
                "type": "import",
                "href": imp.get("href")
            })
        for inc in tree.findall("./xsl:include", namespaces=nsmap):
            result["imports_includes"].append({
                "type": "include",
                "href": inc.get("href")
            })
        
        return result
        
    except Exception as e:
        raise Exception(f"XSLT parsing error: {str(e)}")


def parse_xsd(raw_text: str) -> dict:
    """
    Parse XSD (XML Schema Definition) file using lxml.
    
    Returns structured information about the schema.
    """
    try:
        tree = etree.fromstring(raw_text.encode('utf-8'))
        
        # XSD namespace
        xs_ns = "http://www.w3.org/2001/XMLSchema"
        nsmap = {"xs": xs_ns, "xsd": xs_ns}
        
        result = {
            "target_namespace": tree.get("targetNamespace"),
            "element_form_default": tree.get("elementFormDefault"),
            "attribute_form_default": tree.get("attributeFormDefault"),
            "version": tree.get("version"),
            "elements": [],
            "complex_types": [],
            "simple_types": [],
            "attributes": [],
            "groups": [],
            "imports": [],
            "includes": [],
            "raw_xml": raw_text
        }
        
        # Extract root-level elements
        for elem in tree.findall("./xs:element", namespaces=nsmap) + tree.findall("./xsd:element", namespaces=nsmap):
            result["elements"].append({
                "name": elem.get("name"),
                "type": elem.get("type"),
                "min_occurs": elem.get("minOccurs"),
                "max_occurs": elem.get("maxOccurs"),
                "nillable": elem.get("nillable"),
                "default": elem.get("default"),
                "fixed": elem.get("fixed")
            })
        
        # Extract complex types
        for ct in tree.findall("./xs:complexType", namespaces=nsmap) + tree.findall("./xsd:complexType", namespaces=nsmap):
            ct_info = {
                "name": ct.get("name"),
                "abstract": ct.get("abstract"),
                "mixed": ct.get("mixed"),
                "elements": [],
                "attributes": []
            }
            
            # Get child elements
            for child_elem in ct.findall(".//xs:element", namespaces=nsmap) + ct.findall(".//xsd:element", namespaces=nsmap):
                ct_info["elements"].append({
                    "name": child_elem.get("name"),
                    "type": child_elem.get("type"),
                    "min_occurs": child_elem.get("minOccurs"),
                    "max_occurs": child_elem.get("maxOccurs")
                })
            
            # Get attributes
            for attr in ct.findall(".//xs:attribute", namespaces=nsmap) + ct.findall(".//xsd:attribute", namespaces=nsmap):
                ct_info["attributes"].append({
                    "name": attr.get("name"),
                    "type": attr.get("type"),
                    "use": attr.get("use")
                })
            
            result["complex_types"].append(ct_info)
        
        # Extract simple types
        for st in tree.findall("./xs:simpleType", namespaces=nsmap) + tree.findall("./xsd:simpleType", namespaces=nsmap):
            st_info = {
                "name": st.get("name"),
                "restrictions": []
            }
            
            # Get restrictions
            for restriction in st.findall(".//xs:restriction", namespaces=nsmap) + st.findall(".//xsd:restriction", namespaces=nsmap):
                restriction_info = {
                    "base": restriction.get("base"),
                    "facets": []
                }
                
                # Get facets (minLength, maxLength, pattern, enumeration, etc.)
                for facet in restriction:
                    facet_tag = etree.QName(facet).localname
                    restriction_info["facets"].append({
                        "type": facet_tag,
                        "value": facet.get("value")
                    })
                
                st_info["restrictions"].append(restriction_info)
            
            result["simple_types"].append(st_info)
        
        # Extract attributes
        for attr in tree.findall("./xs:attribute", namespaces=nsmap) + tree.findall("./xsd:attribute", namespaces=nsmap):
            result["attributes"].append({
                "name": attr.get("name"),
                "type": attr.get("type"),
                "use": attr.get("use"),
                "default": attr.get("default")
            })
        
        # Extract groups
        for group in tree.findall("./xs:group", namespaces=nsmap) + tree.findall("./xsd:group", namespaces=nsmap):
            result["groups"].append({
                "name": group.get("name"),
                "ref": group.get("ref")
            })
        
        # Extract imports
        for imp in tree.findall("./xs:import", namespaces=nsmap) + tree.findall("./xsd:import", namespaces=nsmap):
            result["imports"].append({
                "namespace": imp.get("namespace"),
                "schema_location": imp.get("schemaLocation")
            })
        
        # Extract includes
        for inc in tree.findall("./xs:include", namespaces=nsmap) + tree.findall("./xsd:include", namespaces=nsmap):
            result["includes"].append({
                "schema_location": inc.get("schemaLocation")
            })
        
        return result
        
    except Exception as e:
        raise Exception(f"XSD parsing error: {str(e)}")


def ingest_file(
    file_path: Optional[str] = None,
    raw_bytes: Optional[bytes] = None,
    filename: Optional[str] = None
) -> dict:
    """
    Ingest a file and return a standardized dict ready for LLM consumption.
    
    Args:
        file_path: Path to file on disk
        raw_bytes: Raw file bytes (if not using file_path)
        filename: Filename for type detection (required if using raw_bytes)
    
    Returns:
        Standardized dict with metadata and parsed_content
    """
    # Read file content
    if file_path:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        with open(file_path, 'rb') as f:
            raw_bytes = f.read()
        filename = os.path.basename(file_path)
    elif raw_bytes is None:
        raise ValueError("Either file_path or raw_bytes must be provided")
    elif filename is None:
        raise ValueError("filename must be provided when using raw_bytes")
    
    # Decode to text
    try:
        raw_text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        try:
            raw_text = raw_bytes.decode('latin-1')
        except Exception as e:
            return {
                "metadata": {
                    "file_type": "UNKNOWN",
                    "detected_version": None,
                    "filename": filename,
                    "parse_status": "failed",
                    "parse_error": f"Unable to decode file: {str(e)}"
                },
                "parsed_content": {
                    "raw_bytes_length": len(raw_bytes)
                }
            }
    
    # Detect file type
    try:
        file_type, version = detect_file_type(filename, raw_text)
    except UnsupportedFileTypeError as e:
        return {
            "metadata": {
                "file_type": "UNKNOWN",
                "detected_version": None,
                "filename": filename,
                "parse_status": "failed",
                "parse_error": str(e)
            },
            "parsed_content": {
                "raw_text": raw_text
            }
        }
    
    # Parse based on type
    parsed_content = None
    parse_error = None
    parse_status = "success"
    
    try:
        if file_type == "X12_EDI":
            parsed_content = parse_x12_edi(raw_text)
        elif file_type == "EDIFACT":
            parsed_content = parse_edifact(raw_text)
        elif file_type == "XML":
            parsed_content = parse_xml(raw_text)
        elif file_type == "XSLT":
            parsed_content = parse_xslt(raw_text)
        elif file_type == "XSD":
            parsed_content = parse_xsd(raw_text)
        else:
            raise UnsupportedFileTypeError(f"Parser not implemented for: {file_type}")
            
    except Exception as e:
        parse_status = "failed"
        parse_error = str(e)
        parsed_content = {"raw_text": raw_text}
    
    return {
        "metadata": {
            "file_type": file_type,
            "detected_version": version,
            "filename": filename,
            "parse_status": parse_status,
            "parse_error": parse_error
        },
        "parsed_content": parsed_content
    }


# ── CLI test harness ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys
    
    print("\n" + "="*80)
    print("  FILE INGESTION MODULE — Test Harness")
    print("="*80 + "\n")
    
    # Test with sample files or create test data
    if len(sys.argv) > 1:
        # User provided a file path
        file_path = sys.argv[1]
        print(f"[FILE] Ingesting: {file_path}\n")
        
        try:
            result = ingest_file(file_path=file_path)
            
            print("[SUCCESS] INGESTION COMPLETE\n")
            print("METADATA:")
            print("-" * 80)
            for key, value in result["metadata"].items():
                print(f"  {key:20} : {value}")
            
            print("\n" + "="*80)
            print("PARSED CONTENT (first 500 chars):")
            print("-" * 80)
            content_str = json.dumps(result["parsed_content"], indent=2)
            print(content_str[:500])
            if len(content_str) > 500:
                print(f"\n  ... ({len(content_str) - 500} more characters)")
            
            print("\n" + "="*80)
            print(f"[OK] Total output size: {len(json.dumps(result))} bytes")
            print("="*80 + "\n")
            
        except Exception as e:
            print(f"[ERROR] {e}\n")
            sys.exit(1)
    
    else:
        # Demo mode: test with files from test_files folder
        from pathlib import Path
        
        test_files_dir = Path(__file__).parent.parent / "test_files"
        
        if not test_files_dir.exists():
            print("[ERROR] test_files directory not found\n")
            print("Expected location:", test_files_dir)
            print("\nPlease create test_files/ folder with sample files or run with a file path:")
            print("   python modules/file_ingestion.py path/to/your/file.x12\n")
            sys.exit(1)
        
        # Find test files
        test_files = []
        for ext in ['*.edi', '*.x12', '*.edifact', '*.xml', '*.xsd', '*.xsl', '*.xslt']:
            test_files.extend(test_files_dir.glob(ext))
        
        if not test_files:
            print("[WARN] No test files found in test_files/\n")
            print("Expected file types: .edi, .x12, .edifact, .xml, .xsd, .xsl, .xslt\n")
            sys.exit(1)
        
        print(f"[DEMO] Testing with files from: {test_files_dir}\n")
        print(f"Found {len(test_files)} test file(s)\n")
        print("-" * 80 + "\n")
        
        # Process first test file found
        test_file = test_files[0]
        print(f"[FILE] Processing: {test_file.name}\n")
        
        try:
            result = ingest_file(file_path=str(test_file))
            
            print("[SUCCESS] Ingestion complete\n")
            print("METADATA:")
            print("-" * 80)
            for key, value in result["metadata"].items():
                print(f"  {key:20} : {value}")
            
            print("\n" + "="*80)
            print("PARSED CONTENT (first 1000 chars):")
            print("-" * 80)
            content_str = json.dumps(result["parsed_content"], indent=2)
            print(content_str[:1000])
            if len(content_str) > 1000:
                print(f"\n  ... ({len(content_str) - 1000} more characters)")
            
            print("\n" + "="*80)
            print(f"[OK] Total output size: {len(json.dumps(result))} bytes")
            print("="*80 + "\n")
            
            # Show other available test files
            if len(test_files) > 1:
                print("Other test files available:")
                for tf in test_files[1:]:
                    print(f"  - {tf.name}")
                print("\nTest any file with: python modules/file_ingestion.py test_files/<filename>\n")
            
        except Exception as e:
            print(f"[ERROR] {e}\n")
            import traceback
            traceback.print_exc()
            sys.exit(1)
