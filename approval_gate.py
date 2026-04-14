"""
approval_gate.py
────────────────
Thin integration layer between UI actions (Approve/Reject/Rollback) and the
persistent rules/audit store.

This is intentionally small so the rest of the agent remains unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from modules.rules_store import RulesStore, utc_now


def _default_db_path() -> Path:
    # Requirement: database named rules_store.db
    return Path(__file__).resolve().parent / "rules_store.db"


def approve(
    *,
    rule_key: str,
    xslt: str,
    actor: str,
    why: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    with RulesStore(db_path or _default_db_path()) as store:
        rec = store.approve_rule_version(
            rule_key=rule_key,
            xslt=xslt,
            approved_by=actor,
            approved_reason=why,
        )
        store.log_event(
            actor=actor,
            action="approve_rule",
            target=f"{rule_key}@v{rec.version}",
            status="success",
            started_at=rec.approved_at,
            finished_at=rec.approved_at,
            duration_ms=0,
            why=why,
            error=None,
            metadata={"rule_key": rule_key, "version": rec.version},
        )
        return {"ok": True, "rule_key": rec.rule_key, "version": rec.version}


def reject(
    *,
    rule_key: str,
    xslt: str,
    actor: str,
    why: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    # Rejections are logged for audit, but do not create a rule version.
    with RulesStore(db_path or _default_db_path()) as store:
        now = utc_now()
        store.log_event(
            actor=actor,
            action="reject_rule",
            target=rule_key,
            status="success",
            started_at=now,
            finished_at=now,
            duration_ms=0,
            why=why,
            error=None,
            metadata={"rule_key": rule_key, "xslt_chars": len(xslt)},
        )
    return {"ok": True, "rule_key": rule_key}


def rollback(
    *,
    rule_key: str,
    version: int,
    actor: str,
    why: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    with RulesStore(db_path or _default_db_path()) as store:
        rec = store.rollback_rule(rule_key=rule_key, version=version, actor=actor, why=why)
        return {"ok": True, "rule_key": rec.rule_key, "version": rec.version, "xslt": rec.xslt}

