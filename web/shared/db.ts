import {
  AppSettings,
  DEFAULT_SETTINGS,
  PublishTargetRow,
  TaskLogRow,
  TaskPartRow,
  TaskRow,
  ZernioAccountRow,
  ZernioKeyRow,
} from "./types";

export async function getSettings(db: D1Database): Promise<AppSettings> {
  const { results } = await db.prepare("SELECT key, value FROM settings").all<{ key: string; value: string }>();
  const map: Record<string, string> = {};
  for (const r of results || []) map[r.key] = r.value;
  return {
    default_voice_id: map.default_voice_id || DEFAULT_SETTINGS.default_voice_id,
    default_background: map.default_background || DEFAULT_SETTINGS.default_background,
    queue_cadence_count: Math.max(1, Math.min(100, Number(map.queue_cadence_count) || DEFAULT_SETTINGS.queue_cadence_count)),
    queue_cadence_unit: (["day", "week", "month"].includes(map.queue_cadence_unit) ? map.queue_cadence_unit : DEFAULT_SETTINGS.queue_cadence_unit) as AppSettings["queue_cadence_unit"],
    part2_delay_minutes: Math.max(1, Math.min(1440, Number(map.part2_delay_minutes) || DEFAULT_SETTINGS.part2_delay_minutes)),
    dry_run: map.dry_run === "true",
    mock_mode: map.mock_mode === "true",
  };
}

export async function saveSettings(db: D1Database, patch: Partial<AppSettings>): Promise<AppSettings> {
  const now = new Date().toISOString();
  const entries: [string, string][] = [];
  if (patch.default_voice_id !== undefined) entries.push(["default_voice_id", String(patch.default_voice_id)]);
  if (patch.default_background !== undefined) entries.push(["default_background", String(patch.default_background)]);
  if (patch.queue_cadence_count !== undefined) entries.push(["queue_cadence_count", String(Math.max(1, Math.min(100, Math.round(Number(patch.queue_cadence_count) || 3))))]);
  if (patch.queue_cadence_unit !== undefined && ["day", "week", "month"].includes(patch.queue_cadence_unit)) entries.push(["queue_cadence_unit", patch.queue_cadence_unit]);
  if (patch.part2_delay_minutes !== undefined) entries.push(["part2_delay_minutes", String(Math.max(1, Math.min(1440, Math.round(Number(patch.part2_delay_minutes) || 20))))]);
  if (patch.dry_run !== undefined) entries.push(["dry_run", patch.dry_run ? "true" : "false"]);
  if (patch.mock_mode !== undefined) entries.push(["mock_mode", patch.mock_mode ? "true" : "false"]);

  if (entries.length > 0) {
    const stmts = entries.map(([k, v]) =>
      db.prepare("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at").bind(k, v, now)
    );
    await db.batch(stmts);
  }
  return getSettings(db);
}

export async function getRuntimeState(db: D1Database, kv: KVNamespace | undefined, key: string, fallback = "0"): Promise<string> {
  const row = await db.prepare("SELECT value FROM runtime_state WHERE key = ?").bind(key).first<{ value: string }>();
  if (row) return row.value;
  if (key === "bg_index" && kv) {
    const kvVal = await kv.get("state:bg_index");
    if (kvVal !== null) {
      await setRuntimeState(db, "bg_index", kvVal);
      return kvVal;
    }
  }
  return fallback;
}

export async function setRuntimeState(db: D1Database, key: string, value: string): Promise<void> {
  const now = new Date().toISOString();
  await db
    .prepare("INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at")
    .bind(key, value, now)
    .run();
}

export async function appendTaskLogs(
  db: D1Database,
  taskId: string,
  logs: Array<{ ts?: string; level?: string; stage?: string | null; message: string; progress_pct?: number | null }>
): Promise<void> {
  if (!logs.length) return;
  const now = new Date().toISOString();
  const stmts = logs.map((l) =>
    db
      .prepare("INSERT INTO task_logs (task_id, ts, level, stage, message, progress_pct) VALUES (?, ?, ?, ?, ?, ?)")
      .bind(
        taskId,
        l.ts || now,
        l.level === "error" || l.level === "warn" ? l.level : "info",
        l.stage ?? null,
        String(l.message || "").slice(0, 4000),
        l.progress_pct ?? null
      )
  );
  await db.batch(stmts);
}

export async function getTaskLogs(db: D1Database, taskId: string, sinceId = 0, limit = 200): Promise<TaskLogRow[]> {
  const { results } = await db
    .prepare("SELECT * FROM task_logs WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?")
    .bind(taskId, sinceId, limit)
    .all<TaskLogRow>();
  return results || [];
}

export async function getTaskFull(db: D1Database, taskId: string) {
  const task = await db.prepare("SELECT * FROM tasks WHERE task_id = ?").bind(taskId).first<TaskRow>();
  if (!task) return null;
  const [partsRes, targetsRes] = await Promise.all([
    db.prepare("SELECT * FROM task_parts WHERE task_id = ? ORDER BY part_number ASC").bind(taskId).all<TaskPartRow>(),
    db.prepare("SELECT * FROM publish_targets WHERE task_id = ? ORDER BY part_number ASC, platform ASC, account_name ASC").bind(taskId).all<PublishTargetRow>(),
  ]);
  return {
    task,
    parts: partsRes.results || [],
    targets: targetsRes.results || [],
  };
}

export async function listTasksFull(db: D1Database, limit = 25) {
  const { results: tasks } = await db
    .prepare("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?")
    .bind(limit)
    .all<TaskRow>();
  if (!tasks || !tasks.length) return [];
  const ids = tasks.map((t) => t.task_id);
  const placeholders = ids.map(() => "?").join(",");
  const [partsRes, targetsRes] = await Promise.all([
    db.prepare(`SELECT * FROM task_parts WHERE task_id IN (${placeholders}) ORDER BY part_number ASC`).bind(...ids).all<TaskPartRow>(),
    db.prepare(`SELECT * FROM publish_targets WHERE task_id IN (${placeholders}) ORDER BY part_number ASC, platform ASC, account_name ASC`).bind(...ids).all<PublishTargetRow>(),
  ]);
  const partsByTask = new Map<string, TaskPartRow[]>();
  for (const p of partsRes.results || []) {
    const arr = partsByTask.get(p.task_id) || [];
    arr.push(p);
    partsByTask.set(p.task_id, arr);
  }
  const targetsByTask = new Map<string, PublishTargetRow[]>();
  for (const tgt of targetsRes.results || []) {
    const arr = targetsByTask.get(tgt.task_id) || [];
    arr.push(tgt);
    targetsByTask.set(tgt.task_id, arr);
  }
  return tasks.map((t) => {
    const { production_json: _omitted, ...summary } = t;
    return {
      ...summary,
      parts: (partsByTask.get(t.task_id) || []).map((p) => ({
        ...p,
        hashtags: p.hashtags_json ? JSON.parse(p.hashtags_json) : [],
      })),
      publish_targets: targetsByTask.get(t.task_id) || [],
    };
  });
}

export async function listZernioKeys(db: D1Database): Promise<ZernioKeyRow[]> {
  const { results } = await db.prepare("SELECT * FROM zernio_keys ORDER BY created_at ASC").all<ZernioKeyRow>();
  return results || [];
}

export async function listZernioAccounts(db: D1Database): Promise<ZernioAccountRow[]> {
  const { results } = await db.prepare("SELECT * FROM zernio_accounts WHERE is_active = 1 ORDER BY platform ASC, account_name ASC").all<ZernioAccountRow>();
  return results || [];
}
