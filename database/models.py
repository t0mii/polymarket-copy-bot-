SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    username TEXT,
    rank INTEGER,
    volume REAL,
    pnl REAL,
    markets_traded INTEGER,
    score INTEGER,
    strategy_type TEXT,
    strengths TEXT,
    weaknesses TEXT,
    recommendation TEXT,
    reasoning TEXT,
    win_rate REAL,
    total_trades INTEGER,
    profile_url TEXT,
    followed INTEGER DEFAULT 0,
    baseline_scanned INTEGER DEFAULT 0,
    roi REAL DEFAULT 0,
    last_scanned TEXT DEFAULT (datetime('now')),
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallets_scanned INTEGER,
    wallets_filtered INTEGER,
    wallets_analyzed INTEGER,
    top_score INTEGER,
    report_path TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS wallet_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    pnl REAL,
    volume REAL,
    win_rate REAL,
    score INTEGER,
    rank INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (address) REFERENCES wallets(address)
);

CREATE TABLE IF NOT EXISTS copy_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    wallet_username TEXT,
    market_question TEXT NOT NULL,
    market_slug TEXT,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL,
    size REAL NOT NULL,
    pnl_unrealized REAL DEFAULT 0,
    pnl_realized REAL,
    status TEXT DEFAULT 'open',
    end_date TEXT,
    miss_count INTEGER DEFAULT 0,
    outcome_label TEXT DEFAULT '',
    event_slug TEXT DEFAULT '',
    condition_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    closed_at TEXT,
    FOREIGN KEY (wallet_address) REFERENCES wallets(address)
);

CREATE TABLE IF NOT EXISTS copy_portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_value REAL,
    cash_balance REAL,
    open_positions_value REAL,
    pnl_total REAL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS save_point (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value REAL NOT NULL DEFAULT 50,
    is_stopped INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO save_point (id, value, is_stopped) VALUES (1, 50, 0);

CREATE TABLE IF NOT EXISTS trader_position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    market_question TEXT,
    side TEXT,
    size REAL,
    current_price REAL,
    is_open INTEGER DEFAULT 1,
    snapshot_time TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (wallet_address) REFERENCES wallets(address),
    UNIQUE(wallet_address, condition_id, is_open)
);

CREATE TABLE IF NOT EXISTS trader_position_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    open_position_count INTEGER,
    closed_position_count INTEGER,
    new_positions_detected INTEGER DEFAULT 0,
    closed_positions_detected INTEGER DEFAULT 0,
    scan_time TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS trader_closed_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    market_question TEXT,
    side TEXT,
    closed_price REAL,
    pnl_actual REAL,
    last_seen_at TEXT DEFAULT (datetime('now','localtime')),
    is_matched INTEGER DEFAULT 0,
    FOREIGN KEY (wallet_address) REFERENCES wallets(address),
    UNIQUE(wallet_address, condition_id)
);

CREATE TABLE IF NOT EXISTS trader_scan_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    last_position_count INTEGER DEFAULT 0,
    target_scan_count INTEGER DEFAULT 100,
    scans_completed INTEGER DEFAULT 0,
    last_closed_count INTEGER DEFAULT 0,
    last_trade_timestamp INTEGER DEFAULT 0,
    scan_cycle_started_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (wallet_address) REFERENCES wallets(address),
    UNIQUE(wallet_address)
);

CREATE TABLE IF NOT EXISTS confirmed_new_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    market_question TEXT,
    side TEXT,
    entry_price REAL,
    detected_at TEXT DEFAULT (datetime('now','localtime')),
    confirmed_at TEXT,
    is_confirmed INTEGER DEFAULT 0,
    FOREIGN KEY (wallet_address) REFERENCES wallets(address),
    UNIQUE(wallet_address, condition_id)
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    icon TEXT DEFAULT '',
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    pnl REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_activity_time ON activity_log(created_at);

CREATE TABLE IF NOT EXISTS ai_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_text TEXT NOT NULL,
    data_snapshot TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_copy_trades_wallet ON copy_trades(wallet_address);
CREATE INDEX IF NOT EXISTS idx_copy_trades_condition ON copy_trades(condition_id);
CREATE INDEX IF NOT EXISTS idx_copy_trades_status ON copy_trades(status);
CREATE INDEX IF NOT EXISTS idx_snapshots_wallet ON trader_position_snapshots(wallet_address);
CREATE INDEX IF NOT EXISTS idx_closed_pos_wallet ON trader_closed_positions(wallet_address, condition_id);
"""
