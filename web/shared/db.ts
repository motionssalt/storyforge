import {
  AppSettings,
  DEFAULT_SAMPLE_TEXT,
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
    voice_engine: map.voice_engine === "edge" ? "edge" : "voicestudio",
    voicestudio_voice_id: map.voicestudio_voice_id || DEFAULT_SETTINGS.voicestudio_voice_id,
    voicestudio_speedup: Math.max(1, Math.min(2, Number(map.voicestudio_speedup) || 1.25)),
    sample_text: (map.sample_text || "").trim() || DEFAULT_SAMPLE_TEXT,
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
  if (patch.voice_engine !== undefined) entries.push(["voice_engine", patch.voice_engine]);
  if (patch.voicestudio_voice_id !== undefined) entries.push(["voicestudio_voice_id", patch.voicestudio_voice_id]);
  if (patch.voicestudio_speedup !== undefined) entries.push(["voicestudio_speedup", String(patch.voicestudio_speedup)]);
  if (patch.sample_text !== undefined) entries.push(["sample_text", String(patch.sample_text).trim() || DEFAULT_SAMPLE_TEXT]);
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

export async function deleteRuntimeState(db: D1Database, key: string): Promise<void> {
  await db.prepare("DELETE FROM runtime_state WHERE key = ?").bind(key).run();
}

export async function isTaskDeleted(db: D1Database, taskId: string): Promise<boolean> {
  if (!taskId) return false;
  const row = await db
    .prepare("SELECT value FROM runtime_state WHERE key = ?")
    .bind(`deleted_task:${taskId}`)
    .first<{ value: string }>();
  return Boolean(row);
}

export async function markTaskDeleted(
  db: D1Database,
  taskId: string,
  meta: { kind?: string; series_title?: string | null; status?: string | null } = {}
): Promise<void> {
  const value = JSON.stringify({
    deleted_at: new Date().toISOString(),
    kind: meta.kind || "render",
    series_title: meta.series_title || null,
    prior_status: meta.status || null,
  });
  await setRuntimeState(db, `deleted_task:${taskId}`, value);
}

export async function setTaskWorkflowRunId(db: D1Database, taskId: string, runId: string | number): Promise<void> {
  const cleaned = String(runId || "").trim();
  if (!taskId || !/^\d+$/.test(cleaned)) return;
  await setRuntimeState(db, `task_run:${taskId}`, cleaned);
}

export async function getTaskWorkflowRunId(db: D1Database, taskId: string): Promise<string | null> {
  if (!taskId) return null;
  const row = await db
    .prepare("SELECT value FROM runtime_state WHERE key = ?")
    .bind(`task_run:${taskId}`)
    .first<{ value: string }>();
  return row?.value || null;
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


export interface ClonedSlotMeta {
  filename: string;
  content_type: string;
  size_bytes: number;
  ref_text: string;
  auto_transcribed?: boolean;
  prompt_cached?: boolean;
  preview_ready?: boolean;
  updated_at: string;
}

export interface CustomVoiceRecord {
  id: string;
  kind: "designed" | "cloned";
  engine: "voicestudio";
  name: string;
  descriptor: string;
  instruct?: string;
  seed?: number;
  sample_ready?: boolean;
  is_my_voice?: boolean;
  slots?: Record<string, ClonedSlotMeta>;
  created_at: string;
  updated_at: string;
}

export async function listCustomVoices(db: D1Database): Promise<CustomVoiceRecord[]> {
  const { results } = await db
    .prepare("SELECT key, value FROM runtime_state WHERE key LIKE 'custom_voice:%' ORDER BY updated_at ASC")
    .all<{ key: string; value: string }>();
  const out: CustomVoiceRecord[] = [];
  for (const r of results || []) {
    try {
      const parsed = JSON.parse(r.value);
      if (parsed && parsed.id) out.push(parsed);
    } catch {
      /* ignore malformed entry */
    }
  }
  return out;
}

export async function getCustomVoice(db: D1Database, voiceId: string): Promise<CustomVoiceRecord | null> {
  if (!voiceId) return null;
  const raw = await getRuntimeState(db, undefined, `custom_voice:${voiceId}`, "");
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function saveCustomVoice(db: D1Database, voice: CustomVoiceRecord): Promise<void> {
  await setRuntimeState(db, `custom_voice:${voice.id}`, JSON.stringify(voice));
}

export async function deleteCustomVoice(db: D1Database, voiceId: string): Promise<void> {
  await deleteRuntimeState(db, `custom_voice:${voiceId}`);
}
