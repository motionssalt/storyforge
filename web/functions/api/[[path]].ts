import validateSchema from "../../lib/production-validator.js";
import voiceConfig from "../../public/voices.json";

interface Env {
  STORYFORGE_VAULT: KVNamespace;
  AUTH_PASSWORD: string;
  SESSION_SECRET: string;
  INTERNAL_SECRET: string;
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
}

const VALID_EMOTIONS = new Set([
  "calm", "tense", "angry", "shocked", "sad", "sarcastic", "whisper", "excited"
]);
const VALID_CARD_TYPES = new Set(["post", "comment", "reply", "branded"]);
const VALID_THEMES = new Set(["dark", "light"]);
const VALID_AWARD_TYPES = new Set(["gold", "helpful", "heart"]);

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
      try { out[part.slice(0, idx).trim()] = decodeURIComponent(part.slice(idx + 1).trim()); } catch { /* Invalid cookies are unauthenticated, not a server error. */ }
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
  if (!validateSchema(doc)) {
    return (validateSchema.errors || []).map((e: any) => `${e.instancePath || "/"}: ${e.message}${e.params?.additionalProperty ? " (" + e.params.additionalProperty + ")" : ""}`);
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
    return ["Root value must be a JSON object."];
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
    if (part.part_number !== pi + 1) errors.push(`${p}.part_number: expected ${pi + 1}; parts must be in order.`);
    if (!part || typeof part !== "object") {
      errors.push(`${p}: must be an object.`);
      return;
    }
    if (part.part_number !== 1 && part.part_number !== 2) {
      errors.push(`${p}.part_number: must be 1 or 2.`);
    }
    if (typeof part.title !== "string" || part.title.trim().length < 3) {
      errors.push(`${p}.title: must be a string (at least 3 characters).`);
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

    const order = new Map(part.lines.map((line: any, index: number) => [line.id, index]));
    if (part.cards?.length) {
      const first = part.cards[0];
      if (first.from_line !== part.lines[0].id || !["post", "branded"].includes(first.card.type)) errors.push(`${p}.cards[0]: first card must be a post or branded hook starting on the first line.`);
      let previousStart = -1;
      for (const [ci, card] of part.cards.entries()) {
        const start = Number(order.get(card.from_line));
        const end = Number(order.get(card.to_line));
        if (start > end) errors.push(`${p}.cards[${ci}]: from_line must not follow to_line.`);
        if (start < previousStart) errors.push(`${p}.cards[${ci}]: cards must be ordered by narration line.`);
        previousStart = start;
      }
    }
    if (!Array.isArray(part.cards) || part.cards.length === 0) {
      errors.push(`${p}.cards: must be a non-empty array of card timeline entries.`);
    } else {
      part.cards.forEach((c: any, ci: number) => {
        const cp = `${p}.cards[${ci}]`;
        if (!c || typeof c !== "object") {
          errors.push(`${cp}: must be an object.`);
          return;
        }
        if (typeof c.from_line !== "string" || (lineIds.size > 0 && !lineIds.has(c.from_line))) {
          errors.push(`${cp}.from_line: "${c.from_line}" does not match any line id in ${p}.lines.`);
        }
        if (typeof c.to_line !== "string" || (lineIds.size > 0 && !lineIds.has(c.to_line))) {
          errors.push(`${cp}.to_line: "${c.to_line}" does not match any line id in ${p}.lines.`);
        }
        if (!c.card || typeof c.card !== "object") {
          errors.push(`${cp}.card: missing card object.`);
          return;
        }
        if (!VALID_CARD_TYPES.has(c.card.type)) {
          errors.push(`${cp}.card.type: must be one of post, comment, reply, branded.`);
        }
        if (!VALID_THEMES.has(c.card.theme)) {
          errors.push(`${cp}.card.theme: must be "dark" or "light".`);
        }
        if ((c.card.type === "post" || c.card.type === "branded") && !c.card.title) {
          errors.push(`${cp}.card.title: required for ${c.card.type} cards.`);
        }
        if ((c.card.type === "comment" || c.card.type === "reply") && !c.card.body) {
          errors.push(`${cp}.card.body: required for ${c.card.type} cards.`);
        }
        if (c.card.type === "reply" && (!c.card.reply || !c.card.reply.username || !c.card.reply.body)) {
          errors.push(`${cp}.card.reply: must include username and body for reply cards.`);
        }
        if (Array.isArray(c.card.awards)) {
          c.card.awards.forEach((aw: any, ai: number) => {
            if (!aw || !VALID_AWARD_TYPES.has(aw.type)) {
              errors.push(`${cp}.card.awards[${ai}].type: must be gold, helpful, or heart.`);
            }
          });
        }
      });
    }
  });

  return errors;
}

export const onRequest: PagesFunction<Env> = async (context) => {
  const { request, env } = context;
  const url = new URL(request.url);
  const path = url.pathname;



  // Login endpoint
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

  // Internal GitHub Actions endpoints (authenticated by X-Internal-Secret)
  if (path.startsWith("/api/internal/")) {
    const sec = request.headers.get("X-Internal-Secret");
    if (!sec || sec !== env.INTERNAL_SECRET) {
      return jsonResp({ error: "Unauthorized internal call" }, 401);
    }

    if (path === "/api/internal/background-index" && request.method === "GET") {
      return jsonResp({ index: Number(await env.STORYFORGE_VAULT.get("state:bg_index") || 0) });
    }
    if (path === "/api/internal/job" && request.method === "GET") {
      const id = url.searchParams.get("id");
      if (!id) return jsonResp({ error: "Missing id" }, 400);
      const raw = await env.STORYFORGE_VAULT.get(`job:${id}`);
      if (!raw) return jsonResp({ error: "Job not found" }, 404);
      return new Response(raw, { headers: { "Content-Type": "application/json" } });
    }

    if (path === "/api/internal/job-status" && request.method === "POST") {
      const payload: any = await request.json();
      const id = payload.job_id;
      if (!id) return jsonResp({ error: "Missing job_id" }, 400);
      const raw = await env.STORYFORGE_VAULT.get(`job:${id}`);
      if (!raw) return jsonResp({ error: "Job not found" }, 404);
      const job = JSON.parse(raw);
      job.status = payload.status || job.status;
      job.updated_at = new Date().toISOString();
      if (payload.error) job.error = payload.error;
      if (payload.parts) job.parts = payload.parts;
      await env.STORYFORGE_VAULT.put(`job:${id}`, JSON.stringify(job), { expirationTtl: 30 * 24 * 3600 });

      if (typeof payload.next_background_index === "number") {
        await env.STORYFORGE_VAULT.put("state:bg_index", String(payload.next_background_index));
      }
      return jsonResp({ ok: true });
    }

    return jsonResp({ error: "Unknown internal endpoint" }, 404);
  }

  // All remaining /api/* routes require user authentication
  if (!(await isAuthenticated(request, env))) {
    return jsonResp({ error: "Authentication required" }, 401);
  }

  if (path === "/api/health") return jsonResp({ ok: true });

  // Build agent prompt from KV template
  if (path === "/api/prompt" && request.method === "POST") {
    const body: any = await request.json().catch(() => ({}));
    const idea = String(body.idea || "").trim();
    if (!idea) {
      return jsonResp({ error: "Please enter a story idea first." }, 400);
    }
    const targetWpm = Math.max(150, Math.min(350, Number(body.target_wpm) || 250));
    const words90s = Math.round((90 / 60) * targetWpm);

    const template = await env.STORYFORGE_VAULT.get("vault:agent_prompt");
    if (!template) {
      return jsonResp({ error: "Agent prompt template missing in vault KV." }, 500);
    }
    const filled = template
      .replaceAll("{{TARGET_WPM}}", String(targetWpm))
      .replaceAll("{{TARGET_WORDS_90S}}", String(words90s))
      .replaceAll("{{IDEA}}", idea);

    return jsonResp({ prompt: filled, target_wpm: targetWpm, target_words_90s: words90s });
  }

  // Validate Production JSON
  if (path === "/api/validate" && request.method === "POST") {
    const body: any = await request.json().catch(() => null);
    let parsed = body?.production_json ?? body;
    if (typeof parsed === "string") {
      try {
        parsed = JSON.parse(parsed);
      } catch (e: any) {
        return jsonResp({ valid: false, errors: [`Invalid JSON syntax: ${e.message}`] }, 400);
      }
    }
    const errors = validateProductionJson(parsed);
    return jsonResp({ valid: errors.length === 0, errors }, errors.length === 0 ? 200 : 400);
  }

  // Start render job
  if (path === "/api/render" && request.method === "POST") {
    const body: any = await request.json().catch(() => ({}));
    let prod = body.production_json;
    if (typeof prod === "string") {
      try {
        prod = JSON.parse(prod);
      } catch (e: any) {
        return jsonResp({ error: `Invalid JSON syntax: ${e.message}` }, 400);
      }
    }
    const errors = validateProductionJson(prod);
    if (errors.length > 0) {
      return jsonResp({ error: "Validation failed", errors }, 400);
    }

    const voiceId = String(body.voice_id || voiceConfig.voices[0].id);
    const voice = voiceConfig.voices.find((v) => v.id === voiceId);
    if (!voice) return jsonResp({ error: "voice_id: choose a supported voice." }, 400);
    const targetWpm = Number(body.target_wpm ?? 250);
    if (!Number.isFinite(targetWpm) || targetWpm < 150 || targetWpm > 350) return jsonResp({ error: "target_wpm: must be between 150 and 350." }, 400);
    const calibrated = Math.max(-50, Math.min(65, Math.round((targetWpm / voice.base_wpm - 1) * 100)));
    const voiceSpeed = String(body.voice_speed || `${calibrated >= 0 ? "+" : ""}${calibrated}%`);
    if (!/^[+-]\d{1,2}%$/.test(voiceSpeed) || Number.parseInt(voiceSpeed) < -50 || Number.parseInt(voiceSpeed) > 65) return jsonResp({ error: "voice_speed: use a rate between -50% and +65%." }, 400);
    const bgMode = String(body.background_mode || "sequential");

    let bgIndex = 0;
    if (bgMode === "manual" && Number.isFinite(Number(body.background_index))) {
      bgIndex = Math.max(0, Math.floor(Number(body.background_index)));
    } else {
      const storedIdx = await env.STORYFORGE_VAULT.get("state:bg_index");
      bgIndex = storedIdx ? Math.max(0, parseInt(storedIdx, 10) || 0) : 0;
    }

    const jobId = `sf_${Date.now().toString(36)}_${crypto.randomUUID().slice(0, 6)}`;
    const job = {
      job_id: jobId,
      status: "queued",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      series_title: prod.series_title,
      format: prod.format,
      voice_id: voiceId,
      voice_speed: voiceSpeed,
      target_wpm: targetWpm,
      background_index: bgIndex,
      background_mode: bgMode,
      production_json: prod,
      parts: [],
      error: null,
    };

    await env.STORYFORGE_VAULT.put(`job:${jobId}`, JSON.stringify(job), { expirationTtl: 30 * 24 * 3600 });

    // Maintain a compact recent jobs list
    const listRaw = await env.STORYFORGE_VAULT.get("state:recent_jobs");
    const recent: string[] = listRaw ? JSON.parse(listRaw) : [];
    const updatedRecent = [jobId, ...recent.filter((x) => x !== jobId)].slice(0, 20);
    await env.STORYFORGE_VAULT.put("state:recent_jobs", JSON.stringify(updatedRecent));

    // Trigger GitHub Actions workflow_dispatch
    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    const ghRes = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/render.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "StoryForge-Worker",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          ref: "main",
          inputs: { job_id: jobId },
        }),
      }
    );

    if (!ghRes.ok) {
      const errTxt = await ghRes.text();
      job.status = "failed";
      job.error = `GitHub dispatch failed (${ghRes.status}): ${errTxt.slice(0, 300)}`;
      await env.STORYFORGE_VAULT.put(`job:${jobId}`, JSON.stringify(job), { expirationTtl: 30 * 24 * 3600 });
      return jsonResp({ error: job.error, job }, 502);
    }

    return jsonResp({ job });
  }

  // Get single job status
  if (path === "/api/job" && request.method === "GET") {
    const id = url.searchParams.get("id");
    if (!id) return jsonResp({ error: "Missing id" }, 400);
    const raw = await env.STORYFORGE_VAULT.get(`job:${id}`);
    if (!raw) return jsonResp({ error: "Job not found" }, 404);
    const job = JSON.parse(raw);
    const { production_json: _omitted, ...summary } = job;
    return jsonResp({ job: summary });
  }

  // List recent jobs
  if (path === "/api/jobs" && request.method === "GET") {
    const listRaw = await env.STORYFORGE_VAULT.get("state:recent_jobs");
    const ids: string[] = listRaw ? JSON.parse(listRaw) : [];
    const jobs = [];
    for (const id of ids.slice(0, 12)) {
      const raw = await env.STORYFORGE_VAULT.get(`job:${id}`);
      if (raw) {
        const j = JSON.parse(raw);
        const { production_json: _omitted, ...summary } = j;
        jobs.push(summary);
      }
    }
    const bgIdx = (await env.STORYFORGE_VAULT.get("state:bg_index")) || "0";
    return jsonResp({ jobs, next_background_index: Number(bgIdx) });
  }

  // Proxy GitHub Release asset download cleanly through the Worker
  if (path === "/api/download" && request.method === "GET") {
    const jobId = url.searchParams.get("job_id");
    const partNum = Number(url.searchParams.get("part") || "1");
    if (!jobId) return jsonResp({ error: "Missing job_id" }, 400);

    const raw = await env.STORYFORGE_VAULT.get(`job:${jobId}`);
    if (!raw) return jsonResp({ error: "Job not found" }, 404);
    const job = JSON.parse(raw);
    const part = (job.parts || []).find((p: any) => Number(p.part_number) === partNum);
    if (!part) return jsonResp({ error: "Rendered part not found" }, 404);

    const repo = env.GITHUB_REPO || "motionssalt/storyforge";
    let assetApiUrl = part.asset_id
      ? `https://api.github.com/repos/${repo}/releases/assets/${part.asset_id}`
      : null;

    if (!assetApiUrl) {
      const relRes = await fetch(`https://api.github.com/repos/${repo}/releases/tags/renders`, {
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "StoryForge-Worker",
        },
      });
      if (!relRes.ok) return jsonResp({ error: "Renders release not found" }, 404);
      const rel: any = await relRes.json();
      const asset = (rel.assets || []).find((a: any) => a.name === part.filename);
      if (!asset) return jsonResp({ error: "Asset file not found" }, 404);
      assetApiUrl = asset.url;
    }

    const assetRes = await fetch(assetApiUrl!, {
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/octet-stream",
        "User-Agent": "StoryForge-Worker",
      },
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
