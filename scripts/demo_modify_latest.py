#!/usr/bin/env python3
"""
CLI demo for the XSLT modify → revise → compare → test workflow.

Usage:
  python3 scripts/demo_modify_latest.py <xslt_file> <sample_xml> "<request>"
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    if len(sys.argv) < 4:
        print(
            "Usage: python3 scripts/demo_modify_latest.py <xslt_file> <sample_xml> "
            "\"<modification request>\""
        )
        return 1

    xslt_path = Path(sys.argv[1]).resolve()
    sample_xml = Path(sys.argv[2]).resolve()
    request = sys.argv[3]

    if not xslt_path.exists():
        print(f"[ERROR] XSLT file not found: {xslt_path}")
        return 1
    if not sample_xml.exists():
        print(f"[ERROR] Sample input file not found: {sample_xml}")
        return 1

    from modules.file_ingestion import ingest_file
    from modules.modification_engine import modify, _parse_patch
    from modules.simulation_engine import simulate
    from modules.xslt_revision_store import XsltRevisionStore, build_comparison

    ingested = ingest_file(file_path=str(xslt_path))
    response_text, patched_xslt = modify(ingested, modification_request=request)

    print("\n=== MODIFY RESPONSE ===\n")
    print(response_text)

    if not patched_xslt:
        print("\n[INFO] No revised XSLT was produced (auto-apply failed or no change needed).")
        return 0

    patch = _parse_patch(response_text)
    original_xslt = (ingested.get("parsed_content") or {}).get("raw_xml") or ""
    comp = build_comparison(original_xslt, patched_xslt)

    store = XsltRevisionStore(_ROOT / "data" / "revisions")
    rev = store.save_revision(
        source_path=str(xslt_path),
        filename=xslt_path.name,
        xslt_text=patched_xslt,
        change_summary=patch.get("summary", "") or "Updated XSLT revision",
    )

    print("\n=== COMPARISON SUMMARY ===\n")
    print(patch.get("summary", "") or "(no change summary)")
    print(comp["summary"])
    print(f"Latest version path: {rev.latest_version_path}")

    latest_ingested = ingest_file(file_path=rev.latest_version_path)
    test_text, output_xml = simulate(latest_ingested, source_file=str(sample_xml))

    print("\n=== TEST RESULT ===\n")
    print(test_text)
    if output_xml:
        print("\n=== TRANSFORM OUTPUT (first 4000 chars) ===\n")
        print(output_xml[:4000])
    else:
        print("\n[INFO] No real XML transform output produced; simulation used fallback mode.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

