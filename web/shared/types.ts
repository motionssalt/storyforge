export interface Env {
  STORYFORGE_VAULT: KVNamespace;
  STORYFORGE_DB: D1Database;
  AUTH_PASSWORD: string;
  SESSION_SECRET: string;
  INTERNAL_SECRET: string;
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
}

export type PublishMode = "immediate" | "schedule" | "queue";
export type CadenceUnit = "day" | "week" | "month";

export interface AppSettings {
  default_voice_id: string;
  voicestudio_voice_id: string;
  voice_engine: "edge" | "voicestudio";
  voicestudio_speedup: number;
  sample_text: string;
  default_background: string; // "sequential" or clip filename
  queue_cadence_count: number;
  queue_cadence_unit: CadenceUnit;
  part2_delay_minutes: number;
  dry_run: boolean;
  mock_mode: boolean;
}

export const DEFAULT_SAMPLE_TEXT =
  "I found the spare key taped inside the kitchen vent, right behind a stack of old takeout menus. For three weeks everyone swore nobody had been in that apartment, until I checked the hallway camera and saw the door open.";

export const DEFAULT_SETTINGS: AppSettings = {
  default_voice_id: "en-US-AndrewMultilingualNeural",
  voicestudio_voice_id: "vs-harper",
  voice_engine: "voicestudio",
  voicestudio_speedup: 1.25,
  sample_text: DEFAULT_SAMPLE_TEXT,
  default_background: "sequential",
  queue_cadence_count: 3,
  queue_cadence_unit: "day",
  part2_delay_minutes: 20,
  dry_run: false,
  mock_mode: false,
};

export interface ZernioKeyRow {
  id: string;
  label: string;
  api_key: string;
  key_prefix: string;
  key_last4: string;
  last_test_ok: number | null;
  last_test_msg: string | null;
  last_tested_at: string | null;
  created_at: string;
}

export interface ZernioAccountRow {
  id: number;
  key_id: string;
  account_id: string;
  platform: string;
  account_name: string;
  username: string | null;
  profile_id: string | null;
  is_active: number;
  raw_json: string | null;
  updated_at: string;
}

export interface TaskRow {
  task_id: string;
  kind: "render" | "bg_import" | "voice_sample";
  series_title: string | null;
  format: string | null;
  status: string;
  publish_mode: PublishMode;
  scheduled_for_utc: string | null;
  timezone: string | null;
  voice_id: string | null;
  voice_speed: string | null;
  target_wpm: number | null;
  background_mode: string | null;
  background_index: number | null;
  background_clip: string | null;
  production_json: string | null;
  progress_pct: number;
  current_stage: string | null;
  error: string | null;
  dry_run: number;
  mock_mode: number;
  created_at: string;
  updated_at: string;
}

export interface TaskPartRow {
  id: number;
  task_id: string;
  part_number: number;
  title: string | null;
  description: string | null;
  hashtags_json: string | null;
  filename: string | null;
  asset_id: number | null;
  size_bytes: number | null;
  duration_sec: number | null;
  background_clip: string | null;
  attribution: string | null;
  render_status: string;
  publish_status: string;
  published_at: string | null;
  planned_publish_at: string | null;
}

export interface PublishTargetRow {
  id: number;
  task_id: string;
  part_number: number;
  key_id: string;
  account_id: string;
  platform: string;
  account_name: string;
  status: string;
  zernio_post_id: string | null;
  platform_post_url: string | null;
  error: string | null;
  published_at: string | null;
  next_eligible_at: string | null;
  payload_preview: string | null;
  updated_at: string;
}

export interface TaskLogRow {
  id: number;
  task_id: string;
  ts: string;
  level: "info" | "warn" | "error";
  stage: string | null;
  message: string;
  progress_pct: number | null;
}
