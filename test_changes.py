"""
test_changes.py
───────────────
Quick verification script for the three changes:
  1. Saxon-HE XSLT 2.0 execution (simulation engine)
  2. Altova extension call detection
  3. Out-of-scope guardrails
  4. patched_xslt bug fix in dispatcher

Run from the project root:
    python test_changes.py
"""

import sys
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS = "PASS"
FAIL = "FAIL"
all_results = []

def check(label, condition):
    status = PASS if condition else FAIL
    mark   = "✅" if condition else "❌"
    print(f"  [{status}] {mark}  {label}")
    all_results.append(condition)


# ── TEST 1: Saxon-HE executes a real XSLT 2.0 file ───────────────────────────
print()
print("=" * 60)
print("TEST 1: Saxon-HE XSLT 2.0 execution (Nordstrom 810)")
print("=" * 60)

XSLT = (
    "/Users/preetham/Downloads/PartnerlinQ MappingData"
    "/810_C-000340_OUT/810_NordStrom_Xslt_11-08-2023.xml"
)
SRC = (
    "/Users/preetham/Downloads/PartnerlinQ MappingData"
    "/810_C-000340_OUT/810"
    "/99e1c1fe-5f71-4315-b7f5-80dfa1d02d4b/SourceFile.txt"
)

try:
    from modules.simulation_engine import (
        _try_saxon_transform,
        _detect_altova_extensions,
        _detect_xslt_version,
    )

    raw     = Path(XSLT).read_text(errors="replace")
    version = _detect_xslt_version(raw)
    altova  = _detect_altova_extensions(raw)

    print(f"  XSLT version   : {version}")
    print(f"  Altova calls   : {altova}")

    result, err = _try_saxon_transform(raw, SRC)

    check("Saxon returned output (not None)",  result is not None)
    check("No Saxon error",                    err is None)
    check("Output contains <ST> segment",      result and "<ST>" in result)
    check("Output contains <BIG> segment",     result and "<BIG>" in result)
    check("XSLT version detected as 2.0",      version == "2.0")
    check("Altova calls NOT detected (ns only)", not altova)

    if result:
        print()
        print("  First 300 chars of real Saxon output:")
        print("  " + result[:300].replace("\n", "\n  "))

except Exception as exc:
    print(f"  [ERROR] {exc}")
    all_results.append(False)


# ── TEST 2: Altova extension CALL detection ───────────────────────────────────
print()
print("=" * 60)
print("TEST 2: Altova extension call detection")
print("=" * 60)

try:
    from modules.simulation_engine import _detect_altova_extensions

    ns_only   = 'xmlns:altova="http://www.altova.com/xslt-extensions"'
    with_call = 'xmlns:altova="..." ... altova:format-date(date, "YYYY")'
    with_ext  = "altovaext:node-set(something)"
    fn_user   = "fn-user-defined:myFunc(x)"

    check("Namespace declaration only  → False", not _detect_altova_extensions(ns_only))
    check("altova:format-date( call   → True",       _detect_altova_extensions(with_call))
    check("altovaext:node-set( call   → True",        _detect_altova_extensions(with_ext))
    check("fn-user-defined:myFunc(    → True",         _detect_altova_extensions(fn_user))

except Exception as exc:
    print(f"  [ERROR] {exc}")
    all_results.append(False)


# ── TEST 3: Out-of-scope guardrails ───────────────────────────────────────────
print()
print("=" * 60)
print("TEST 3: Out-of-scope guardrails (_is_in_scope)")
print("=" * 60)

try:
    from modules.dispatcher import _is_in_scope

    cases = [
        ("What does the ISA segment do?",           True),
        ("Simulate the Nordstrom 810 XSLT",         True),
        ("Audit this mapping for issues",            True),
        ("Add a DTM segment to the stylesheet",      True),
        ("Explain xslt templates",                   True),
        ("How do I modify the EDI mapping?",         True),
        ("Generate an XSLT for X12 850",             True),
        ("What is the capital of France?",           False),
        ("Write me a Python script to sort a list",  False),
        ("Can you help me with my homework?",        False),
        ("Tell me a joke",                           False),
        ("What is 2+2?",                             False),
        ("What is the weather today?",               False),
    ]

    for msg, expected in cases:
        got    = _is_in_scope(msg)
        label  = f"in_scope={str(got):<5}  '{msg}'"
        check(label, got == expected)

except Exception as exc:
    print(f"  [ERROR] {exc}")
    all_results.append(False)


# ── TEST 4: patched_xslt and guardrail wired into dispatch() ─────────────────
print()
print("=" * 60)
print("TEST 4: dispatcher.py code-level checks")
print("=" * 60)

try:
    from modules.dispatcher import dispatch
    src = inspect.getsource(dispatch)

    check(
        "patched_xslt captured from modify() (not _mod_agent)",
        "response, patched_xslt = modify(" in src,
    )
    check(
        "_is_in_scope() guardrail wired into dispatch()",
        "_is_in_scope(user_message)" in src,
    )
    check(
        "out-of-scope early-return present",
        "_OUT_OF_SCOPE_RESPONSE" in src,
    )

except Exception as exc:
    print(f"  [ERROR] {exc}")
    all_results.append(False)


# ── TEST 5: simulate auto-resolves source_file from session ──────────────────
print()
print("=" * 60)
print("TEST 5: simulate source_file auto-resolution from session")
print("=" * 60)

try:
    from modules.dispatcher import dispatch
    src = inspect.getsource(dispatch)

    check(
        "resolved_source variable introduced in simulate block",
        "resolved_source = source_file" in src,
    )
    check(
        "session.ingested_files scanned for non-XSLT source files",
        "_source_types" in src,
    )
    check(
        "D365_XML included in source type set",
        '"D365_XML"' in src,
    )
    check(
        "Path existence check before using resolved_source",
        "Path(_fpath).exists()" in src,
    )
    check(
        "resolved_source passed to simulate() not source_file",
        "source_file=resolved_source" in src,
    )

except Exception as exc:
    print(f"  [ERROR] {exc}")
    all_results.append(False)


# ── SUMMARY ───────────────────────────────────────────────────────────────────
print()
print("=" * 60)
passed = sum(all_results)
total  = len(all_results)
if passed == total:
    print(f"ALL {total} CHECKS PASSED ✅")
else:
    print(f"{passed}/{total} CHECKS PASSED — {total - passed} FAILED ❌")
print("=" * 60)
print()
