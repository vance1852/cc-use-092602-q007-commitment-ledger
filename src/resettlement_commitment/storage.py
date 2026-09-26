"""安置承诺核算服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS commitment_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN
        ('resettlement','resettlement_manager','finance','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    current_version_no INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, batch_id)
);

CREATE TABLE IF NOT EXISTS commitment_versions (
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    version_no INTEGER NOT NULL,
    based_on_version INTEGER,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    correction_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(commitment_id, version_no)
);

CREATE TABLE IF NOT EXISTS stat_windows (
    window_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(commitment_id, window_id),
    CHECK(ends_on >= starts_on)
);

CREATE INDEX IF NOT EXISTS idx_windows_commitment
ON stat_windows(commitment_id, starts_on);

CREATE TABLE IF NOT EXISTS exclusion_rules (
    rule_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES stat_windows(window_id),
    event_kind TEXT NOT NULL,
    require_advance_notice INTEGER NOT NULL CHECK(require_advance_notice IN (0,1)),
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(window_id, event_kind)
);

CREATE TABLE IF NOT EXISTS household_pauses (
    pause_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    household_id TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    ends_on TEXT,
    reason_code TEXT NOT NULL CHECK(reason_code IN
        ('family_extension','policy_exemption','family_unavailable')),
    evidence TEXT NOT NULL,
    confirmed_by_household INTEGER NOT NULL CHECK(confirmed_by_household IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(ends_on IS NULL OR ends_on >= starts_on)
);

CREATE INDEX IF NOT EXISTS idx_pauses_commitment_household
ON household_pauses(commitment_id, household_id, starts_on);

-- 领域事件只追加、不可修改、不可删除。
CREATE TABLE IF NOT EXISTS commitment_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK(event_kind IN
        ('temporary_keys_handover','formal_delivery','public_service_resume','construction_plan')),
    household_id TEXT NOT NULL,
    event_date TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    notice_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    recorded_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_project_batch
ON commitment_events(project_id, batch_id, event_date, event_id);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    commitment_version_no INTEGER NOT NULL,
    window_id TEXT NOT NULL,
    settlement_no INTEGER NOT NULL,
    as_of_date TEXT NOT NULL,
    include_observed_until TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'preliminary' CHECK(state IN
        ('preliminary','operations_approved','settled','corrected')),
    input_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    supersedes_settlement_id TEXT REFERENCES settlements(settlement_id),
    correction_reason TEXT NOT NULL DEFAULT '',
    diff_json TEXT,
    prepared_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    prepared_at TEXT NOT NULL,
    operations_approved_by TEXT REFERENCES commitment_users(user_id),
    operations_approved_at TEXT,
    finance_confirmed_by TEXT REFERENCES commitment_users(user_id),
    finance_confirmed_at TEXT,
    UNIQUE(commitment_id, window_id, settlement_no),
    FOREIGN KEY(commitment_id, commitment_version_no)
        REFERENCES commitment_versions(commitment_id, version_no),
    FOREIGN KEY(commitment_id, window_id) REFERENCES stat_windows(commitment_id, window_id)
);

CREATE INDEX IF NOT EXISTS idx_settlements_project_batch
ON settlements(project_id, batch_id, prepared_at);

CREATE TABLE IF NOT EXISTS commitment_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commitment_audit_entity
ON commitment_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 每请求一线程；WAL、BEGIN IMMEDIATE 与 busy_timeout
    # 已串行化写事务，因此允许连接跨线程复用。
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
