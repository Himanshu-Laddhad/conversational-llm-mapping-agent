"""
xslt_revision_store.py
──────────────────────
Versioned, non-destructive XSLT revision management.

- Preserves the original XSLT for comparison
- Saves each revision as revised_vN
- Tracks "latest revision" for future tests/follow-up edits
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


def _strip_session_prefix(filename: str) -> str:
    """
    app.py prefixes uploads with an 8-hex session id: <sid>_<name>.
    Strip that to get a stable mapping key.
    """
    parts = filename.split("_", 1)
    if len(parts) == 2 and len(parts[0]) == 8 and all(c in "0123456789abcdef" for c in parts[0].lower()):
        return parts[1]
    return filename


def mapping_key_from_filename(filename: str) -> str:
    clean = _strip_session_prefix(Path(filename).name)
    stem = Path(clean).stem
    stem = re.sub(r"_revised_v\d+$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"_patched$", "", stem, flags=re.IGNORECASE)
    stem = stem.strip() or "mapping"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem)


@dataclass(frozen=True)
class RevisionRecord:
    mapping_key: str
    original_path: str
    latest_version_path: str
    latest_version_number: int
    all_version_paths: List[str]


class XsltRevisionStore:
    """Filesystem-backed revision manager."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _mapping_dir(self, mapping_key: str) -> Path:
        safe = mapping_key_from_filename(mapping_key)
        return self.base_dir / safe

    def _meta_path(self, mapping_key: str) -> Path:
        return self._mapping_dir(mapping_key) / "metadata.json"

    def _read_meta(self, mapping_key: str) -> Dict:
        p = self._meta_path(mapping_key)
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def _write_meta(self, mapping_key: str, data: Dict) -> None:
        p = self._meta_path(mapping_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def ensure_original(self, source_path: str, filename: str) -> str:
        key = mapping_key_from_filename(filename)
        md = self._mapping_dir(key)
        md.mkdir(parents=True, exist_ok=True)
        ext = Path(filename).suffix or ".xml"
        original_dest = md / f"{key}_original{ext}"
        if not original_dest.exists():
            shutil.copyfile(source_path, original_dest)

        meta = self._read_meta(key)
        meta.setdefault("mapping_key", key)
        meta.setdefault("original_path", str(original_dest))
        meta.setdefault("latest_version_number", 0)
        meta.setdefault("latest_version_path", str(original_dest))
        meta.setdefault("versions", [])
        self._write_meta(key, meta)
        return key

    def save_revision(
        self,
        *,
        source_path: str,
        filename: str,
        xslt_text: str,
        change_summary: str,
    ) -> RevisionRecord:
        key = self.ensure_original(source_path, filename)
        meta = self._read_meta(key)
        ver = int(meta.get("latest_version_number", 0)) + 1
        md = self._mapping_dir(key)
        md.mkdir(parents=True, exist_ok=True)
        latest_path = md / f"{key}_revised_v{ver}.xml"
        latest_path.write_text(xslt_text, encoding="utf-8")

        versions = list(meta.get("versions", []))
        versions.append({"version": ver, "path": str(latest_path), "change_summary": change_summary})
        meta["latest_version_number"] = ver
        meta["latest_version_path"] = str(latest_path)
        meta["versions"] = versions
        self._write_meta(key, meta)

        return RevisionRecord(
            mapping_key=key,
            original_path=str(meta["original_path"]),
            latest_version_path=str(latest_path),
            latest_version_number=ver,
            all_version_paths=[v["path"] for v in versions],
        )

    def get_latest(self, filename: str) -> Optional[RevisionRecord]:
        key = mapping_key_from_filename(filename)
        meta = self._read_meta(key)
        if not meta:
            return None
        versions = list(meta.get("versions", []))
        return RevisionRecord(
            mapping_key=key,
            original_path=str(meta.get("original_path", "")),
            latest_version_path=str(meta.get("latest_version_path", "")),
            latest_version_number=int(meta.get("latest_version_number", 0)),
            all_version_paths=[v["path"] for v in versions],
        )


def build_comparison(old_xslt: str, new_xslt: str) -> Dict[str, object]:
    """Structured diff data for UI and scripts."""
    old_lines = old_xslt.splitlines()
    new_lines = new_xslt.splitlines()
    diff_lines = list(
        difflib.unified_diff(old_lines, new_lines, fromfile="old", tofile="new", lineterm="")
    )
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
    return {
        "old_xslt": old_xslt,
        "new_xslt": new_xslt,
        "diff_text": "\n".join(diff_lines),
        "summary": f"{added} added line(s), {removed} removed line(s)",
    }

