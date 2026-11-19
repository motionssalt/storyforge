-- StoryForge D1 Schema (Round 2: SF-004, SF-005, SF-006, SF-007, SF-008, SF-009)

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zernio_keys (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  api_key TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_last4 TEXT NOT NULL,
  last_test_ok INTEGER,
  last_test_msg TEXT,
  last_tested_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zernio_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  account_name TEXT NOT NULL,
  username TEXT,
  profile_id TEXT,
  is_active INTEGER DEFAULT 1,
  raw_json TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(key_id, account_id)
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'render',
  series_title TEXT,
  format TEXT,
  status TEXT NOT NULL,
  publish_mode TEXT NOT NULL DEFAULT 'immediate',
  scheduled_for_utc TEXT,
  timezone TEXT DEFAULT 'Africa/Lagos',
  voice_id TEXT,
  voice_speed TEXT,
  target_wpm INTEGER,
  background_mode TEXT,
  background_index INTEGER,
  background_clip TEXT,
  production_json TEXT,
  progress_pct REAL NOT NULL DEFAULT 0,
  current_stage TEXT,
  error TEXT,
  dry_run INTEGER NOT NULL DEFAULT 0,
  mock_mode INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_parts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  part_number INTEGER NOT NULL,
  title TEXT,
  description TEXT,
  hashtags_json TEXT,
  filename TEXT,
  asset_id INTEGER,
  size_bytes INTEGER,
  duration_sec REAL,
  background_clip TEXT,
  attribution TEXT,
  render_status TEXT NOT NULL DEFAULT 'pending',
  publish_status TEXT NOT NULL DEFAULT 'pending',
  published_at TEXT,
  planned_publish_at TEXT,
  UNIQUE(task_id, part_number)
);

CREATE TABLE IF NOT EXISTS publish_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  part_number INTEGER NOT NULL,
  key_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  account_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  zernio_post_id TEXT,
  platform_post_url TEXT,
  error TEXT,
  published_at TEXT,
  next_eligible_at TEXT,
  payload_preview TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(task_id, part_number, key_id, account_id)
);

CREATE TABLE IF NOT EXISTS task_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  stage TEXT,
  message TEXT NOT NULL,
  progress_pct REAL
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_publish_targets_task ON publish_targets(task_id, part_number, status);
CREATE INDEX IF NOT EXISTS idx_task_parts_task ON task_parts(task_id, part_number);
