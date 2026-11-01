from __future__ import annotations

LATEST_SCHEMA_VERSION = 16

SOURCE_CURSOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_history_cursors (
    channel_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL CHECK(message_id >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

ZONE_HISTORY_SCHEMA_SQL = """
CREATE TABLE zone_history_queries (
    symbol TEXT NOT NULL,
    lane TEXT NOT NULL CHECK(lane IN ('recent','archive')),
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    cursor TEXT NOT NULL DEFAULT '',
    seen_cursors_json TEXT NOT NULL DEFAULT '[]',
    last_attempt_ms INTEGER NOT NULL DEFAULT 0,
    limited_before_ms INTEGER NOT NULL,
    last_error TEXT,
    completed_start_ms INTEGER,
    completed_end_ms INTEGER,
    completed_at_ms INTEGER,
    PRIMARY KEY(symbol,lane),
    CHECK(start_ms > 0 AND end_ms >= start_ms),
    CHECK(end_ms - start_ms <= 604800000)
);
"""

ZONE_LEDGER_SCHEMA_SQL = """
CREATE TABLE zone_groups (
    group_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT'))
);
CREATE TABLE zone_allocations (
    zone_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES zone_groups(group_id),
    plan_json TEXT NOT NULL,
    source_json TEXT NOT NULL
);
CREATE INDEX ix_zone_allocations_group ON zone_allocations(group_id);
CREATE TABLE zone_orders (
    order_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL REFERENCES zone_allocations(zone_id),
    order_link_id TEXT UNIQUE CHECK(order_link_id IS NULL OR length(order_link_id)>0),
    payload_json TEXT NOT NULL
);
CREATE TABLE zone_executions (
    execution_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES zone_orders(order_id),
    payload_json TEXT NOT NULL
);
CREATE INDEX ix_zone_executions_order ON zone_executions(order_id);
"""

SOURCE_DECISION_SCHEMA_SQL = """
CREATE TABLE source_decisions (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    claim_token TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(channel_id, message_id, claim_token)
);
CREATE INDEX ix_source_decisions_attempt
ON source_decisions(channel_id, message_id, attempt_count);
"""

RECOVERY_NOTICE_SCHEMA_SQL = """
CREATE TABLE interrupted_execution_notices (
    kind TEXT NOT NULL CHECK(kind IN ('OPEN', 'ACTION')),
    record_id TEXT NOT NULL,
    PRIMARY KEY(kind, record_id)
);
"""

LATEST_SCHEMA_SQL = "\nCREATE TABLE source_messages (\n    channel_id INTEGER NOT NULL,\n    message_id INTEGER NOT NULL,\n    payload_json TEXT NOT NULL,\n    status TEXT NOT NULL DEFAULT 'COMPLETED',\n    attempt_count INTEGER NOT NULL DEFAULT 1,\n    last_error TEXT,\n    claim_token TEXT,\n    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n    PRIMARY KEY (channel_id, message_id)\n);\n\nCREATE TABLE intents (\n    intent_id TEXT PRIMARY KEY,\n    channel_id INTEGER NOT NULL,\n    message_id INTEGER NOT NULL,\n    payload_json TEXT NOT NULL,\n    status TEXT NOT NULL,\n    approval_mode TEXT NOT NULL DEFAULT 'MANUAL',\n    decision_user_id INTEGER,\n    bybit_order_id TEXT,\n    error TEXT,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL\n);\n\nCREATE INDEX ix_intents_approval_status\nON intents(approval_mode, status);\n\nCREATE TABLE signal_guidance (\n    scope TEXT NOT NULL CHECK(scope IN ('global', 'channel')),\n    channel_id INTEGER,\n    content TEXT NOT NULL,\n    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n    CHECK(\n        (scope = 'global' AND channel_id IS NULL)\n        OR\n        (scope = 'channel' AND channel_id IS NOT NULL)\n    )\n);\n\nCREATE UNIQUE INDEX ux_signal_guidance_global\nON signal_guidance(scope)\nWHERE scope = 'global';\n\nCREATE UNIQUE INDEX ux_signal_guidance_channel\nON signal_guidance(channel_id)\nWHERE scope = 'channel';\n\nCREATE TABLE execution_policy (\n    id INTEGER PRIMARY KEY CHECK(id = 1),\n    risk_per_trade_pct TEXT NOT NULL,\n    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\n\nCREATE TABLE execution_plans (\n    intent_id TEXT PRIMARY KEY,\n    payload_json TEXT NOT NULL,\n    bybit_order_ids_json TEXT,\n    created_at TEXT NOT NULL\n);\n\nCREATE TABLE position_strategies (\n    strategy_id TEXT PRIMARY KEY,\n    symbol TEXT NOT NULL,\n    side TEXT NOT NULL,\n    status TEXT NOT NULL,\n    entry_frozen INTEGER NOT NULL DEFAULT 0,\n    base_position_qty TEXT,\n    last_position_qty TEXT,\n    last_avg_price TEXT,\n    tp1_done INTEGER NOT NULL DEFAULT 0,\n    tp2_done INTEGER NOT NULL DEFAULT 0,\n    tp3_done INTEGER NOT NULL DEFAULT 0,\n    trailing_active INTEGER NOT NULL DEFAULT 0,\n    protected_stop_loss TEXT,\n    trailing_distance TEXT,\n    exit_revision INTEGER NOT NULL DEFAULT 0,\n    rebalance_needed INTEGER NOT NULL DEFAULT 0,\n    installing_exits INTEGER NOT NULL DEFAULT 0,\n    expected_exits_json TEXT,\n    pause_reason TEXT,\n    pause_notified INTEGER NOT NULL DEFAULT 0,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL\n);\n\nCREATE INDEX ix_position_strategies_active\nON position_strategies(status, symbol);\n\nCREATE TABLE position_actions (\n    action_id TEXT PRIMARY KEY,\n    channel_id INTEGER NOT NULL,\n    message_id INTEGER NOT NULL,\n    payload_json TEXT NOT NULL,\n    status TEXT NOT NULL,\n    approval_mode TEXT NOT NULL DEFAULT 'MANUAL',\n    decision_user_id INTEGER,\n    bybit_order_id TEXT,\n    error TEXT,\n    created_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL\n);\n\nCREATE INDEX ix_position_actions_source\nON position_actions(channel_id, message_id);\n\nCREATE INDEX ix_position_actions_approval_status\nON position_actions(approval_mode, status);\n\nCREATE TABLE account_closed_pnl (\n    record_id TEXT PRIMARY KEY,\n    order_id TEXT NOT NULL,\n    symbol TEXT NOT NULL,\n    position_side TEXT NOT NULL,\n    closed_pnl TEXT NOT NULL,\n    closed_size TEXT NOT NULL,\n    avg_entry_price TEXT,\n    avg_exit_price TEXT,\n    closed_at TEXT NOT NULL,\n    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\n\nCREATE INDEX ix_account_closed_pnl_closed_at\nON account_closed_pnl(closed_at);\n\nCREATE TABLE account_pnl_sync (\n    id INTEGER PRIMARY KEY CHECK(id = 1),\n    history_start_at TEXT NOT NULL,\n    last_synced_at TEXT NOT NULL,\n    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\n\nINSERT INTO execution_policy(\n    id,\n    risk_per_trade_pct\n)\nVALUES (1, '1');\n\nCREATE TABLE manual_deliveries (\n    record_id TEXT PRIMARY KEY,\n    kind TEXT NOT NULL CHECK(kind IN ('OPEN', 'ACTION')),\n    claim_token TEXT,\n    retry_after TEXT,\n    delivered_at TEXT\n);\n"

LATEST_SCHEMA_SQL += RECOVERY_NOTICE_SCHEMA_SQL
LATEST_SCHEMA_SQL += SOURCE_DECISION_SCHEMA_SQL
LATEST_SCHEMA_SQL += ZONE_LEDGER_SCHEMA_SQL
LATEST_SCHEMA_SQL += ZONE_HISTORY_SCHEMA_SQL
LATEST_SCHEMA_SQL += SOURCE_CURSOR_SCHEMA_SQL

PLANNING_CANDIDATE_SCHEMA_SQL = """
CREATE TABLE planning_candidates (
    intent_id TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    duplicate_in_batch INTEGER NOT NULL CHECK(duplicate_in_batch IN (0, 1)),
    deadline TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING', 'TRANSFERRED', 'REJECTED', 'EXPIRED')),
    claim_token TEXT,
    retry_after TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_planning_candidates_due
ON planning_candidates(status, retry_after);
"""

LATEST_SCHEMA_SQL += PLANNING_CANDIDATE_SCHEMA_SQL
