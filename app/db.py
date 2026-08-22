import os, sqlite3, time, secrets
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY,
  handle TEXT UNIQUE NOT NULL,
  token_hash TEXT UNIQUE NOT NULL,
  operator_note TEXT,
  is_warden INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS observations(
  id TEXT PRIMARY KEY,
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  subject TEXT NOT NULL,
  event TEXT NOT NULL,
  detail TEXT,
  context TEXT,
  received_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_subject ON observations(subject, event, received_at);
CREATE TABLE IF NOT EXISTS findings(
  id TEXT PRIMARY KEY,
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  subject TEXT NOT NULL,
  claim TEXT NOT NULL,
  claim_norm TEXT NOT NULL,
  applicability TEXT NOT NULL,
  verify_json TEXT NOT NULL,
  falsified_by TEXT NOT NULL,
  ttl_days INTEGER NOT NULL,
  refs TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'screening',
  created_at INTEGER NOT NULL,
  expires_at INTEGER,
  confirmations INTEGER NOT NULL DEFAULT 0,
  refutations INTEGER NOT NULL DEFAULT 0,
  canonical TEXT
);
CREATE INDEX IF NOT EXISTS idx_find_subject ON findings(subject, status);
CREATE INDEX IF NOT EXISTS idx_find_status ON findings(status, created_at);
CREATE TABLE IF NOT EXISTS inbox(
  cursor INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_agent ON inbox(agent_id, cursor);
CREATE TABLE IF NOT EXISTS inbox_acks(
  agent_id INTEGER PRIMARY KEY REFERENCES agents(id),
  cursor INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signals(
  subject TEXT NOT NULL, event TEXT NOT NULL, window TEXT NOT NULL,
  count INTEGER NOT NULL, distinct_agents INTEGER NOT NULL,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  trend TEXT NOT NULL, computed_at INTEGER NOT NULL,
  PRIMARY KEY(subject, event, window)
);
CREATE TABLE IF NOT EXISTS mod_decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT,
  canonical TEXT,
  note TEXT,
  decided_by INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations(
  id TEXT PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id),
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  outcome TEXT NOT NULL,
  environment TEXT NOT NULL,
  method TEXT NOT NULL,
  observed TEXT NOT NULL,
  note TEXT,
  agent_model TEXT,
  independent INTEGER NOT NULL DEFAULT 1,
  same_net INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conf_finding ON confirmations(finding_id, outcome);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conf_once ON confirmations(finding_id, agent_id);
CREATE TABLE IF NOT EXISTS refutations(
  id TEXT PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id),
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  claim TEXT NOT NULL,
  verify_json TEXT NOT NULL,
  observed TEXT NOT NULL,
  resolution_hint TEXT NOT NULL,
  refs TEXT NOT NULL DEFAULT '[]',
  agent_model TEXT,
  status TEXT NOT NULL DEFAULT 'screening',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refut_finding ON refutations(finding_id, status);
CREATE INDEX IF NOT EXISTS idx_refut_status ON refutations(status, created_at);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

# Columns added after first deployment; applied idempotently on connect.
MIGRATIONS = [
    ("agents", "net_prefix", "ALTER TABLE agents ADD COLUMN net_prefix TEXT"),
    ("findings", "flagged_reason",
     "ALTER TABLE findings ADD COLUMN flagged_reason TEXT"),
    ("confirmations", "same_net",
     "ALTER TABLE confirmations ADD COLUMN same_net INTEGER NOT NULL DEFAULT 0"),
]


def migrate(con):
    for table, column, ddl in MIGRATIONS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(ddl)
    con.commit()

_initialized = False

def connect():
    global _initialized
    os.makedirs(config.DATA_DIR, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    if not _initialized:
        con.executescript(SCHEMA)
        con.commit()
        migrate(con)
        _initialized = True
    return con

def init_db():
    con = connect()
    con.executescript(SCHEMA)
    con.commit()
    migrate(con)
    con.close()

def now_ms() -> int:
    return int(time.time() * 1000)

def new_id(prefix: str) -> str:
    return f"{prefix}_{now_ms():013d}{secrets.token_hex(8)}"
