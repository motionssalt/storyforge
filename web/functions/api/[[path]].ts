import { normalizeProduction } from '../../shared/production.mjs';
import validateSchema from "../../lib/production-validator.js";
import {
  appendTaskLogs,
  CustomVoiceRecord,
  deleteCustomVoice,
  getCustomVoice,
  getRuntimeState,
  getSettings,
  getTaskFull,
  getTaskLogs,
  isTaskDeleted,
  listCustomVoices,
  listTasksFull,
  listZernioAccounts,
  listZernioKeys,
  saveCustomVoice,
  saveSettings,
  setRuntimeState,
  setTaskWorkflowRunId,
} from "../../shared/db";
import {
  BUILTIN_EDGE_VOICES,
  BUILTIN_VOICESTUDIO_MAP,
  BUILTIN_VOICESTUDIO_VOICES,
  deleteKvBinary,
  getKvBinary,
  putKvBinary,
  VALID_CLONE_SLOTS,
  validateOmnivoiceInstruct,
} from "../../shared/voices";
import {
  cleanupPublishedTask,
  deleteGitHubRenderAssetsForTask,
  deleteTaskById,
  initializePublishingForTask,
  processPublishingTick,
  retryFailedTargetsForTask,
  triggerWaitingTasksOnKeysReady,
} from "../../shared/publisher";
import { AppSettings, CadenceUnit, Env, PublishMode, ZernioKeyRow } from "../../shared/types";
import { renderAgentPrompt } from "../../shared/prompt";
import { getZernioAdapter } from "../../shared/zernio";

const VALID_EMOTIONS = new Set(["calm", "tense", "angry", "shocked", "sad", "sarcastic", "whisper", "excited"]);
const VALID_THEMES = new Set(["dark", "light"]);
const VALID_AWARD_TYPES = new Set(["gold", "helpful", "heart"]);
const VALID_CADENCE_UNITS = new Set<CadenceUnit>(["day", "week", "month"]);
const VALID_PUBLISH_MODES = new Set<PublishMode>(["immediate", "schedule", "queue"]);
const MAX_UPLOAD_BODY = 99614720; // 95 MiB

function formatInstancePath(raw: string): string {
  if (!raw) return "root";
  return raw
    .slice(1)
    .split("/")
    .map((seg) => (/^\d+$/.test(seg) ? `[${seg}]` : `.${seg}`))
    .join("")
    .replace(/^\./, "");
}

function unescapeMarkdownArtifacts(text: string): { result: string; changed: boolean } {
  let changed = false;
  const result = text.replace(/(\\+)([\[\]_*])/g, (full, slashes: string, ch: string) => {
    if (slashes.length % 2 === 1) {
      changed = true;
      return slashes.slice(0, -1) + ch;
    }
    return full;
  });
  return { result, changed };
}

function repairPastedProductionText(raw: string): { ok: true; parsed: any; repairs: string[] } | { ok: false; error: string } {
  try {
    return { ok: true, parsed: JSON.parse(raw), repairs: [] };
  } catch {
    // Fall through to deterministic artifact repairs
  }
  const repairs: string[] = [];
  let candidate = String(raw || "").trim();

  const fenceMatch = candidate.match(/```(?:json|JSON)?\s*\r?\n?([\s\S]*?)\r?\n?```/);
  if (fenceMatch && fenceMatch[1] && fenceMatch[1].includes("{")) {
    candidate = fenceMatch[1].trim();
    repairs.push("stripped markdown code fence");
  }

  const firstBrace = candidate.indexOf("{");
  const lastBrace = candidate.lastIndexOf("}");
  if (firstBrace !== -1 && lastBrace > firstBrace) {
    if (firstBrace > 0 || lastBrace < candidate.length - 1) {
      candidate = candidate.slice(firstBrace, lastBrace + 1);
      repairs.push("dropped text outside outer { ... }");
    }
  }

  const unescaped = unescapeMarkdownArtifacts(candidate);
  if (unescaped.changed) {
    candidate = unescaped.result;
    repairs.push("removed stray markdown escapes (\\[, \\], \\_, \\*)");
  }

  try {
    const parsed = JSON.parse(candidate);
    return { ok: true, parsed, repairs };
  } catch (e: any) {
    return { ok: false, error: `Invalid JSON syntax: ${e?.message || "unable to parse JSON"}` };
  }
}

function jsonResp(data: unknown, status = 200, extraHeaders: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...extraHeaders,
    },
  });
}

async function hmacHex(secret: string, message: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function parseCookies(cookieHeader: string | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!cookieHeader) return out;
  for (const part of cookieHeader.split(";")) {
    const idx = part.indexOf("=");
    if (idx > 0) {
      try {
        out[part.slice(0, idx).trim()] = decodeURIComponent(part.slice(idx + 1).trim());
      } catch {
        /* Invalid cookies are unauthenticated */
      }
    }
  }
  return out;
}

export async function isAuthenticated(request: Request, env: Env): Promise<boolean> {
  const headerPw = request.headers.get("X-Auth-Password");
  if (headerPw && env.AUTH_PASSWORD && headerPw === env.AUTH_PASSWORD) {
    return true;
  }
  const cookies = parseCookies(request.headers.get("Cookie"));
  const tok = cookies["sf_session"];
  if (!tok || !env.SESSION_SECRET) return false;
  const parts = tok.split(".");
  if (parts.length !== 2) return false;
  const [expStr, sig] = parts;
  const exp = Number(expStr);
  if (!Number.isFinite(exp) || Date.now() > exp) return false;
  const expected = await hmacHex(env.SESSION_SECRET, expStr);
  return sig === expected;
}

function validateProductionJson(doc: any): string[] {
  const errors: string[] = [];
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
    return ["Root value must be a JSON object."];
  }
  if ("$schema" in doc || "$defs" in doc || ("properties" in doc && !("parts" in doc))) {
    errors.push("It looks like the AI echoed the JSON Schema itself instead of producing story data.");
  }
  if (!validateSchema(doc)) {
    for (const e of (validateSchema.errors || []) as any[]) {
      const p = formatInstancePath(e.instancePath || "");
      if (e.keyword === "required" && e.params?.missingProperty) {
        errors.push(`${p}: missing required field "${e.params.missingProperty}".`);
      } else if (e.keyword === "additionalProperties" && e.params?.additionalProperty) {
        errors.push(`${p}: unexpected field "${e.params.additionalProperty}".`);
      } else if (e.keyword === "enum" && Array.isArray(e.params?.allowedValues)) {
        errors.push(`${p}: must be one of ${e.params.allowedValues.join(", ")}.`);
      } else {
        errors.push(`${p}: ${e.message}`);
      }
    }
    return errors;
  }
  if (doc.schema_version !== 3) {
    errors.push("schema_version: must be 3.");
  }
  if (typeof doc.series_title !== "string" || doc.series_title.trim().length < 2) {
    errors.push("series_title: must be a non-empty string (at least 2 characters).");
  }
  if (doc.format !== "single" && doc.format !== "two_part") {
    errors.push('format: must be "single" or "two_part".');
  }
  if (!Array.isArray(doc.parts) || doc.parts.length === 0) {
    errors.push("parts: must be a non-empty array.");
    return errors;
  }
  if (doc.format === "single" && doc.parts.length !== 1) {
    errors.push(`parts: expected 1 part for format "single", got ${doc.parts.length}.`);
  }
  if (doc.format === "two_part" && doc.parts.length !== 2) {
    errors.push(`parts: expected 2 parts for format "two_part", got ${doc.parts.length}.`);
  }
  doc.parts.forEach((part: any, pi: number) => {
    const p = `parts[${pi}]`;
    if (!part || typeof part !== "object") {
      errors.push(`${p}: must be an object.`);
      return;
    }
    if (part.part_number !== pi + 1) errors.push(`${p}.part_number: expected ${pi + 1}; parts must be in order.`);
    if (typeof part.title !== "string" || part.title.trim().length < 3) {
      errors.push(`${p}.title: must be a string (at least 3 characters).`);
    }
    if (typeof part.description !== "string" || part.description.trim().length < 10) {
      errors.push(`${p}.description: must be a social post description string (at least 10 characters).`);
    }
    if (!Array.isArray(part.hashtags) || part.hashtags.length === 0) {
      errors.push(`${p}.hashtags: must be a non-empty array of #hashtag strings.`);
    } else {
      part.hashtags.forEach((h: any, hi: number) => {
        if (typeof h !== "string" || !/^#[A-Za-z0-9_]+$/.test(h)) {
          errors.push(`${p}.hashtags[${hi}]: must start with # and contain only letters, numbers, or underscores.`);
        }
      });
    }
    if (typeof part.story_so_far !== "string") {
      errors.push(`${p}.story_so_far: must be a string.`);
    }
    if (!part.post || typeof part.post !== "object") {
      errors.push(`${p}.post: missing required post object.`);
    } else {
      if (!VALID_THEMES.has(part.post.theme)) errors.push(`${p}.post.theme: must be "dark" or "light".`);
      if (typeof part.post.subreddit !== "string" || !part.post.subreddit.trim()) {
        errors.push(`${p}.post.subreddit: must be a non-empty string.`);
      }
      if (typeof part.post.username !== "string" || !part.post.username.trim()) {
        errors.push(`${p}.post.username: must be a non-empty string.`);
      }
      if (typeof part.post.title !== "string" || !part.post.title.trim()) {
        errors.push(`${p}.post.title: must be a non-empty string.`);
      }
      if (Array.isArray(part.post.awards)) {
        part.post.awards.forEach((aw: any, ai: number) => {
          if (!aw || !VALID_AWARD_TYPES.has(aw.type)) {
            errors.push(`${p}.post.awards[${ai}].type: must be gold, helpful, or heart.`);
          }
        });
      }
    }
    const lineIds = new Set<string>();
    if (!Array.isArray(part.lines) || part.lines.length < 3) {
      errors.push(`${p}.lines: must be an array with at least 3 narration lines.`);
    } else {
      part.lines.forEach((ln: any, li: number) => {
        const lp = `${p}.lines[${li}]`;
        if (!ln || typeof ln !== "object") {
          errors.push(`${lp}: must be an object.`);
          return;
        }
        if (typeof ln.id !== "string" || !/^L[0-9]+$/.test(ln.id)) {
          errors.push(`${lp}.id: must match ^L[0-9]+$ (e.g. "L1").`);
        } else if (lineIds.has(ln.id)) {
          errors.push(`${lp}.id: duplicate line id "${ln.id}".`);
        } else {
          lineIds.add(ln.id);
        }
        if (typeof ln.text !== "string" || ln.text.trim().length === 0) {
          errors.push(`${lp}.text: must be a non-empty string.`);
        }
        if (!VALID_EMOTIONS.has(ln.emotion)) {
          errors.push(`${lp}.emotion: "${ln.emotion}" is invalid (allowed: ${Array.from(VALID_EMOTIONS).join(", ")}).`);
        }
      });
    }
  });
  return errors;
}

function ghHeaders(env: Env, extra: Record<string, string> = {}): Record<string, string> {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "StoryForge-Worker",
    ...extra,
  };
}

async function getOrCreateBackgroundsRelease(env: Env): Promise<any> {
  const repo = env.GITHUB_REPO || "motionssalt/storyforge";
  const res = await fetch(`https://api.github.com/repos/${repo}/releases/tags/backgrounds`, {
    headers: ghHeaders(env),
  });
  if (res.ok) return res.json();
  if (res.status !== 404) {
    throw new Error(`GitHub backgrounds release lookup failed (${res.status})`);
  }
  const createRes = await fetch(`https://api.github.com/repos/${repo}/releases`, {
    method: "POST",
    headers: ghHeaders(env, { "Content-Type": "application/json" }),
    body: JSON.stringify({
      tag_name: "backgrounds",
      name: "backgrounds",
      body: "StoryForge background gameplay library",
    }),
  });
  if (!createRes.ok) {
    throw new Error(`Failed to create backgrounds release (${createRes.status})`);
  }
  return createRes.json();
}

// GitHub's asset API can either stream directly or redirect to signed storage.
// Never forward the API Authorization header to the storage host.
async function fetchReleaseMedia(env: Env, asset: any, method = "GET", range?: string | null): Promise<Response> {
  const mediaHeaders: Record<string, string> = {};
  if (range) mediaHeaders.Range = range;
  const upstream = await fetch(asset.url, {
    method,
    headers: ghHeaders(env, { ...mediaHeaders, Accept: "application/octet-stream" }),
    redirect: "manual",
  });
  const location = upstream.status >= 300 && upstream.status < 400 ? upstream.headers.get("Location") : null;
  if (!location) return upstream;
  return fetch(location, { method, headers: mediaHeaders, redirect: "follow" });
}

const BG_DETAILS_PREFIX = "background_details:";
type BackgroundDetails = { title: string; note: string; additional_credit: string };

async function getBackgroundDetails(db: D1Database, filename: string): Promise<Partial<BackgroundDetails>> {
  const value = await getRuntimeState(db, undefined, BG_DETAILS_PREFIX + filename, "{}");
  try { return JSON.parse(value); } catch { return {}; }
}

async function loadBackgroundsManifest(env: Env, rel: any): Promise<{ clips: any[] }> {
  const assets: any[] = rel?.assets || [];
  const manAsset = assets.find((a) => a.name === "manifest.json");
  if (!manAsset) return { clips: [] };
  const dl = await fetchReleaseMedia(env, manAsset);
  if (!dl.ok) throw new Error(`Background manifest unavailable (${dl.status})`);
  try {
    const parsed = await dl.json();
    if (!Array.isArray(parsed?.clips)) throw new Error("Invalid background manifest");
    return parsed;
  } catch {
    throw new Error("Invalid background manifest");
  }
}

async function uploadJsonAssetToRelease(env: Env, rel: any, assetName: string, data: unknown): Promise<void> {
  const repo = env.GITHUB_REPO || "motionssalt/storyforge";
  for (const a of rel.assets || []) {
    if (a.name === assetName) {
      await fetch(`https://api.github.com/repos/${repo}/releases/assets/${a.id}`, {
        method: "DELETE",
        headers: ghHeaders(env),
      });
    }
  }
  const uploadBase = String(rel.upload_url || "").split("{")[0];
  const upRes = await fetch(`${uploadBase}?name=${encodeURIComponent(assetName)}`, {
    method: "POST",
    headers: ghHeaders(env, { "Content-Type": "application/json" }),
    body: JSON.stringify(data, null, 2),
  });
  if (!upRes.ok) {
    throw new Error(`Failed uploading ${assetName} (${upRes.status})`);
  }
}

async function refreshAccountsForKey(
  env: Env,
  keyRow: ZernioKeyRow,
  isMock: boolean
): Promise<{ ok: boolean; accounts_count: number; error?: string }> {
  const db = env.STORYFORGE_DB;
  const adapter = getZernioAdapter(db, isMock);
  const res = await adapter.listAccounts(keyRow.api_key, keyRow.id);
  const nowIso = new Date().toISOString();

  await db
    .prepare("UPDATE zernio_keys SET last_test_ok = ?, last_test_msg = ?, last_tested_at = ? WHERE id = ?")
    .bind(
      res.ok ? 1 : 0,
      res.ok ? `Connected (${res.accounts.length} account(s))` : res.error || "Test failed",
      nowIso,
      keyRow.id
    )
    .run();

  if (!res.ok) {
    return { ok: false, accounts_count: 0, error: res.error };
  }

  // Deactivate previously stored accounts under this key first, then upsert fresh active list
  await db.prepare("UPDATE zernio_accounts SET is_active = 0, updated_at = ? WHERE key_id = ?").bind(nowIso, keyRow.id).run();

  const stmts = res.accounts.map((acc) =>
    db
      .prepare(
        `INSERT INTO zernio_accounts (key_id, account_id, platform, account_name, username, profile_id, is_active, raw_json, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(key_id, account_id) DO UPDATE SET
           platform = excluded.platform,
           account_name = excluded.account_name,
           username = excluded.username,
           profile_id = excluded.profile_id,
           is_active = excluded.is_active,
           raw_json = excluded.raw_json,
           updated_at = excluded.updated_at`
      )
      .bind(
        keyRow.id,
        acc.accountId,
        acc.platform,
        acc.accountName,
        acc.username,
        acc.profileId,
        acc.isActive ? 1 : 0,
        JSON.stringify(acc.raw || {}),
        nowIso
      )
  );
  if (stmts.length) await db.batch(stmts);
  return { ok: true, accounts_count: res.accounts.length };
}

function maskKey(k: ZernioKeyRow) {
  return {
    id: k.id,
    label: k.label,
    masked: `${k.key_prefix}…${k.key_last4}`,
    last_test_ok: k.last_test_ok,
    last_test_msg: k.last_test_msg,
    last_tested_at: k.last_tested_at,
    created_at: k.created_at,
  };
}

export const onRequest: PagesFunction<Env> = async (context) => {
  const { request, env } = context;
  const url = new URL(request.url);
  const path = url.pathname;
  const db = env.STORYFORGE_DB;

  // --- Auth endpoints ---
  if (path === "/api/login" && request.method === "POST") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    if (!body.password || body.password !== env.AUTH_PASSWORD) {
      return jsonResp({ error: "Incorrect password" }, 401);
    }
    const exp = Date.now() + 7 * 24 * 60 * 60 * 1000;
    const sig = await hmacHex(env.SESSION_SECRET, String(exp));
    const cookieVal = `${exp}.${sig}`;
    return jsonResp(
      { ok: true },
      200,
      {
        "Set-Cookie": `sf_session=${encodeURIComponent(cookieVal)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${7 * 24 * 3600}`,
      }
    );
  }

  if (path === "/api/logout" && request.method === "POST") {
    return jsonResp(
      { ok: true },
      200,
      { "Set-Cookie": "sf_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0" }
    );
  }

  if (path === "/api/me" && request.method === "GET") {
    const authed = await isAuthenticated(request, env);
    return jsonResp({ authenticated: authed }, authed ? 200 : 401);
  }

  // --- Internal GitHub Actions endpoints (authenticated by X-Internal-Secret) ---
  if (path.startsWith("/api/internal/")) {
    const sec = request.headers.get("X-Internal-Secret");
    if (!sec || sec !== env.INTERNAL_SECRET) {
      return jsonResp({ error: "Unauthorized internal call" }, 401);
    }

    if (path === "/api/internal/background-index" && request.method === "GET") {
      const idx = await getRuntimeState(db, env.STORYFORGE_VAULT, "bg_index", "0");
      return jsonResp({ index: Number(idx) || 0 });
    }

    if (path === "/api/internal/job" && request.method === "GET") {
      const id = url.searchParams.get("id");
      if (!id) return jsonResp({ error: "Missing id" }, 400);
      if (await isTaskDeleted(db, id)) {
        return jsonResp({ error: "Task was deleted", deleted: true }, 410);
      }
      const row = await db.prepare("SELECT * FROM tasks WHERE task_id = ?").bind(id).first<any>();
      if (!row) {
        // Legacy KV fallback if any old job still triggered
        const legacy = await env.STORYFORGE_VAULT.get(`job:${id}`);
        if (!legacy) return jsonResp({ error: "Job not found" }, 404);
        return new Response(legacy, { headers: { "Content-Type": "application/json" } });
      }
      const taskVoiceRaw = await getRuntimeState(db, undefined, `task_voice:${id}`, "{}");
      let taskVoice: any = {};
      try { taskVoice = JSON.parse(taskVoiceRaw); } catch {}
      const customVoice = row.voice_id ? await getCustomVoice(db, String(row.voice_id)) : null;
      return jsonResp({
        job_id: row.task_id,
        voice_id: row.voice_id,
        voice_speed: row.voice_speed,
        voice_engine: taskVoice.voice_engine || "voicestudio",
        voicestudio_speedup: taskVoice.voicestudio_speedup ?? 1.25,
        voice_config: customVoice || taskVoice.voice_config || null,
        target_wpm: row.target_wpm,
        background_mode: row.background_mode || "sequential",
        background_index: Number(row.background_index || 0),
        background_clip: row.background_clip || null,
        production_json: row.production_json ? JSON.parse(row.production_json) : null,
      });
    }

    if (path === "/api/internal/task-log" && request.method === "POST") {
      const payload: any = await request.json();
      const taskId = String(payload.task_id || "");
      if (!taskId) return jsonResp({ error: "Missing task_id" }, 400);

      // SF-010: Never resurrect or append logs for deleted/missing tasks
      if (await isTaskDeleted(db, taskId)) {
        return jsonResp({ ok: true, ignored: true, deleted: true });
      }
      const existingTask = await db.prepare("SELECT task_id FROM tasks WHERE task_id = ?").bind(taskId).first<{ task_id: string }>();
      if (!existingTask) {
        return jsonResp({ ok: true, ignored: true, deleted: true });
      }
      if (payload.github_run_id) {
        await setTaskWorkflowRunId(db, taskId, payload.github_run_id);
      }

      const nowIso = new Date().toISOString();
      const logs: any[] = Array.isArray(payload.logs) ? payload.logs : [];
      if (logs.length > 0) {
        await appendTaskLogs(
          db,
          taskId,
          logs.map((l) => ({
            ts: String(l.ts || nowIso),
            level: (l.level === "warn" || l.level === "error" ? l.level : "info") as "info" | "warn" | "error",
            stage: l.stage ? String(l.stage) : null,
            message: String(l.message || ""),
            progress_pct: typeof l.progress_pct === "number" ? l.progress_pct : null,
          }))
        );
      }

      const sets: string[] = ["updated_at = ?"];
      const binds: any[] = [nowIso];
      if (typeof payload.progress_pct === "number") {
        sets.push("progress_pct = ?");
        binds.push(Math.max(0, Math.min(100, payload.progress_pct)));
      }
      if (payload.current_stage) {
        sets.push("current_stage = ?");
        binds.push(String(payload.current_stage));
      }
      if (payload.status) {
        sets.push("status = ?");
        binds.push(String(payload.status));
      }
      if (payload.error) {
        sets.push("error = ?");
        binds.push(String(payload.error));
      }
      binds.push(taskId);
      await db.prepare(`UPDATE tasks SET ${sets.join(", ")} WHERE task_id = ?`).bind(...binds).run();

      return jsonResp({ ok: true });
    }

    if (path === "/api/internal/job-status" && request.method === "POST") {
      const payload: any = await request.json();
      const id = String(payload.job_id || "");
      if (!id) return jsonResp({ error: "Missing job_id" }, 400);

      // SF-010: Ignore late callbacks for deleted tasks and clean up any late-uploaded render assets
      if (await isTaskDeleted(db, id)) {
        if (Array.isArray(payload.parts) && payload.parts.length > 0) {
          try {
            await deleteGitHubRenderAssetsForTask(env, payload.parts, id);
          } catch {
            /* best-effort cleanup of late upload */
          }
        }
        return jsonResp({ ok: true, ignored: true, deleted: true });
      }

      const task = await db.prepare("SELECT * FROM tasks WHERE task_id = ?").bind(id).first<any>();
      if (!task) return jsonResp({ ok: true, ignored: true, deleted: true });

      if (payload.github_run_id) {
        await setTaskWorkflowRunId(db, id, payload.github_run_id);
      }

      const nowIso = new Date().toISOString();
      const incomingStatus = String(payload.status || task.status);
      const isClearErr = incomingStatus === "running" || incomingStatus === "done";

      await db
        .prepare(
          `UPDATE tasks SET
             status = ?,
             current_stage = COALESCE(?, current_stage),
             progress_pct = CASE WHEN ? = 'done' THEN 100 ELSE progress_pct END,
             error = CASE WHEN ? = 1 THEN NULL ELSE COALESCE(?, error) END,
             background_clip = COALESCE(?, background_clip),
             updated_at = ?
           WHERE task_id = ?`
        )
        .bind(
          incomingStatus,
          payload.stage ? String(payload.stage) : incomingStatus,
          incomingStatus,
          isClearErr ? 1 : 0,
          payload.error ? String(payload.error) : null,
          payload.background_clip ? String(payload.background_clip) : null,
          nowIso,
          id
        )
        .run();

      if (Array.isArray(payload.parts)) {
        const partsWithCredits = await Promise.all(payload.parts.map(async (part: any) => {
          if (!part.background_clip || !/^[A-Za-z0-9_.-]+\.mp4$/i.test(part.background_clip)) return part;
          const { additional_credit } = await getBackgroundDetails(db, part.background_clip);
          return { ...part, attribution: [...new Set([part.attribution, additional_credit].filter(Boolean))].join("\n") };
        }));
        const stmts = partsWithCredits.map((p: any) =>
          db
            .prepare(
              `INSERT INTO task_parts (task_id, part_number, title, description, hashtags_json, filename, asset_id, size_bytes, duration_sec, background_clip, attribution, render_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done')
               ON CONFLICT(task_id, part_number) DO UPDATE SET
                 title = COALESCE(excluded.title, task_parts.title),
                 description = COALESCE(excluded.description, task_parts.description),
                 hashtags_json = COALESCE(excluded.hashtags_json, task_parts.hashtags_json),
                 filename = excluded.filename,
                 asset_id = excluded.asset_id,
                 size_bytes = excluded.size_bytes,
                 duration_sec = excluded.duration_sec,
                 background_clip = excluded.background_clip,
                 attribution = excluded.attribution,
                 render_status = 'done'`
            )
            .bind(
              id,
              Number(p.part_number || 1),
              p.title || null,
              p.description || null,
              JSON.stringify(p.hashtags || []),
              p.filename || null,
              p.asset_id ?? null,
              p.size_bytes ?? null,
              p.duration_sec ?? null,
              p.background_clip || null,
              p.attribution || null
            )
        );
        if (stmts.length) await db.batch(stmts);
      }

      if (typeof payload.next_background_index === "number") {
        await setRuntimeState(db, "bg_index", String(payload.next_background_index));
      }

      if (incomingStatus === "failed" && task.kind === "render") {
        await db
          .prepare("UPDATE task_parts SET render_status = 'failed' WHERE task_id = ? AND render_status != 'done'")
          .bind(id)
          .run();
      }

      // Trigger SF-005 / SF-009 publishing once ALL parts of a render task complete
      if (task.kind === "render" && incomingStatus === "done") {
        await initializePublishingForTask(env, id);
      }

      return jsonResp({ ok: true });
    }

    if (path === "/api/internal/voice-sample-config" && request.method === "GET") {
      const settings = await getSettings(db);
      return jsonResp({
        sample_text: settings.sample_text,
        voicestudio_speedup: settings.voicestudio_speedup,
      });
    }

    if (path === "/api/internal/voice-config" && request.method === "GET") {
      const voiceId = String(url.searchParams.get("voice_id") || "").trim();
      if (!voiceId) return jsonResp({ error: "Missing voice_id" }, 400);
      const custom = await getCustomVoice(db, voiceId);
      if (custom) return jsonResp(custom);
      const builtin = BUILTIN_VOICESTUDIO_MAP.get(voiceId);
      if (builtin) return jsonResp({ ...builtin, kind: "designed" });
      return jsonResp({ error: "Voice not found" }, 404);
    }

    if (path === "/api/internal/voice-clip" && request.method === "GET") {
      const voiceId = String(url.searchParams.get("voice_id") || "").trim();
      const slot = String(url.searchParams.get("slot") || "base").trim();
      if (!voiceId || !VALID_CLONE_SLOTS.has(slot)) return jsonResp({ error: "Invalid voice_id or slot" }, 400);
      const item = await getKvBinary(env.STORYFORGE_VAULT, `voice_clip:${voiceId}:${slot}`);
      if (!item) return jsonResp({ error: "Clip not found" }, 404);
      return new Response(item.data, {
        status: 200,
        headers: {
          "Content-Type": String(item.meta?.content_type || "application/octet-stream"),
          "Cache-Control": "no-store",
        },
      });
    }

    if (path === "/api/internal/voice-prompt" && request.method === "GET") {
      const voiceId = String(url.searchParams.get("voice_id") || "").trim();
      const slot = String(url.searchParams.get("slot") || "base").trim();
      if (!voiceId || !VALID_CLONE_SLOTS.has(slot)) return jsonResp({ error: "Invalid voice_id or slot" }, 400);
      const item = await getKvBinary(env.STORYFORGE_VAULT, `voice_prompt:${voiceId}:${slot}`);
      if (!item) return jsonResp({ error: "Prompt not cached" }, 404);
      return new Response(item.data, {
        status: 200,
        headers: { "Content-Type": "application/octet-stream", "Cache-Control": "no-store" },
      });
    }

    if (path === "/api/internal/voice-prompt" && request.method === "POST") {
      const voiceId = String(url.searchParams.get("voice_id") || "").trim();
      const slot = String(url.searchParams.get("slot") || "base").trim();
      const refText = String(url.searchParams.get("ref_text") || "").trim();
      const autoTranscribed = url.searchParams.get("auto_transcribed") === "1";
      if (!voiceId || !VALID_CLONE_SLOTS.has(slot)) return jsonResp({ error: "Invalid voice_id or slot" }, 400);
      const bytes = await request.arrayBuffer();
      if (!bytes.byteLength) return jsonResp({ error: "Empty prompt payload" }, 400);
      await putKvBinary(env.STORYFORGE_VAULT, `voice_prompt:${voiceId}:${slot}`, bytes, {
        content_type: "application/octet-stream",
      });
      const existing = await getCustomVoice(db, voiceId);
      if (existing && existing.slots && existing.slots[slot]) {
        existing.slots[slot].prompt_cached = true;
        if (refText) existing.slots[slot].ref_text = refText;
        existing.slots[slot].auto_transcribed = autoTranscribed;
        existing.updated_at = new Date().toISOString();
        await saveCustomVoice(db, existing);
      }
      return jsonResp({ ok: true, size_bytes: bytes.byteLength });
    }

    if (path === "/api/internal/voice-preview" && request.method === "POST") {
      const explicitKey = String(url.searchParams.get("key") || "").trim();
      const voiceId = String(url.searchParams.get("voice_id") || "").trim();
      const slot = String(url.searchParams.get("slot") || "base").trim();
      const kvKey = explicitKey || (voiceId ? `voice_preview:${voiceId}:${slot}` : "");
      if (!kvKey || !kvKey.startsWith("voice_preview:")) {
        return jsonResp({ error: "Invalid preview key" }, 400);
      }
      const bytes = await request.arrayBuffer();
      if (!bytes.byteLength) return jsonResp({ error: "Empty preview audio" }, 400);
      await putKvBinary(env.STORYFORGE_VAULT, kvKey, bytes, { content_type: "audio/mpeg" });
      if (voiceId) {
        const existing = await getCustomVoice(db, voiceId);
        if (existing) {
          if (existing.kind === "designed") existing.sample_ready = true;
          if (existing.kind === "cloned" && existing.slots?.[slot]) {
            existing.slots[slot].preview_ready = true;
          }
          existing.updated_at = new Date().toISOString();
          await saveCustomVoice(db, existing);
        }
      }
      return jsonResp({ ok: true, key: kvKey, size_bytes: bytes.byteLength });
    }

    return jsonResp({ error: "Unknown internal endpoint" }, 404);
  }

  // All remaining routes require authentication
  if (!(await isAuthenticated(request, env))) {
    return jsonResp({ error: "Unauthorized" }, 401);
  }

  const voiceAsset = path.match(/^\/api\/voice-samples\/(voices\.json|[A-Za-z0-9_-]+\.mp3)$/);
  if (voiceAsset && (request.method === 'GET' || request.method === 'HEAD')) {
    const repo = env.GITHUB_REPO || 'motionssalt/storyforge';
    const release = await fetch('https://api.github.com/repos/' + repo + '/releases/tags/voice-samples', {headers: ghHeaders(env)});
    if (!release.ok) return jsonResp({error: 'Voice samples are not published yet.'}, 404);
    const asset = (await release.json() as any).assets?.find((a: any) => a.name === voiceAsset[1]);
    if (!asset) return jsonResp({error: 'Voice sample not found.'}, 404);
    const upstream = await fetchReleaseMedia(env, asset, request.method, request.headers.get('Range'));
    if (!upstream.ok && upstream.status !== 416) return jsonResp({error: 'Could not load the voice sample.'}, 502);
    const headers = new Headers({'Content-Type': voiceAsset[1].endsWith('.json') ? 'application/json' : 'audio/mpeg'});
    for (const key of ['Content-Range','Accept-Ranges','Content-Length']) if (upstream.headers.has(key)) headers.set(key, upstream.headers.get(key)!);
    return new Response(upstream.body, {status: upstream.status, headers});
  }

  // --- SF-006: Settings ---
  if (path === "/api/settings" && request.method === "GET") {
    const settings = await getSettings(db);
    return jsonResp({ settings });
  }

  if (path === "/api/settings" && request.method === "PUT") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    const patch: Partial<AppSettings> = {};
    if (body.voice_engine !== undefined) {
      if (body.voice_engine !== 'edge' && body.voice_engine !== 'voicestudio') return jsonResp({error: 'Invalid voice engine.'}, 400);
      patch.voice_engine = body.voice_engine;
    }
    if (body.voicestudio_speedup !== undefined) {
      const speed = Number(body.voicestudio_speedup);
      if (!(speed >= 1 && speed <= 2)) return jsonResp({error: 'VoiceStudio speed-up must be between 1 and 2.'}, 400);
      patch.voicestudio_speedup = speed;
    }
    if (typeof body.voicestudio_voice_id === "string" && body.voicestudio_voice_id.trim()) {
      const vid = body.voicestudio_voice_id.trim();
      if (BUILTIN_VOICESTUDIO_MAP.has(vid) || (await getCustomVoice(db, vid))) {
        patch.voicestudio_voice_id = vid;
      }
    }
    if (typeof body.sample_text === "string") {
      const st = body.sample_text.trim();
      if (!st || st.length > 600) {
        return jsonResp({ error: "Sample text must be 1 to 600 characters." }, 400);
      }
      patch.sample_text = st;
    }
    if (typeof body.default_voice_id === "string" && body.default_voice_id.trim()) {
      patch.default_voice_id = body.default_voice_id.trim();
    }
    if (typeof body.default_background === "string" && body.default_background.trim()) {
      patch.default_background = body.default_background.trim();
    }
    if (body.queue_cadence_count !== undefined) {
      const cnt = Number(body.queue_cadence_count);
      if (!Number.isFinite(cnt) || cnt < 1 || cnt > 100) {
        return jsonResp({ error: "queue_cadence_count must be between 1 and 100" }, 400);
      }
      patch.queue_cadence_count = Math.round(cnt);
    }
    if (body.queue_cadence_unit !== undefined) {
      if (!VALID_CADENCE_UNITS.has(body.queue_cadence_unit)) {
        return jsonResp({ error: "queue_cadence_unit must be day, week, or month" }, 400);
      }
      patch.queue_cadence_unit = body.queue_cadence_unit;
    }
    if (body.part2_delay_minutes !== undefined) {
      const delay = Number(body.part2_delay_minutes);
      if (!Number.isFinite(delay) || delay < 1 || delay > 1440) {
        return jsonResp({ error: "part2_delay_minutes must be between 1 and 1440" }, 400);
      }
      patch.part2_delay_minutes = Math.round(delay);
    }
    if (typeof body.dry_run === "boolean") patch.dry_run = body.dry_run;
    if (typeof body.mock_mode === "boolean") patch.mock_mode = body.mock_mode;

    const settings = await saveSettings(db, patch);
    return jsonResp({ settings });
  }


  // --- Voices (Built-in, Create a voice, My voices cloning) ---
  if (path === "/api/voices" && request.method === "GET") {
    const [settings, customVoices] = await Promise.all([getSettings(db), listCustomVoices(db)]);
    return jsonResp({
      edge: BUILTIN_EDGE_VOICES,
      voicestudio_builtin: BUILTIN_VOICESTUDIO_VOICES,
      custom_designed: customVoices.filter((v) => v.kind === "designed"),
      my_voices: customVoices.filter((v) => v.kind === "cloned"),
      sample_text: settings.sample_text,
      voicestudio_speedup: settings.voicestudio_speedup,
    });
  }

  if (path === "/api/voices/preview" && (request.method === "GET" || request.method === "HEAD")) {
    const explicitKey = String(url.searchParams.get("key") || "").trim();
    const voiceId = String(url.searchParams.get("voice_id") || "").trim();
    const slot = String(url.searchParams.get("slot") || "base").trim();
    const kvKey = explicitKey || (voiceId ? `voice_preview:${voiceId}:${slot}` : "");
    if (!kvKey || !kvKey.startsWith("voice_preview:")) {
      return jsonResp({ error: "Invalid preview key" }, 400);
    }
    const item = await getKvBinary(env.STORYFORGE_VAULT, kvKey);
    if (!item) return jsonResp({ error: "Preview not generated yet." }, 404);
    const len = item.data.byteLength;
    return new Response(request.method === "HEAD" ? null : item.data, {
      status: 200,
      headers: {
        "Content-Type": "audio/mpeg",
        "Content-Length": String(len),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
      },
    });
  }

  if (path === "/api/voices/preview-design" && request.method === "POST") {
    let body: any = {};
    try { body = await request.json(); } catch { return jsonResp({ error: "Invalid JSON body" }, 400); }
    const check = validateOmnivoiceInstruct(String(body.instruct || ""));
    if (!check.ok) return jsonResp({ error: check.error }, 400);
    const seed = Math.max(1, Math.min(2147483647, Math.round(Number(body.seed) || 1001)));
    const taskId = `vt_${Date.now().toString(36)}_${crypto.randomUUID().slice(0, 6)}`;
    const previewKey = `voice_preview:draft_${taskId}:base`;
    const nowIso = new Date().toISOString();
    await db
      .prepare(
        `INSERT INTO tasks (task_id, kind, series_title, status, progress_pct, current_stage, created_at, updated_at)
         VALUES (?, 'voice_sample', ?, 'queued', 0, 'queued', ?, ?)`
      )
      .bind(taskId, `Preview voice design (${check.normalized})`, nowIso, nowIso)
      .run();
    await appendTaskLogs(db, taskId, [
      { ts: nowIso, level: "info", stage: "queued", message: `Queued Voice Design preview (${check.normalized}, seed=${seed})`, progress_pct: 0 },
    ]);
    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const ghRes = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/voice-tool.yml/dispatches`,
      {
        method: "POST",
        headers: ghHeaders(env, { "Content-Type": "application/json" }),
        body: JSON.stringify({
          ref: "main",
          inputs: {
            task_id: taskId,
            mode: "design_preview",
            instruct: check.normalized,
            seed: String(seed),
            preview_key: previewKey,
          },
        }),
      }
    );
    if (!ghRes.ok) {
      const errTxt = (await ghRes.text()).slice(0, 300);
      await db.prepare("UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE task_id = ?").bind(errTxt, new Date().toISOString(), taskId).run();
      return jsonResp({ error: `GitHub dispatch failed (${ghRes.status}): ${errTxt}` }, 502);
    }
    return jsonResp({ task_id: taskId, preview_key: previewKey, instruct: check.normalized, seed });
  }

  if (path === "/api/voices/custom" && request.method === "POST") {
    let body: any = {};
    try { body = await request.json(); } catch { return jsonResp({ error: "Invalid JSON body" }, 400); }
    const name = String(body.name || "").trim().slice(0, 60);
    if (!name) return jsonResp({ error: "Enter a voice name." }, 400);
    const check = validateOmnivoiceInstruct(String(body.instruct || ""));
    if (!check.ok) return jsonResp({ error: check.error }, 400);
    const seed = Math.max(1, Math.min(2147483647, Math.round(Number(body.seed) || 1001)));
    const descriptor = String(body.descriptor || "").trim().slice(0, 120) || check.normalized;
    const id = `vsc_${crypto.randomUUID().slice(0, 8)}`;
    const nowIso = new Date().toISOString();

    let sampleReady = false;
    const draftKey = String(body.draft_preview_key || "").trim();
    if (draftKey && draftKey.startsWith("voice_preview:draft_")) {
      const draftAudio = await getKvBinary(env.STORYFORGE_VAULT, draftKey);
      if (draftAudio) {
        await putKvBinary(env.STORYFORGE_VAULT, `voice_preview:${id}:base`, draftAudio.data, { content_type: "audio/mpeg" });
        await deleteKvBinary(env.STORYFORGE_VAULT, draftKey);
        sampleReady = true;
      }
    }

    const rec: CustomVoiceRecord = {
      id,
      kind: "designed",
      engine: "voicestudio",
      name,
      descriptor,
      instruct: check.normalized,
      seed,
      sample_ready: sampleReady,
      created_at: nowIso,
      updated_at: nowIso,
    };
    await saveCustomVoice(db, rec);
    return jsonResp({ voice: rec });
  }

  if (path === "/api/voices/custom" && request.method === "DELETE") {
    const id = String(url.searchParams.get("id") || "").trim();
    if (!id) return jsonResp({ error: "Missing id" }, 400);
    const existing = await getCustomVoice(db, id);
    if (!existing) return jsonResp({ error: "Voice not found" }, 404);
    for (const slot of VALID_CLONE_SLOTS) {
      await Promise.all([
        deleteKvBinary(env.STORYFORGE_VAULT, `voice_clip:${id}:${slot}`),
        deleteKvBinary(env.STORYFORGE_VAULT, `voice_prompt:${id}:${slot}`),
        deleteKvBinary(env.STORYFORGE_VAULT, `voice_preview:${id}:${slot}`),
      ]);
    }
    await deleteCustomVoice(db, id);
    const settings = await getSettings(db);
    if (settings.voicestudio_voice_id === id) {
      await saveSettings(db, { voicestudio_voice_id: "vs-harper" });
    }
    return jsonResp({ ok: true, deleted_id: id });
  }

  if (path === "/api/voices/cloned" && request.method === "POST") {
    let body: any = {};
    try { body = await request.json(); } catch { return jsonResp({ error: "Invalid JSON body" }, 400); }
    const name = String(body.name || "").trim().slice(0, 60);
    if (!name) return jsonResp({ error: "Enter a name for your voice." }, 400);
    const existingId = String(body.id || "").trim();
    const nowIso = new Date().toISOString();
    let rec: CustomVoiceRecord | null = existingId ? await getCustomVoice(db, existingId) : null;
    if (existingId && (!rec || rec.kind !== "cloned")) {
      return jsonResp({ error: "Cloned voice not found." }, 404);
    }
    const id = rec ? rec.id : `vsm_${crypto.randomUUID().slice(0, 8)}`;
    const descriptor = String(body.descriptor || "").trim().slice(0, 120) || "My voice (cloned)";
    rec = {
      id,
      kind: "cloned",
      engine: "voicestudio",
      name,
      descriptor,
      is_my_voice: true,
      slots: rec?.slots || {},
      created_at: rec?.created_at || nowIso,
      updated_at: nowIso,
    };
    await saveCustomVoice(db, rec);
    return jsonResp({ voice: rec });
  }

  if (path === "/api/voices/cloned/clip" && request.method === "POST") {
    const voiceId = String(url.searchParams.get("voice_id") || "").trim();
    const slot = String(url.searchParams.get("slot") || "base").trim();
    const filename = String(url.searchParams.get("filename") || "clip.wav").replace(/[^A-Za-z0-9._-]/g, "_").slice(-64);
    const refText = String(url.searchParams.get("ref_text") || "").trim().slice(0, 1000);
    if (!voiceId || !VALID_CLONE_SLOTS.has(slot)) {
      return jsonResp({ error: "Invalid voice_id or emotion slot." }, 400);
    }
    const rec = await getCustomVoice(db, voiceId);
    if (!rec || rec.kind !== "cloned") {
      return jsonResp({ error: "Cloned voice not found." }, 404);
    }
    const contentLen = Number(request.headers.get("Content-Length") || 0);
    if (contentLen > 20 * 1024 * 1024) {
      return jsonResp({ error: "Clip exceeds 20 MiB limit. Use a clean 3-10 second audio clip." }, 413);
    }
    const bytes = await request.arrayBuffer();
    if (!bytes.byteLength) return jsonResp({ error: "Empty audio clip." }, 400);
    if (bytes.byteLength > 20 * 1024 * 1024) {
      return jsonResp({ error: "Clip exceeds 20 MiB limit." }, 413);
    }
    const ct = request.headers.get("Content-Type") || "application/octet-stream";
    await putKvBinary(env.STORYFORGE_VAULT, `voice_clip:${voiceId}:${slot}`, bytes, {
      content_type: ct,
      filename,
    });
    // Invalidate cached .pt prompt and preview when clip changes
    await Promise.all([
      deleteKvBinary(env.STORYFORGE_VAULT, `voice_prompt:${voiceId}:${slot}`),
      deleteKvBinary(env.STORYFORGE_VAULT, `voice_preview:${voiceId}:${slot}`),
    ]);
    rec.slots = rec.slots || {};
    rec.slots[slot] = {
      filename,
      content_type: ct,
      size_bytes: bytes.byteLength,
      ref_text: refText,
      auto_transcribed: false,
      prompt_cached: false,
      preview_ready: false,
      updated_at: new Date().toISOString(),
    };
    rec.updated_at = new Date().toISOString();
    await saveCustomVoice(db, rec);
    return jsonResp({ voice: rec });
  }

  if (path === "/api/voices/cloned/clip" && request.method === "DELETE") {
    const voiceId = String(url.searchParams.get("voice_id") || "").trim();
    const slot = String(url.searchParams.get("slot") || "").trim();
    if (!voiceId || !VALID_CLONE_SLOTS.has(slot) || slot === "base") {
      return jsonResp({ error: "Cannot delete base slot; delete the whole voice instead." }, 400);
    }
    const rec = await getCustomVoice(db, voiceId);
    if (!rec || rec.kind !== "cloned") return jsonResp({ error: "Cloned voice not found." }, 404);
    await Promise.all([
      deleteKvBinary(env.STORYFORGE_VAULT, `voice_clip:${voiceId}:${slot}`),
      deleteKvBinary(env.STORYFORGE_VAULT, `voice_prompt:${voiceId}:${slot}`),
      deleteKvBinary(env.STORYFORGE_VAULT, `voice_preview:${voiceId}:${slot}`),
    ]);
    if (rec.slots) delete rec.slots[slot];
    rec.updated_at = new Date().toISOString();
    await saveCustomVoice(db, rec);
    return jsonResp({ voice: rec });
  }

  if (path === "/api/voices/cloned/prepare" && request.method === "POST") {
    let body: any = {};
    try { body = await request.json(); } catch { return jsonResp({ error: "Invalid JSON body" }, 400); }
    const voiceId = String(body.voice_id || "").trim();
    const rec = await getCustomVoice(db, voiceId);
    if (!rec || rec.kind !== "cloned" || !rec.slots?.base) {
      return jsonResp({ error: "Upload a base clip before preparing this voice." }, 400);
    }
    const taskId = `vt_${Date.now().toString(36)}_${crypto.randomUUID().slice(0, 6)}`;
    const nowIso = new Date().toISOString();
    await db
      .prepare(
        `INSERT INTO tasks (task_id, kind, series_title, status, progress_pct, current_stage, created_at, updated_at)
         VALUES (?, 'voice_sample', ?, 'queued', 0, 'queued', ?, ?)`
      )
      .bind(taskId, `Prepare cloned voice: ${rec.name}`, nowIso, nowIso)
      .run();
    await appendTaskLogs(db, taskId, [
      { ts: nowIso, level: "info", stage: "queued", message: `Queued prompt cache & preview generation for "${rec.name}"`, progress_pct: 0 },
    ]);
    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const ghRes = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/voice-tool.yml/dispatches`,
      {
        method: "POST",
        headers: ghHeaders(env, { "Content-Type": "application/json" }),
        body: JSON.stringify({
          ref: "main",
          inputs: {
            task_id: taskId,
            mode: "prepare_cloned",
            voice_id: voiceId,
          },
        }),
      }
    );
    if (!ghRes.ok) {
      const errTxt = (await ghRes.text()).slice(0, 300);
      await db.prepare("UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE task_id = ?").bind(errTxt, new Date().toISOString(), taskId).run();
      return jsonResp({ error: `GitHub dispatch failed (${ghRes.status}): ${errTxt}` }, 502);
    }
    return jsonResp({ task_id: taskId });
  }

  if (path === "/api/voices/regenerate-samples" && request.method === "POST") {
    const settings = await getSettings(db);
    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const ghRes = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/generate-voice-samples.yml/dispatches`,
      {
        method: "POST",
        headers: ghHeaders(env, { "Content-Type": "application/json" }),
        body: JSON.stringify({
          ref: "main",
          inputs: {
            sample_text: settings.sample_text,
            voicestudio_speedup: String(settings.voicestudio_speedup),
          },
        }),
      }
    );
    if (!ghRes.ok) {
      const errTxt = (await ghRes.text()).slice(0, 300);
      return jsonResp({ error: `GitHub dispatch failed (${ghRes.status}): ${errTxt}` }, 502);
    }
    return jsonResp({ ok: true });
  }

  // --- SF-005: Zernio Keys & Accounts ---
  if (path === "/api/zernio/keys" && request.method === "GET") {
    const keys = await listZernioKeys(db);
    return jsonResp({ keys: keys.map(maskKey) });
  }

  if (path === "/api/zernio/keys" && request.method === "POST") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    const label = String(body.label || "").trim();
    const apiKey = String(body.api_key || "").trim();
    if (!label) return jsonResp({ error: "Enter a label (e.g. owner name) for this key." }, 400);
    if (!apiKey || apiKey.length < 8) return jsonResp({ error: "Enter a valid Zernio API key." }, 400);

    const id = `zk_${crypto.randomUUID().slice(0, 8)}`;
    const prefix = apiKey.slice(0, Math.min(6, Math.max(3, apiKey.length - 4)));
    const last4 = apiKey.slice(-4);
    const nowIso = new Date().toISOString();

    await db
      .prepare(
        `INSERT INTO zernio_keys (id, label, api_key, key_prefix, key_last4, created_at)
         VALUES (?, ?, ?, ?, ?, ?)`
      )
      .bind(id, label, apiKey, prefix, last4, nowIso)
      .run();

    const keyRow = (await db.prepare("SELECT * FROM zernio_keys WHERE id = ?").bind(id).first<ZernioKeyRow>())!;
    const settings = await getSettings(db);
    const testRes = await refreshAccountsForKey(env, keyRow, settings.mock_mode);
    let unlockedTasks = 0;
    if (testRes.ok && testRes.accounts_count > 0) {
      unlockedTasks = await triggerWaitingTasksOnKeysReady(env);
    }
    const updatedRow = (await db.prepare("SELECT * FROM zernio_keys WHERE id = ?").bind(id).first<ZernioKeyRow>())!;
    return jsonResp({
      key: maskKey(updatedRow),
      test_result: testRes,
      unlocked_tasks: unlockedTasks,
    });
  }

  if (path === "/api/zernio/keys" && request.method === "DELETE") {
    const id = url.searchParams.get("id");
    if (!id) return jsonResp({ error: "Missing key id" }, 400);
    await db.batch([
      db.prepare("DELETE FROM zernio_accounts WHERE key_id = ?").bind(id),
      db.prepare("DELETE FROM zernio_keys WHERE id = ?").bind(id),
    ]);
    return jsonResp({ ok: true });
  }

  if (path === "/api/zernio/keys/test" && request.method === "POST") {
    const id = url.searchParams.get("id");
    if (!id) return jsonResp({ error: "Missing key id" }, 400);
    const keyRow = await db.prepare("SELECT * FROM zernio_keys WHERE id = ?").bind(id).first<ZernioKeyRow>();
    if (!keyRow) return jsonResp({ error: "Key not found" }, 404);
    const settings = await getSettings(db);
    const testRes = await refreshAccountsForKey(env, keyRow, settings.mock_mode);
    let unlockedTasks = 0;
    if (testRes.ok && testRes.accounts_count > 0) {
      unlockedTasks = await triggerWaitingTasksOnKeysReady(env);
    }
    const updatedRow = (await db.prepare("SELECT * FROM zernio_keys WHERE id = ?").bind(id).first<ZernioKeyRow>())!;
    return jsonResp({ key: maskKey(updatedRow), test_result: testRes, unlocked_tasks: unlockedTasks });
  }

  if (path === "/api/zernio/accounts" && request.method === "GET") {
    const [accounts, keys] = await Promise.all([listZernioAccounts(db), listZernioKeys(db)]);
    const labelMap = new Map(keys.map((k) => [k.id, k.label]));
    return jsonResp({
      accounts: accounts.map((a) => ({
        ...a,
        key_label: labelMap.get(a.key_id) || a.key_id,
      })),
    });
  }

  if (path === "/api/zernio/accounts/refresh" && request.method === "POST") {
    const settings = await getSettings(db);
    const keys = await listZernioKeys(db);
    const keyResults: Array<{ id: string; label: string; ok: boolean; accounts_count: number; error?: string }> = [];
    let totalAccounts = 0;
    for (const k of keys) {
      const r = await refreshAccountsForKey(env, k, settings.mock_mode);
      totalAccounts += r.accounts_count;
      keyResults.push({ id: k.id, label: k.label, ...r });
    }
    let unlockedTasks = 0;
    if (totalAccounts > 0) {
      unlockedTasks = await triggerWaitingTasksOnKeysReady(env);
    }
    const accounts = await listZernioAccounts(db);
    const labelMap = new Map(keys.map((k) => [k.id, k.label]));
    return jsonResp({
      key_results: keyResults,
      unlocked_tasks: unlockedTasks,
      accounts: accounts.map((a) => ({
        ...a,
        key_label: labelMap.get(a.key_id) || a.key_id,
      })),
    });
  }

  // --- SF-008: Background Video Manager ---
  if (path === "/api/backgrounds" && request.method === "GET") {
    const rel = await getOrCreateBackgroundsRelease(env);
    const manifest = await loadBackgroundsManifest(env, rel);
    const assetsByName = new Map<string, any>((rel.assets || []).map((a: any) => [a.name, a]));
    const bgIdx = Number(await getRuntimeState(db, env.STORYFORGE_VAULT, "bg_index", "0")) || 0;
    // Show playable library assets even if an older/manual upload did not update the manifest.
    // Such clips do not participate in rotation until they have a manifest entry.
    const all = [...manifest.clips];
    const listed = new Set(all.map((c) => c.filename));
    for (const name of assetsByName.keys()) {
      if (/^[A-Za-z0-9_.-]+\.mp4$/i.test(name) && !/^(?:pending_upload_|original_|backup_)/i.test(name) && !listed.has(name)) {
        all.push({ filename: name, title: name, unlisted: true });
      }
    }
    const { results } = await db.prepare("SELECT key, value FROM runtime_state WHERE key >= ? AND key < ?")
      .bind(BG_DETAILS_PREFIX, "background_details;").all<{ key: string; value: string }>();
    const details = new Map<string, BackgroundDetails>();
    for (const row of results || []) {
      try { details.set(row.key.slice(BG_DETAILS_PREFIX.length), JSON.parse(row.value)); } catch { /* Ignore a malformed override. */ }
    }
    const clips = all.map((c: any) => {
      const mp4Asset = assetsByName.get(c.filename);
      const thumbName = c.thumbnail || String(c.filename || "").replace(/\.mp4$/i, ".jpg");
      const hasThumb = assetsByName.has(thumbName);
      return {
        ...c,
        ...details.get(c.filename),
        thumbnail: hasThumb ? thumbName : null,
        size_bytes: c.size_bytes ?? mp4Asset?.size ?? null,
        asset_exists: Boolean(mp4Asset),
      };
    });
    return jsonResp({ clips, next_background_index: bgIdx });
  }

  if (path === "/api/backgrounds" && request.method === "PATCH") {
    const filename = url.searchParams.get("filename") || "";
    if (!/^[A-Za-z0-9_.-]+\.mp4$/i.test(filename) || /^(?:pending_upload_|original_|backup_)/i.test(filename)) {
      return jsonResp({ error: "Invalid clip filename." }, 400);
    }
    const body: any = await request.json().catch(() => null);
    const title = typeof body?.title === "string" ? body.title.trim() : "";
    const note = body?.note;
    const additionalCredit = body?.additional_credit;
    if (!title || title.length > 100 || typeof note !== "string" || note.length > 300 ||
        typeof additionalCredit !== "string" || additionalCredit.length > 400) {
      return jsonResp({ error: "Enter a title (up to 100 characters), note (up to 300), and additional credit (up to 400)." }, 400);
    }
    const rel = await getOrCreateBackgroundsRelease(env);
    if (!(rel.assets || []).some((asset: any) => asset.name === filename)) {
      return jsonResp({ error: "This clip is no longer in the library." }, 404);
    }
    const details: BackgroundDetails = { title, note: note.trim(), additional_credit: additionalCredit.trim() };
    await setRuntimeState(db, BG_DETAILS_PREFIX + filename, JSON.stringify(details));
    return jsonResp({ ok: true, details });
  }

  if (path === "/api/backgrounds/thumb" && request.method === "GET") {
    const file = url.searchParams.get("file") || "";
    if (!/^[A-Za-z0-9_.-]+\.(?:jpg|jpeg)$/i.test(file)) {
      return jsonResp({ error: "Invalid thumbnail filename" }, 400);
    }
    const rel = await getOrCreateBackgroundsRelease(env);
    const asset = (rel.assets || []).find((a: any) => a.name === file);
    if (!asset) return jsonResp({ error: "Thumbnail not found" }, 404);
    const res = await fetchReleaseMedia(env, asset);
    if (!res.ok) return jsonResp({ error: `Thumbnail fetch failed (${res.status})` }, 502);
    return new Response(res.body, {
      status: 200,
      headers: {
        "Content-Type": "image/jpeg",
        "Cache-Control": "private, max-age=3600",
      },
    });
  }

  if (path === "/api/backgrounds/stream" && ["GET", "HEAD"].includes(request.method)) {
    const file = url.searchParams.get("file") || "";
    if (!/^[A-Za-z0-9_.-]+\.mp4$/i.test(file)) {
      return jsonResp({ error: "Invalid video filename" }, 400);
    }
    const rel = await getOrCreateBackgroundsRelease(env);
    const asset = (rel.assets || []).find((a: any) => a.name === file);
    if (!asset) return jsonResp({ error: "Background video not found" }, 404);

    const range = request.headers.get("Range");
    if (range && !/^bytes=(?:\d+-\d*|-\d+)$/i.test(range)) {
      return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${asset.size}` } });
    }
    const mediaRes = await fetchReleaseMedia(env, asset, request.method, range);
    if (!mediaRes.ok && mediaRes.status !== 416) {
      return jsonResp({ error: `Stream failed (${mediaRes.status})` }, 502);
    }

    const outHeaders = new Headers();
    outHeaders.set("Content-Type", "video/mp4");
    outHeaders.set("Accept-Ranges", "bytes");
    outHeaders.set("Cache-Control", "private, max-age=3600");
    for (const h of ["Content-Length", "Content-Range"]) {
      const val = mediaRes.headers.get(h);
      if (val) outHeaders.set(h, val);
    }
    return new Response(request.method === "HEAD" ? null : mediaRes.body, {
      status: mediaRes.status,
      headers: outHeaders,
    });
  }

  if (path === "/api/backgrounds" && request.method === "DELETE") {
    const filename = url.searchParams.get("filename") || "";
    if (!/^[A-Za-z0-9_.-]+\.mp4$/i.test(filename)) {
      return jsonResp({ error: "Invalid filename" }, 400);
    }
    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const rel = await getOrCreateBackgroundsRelease(env);
    const manifest = await loadBackgroundsManifest(env, rel);
    const clips: any[] = manifest.clips || [];
    const oldLen = clips.length;
    const oldIdxPos = clips.findIndex((c) => c.filename === filename);
    if (oldIdxPos === -1 && (/^(?:pending_upload_|original_|backup_)/i.test(filename) || !(rel.assets || []).some((a: any) => a.name === filename))) {
      return jsonResp({ error: "Background clip not found" }, 404);
    }

    const thumbName = clips[oldIdxPos]?.thumbnail || filename.replace(/\.mp4$/i, ".jpg");
    for (const a of rel.assets || []) {
      if (a.name === filename || a.name === thumbName) {
        const removed = await fetch(`https://api.github.com/repos/${repo}/releases/assets/${a.id}`, {
          method: "DELETE",
          headers: ghHeaders(env),
        });
        if (!removed.ok && removed.status !== 404) return jsonResp({ error: `Could not delete ${a.name} (${removed.status}). Retry from the library.` }, 502);
      }
    }

    const newClips = clips.filter((c) => c.filename !== filename);
    if (oldIdxPos !== -1) await uploadJsonAssetToRelease(env, rel, "manifest.json", { ...manifest, clips: newClips });
    await db.prepare("DELETE FROM runtime_state WHERE key = ?").bind(BG_DETAILS_PREFIX + filename).run();

    // Safely adjust D1 sequential pointer so the sequence stays continuous and valid
    const curBgIdx = Number(await getRuntimeState(db, env.STORYFORGE_VAULT, "bg_index", "0")) || 0;
    let nextBgIdx = 0;
    if (oldIdxPos === -1) {
      nextBgIdx = curBgIdx;
    } else if (newClips.length > 0 && oldLen > 0) {
      const curPos = curBgIdx % oldLen;
      const adjustedPos = curPos > oldIdxPos ? curPos - 1 : curPos % newClips.length;
      nextBgIdx = adjustedPos;
    }
    if (oldIdxPos !== -1) await setRuntimeState(db, "bg_index", String(nextBgIdx));

    // Reset default_background if this deleted clip was selected as default
    const settings = await getSettings(db);
    if (settings.default_background === filename) {
      await saveSettings(db, { default_background: "sequential" });
    }

    return jsonResp({ ok: true, clips: newClips, next_background_index: nextBgIdx });
  }

  if (path === "/api/backgrounds/import-url" && request.method === "POST") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    const sourceUrl = String(body.url || "").trim();
    const title = String(body.title || "").trim() || "Imported Background";
    const note = String(body.note || "").trim();
    const attribution = String(body.attribution || "").trim();
    if (!/^https?:\/\/\S+/i.test(sourceUrl)) {
      return jsonResp({ error: "Enter a valid http:// or https:// video or Google Drive URL." }, 400);
    }

    const taskId = `bg_${Date.now().toString(36)}_${crypto.randomUUID().slice(0, 6)}`;
    const nowIso = new Date().toISOString();
    await db
      .prepare(
        `INSERT INTO tasks (task_id, kind, series_title, status, progress_pct, current_stage, created_at, updated_at)
         VALUES (?, 'bg_import', ?, 'queued', 0, 'queued', ?, ?)`
      )
      .bind(taskId, `Import background: ${title}`, nowIso, nowIso)
      .run();

    await appendTaskLogs(db, taskId, [
      { ts: nowIso, level: "info", stage: "queued", message: `Queued URL import for "${title}"`, progress_pct: 0 },
    ]);

    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const ghRes = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/background-import.yml/dispatches`,
      {
        method: "POST",
        headers: ghHeaders(env, { "Content-Type": "application/json" }),
        body: JSON.stringify({
          ref: "main",
          inputs: {
            task_id: taskId,
            mode: "url",
            source_url: sourceUrl,
            title,
            note,
            attribution,
          },
        }),
      }
    );
    if (!ghRes.ok) {
      const errTxt = (await ghRes.text()).slice(0, 300);
      await db
        .prepare("UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE task_id = ?")
        .bind(`GitHub dispatch failed (${ghRes.status}): ${errTxt}`, nowIso, taskId)
        .run();
      return jsonResp({ error: `GitHub dispatch failed (${ghRes.status}): ${errTxt}` }, 502);
    }
    return jsonResp({ task_id: taskId });
  }

  if (path === "/api/backgrounds/upload" && request.method === "POST") {
    const contentLen = Number(request.headers.get("Content-Length") || 0);
    if (contentLen > MAX_UPLOAD_BODY) {
      return jsonResp(
        { error: "File is over StoryForge's direct-upload limit (95 MiB). Use URL / Google Drive import instead." },
        413
      );
    }
    if (!request.body) {
      return jsonResp({ error: "Empty upload body." }, 400);
    }

    const rawName = url.searchParams.get("filename") || "clip.mp4";
    const safeName = rawName.replace(/[^A-Za-z0-9._-]/g, "_").slice(-60) || "clip.mp4";
    const title = (url.searchParams.get("title") || safeName.replace(/\.[^.]+$/, "")).trim().slice(0, 100);
    const note = (url.searchParams.get("note") || "").trim().slice(0, 300);
    const attribution = (url.searchParams.get("attribution") || "").trim().slice(0, 400);

    const taskId = `bg_${Date.now().toString(36)}_${crypto.randomUUID().slice(0, 6)}`;
    const pendingAssetName = `pending_upload_${taskId}_${safeName.endsWith(".mp4") ? safeName : `${safeName}.mp4`}`;
    const rel = await getOrCreateBackgroundsRelease(env);
    const uploadBase = String(rel.upload_url || "").split("{")[0];

    const upHeaders = ghHeaders(env, { "Content-Type": "application/octet-stream" });
    if (contentLen > 0) upHeaders["Content-Length"] = String(contentLen);

    // Stream request.body directly to GitHub Release asset without buffering in Worker memory
    const upRes = await fetch(`${uploadBase}?name=${encodeURIComponent(pendingAssetName)}`, {
      method: "POST",
      headers: upHeaders,
      body: request.body,
    });
    if (!upRes.ok) {
      const txt = (await upRes.text()).slice(0, 300);
      return jsonResp({ error: `Release staging upload failed (${upRes.status}): ${txt}` }, 502);
    }

    const nowIso = new Date().toISOString();
    await db
      .prepare(
        `INSERT INTO tasks (task_id, kind, series_title, status, progress_pct, current_stage, created_at, updated_at)
         VALUES (?, 'bg_import', ?, 'queued', 5, 'queued', ?, ?)`
      )
      .bind(taskId, `Upload background: ${title}`, nowIso, nowIso)
      .run();
    await appendTaskLogs(db, taskId, [
      {
        ts: nowIso,
        level: "info",
        stage: "queued",
        message: `Uploaded staging asset (${(contentLen / (1024 * 1024)).toFixed(1)} MB), dispatching normalization...`,
        progress_pct: 5,
      },
    ]);

    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const ghRes = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/background-import.yml/dispatches`,
      {
        method: "POST",
        headers: ghHeaders(env, { "Content-Type": "application/json" }),
        body: JSON.stringify({
          ref: "main",
          inputs: {
            task_id: taskId,
            mode: "pending_asset",
            pending_asset_name: pendingAssetName,
            title,
            note,
            attribution,
          },
        }),
      }
    );
    if (!ghRes.ok) {
      const errTxt = (await ghRes.text()).slice(0, 300);
      await db
        .prepare("UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE task_id = ?")
        .bind(`GitHub dispatch failed (${ghRes.status}): ${errTxt}`, nowIso, taskId)
        .run();
      return jsonResp({ error: `GitHub dispatch failed (${ghRes.status}): ${errTxt}` }, 502);
    }

    return jsonResp({ task_id: taskId });
  }

  if (path === "/api/backgrounds/normalize-existing" && request.method === "POST") {
    const taskId = `bg_${Date.now().toString(36)}_${crypto.randomUUID().slice(0, 6)}`;
    const nowIso = new Date().toISOString();
    await db
      .prepare(
        `INSERT INTO tasks (task_id, kind, series_title, status, progress_pct, current_stage, created_at, updated_at)
         VALUES (?, 'bg_import', 'Normalize existing backgrounds & generate thumbnails', 'queued', 0, 'queued', ?, ?)`
      )
      .bind(taskId, nowIso, nowIso)
      .run();

    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const ghRes = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/background-import.yml/dispatches`,
      {
        method: "POST",
        headers: ghHeaders(env, { "Content-Type": "application/json" }),
        body: JSON.stringify({
          ref: "main",
          inputs: {
            task_id: taskId,
            mode: "normalize_existing",
          },
        }),
      }
    );
    if (!ghRes.ok) {
      const errTxt = (await ghRes.text()).slice(0, 300);
      return jsonResp({ error: `Dispatch failed: ${errTxt}` }, 502);
    }
    return jsonResp({ task_id: taskId });
  }

  // --- Prompt & Production Validation ---
  if (path === "/api/prompt" && request.method === "POST") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    const template = await env.STORYFORGE_VAULT.get("vault:agent_prompt");
    const rendered = renderAgentPrompt(template, body.idea, body.target_wpm, body.format);
    if (!rendered.ok) {
      return jsonResp({ error: rendered.error }, rendered.status);
    }
    return jsonResp({ prompt: rendered.prompt });
  }

  if (path === "/api/validate" && request.method === "POST") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    let prod = body.production_json;
    let notices: string[] = [];
    if (typeof prod === "string") {
      const repaired = repairPastedProductionText(prod);
      if (!repaired.ok) {
        return jsonResp({ valid: false, errors: [repaired.error] }, 400);
      }
      prod = repaired.parsed;
      notices.push(...repaired.repairs);
    }
    const migrated = normalizeProduction(prod);
    prod = migrated.doc;
    notices.push(...migrated.notices);

    const errors = [...migrated.errors, ...validateProductionJson(prod)];
    if (errors.length > 0) {
      return jsonResp({ valid: false, errors, notices }, 400);
    }
    return jsonResp({ valid: true, errors: [], notices, production_json: prod });
  }

  // --- Dispatch Render Job (D1 backed) ---
  if (path === "/api/render" && request.method === "POST") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }

    if (body.retry_task !== undefined) {
      if (typeof body.retry_task !== "string" || !body.retry_task) return jsonResp({ error: "Invalid retry task." }, 400);
      const retry = await db.prepare(
        "SELECT production_json, publish_mode, scheduled_for_utc, timezone, voice_id, voice_speed, target_wpm, background_mode, background_clip, background_index FROM tasks WHERE task_id = ? AND status = 'failed' AND kind = 'render'"
      ).bind(body.retry_task).first<any>();
      if (!retry?.production_json) return jsonResp({ error: "No failed render task to re-run." }, 409);
      let config: any = {};
      try { config = JSON.parse(await getRuntimeState(db, undefined, "task_voice:" + body.retry_task, "{}")); } catch {}
      const inferredEngine = String(retry.voice_id || "").startsWith("vs") ? "voicestudio" : "edge";
      body.production_json = JSON.parse(retry.production_json);
      body.voice_engine = config.voice_engine || inferredEngine;
      if (retry.voice_id) body.voice_id = retry.voice_id;
      if (retry.voice_speed) body.voice_speed = retry.voice_speed;
      if (config.voicestudio_speedup !== undefined && config.voicestudio_speedup !== null) {
        body.voicestudio_speedup = config.voicestudio_speedup;
      }
      body.publish_mode = retry.publish_mode || "immediate";
      if (retry.scheduled_for_utc) body.scheduled_for_utc = retry.scheduled_for_utc;
      if (retry.timezone) body.timezone = retry.timezone;
      if (retry.target_wpm) body.target_wpm = retry.target_wpm;
      if (retry.background_mode) body.background_mode = retry.background_mode;
      if (retry.background_clip) body.background_clip = retry.background_clip;
      if (retry.background_index !== null && retry.background_index !== undefined) body.background_index = retry.background_index;
    }
    if (body.retry_edge_task !== undefined) {
      if (typeof body.retry_edge_task !== 'string' || !body.retry_edge_task) return jsonResp({error: 'Invalid retry task.'}, 400);
      const retry = await db.prepare(
        "SELECT voice_id, production_json, publish_mode, scheduled_for_utc, timezone, target_wpm, background_mode, background_clip, background_index FROM tasks WHERE task_id = ? AND status = 'failed' AND kind = 'render'"
      ).bind(body.retry_edge_task).first<any>();
      const config = JSON.parse(await getRuntimeState(db, undefined, 'task_voice:' + body.retry_edge_task, "{}"));
      const isVs = config.voice_engine === 'voicestudio' || String(retry?.voice_id || '').startsWith('vs');
      if (!retry?.production_json || !isVs) return jsonResp({error: 'No failed VoiceStudio render to retry.'}, 409);
      body.production_json = JSON.parse(retry.production_json);
      body.publish_mode = retry.publish_mode || 'immediate';
      if (retry.scheduled_for_utc) body.scheduled_for_utc = retry.scheduled_for_utc;
      if (retry.timezone) body.timezone = retry.timezone;
      if (retry.target_wpm) body.target_wpm = retry.target_wpm;
      if (retry.background_mode) body.background_mode = retry.background_mode;
      if (retry.background_clip) body.background_clip = retry.background_clip;
      if (retry.background_index !== null && retry.background_index !== undefined) body.background_index = retry.background_index;
    }
    let prod = body.production_json;
    if (typeof prod === "string") {
      const repaired = repairPastedProductionText(prod);
      if (!repaired.ok) return jsonResp({ error: repaired.error }, 400);
      prod = repaired.parsed;
    }
    const migrated = normalizeProduction(prod);
    prod = migrated.doc;
    const errors = [...migrated.errors, ...validateProductionJson(prod)];
    if (errors.length > 0) {
      return jsonResp({ error: "Invalid production JSON", errors }, 400);
    }

    const settings = await getSettings(db);
    const requestedEngine = body.voice_engine === "edge" || body.voice_engine === "voicestudio" ? body.voice_engine : settings.voice_engine;
    const voiceEngine: "edge" | "voicestudio" = body.retry_edge_task ? "edge" : requestedEngine;
    const voiceId = String(
      body.retry_edge_task
        ? settings.default_voice_id
        : body.voice_id || (voiceEngine === "voicestudio" ? settings.voicestudio_voice_id : settings.default_voice_id)
    ).trim();
    if (voiceEngine === "voicestudio") {
      const validBuiltin = BUILTIN_VOICESTUDIO_MAP.has(voiceId);
      const validCustom = validBuiltin ? null : await getCustomVoice(db, voiceId);
      if (!validBuiltin && !validCustom) {
        return jsonResp({ error: "Choose a valid VoiceStudio narrator." }, 400);
      }
      if (validCustom?.kind === "cloned" && !validCustom.slots?.base) {
        return jsonResp({ error: "This cloned voice has no base audio clip." }, 400);
      }
    }
    if (voiceEngine === "edge" && !/^[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9]+Neural$/.test(voiceId)) {
      return jsonResp({ error: "Choose an Edge TTS narrator." }, 400);
    }
    const voiceSpeed = String(body.voice_speed || "+37%");
    const targetWpm = Number(body.target_wpm) || 250;

    let bgMode = String(body.background_mode || "sequential");
    let bgClip: string | null = body.background_clip ? String(body.background_clip).trim() : null;
    if (bgMode === "specific" && !bgClip && settings.default_background !== "sequential") {
      bgClip = settings.default_background;
    }
    if (bgMode === "specific" && !bgClip) {
      bgMode = "sequential";
    }
    const bgIndex =
      bgMode === "manual"
        ? Number(body.background_index || 0)
        : Number(await getRuntimeState(db, env.STORYFORGE_VAULT, "bg_index", "0")) || 0;

    const publishMode: PublishMode = VALID_PUBLISH_MODES.has(body.publish_mode)
      ? body.publish_mode
      : "immediate";

    let scheduledForUtc: string | null = null;
    if (publishMode === "schedule") {
      const rawSched = String(body.scheduled_for_utc || "").trim();
      const ms = Date.parse(rawSched);
      if (!rawSched || Number.isNaN(ms)) {
        return jsonResp({ error: "Choose a valid future schedule date and time." }, 400);
      }
      scheduledForUtc = new Date(ms).toISOString();
    }
    const timezone = String(body.timezone || "Africa/Lagos");

    const jobId = `sf_${Date.now().toString(36)}_${crypto.randomUUID().slice(0, 6)}`;
    const nowIso = new Date().toISOString();

    await db
      .prepare(
        `INSERT INTO tasks (
           task_id, kind, series_title, format, status, publish_mode, scheduled_for_utc, timezone,
           voice_id, voice_speed, target_wpm, background_mode, background_index, background_clip,
           production_json, progress_pct, current_stage, dry_run, mock_mode, created_at, updated_at
         ) VALUES (?, 'render', ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'queued', ?, ?, ?, ?)`
      )
      .bind(
        jobId,
        prod.series_title,
        prod.format,
        publishMode,
        scheduledForUtc,
        timezone,
        voiceId,
        voiceSpeed,
        targetWpm,
        bgMode,
        bgIndex,
        bgClip,
        JSON.stringify(prod),
        settings.dry_run ? 1 : 0,
        settings.mock_mode ? 1 : 0,
        nowIso,
        nowIso
      )
      .run();

    const vsSpeedup = Number(body.voicestudio_speedup) || settings.voicestudio_speedup;
    await setRuntimeState(db, `task_voice:${jobId}`, JSON.stringify({voice_engine: voiceEngine, voicestudio_speedup: vsSpeedup}));
    const partStmts = (prod.parts || []).map((p: any) =>
      db
        .prepare(
          `INSERT INTO task_parts (task_id, part_number, title, description, hashtags_json, render_status, publish_status)
           VALUES (?, ?, ?, ?, ?, 'pending', 'pending')`
        )
        .bind(jobId, Number(p.part_number), p.title, p.description || "", JSON.stringify(p.hashtags || []))
    );
    if (partStmts.length) await db.batch(partStmts);

    await appendTaskLogs(db, jobId, [
      {
        ts: nowIso,
        level: "info",
        stage: "queued",
        message: `Task queued (publish mode: ${publishMode}, Voice: ${voiceEngine === "voicestudio" ? "VoiceStudio" : "Edge TTS"} / ${voiceId}, bg: ${bgClip || bgMode}). Dispatching GitHub Actions...`,
        progress_pct: 0,
      },
    ]);

    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const ghRes = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/render.yml/dispatches`,
      {
        method: "POST",
        headers: ghHeaders(env, { "Content-Type": "application/json" }),
        body: JSON.stringify({
          ref: "main",
          inputs: {
            job_id: jobId,
            voice_engine: voiceEngine,
            test_background: body.test_background === true ? "true" : "false",
          },
        }),
      }
    );
    if (!ghRes.ok) {
      const errTxt = (await ghRes.text()).slice(0, 300);
      const failMsg = `GitHub dispatch failed (${ghRes.status}): ${errTxt}`;
      await db
        .prepare("UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE task_id = ?")
        .bind(failMsg, new Date().toISOString(), jobId)
        .run();
      await appendTaskLogs(db, jobId, [
        { ts: new Date().toISOString(), level: "error", stage: "dispatch", message: failMsg },
      ]);
      const full = await getTaskFull(db, jobId);
      return jsonResp({ error: failMsg, job: full?.task }, 502);
    }

    const full = await getTaskFull(db, jobId);
    return jsonResp({ job: full?.task, parts: full?.parts });
  }

  // --- Tasks & Live Logs ---
  if (path === "/api/job" && request.method === "GET") {
    const id = url.searchParams.get("id");
    if (!id) return jsonResp({ error: "Missing id" }, 400);
    const full = await getTaskFull(db, id);
    if (!full) return jsonResp({ error: "Task not found" }, 404);
    const { production_json: _omitted, ...summary } = full.task;
    let vc: any = {};
    try { vc = JSON.parse(await getRuntimeState(db, undefined, `task_voice:${id}`, "{}")); } catch {}
    const inferredEngine = String(summary.voice_id || "").startsWith("vs") ? "voicestudio" : "edge";
    return jsonResp({
      job: {
        ...summary,
        voice_engine: vc.voice_engine || inferredEngine,
        voicestudio_speedup: vc.voicestudio_speedup ?? null,
        parts: full.parts,
        publish_targets: full.targets,
      },
    });
  }

  if ((path === "/api/jobs" || path === "/api/tasks") && request.method === "GET") {
    const [tasks, bgIdx, voiceRows] = await Promise.all([
      listTasksFull(db, 30),
      getRuntimeState(db, env.STORYFORGE_VAULT, "bg_index", "0"),
      db.prepare("SELECT key, value FROM runtime_state WHERE key LIKE 'task_voice:%'").all<{ key: string; value: string }>(),
    ]);
    const voiceMap = new Map<string, any>();
    for (const r of voiceRows.results || []) {
      try { voiceMap.set(r.key.slice("task_voice:".length), JSON.parse(r.value)); } catch {}
    }
    const enriched = tasks.map((t: any) => {
      const vc = voiceMap.get(t.task_id);
      return {
        ...t,
        voice_engine: vc?.voice_engine || (String(t.voice_id || "").startsWith("vs") ? "voicestudio" : "edge"),
        voicestudio_speedup: vc?.voicestudio_speedup ?? null,
      };
    });
    return jsonResp({ jobs: enriched, tasks: enriched, next_background_index: Number(bgIdx) || 0 });
  }

  if (path === "/api/task-logs" && request.method === "GET") {
    const taskId = url.searchParams.get("task_id") || "";
    if (!taskId) return jsonResp({ error: "Missing task_id" }, 400);
    const sinceId = Number(url.searchParams.get("since_id") || 0) || 0;
    const logs = await getTaskLogs(db, taskId, sinceId, 200);
    return jsonResp({ logs });
  }

  if (path === "/api/tasks/retry-publish" && request.method === "POST") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    const taskId = String(body.task_id || "").trim();
    if (!taskId) return jsonResp({ error: "Missing task_id" }, 400);
    const res = await retryFailedTargetsForTask(env, taskId);
    return jsonResp({ ok: true, ...res });
  }

  if (path === "/api/tasks/publish-tick" && request.method === "POST") {
    const res = await processPublishingTick(env);
    return jsonResp({ ok: true, ...res });
  }

  if (path === "/api/tasks/cleanup" && request.method === "POST") {
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    const taskId = String(body.task_id || "").trim();
    if (!taskId) return jsonResp({ error: "Missing task_id" }, 400);
    const res = await cleanupPublishedTask(env, taskId);
    if (!res.removed) return jsonResp({ error: "Cleanup requires a real, confirmed publish for every part and target account." }, 409);
    return jsonResp({ ok: true, ...res });
  }

  // --- SF-010: Delete any task (idempotent, state-aware, partial-failure tolerant) ---
  if (
    (path === "/api/tasks" && request.method === "DELETE") ||
    (path === "/api/tasks/delete" && request.method === "POST")
  ) {
    let taskId = String(url.searchParams.get("task_id") || "").trim();
    if (!taskId) {
      try {
        const body: any = await request.json();
        taskId = String(body.task_id || "").trim();
      } catch {
        /* ignore */
      }
    }
    if (!taskId) return jsonResp({ error: "Missing task_id" }, 400);
    const res = await deleteTaskById(env, taskId);
    return jsonResp(
      res.ok ? res : { ...res, error: res.failures.join("; ") || "Partial delete failure" },
      res.ok ? 200 : 500
    );
  }

  if (path === "/api/test/mock-behavior" && request.method === "POST") {
    const settings = await getSettings(db);
    if (!settings.mock_mode) {
      return jsonResp({ error: "Enable Mock Mode in Settings to configure mock behavior." }, 400);
    }
    let body: any = {};
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "Invalid JSON body" }, 400);
    }
    await setRuntimeState(db, "mock_behavior", JSON.stringify({
      failPlatforms: Array.isArray(body.failPlatforms) ? body.failPlatforms : [],
      delayPlatforms: Array.isArray(body.delayPlatforms) ? body.delayPlatforms : [],
    }));
    return jsonResp({ ok: true });
  }

  // --- Download MP4 proxy (D1 primary + KV legacy fallback) ---
  if (path === "/api/download" && request.method === "GET") {
    const jobId = url.searchParams.get("job_id");
    const partNum = Number(url.searchParams.get("part") || "1");
    if (!jobId) return jsonResp({ error: "Missing job_id" }, 400);

    let part: any = null;
    const full = await getTaskFull(db, jobId);
    if (full) {
      part = full.parts.find((p) => Number(p.part_number) === partNum) || null;
    } else {
      const raw = await env.STORYFORGE_VAULT.get(`job:${jobId}`);
      if (raw) {
        const legacyJob = JSON.parse(raw);
        part = (legacyJob.parts || []).find((p: any) => Number(p.part_number) === partNum) || null;
      }
    }
    if (!part) return jsonResp({ error: "Rendered part not found" }, 404);

    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    let assetApiUrl = part.asset_id
      ? `https://api.github.com/repos/${repo}/releases/assets/${part.asset_id}`
      : null;

    if (!assetApiUrl) {
      const relRes = await fetch(`https://api.github.com/repos/${repo}/releases/tags/renders`, {
        headers: ghHeaders(env),
      });
      if (!relRes.ok) return jsonResp({ error: "Renders release not found" }, 404);
      const rel: any = await relRes.json();
      const asset = (rel.assets || []).find((a: any) => a.name === part.filename);
      if (!asset) return jsonResp({ error: "Asset file not found" }, 404);
      assetApiUrl = asset.url;
    }

    const assetRes = await fetch(assetApiUrl!, {
      headers: ghHeaders(env, { Accept: "application/octet-stream" }),
      redirect: "follow",
    });
    if (!assetRes.ok) {
      return jsonResp({ error: `Asset fetch failed (${assetRes.status})` }, 502);
    }

    return new Response(assetRes.body, {
      status: 200,
      headers: {
        "Content-Type": "video/mp4",
        "Content-Disposition": `attachment; filename="${part.filename || `${jobId}_part${partNum}.mp4`}"`,
        "Cache-Control": "private, max-age=3600",
      },
    });
  }

  return jsonResp({ error: "Not found" }, 404);
};
