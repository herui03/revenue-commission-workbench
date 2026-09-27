-- Revenue & Commission Operations Workbench — SQLite schema.
-- Money columns (*_minor) are INTEGER minor units. Rates (*_bps) are INTEGER basis points.
-- Source tables are append-only: accepted rows can never be updated or deleted by the
-- application (triggers below). Corrections flow through new events, decisions or
-- adjustments. Honest limit: anyone who owns the database file can drop the triggers.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    batch_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_kind         TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    mode              TEXT NOT NULL CHECK (mode IN ('strict', 'quarantine')),
    status            TEXT NOT NULL CHECK (status IN ('COMMITTED', 'COMMITTED_WITH_QUARANTINE', 'REJECTED', 'DUPLICATE_FILE')),
    duplicate_of      INTEGER REFERENCES import_batches(batch_id),
    rows_read         INTEGER NOT NULL DEFAULT 0,
    rows_accepted     INTEGER NOT NULL DEFAULT 0,
    rows_duplicate    INTEGER NOT NULL DEFAULT 0,
    rows_quarantined  INTEGER NOT NULL DEFAULT 0,
    file_errors_json  TEXT NOT NULL DEFAULT '[]',
    control_json      TEXT NOT NULL DEFAULT '{}',
    actor             TEXT NOT NULL,
    imported_at       TEXT NOT NULL
);
-- Content-hash idempotency: one committed batch per file content, whatever the filename.
CREATE UNIQUE INDEX IF NOT EXISTS ux_batches_committed_hash
    ON import_batches(sha256) WHERE status IN ('COMMITTED', 'COMMITTED_WITH_QUARANTINE');

CREATE TABLE IF NOT EXISTS reps (
    rep_id       TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    team         TEXT NOT NULL,
    batch_id     INTEGER NOT NULL REFERENCES import_batches(batch_id),
    source_row   INTEGER NOT NULL,
    row_hash     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    contract_id         TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL,
    account_name        TEXT NOT NULL,
    currency            TEXT NOT NULL,
    booking_date        TEXT NOT NULL,
    booked_amount_minor INTEGER NOT NULL CHECK (booked_amount_minor >= 0),
    product             TEXT NOT NULL,
    batch_id            INTEGER NOT NULL REFERENCES import_batches(batch_id),
    source_row          INTEGER NOT NULL,
    row_hash            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS splits (
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    rep_id      TEXT NOT NULL REFERENCES reps(rep_id),
    split_bps   INTEGER NOT NULL CHECK (split_bps > 0 AND split_bps <= 10000),
    batch_id    INTEGER NOT NULL REFERENCES import_batches(batch_id),
    source_row  INTEGER NOT NULL,
    row_hash    TEXT NOT NULL,
    PRIMARY KEY (contract_id, rep_id)
);

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id         TEXT NOT NULL,
    version         INTEGER NOT NULL CHECK (version >= 1),
    currency        TEXT NOT NULL,
    effective_from  TEXT NOT NULL,
    threshold_minor INTEGER NOT NULL CHECK (threshold_minor >= 0),
    base_rate_bps   INTEGER NOT NULL CHECK (base_rate_bps BETWEEN 0 AND 10000),
    accel_rate_bps  INTEGER NOT NULL CHECK (accel_rate_bps BETWEEN 0 AND 10000),
    description     TEXT NOT NULL,
    batch_id        INTEGER NOT NULL REFERENCES import_batches(batch_id),
    source_row      INTEGER NOT NULL,
    row_hash        TEXT NOT NULL,
    PRIMARY KEY (plan_id, version, currency),
    UNIQUE (plan_id, currency, effective_from)
);

CREATE TABLE IF NOT EXISTS plan_assignments (
    assignment_id  TEXT PRIMARY KEY,
    rep_id         TEXT NOT NULL REFERENCES reps(rep_id),
    plan_id        TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    batch_id       INTEGER NOT NULL REFERENCES import_batches(batch_id),
    source_row     INTEGER NOT NULL,
    row_hash       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_events (
    event_id          TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL CHECK (event_type IN ('COLLECTION', 'REFUND')),
    contract_id       TEXT NOT NULL REFERENCES contracts(contract_id),
    event_date        TEXT NOT NULL,
    currency          TEXT NOT NULL,
    amount_minor      INTEGER NOT NULL CHECK (amount_minor > 0),
    original_event_id TEXT REFERENCES cash_events(event_id),
    invoice_ref       TEXT NOT NULL DEFAULT '',
    memo              TEXT NOT NULL DEFAULT '',
    batch_id          INTEGER NOT NULL REFERENCES import_batches(batch_id),
    source_row        INTEGER NOT NULL,
    row_hash          TEXT NOT NULL,
    CHECK ((event_type = 'REFUND') = (original_event_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_cash_events_date ON cash_events(event_date);
CREATE INDEX IF NOT EXISTS ix_cash_events_original ON cash_events(original_event_id);

CREATE TABLE IF NOT EXISTS recorded_payouts (
    record_id     TEXT PRIMARY KEY,
    rep_id        TEXT NOT NULL REFERENCES reps(rep_id),
    period        TEXT NOT NULL CHECK (length(period) = 7 AND period GLOB '20[0-9][0-9]-[01][0-9]' AND CAST(substr(period, 6, 2) AS INTEGER) BETWEEN 1 AND 12),
    currency      TEXT NOT NULL,
    amount_minor  INTEGER NOT NULL,
    source_system TEXT NOT NULL,
    memo          TEXT NOT NULL DEFAULT '',
    batch_id      INTEGER NOT NULL REFERENCES import_batches(batch_id),
    source_row    INTEGER NOT NULL,
    row_hash      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine_rows (
    q_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES import_batches(batch_id),
    file_kind     TEXT NOT NULL,
    source_row    INTEGER NOT NULL,
    natural_key   TEXT NOT NULL,          -- canonical JSON array, e.g. ["E-1"]
    raw_json      TEXT NOT NULL,
    reasons_json  TEXT NOT NULL,          -- [{"code": ..., "message": ...}]
    event_date    TEXT,                   -- parsed business date when available
    currency      TEXT,
    amount_minor  INTEGER,
    status        TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'DISMISSED', 'SUPERSEDED')),
    resolution    TEXT,
    resolved_by   TEXT,
    resolved_at   TEXT
);

CREATE TABLE IF NOT EXISTS periods (
    period          TEXT PRIMARY KEY CHECK (length(period) = 7 AND period GLOB '20[0-9][0-9]-[01][0-9]' AND CAST(substr(period, 6, 2) AS INTEGER) BETWEEN 1 AND 12),  -- defense in depth for R-5
    status          TEXT NOT NULL CHECK (status IN ('OPEN', 'IN_REVIEW', 'CLOSED')),
    review_run_id   INTEGER REFERENCES calc_runs(run_id),
    prepared_by     TEXT,
    submitted_at    TEXT,
    closed_run_id   INTEGER REFERENCES calc_runs(run_id),
    reviewed_by     TEXT,
    closed_at       TEXT,
    snapshot_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS calc_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    result_digest   TEXT NOT NULL,
    line_count      INTEGER NOT NULL,
    blocking_holds  INTEGER NOT NULL,
    result_json     TEXT NOT NULL          -- totals, holds, controls, accounted event ids, inputs used
);

CREATE TABLE IF NOT EXISTS calc_lines (
    run_id            INTEGER NOT NULL REFERENCES calc_runs(run_id),
    line_key          TEXT NOT NULL,
    line_type         TEXT NOT NULL,
    period            TEXT NOT NULL,        -- posting period
    original_period   TEXT,                 -- period whose ladder / earning this line belongs to
    rep_id            TEXT,
    currency          TEXT NOT NULL,
    event_id          TEXT,
    original_event_id TEXT,
    contract_id       TEXT,
    event_date        TEXT,
    plan_id           TEXT,
    plan_version      INTEGER,
    split_bps         INTEGER,
    credited_minor    INTEGER NOT NULL DEFAULT 0,
    credit_reversed_minor INTEGER NOT NULL DEFAULT 0,
    base_portion_minor  INTEGER NOT NULL DEFAULT 0,
    accel_portion_minor INTEGER NOT NULL DEFAULT 0,
    base_rate_bps     INTEGER,
    accel_rate_bps    INTEGER,
    exact_amount      TEXT NOT NULL,
    amount_minor      INTEGER NOT NULL,
    detail_json       TEXT NOT NULL,
    PRIMARY KEY (run_id, line_key)
);
CREATE INDEX IF NOT EXISTS ix_calc_lines_event ON calc_lines(event_id);

CREATE TABLE IF NOT EXISTS period_snapshots (
    period        TEXT PRIMARY KEY REFERENCES periods(period),
    run_id        INTEGER NOT NULL REFERENCES calc_runs(run_id),
    snapshot_json TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    closed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payables (
    period          TEXT NOT NULL REFERENCES periods(period),
    rep_id          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (period, rep_id, currency)   -- duplicate payables are impossible
);

CREATE TABLE IF NOT EXISTS event_decisions (
    event_id       TEXT PRIMARY KEY REFERENCES cash_events(event_id),
    decision       TEXT NOT NULL CHECK (decision IN ('POST_LATE', 'EXCLUDE')),
    posting_period TEXT NOT NULL,
    reason         TEXT NOT NULL CHECK (length(trim(reason)) >= 10),
    actor          TEXT NOT NULL,
    decided_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjustments (
    adj_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    period       TEXT NOT NULL CHECK (length(period) = 7 AND period GLOB '20[0-9][0-9]-[01][0-9]' AND CAST(substr(period, 6, 2) AS INTEGER) BETWEEN 1 AND 12),
    rep_id       TEXT NOT NULL REFERENCES reps(rep_id),
    currency     TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor <> 0),
    category     TEXT NOT NULL CHECK (category IN ('COMMISSION_ADJUSTMENT', 'PAYOUT_CORRECTION')),
    reason       TEXT NOT NULL CHECK (length(trim(reason)) >= 10),
    case_id      TEXT,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS variance_cases (
    case_id           TEXT PRIMARY KEY,
    period            TEXT NOT NULL,
    rep_id            TEXT NOT NULL,
    currency          TEXT NOT NULL,
    expected_minor    INTEGER NOT NULL,
    expected_basis    TEXT NOT NULL CHECK (expected_basis IN ('CLOSED', 'DRAFT', 'NONE')),
    recorded_minor    INTEGER NOT NULL,
    variance_minor    INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('OPEN', 'INVESTIGATING', 'RESOLVED', 'REOPENED', 'CLEARED')),
    reason_code       TEXT,
    owner             TEXT,
    resolution_note   TEXT,
    variance_at_resolution INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (period, rep_id, currency)
);

CREATE TABLE IF NOT EXISTS case_notes (
    note_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL REFERENCES variance_cases(case_id),
    kind       TEXT NOT NULL CHECK (kind IN ('USER', 'SYSTEM')),
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    details_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);

-- ---------------------------------------------------------------- immutability
CREATE TRIGGER IF NOT EXISTS trg_reps_no_update BEFORE UPDATE ON reps BEGIN SELECT RAISE(ABORT, 'reps rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_reps_no_delete BEFORE DELETE ON reps BEGIN SELECT RAISE(ABORT, 'reps rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_contracts_no_update BEFORE UPDATE ON contracts BEGIN SELECT RAISE(ABORT, 'contracts rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_contracts_no_delete BEFORE DELETE ON contracts BEGIN SELECT RAISE(ABORT, 'contracts rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_splits_no_update BEFORE UPDATE ON splits BEGIN SELECT RAISE(ABORT, 'splits rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_splits_no_delete BEFORE DELETE ON splits BEGIN SELECT RAISE(ABORT, 'splits rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_plans_no_update BEFORE UPDATE ON plan_versions BEGIN SELECT RAISE(ABORT, 'plan versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_plans_no_delete BEFORE DELETE ON plan_versions BEGIN SELECT RAISE(ABORT, 'plan versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_assign_no_update BEFORE UPDATE ON plan_assignments BEGIN SELECT RAISE(ABORT, 'plan assignments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_assign_no_delete BEFORE DELETE ON plan_assignments BEGIN SELECT RAISE(ABORT, 'plan assignments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_cash_no_update BEFORE UPDATE ON cash_events BEGIN SELECT RAISE(ABORT, 'cash events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_cash_no_delete BEFORE DELETE ON cash_events BEGIN SELECT RAISE(ABORT, 'cash events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_payout_no_update BEFORE UPDATE ON recorded_payouts BEGIN SELECT RAISE(ABORT, 'recorded payouts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_payout_no_delete BEFORE DELETE ON recorded_payouts BEGIN SELECT RAISE(ABORT, 'recorded payouts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_runs_no_update BEFORE UPDATE ON calc_runs BEGIN SELECT RAISE(ABORT, 'calculation runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_runs_no_delete BEFORE DELETE ON calc_runs BEGIN SELECT RAISE(ABORT, 'calculation runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_lines_no_update BEFORE UPDATE ON calc_lines BEGIN SELECT RAISE(ABORT, 'calculation lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_lines_no_delete BEFORE DELETE ON calc_lines BEGIN SELECT RAISE(ABORT, 'calculation lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_snap_no_update BEFORE UPDATE ON period_snapshots BEGIN SELECT RAISE(ABORT, 'closed snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_snap_no_delete BEFORE DELETE ON period_snapshots BEGIN SELECT RAISE(ABORT, 'closed snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_payables_no_update BEFORE UPDATE ON payables BEGIN SELECT RAISE(ABORT, 'payables are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_payables_no_delete BEFORE DELETE ON payables BEGIN SELECT RAISE(ABORT, 'payables are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_decisions_no_update BEFORE UPDATE ON event_decisions BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_decisions_no_delete BEFORE DELETE ON event_decisions BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_adj_no_update BEFORE UPDATE ON adjustments BEGIN SELECT RAISE(ABORT, 'adjustments are append-only; post a reversing adjustment'); END;
CREATE TRIGGER IF NOT EXISTS trg_adj_no_delete BEFORE DELETE ON adjustments BEGIN SELECT RAISE(ABORT, 'adjustments are append-only; post a reversing adjustment'); END;
CREATE TRIGGER IF NOT EXISTS trg_notes_no_update BEFORE UPDATE ON case_notes BEGIN SELECT RAISE(ABORT, 'case notes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_notes_no_delete BEFORE DELETE ON case_notes BEGIN SELECT RAISE(ABORT, 'case notes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_closed_period_frozen BEFORE UPDATE ON periods WHEN OLD.status = 'CLOSED'
BEGIN SELECT RAISE(ABORT, 'closed periods are frozen'); END;
CREATE TRIGGER IF NOT EXISTS trg_periods_no_delete BEFORE DELETE ON periods BEGIN SELECT RAISE(ABORT, 'periods cannot be deleted'); END;
-- Adjustments may only be written into a period that is not closed.
CREATE TRIGGER IF NOT EXISTS trg_adj_open_period BEFORE INSERT ON adjustments
WHEN EXISTS (SELECT 1 FROM periods p WHERE p.period = NEW.period AND p.status = 'CLOSED')
BEGIN SELECT RAISE(ABORT, 'cannot adjust a closed period'); END;
