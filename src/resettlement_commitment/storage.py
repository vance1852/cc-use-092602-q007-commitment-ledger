"""安置承诺核算的 SQLite 模式、不可变事件触发器与事务辅助。"""

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
    role TEXT NOT NULL CHECK(role IN ('placement_admin','finance','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 承诺版本：同一项目批次逐版演进，旧版本保留且不再改写。
CREATE TABLE IF NOT EXISTS commitment_versions (
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    promised_turnover_date TEXT NOT NULL,
    promised_delivery_date TEXT NOT NULL,
    promised_service_date TEXT NOT NULL,
    daily_compensation_cny TEXT NOT NULL,
    note TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    supersedes_version_no INTEGER,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, batch_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_commitment_versions_batch
ON commitment_versions(project_id, batch_id, version_no);

CREATE TABLE IF NOT EXISTS resettlement_households (
    household_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    head_name TEXT NOT NULL,
    members INTEGER NOT NULL,
    enrolled_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_households_batch
ON resettlement_households(project_id, batch_id, household_id);

CREATE TABLE IF NOT EXISTS settlement_windows (
    window_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    daily_rate_cny TEXT NOT NULL,
    label TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_windows_batch
ON settlement_windows(project_id, batch_id, window_start);

CREATE TABLE IF NOT EXISTS exclusion_rules (
    rule_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES settlement_windows(window_id),
    kind TEXT NOT NULL CHECK(kind IN ('construction_plan','policy_exemption','household_deferral')),
    min_advance_notice_days INTEGER NOT NULL DEFAULT 0,
    allowed_reason_codes_json TEXT NOT NULL DEFAULT '[]',
    effective_start TEXT,
    effective_end TEXT,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_window ON exclusion_rules(window_id);

-- 不可变履约事件：服务端只允许插入，触发器拒绝更新和删除。
CREATE TABLE IF NOT EXISTS commitment_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'construction.announced','policy.exempted','household.paused','delivery.confirmed'
    )),
    household_id TEXT,
    event_date TEXT NOT NULL,
    end_date TEXT,
    advance_notice_days INTEGER,
    reason_code TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
    note TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_batch
ON commitment_events(project_id, batch_id, event_date, event_id);

CREATE TRIGGER IF NOT EXISTS trg_events_no_update
BEFORE UPDATE ON commitment_events
BEGIN
    SELECT RAISE(ABORT, '履约事件不可变，禁止更新；纠错必须形成结算新版本');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
BEFORE DELETE ON commitment_events
BEGIN
    SELECT RAISE(ABORT, '履约事件不可变，禁止删除；纠错必须形成结算新版本');
END;

-- 周期结算运行：同一窗口逐版本追加，迟到事件只能触发纠错新版本。
CREATE TABLE IF NOT EXISTS settlement_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id TEXT NOT NULL REFERENCES settlement_windows(window_id),
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    commitment_version_no INTEGER NOT NULL,
    settlement_version_no INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('closed','corrected')),
    input_sha256 TEXT NOT NULL,
    event_basis_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    diff_json TEXT,
    correction_reason TEXT,
    supersedes_run_id INTEGER REFERENCES settlement_runs(run_id),
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(window_id, settlement_version_no)
);

CREATE INDEX IF NOT EXISTS idx_runs_batch
ON settlement_runs(project_id, batch_id, run_id);

-- 补偿确认：安置运营与财务分离授权，两个角色分别确认后才成立。
CREATE TABLE IF NOT EXISTS compensation_approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE REFERENCES settlement_runs(run_id),
    net_compensation_cny TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','operations_confirmed','finance_confirmed','rejected')),
    operations_by TEXT REFERENCES commitment_users(user_id),
    operations_at TEXT,
    finance_by TEXT REFERENCES commitment_users(user_id),
    finance_at TEXT,
    reject_reason TEXT,
    created_by TEXT NOT NULL REFERENCES commitment_users(user_id),
    created_at TEXT NOT NULL
);

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


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=check_same_thread
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


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
