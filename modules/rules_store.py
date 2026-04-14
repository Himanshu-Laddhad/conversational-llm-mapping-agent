"""
rules_store.py
──────────────
SQLite-backed storage for:
- Approved rule (XSLT) versions with rollback
- Audit events capturing who/what/when/why for agent actions

This module is designed to be used by:
- Streamlit UI (approve/reject/rollback)
- Non-invasive logging middleware (e.g., around dispatch())

Database file: rules_store.db (at project root by default)
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

PathLike = Union[str, Path]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _parse_utc(iso: str) -> datetime:
    raw = iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


@dataclass(frozen=True)
class RuleVersionRecord:
    rule_key: str
    version: int
    xslt: str
    approved_at: datetime
    approved_by: str
    approved_reason: str


@dataclass(frozen=True)
class AuditEventRecord:
    id: int
    actor: str
    action: str
    target: str
    status: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    why: str
    error: Optional[str]


class RulesStore:
    """
    Production-style SQLite store (sqlite3 only).

    Schema:
      rule_versions(rule_key, version, xslt, approved_at, approved_by, approved_reason)
      rule_current(rule_key, current_version, updated_at)
      audit_events(actor, action, target, status, started_at, finished_at, duration_ms, why, error, metadata_json)

    Notes:
    - WAL mode enabled for better concurrency on Streamlit.
    - All timestamps stored as ISO-8601 UTC strings.
    """

    def __init__(self, db_path: PathLike) -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("RulesStore is not connected; call connect() first")
        return self._conn

    def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        cur = self._conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
        self._ensure_schema()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "RulesStore":
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rule_versions (
                rule_key TEXT NOT NULL,
                version INTEGER NOT NULL,
                xslt TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                approved_reason TEXT NOT NULL,
                PRIMARY KEY (rule_key, version)
            );

            CREATE TABLE IF NOT EXISTS rule_current (
                rule_key TEXT PRIMARY KEY NOT NULL,
                current_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                why TEXT NOT NULL,
                error TEXT,
                metadata_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_rule_versions_rule_key
                ON rule_versions(rule_key);
            CREATE INDEX IF NOT EXISTS idx_rule_current_updated_at
                ON rule_current(updated_at);
            CREATE INDEX IF NOT EXISTS idx_audit_events_action_started
                ON audit_events(action, started_at);
            """
        )

    def _next_version(self, rule_key: str) -> int:
        cur = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM rule_versions WHERE rule_key = ?",
            (rule_key,),
        )
        (nxt,) = cur.fetchone()
        return int(nxt)

    def approve_rule_version(
        self,
        *,
        rule_key: str,
        xslt: str,
        approved_by: str,
        approved_reason: str,
    ) -> RuleVersionRecord:
        if not rule_key.strip():
            raise ValueError("rule_key must be a non-empty string")
        if not xslt.strip():
            raise ValueError("xslt must be a non-empty string")
        if not approved_by.strip():
            raise ValueError("approved_by must be provided")
        if not approved_reason.strip():
            raise ValueError("approved_reason must be provided")

        version = self._next_version(rule_key)
        now = _iso_utc(utc_now())
        self.conn.execute(
            """
            INSERT INTO rule_versions
              (rule_key, version, xslt, approved_at, approved_by, approved_reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rule_key, version, xslt, now, approved_by, approved_reason),
        )
        self.conn.execute(
            """
            INSERT INTO rule_current (rule_key, current_version, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(rule_key) DO UPDATE SET
              current_version=excluded.current_version,
              updated_at=excluded.updated_at
            """,
            (rule_key, version, now),
        )
        return RuleVersionRecord(
            rule_key=rule_key,
            version=version,
            xslt=xslt,
            approved_at=_parse_utc(now),
            approved_by=approved_by,
            approved_reason=approved_reason,
        )

    def list_rule_versions(self, rule_key: str) -> List[RuleVersionRecord]:
        cur = self.conn.execute(
            """
            SELECT rule_key, version, xslt, approved_at, approved_by, approved_reason
            FROM rule_versions
            WHERE rule_key = ?
            ORDER BY version DESC
            """,
            (rule_key,),
        )
        out: List[RuleVersionRecord] = []
        for rk, ver, xslt, at, by, reason in cur.fetchall():
            out.append(
                RuleVersionRecord(
                    rule_key=str(rk),
                    version=int(ver),
                    xslt=str(xslt),
                    approved_at=_parse_utc(str(at)),
                    approved_by=str(by),
                    approved_reason=str(reason),
                )
            )
        return out

    def get_current_rule(self, rule_key: str) -> Optional[RuleVersionRecord]:
        cur = self.conn.execute(
            """
            SELECT rv.rule_key, rv.version, rv.xslt, rv.approved_at, rv.approved_by, rv.approved_reason
            FROM rule_current rc
            JOIN rule_versions rv
              ON rv.rule_key = rc.rule_key AND rv.version = rc.current_version
            WHERE rc.rule_key = ?
            """,
            (rule_key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        rk, ver, xslt, at, by, reason = row
        return RuleVersionRecord(
            rule_key=str(rk),
            version=int(ver),
            xslt=str(xslt),
            approved_at=_parse_utc(str(at)),
            approved_by=str(by),
            approved_reason=str(reason),
        )

    def rollback_rule(
        self,
        *,
        rule_key: str,
        version: int,
        actor: str,
        why: str,
    ) -> RuleVersionRecord:
        if version <= 0:
            raise ValueError("version must be >= 1")
        recs = self.conn.execute(
            """
            SELECT rule_key, version, xslt, approved_at, approved_by, approved_reason
            FROM rule_versions
            WHERE rule_key = ? AND version = ?
            """,
            (rule_key, version),
        ).fetchone()
        if not recs:
            raise ValueError(f"No approved version {version} exists for {rule_key!r}")

        now = _iso_utc(utc_now())
        self.conn.execute(
            """
            INSERT INTO rule_current (rule_key, current_version, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(rule_key) DO UPDATE SET
              current_version=excluded.current_version,
              updated_at=excluded.updated_at
            """,
            (rule_key, version, now),
        )

        rk, ver, xslt, at, by, reason = recs
        self.log_event(
            actor=actor,
            action="rollback_rule",
            target=f"{rule_key}@v{version}",
            status="success",
            started_at=_parse_utc(now),
            finished_at=_parse_utc(now),
            duration_ms=0,
            why=why,
            error=None,
            metadata={"rule_key": rule_key, "version": version},
        )
        return RuleVersionRecord(
            rule_key=str(rk),
            version=int(ver),
            xslt=str(xslt),
            approved_at=_parse_utc(str(at)),
            approved_by=str(by),
            approved_reason=str(reason),
        )

    def log_event(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: int,
        why: str,
        error: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
        self.conn.execute(
            """
            INSERT INTO audit_events
              (actor, action, target, status, started_at, finished_at, duration_ms, why, error, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor,
                action,
                target,
                status,
                _iso_utc(started_at),
                _iso_utc(finished_at),
                int(duration_ms),
                why,
                error,
                meta_json,
            ),
        )

    @contextmanager
    def audit_span(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        why: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Iterator[None]:
        t0 = time.time()
        started = utc_now()
        try:
            yield
            finished = utc_now()
            self.log_event(
                actor=actor,
                action=action,
                target=target,
                status="success",
                started_at=started,
                finished_at=finished,
                duration_ms=int((time.time() - t0) * 1000),
                why=why,
                error=None,
                metadata=metadata,
            )
        except Exception as exc:
            finished = utc_now()
            self.log_event(
                actor=actor,
                action=action,
                target=target,
                status="error",
                started_at=started,
                finished_at=finished,
                duration_ms=int((time.time() - t0) * 1000),
                why=why,
                error=f"{type(exc).__name__}: {exc}",
                metadata=metadata,
            )
            raise

