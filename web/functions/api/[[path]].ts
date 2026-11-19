import validateSchema from "../../lib/production-validator.js";
import {
  appendTaskLogs,
  getRuntimeState,
  getSettings,
  getTaskFull,
  getTaskLogs,
  listTasksFull,
  listZernioAccounts,
  listZernioKeys,
  saveSettings,
  setRuntimeState,
} from "../../shared/db";
import {
  cleanupPublishedTask,
  initializePublishingForTask,
  processPublishingTick,
  retryFailedTargetsForTask,
  triggerWaitingTasksOnKeysReady,
} from "../../shared/publisher";
import { AppSettings, CadenceUnit, Env, PublishMode, ZernioKeyRow } from "../../shared/types";
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

function migrateProductionJson(doc: any): { doc: any; notices: string[] } {
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
    return { doc, notices: [] };
  }
  const notices: string[] = [];
  const cloned = JSON.parse(JSON.stringify(doc));
  if (Array.isArray(cloned.parts)) {
    let migratedLegacy = false;
    cloned.parts.forEach((part: any, pi: number) => {
      if (!part || typeof part !== "object") return;
      if (!part.post && Array.isArray(part.cards) && part.cards.length > 0) {
        migratedLegacy = true;
        const cards: any[] = part.cards;
        let picked: any = null;
        for (const item of cards) {
          const c = item?.card;
          if (c && (c.type === "post" || c.type === "branded")) {
            picked = c;
            break;
          }
        }
        if (!picked) {
          picked = cards[0]?.card || {};
        }
        const postObj: any = {
          theme: picked.theme === "light" ? "light" : "dark",
          subreddit: String(picked.subreddit || "r/stories"),
          username: String(picked.username || picked.handle || "u/storyteller"),
          title: String(picked.title || part.title || "Story"),
        };
        if (picked.age) postObj.age = String(picked.age);
        if (Array.isArray(picked.awards)) postObj.awards = picked.awards;
        if (typeof picked.nsfw === "boolean") postObj.nsfw = picked.nsfw;
        if (typeof picked.verified === "boolean") {
          postObj.verified = picked.verified;
        } else if (picked.type === "branded") {
          postObj.verified = true;
        }
        if (picked.upvotes) postObj.upvotes = String(picked.upvotes);
        if (picked.comments_count) postObj.comments_count = String(picked.comments_count);
        if (typeof picked.show_footer === "boolean") postObj.show_footer = picked.show_footer;
        part.post = postObj;
        const ignored = Math.max(0, cards.length - 1);
        delete part.cards;
        if (!part.description) {
          let fallbackDesc = String(part.story_so_far || postObj.title || part.title || "").trim();
          if (fallbackDesc.length < 10) {
            fallbackDesc = `${fallbackDesc || "Story segment"} — full story narrated in this clip.`;
          }
          part.description = fallbackDesc;
        }
        notices.push(
          `Converted legacy "cards" timeline in Part ${pi + 1} to a v2 "post" using its first ${picked.type || "post"} card${ignored > 0 ? ` and ignored ${ignored} other card(s)` : ""}.`
        );
      }
    });
    if (
      migratedLegacy ||
      (cloned.schema_version === undefined &&
        cloned.parts.length > 0 &&
        cloned.parts.every((p: any) => p && typeof p === "object" && p.post))
    ) {
      cloned.schema_version = 2;
      if (!migratedLegacy) {
        notices.push('Added missing "schema_version": 2.');
      }
    }
  }
  return { doc: cloned, notices };
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
  if (doc.schema_version !== 2) {
    errors.push("schema_version: must be 2.");
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

async function loadBackgroundsManifest(env: Env, rel: any): Promise<{ clips: any[] }> {
  const assets: any[] = rel?.assets || [];
  const manAsset = assets.find((a) => a.name === "manifest.json");
  if (!manAsset) return { clips: [] };
  const dl = await fetch(manAsset.url, {
    headers: ghHeaders(env, { Accept: "application/octet-stream" }),
    redirect: "follow",
  });
  if (!dl.ok) return { clips: [] };
  try {
    const parsed = await dl.json();
    return Array.isArray((parsed as any)?.clips) ? (parsed as { clips: any[] }) : { clips: [] };
  } catch {
    return { clips: [] };
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
      const row = await db.prepare("SELECT * FROM tasks WHERE task_id = ?").bind(id).first<any>();
      if (!row) {
        // Legacy KV fallback if any old job still triggered
        const legacy = await env.STORYFORGE_VAULT.get(`job:${id}`);
        if (!legacy) return jsonResp({ error: "Job not found" }, 404);
        return new Response(legacy, { headers: { "Content-Type": "application/json" } });
      }
      return jsonResp({
        job_id: row.task_id,
        voice_id: row.voice_id,
        voice_speed: row.voice_speed,
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

      const task = await db.prepare("SELECT * FROM tasks WHERE task_id = ?").bind(id).first<any>();
      if (!task) return jsonResp({ error: "Job not found" }, 404);

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
        const stmts = payload.parts.map((p: any) =>
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

    return jsonResp({ error: "Unknown internal endpoint" }, 404);
  }

  // All remaining routes require authentication
  if (!(await isAuthenticated(request, env))) {
    return jsonResp({ error: "Unauthorized" }, 401);
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
    const clips = (manifest.clips || []).map((c: any) => {
      const mp4Asset = assetsByName.get(c.filename);
      const thumbName = c.thumbnail || String(c.filename || "").replace(/\.mp4$/i, ".jpg");
      const hasThumb = assetsByName.has(thumbName);
      return {
        ...c,
        thumbnail: hasThumb ? thumbName : null,
        size_bytes: c.size_bytes ?? mp4Asset?.size ?? null,
        asset_exists: Boolean(mp4Asset),
      };
    });
    return jsonResp({ clips, next_background_index: bgIdx });
  }

  if (path === "/api/backgrounds/thumb" && request.method === "GET") {
    const file = url.searchParams.get("file") || "";
    if (!/^[A-Za-z0-9_.-]+\.(?:jpg|jpeg)$/i.test(file)) {
      return jsonResp({ error: "Invalid thumbnail filename" }, 400);
    }
    const rel = await getOrCreateBackgroundsRelease(env);
    const asset = (rel.assets || []).find((a: any) => a.name === file);
    if (!asset) return jsonResp({ error: "Thumbnail not found" }, 404);
    const res = await fetch(asset.url, {
      headers: ghHeaders(env, { Accept: "application/octet-stream" }),
      redirect: "follow",
    });
    if (!res.ok) return jsonResp({ error: `Thumbnail fetch failed (${res.status})` }, 502);
    return new Response(res.body, {
      status: 200,
      headers: {
        "Content-Type": "image/jpeg",
        "Cache-Control": "private, max-age=3600",
      },
    });
  }

  if (path === "/api/backgrounds/stream" && request.method === "GET") {
    const file = url.searchParams.get("file") || "";
    if (!/^[A-Za-z0-9_.-]+\.mp4$/i.test(file)) {
      return jsonResp({ error: "Invalid video filename" }, 400);
    }
    const rel = await getOrCreateBackgroundsRelease(env);
    const asset = (rel.assets || []).find((a: any) => a.name === file);
    if (!asset) return jsonResp({ error: "Background video not found" }, 404);

    // Step 1: Resolve signed GitHub CDN URL (never leak GitHub token to CDN URL)
    const initRes = await fetch(asset.url, {
      headers: ghHeaders(env, { Accept: "application/octet-stream" }),
      redirect: "manual",
    });
    const signedUrl =
      initRes.status >= 300 && initRes.status < 400 ? initRes.headers.get("Location") : null;

    const rangeHeader = request.headers.get("Range");
    const upstreamHeaders: Record<string, string> = {};
    if (rangeHeader) upstreamHeaders["Range"] = rangeHeader;

    const mediaRes = signedUrl
      ? await fetch(signedUrl, { headers: upstreamHeaders })
      : await fetch(asset.url, {
          headers: ghHeaders(env, { Accept: "application/octet-stream", ...upstreamHeaders }),
          redirect: "follow",
        });

    if (!mediaRes.ok && mediaRes.status !== 206) {
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
    return new Response(mediaRes.body, {
      status: mediaRes.status === 206 ? 206 : 200,
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
    if (oldIdxPos === -1) {
      return jsonResp({ error: "Background clip not found in manifest" }, 404);
    }

    const thumbName = clips[oldIdxPos].thumbnail || filename.replace(/\.mp4$/i, ".jpg");
    for (const a of rel.assets || []) {
      if (a.name === filename || a.name === thumbName) {
        await fetch(`https://api.github.com/repos/${repo}/releases/assets/${a.id}`, {
          method: "DELETE",
          headers: ghHeaders(env),
        });
      }
    }

    const newClips = clips.filter((c) => c.filename !== filename);
    await uploadJsonAssetToRelease(env, rel, "manifest.json", { clips: newClips });

    // Safely adjust D1 sequential pointer so the sequence stays continuous and valid
    const curBgIdx = Number(await getRuntimeState(db, env.STORYFORGE_VAULT, "bg_index", "0")) || 0;
    let nextBgIdx = 0;
    if (newClips.length > 0 && oldLen > 0) {
      const curPos = curBgIdx % oldLen;
      const adjustedPos = curPos > oldIdxPos ? curPos - 1 : curPos % newClips.length;
      nextBgIdx = adjustedPos;
    }
    await setRuntimeState(db, "bg_index", String(nextBgIdx));

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
        { error: "File is over Cloudflare's direct-upload limit (95 MB). Use URL / Google Drive import instead." },
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
    const idea = String(body.idea || "").trim();
    const targetWpm = Number(body.target_wpm) || 250;
    if (!idea) return jsonResp({ error: "Enter a story idea first." }, 400);

    const template = await env.STORYFORGE_VAULT.get("vault:agent_prompt");
    if (!template) {
      return jsonResp({ error: "Prompt template not configured in KV vault." }, 500);
    }
    const prompt = template
      .replace(/\{\{STORY_IDEA\}\}/g, idea)
      .replace(/\{\{TARGET_WPM\}\}/g, String(targetWpm));
    return jsonResp({ prompt });
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
    const migrated = migrateProductionJson(prod);
    prod = migrated.doc;
    notices.push(...migrated.notices);

    const errors = validateProductionJson(prod);
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

    let prod = body.production_json;
    if (typeof prod === "string") {
      const repaired = repairPastedProductionText(prod);
      if (!repaired.ok) return jsonResp({ error: repaired.error }, 400);
      prod = repaired.parsed;
    }
    prod = migrateProductionJson(prod).doc;
    const errors = validateProductionJson(prod);
    if (errors.length > 0) {
      return jsonResp({ error: "Invalid production JSON", errors }, 400);
    }

    const settings = await getSettings(db);
    const voiceId = String(body.voice_id || settings.default_voice_id || "en-US-AndrewMultilingualNeural");
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
        message: `Task queued (publish mode: ${publishMode}, voice: ${voiceId}, bg: ${bgClip || bgMode}). Dispatching GitHub Actions...`,
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
    return jsonResp({ job: { ...summary, parts: full.parts, publish_targets: full.targets } });
  }

  if ((path === "/api/jobs" || path === "/api/tasks") && request.method === "GET") {
    const [tasks, bgIdx] = await Promise.all([
      listTasksFull(db, 30),
      getRuntimeState(db, env.STORYFORGE_VAULT, "bg_index", "0"),
    ]);
    return jsonResp({ jobs: tasks, tasks, next_background_index: Number(bgIdx) || 0 });
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
    return jsonResp({ ok: true, ...res });
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
