import {
  appendTaskLogs,
  getRuntimeState,
  getSettings,
  getTaskFull,
  listZernioAccounts,
  listZernioKeys,
  setRuntimeState,
} from "./db";
import { buildPlatformTarget, ensurePartTitle, PartMeta } from "./platforms";
import {
  Env,
  PublishTargetRow,
  TaskPartRow,
  TaskRow,
  ZernioAccountRow,
  ZernioKeyRow,
} from "./types";
import { CreatePostInput, getZernioAdapter, ZernioAdapter } from "./zernio";

function addMinutesIso(iso: string, minutes: number): string {
  const ms = Date.parse(iso);
  const base = Number.isFinite(ms) ? ms : Date.now();
  return new Date(base + minutes * 60_000).toISOString();
}

function cadenceIntervalMs(count: number, unit: string): number {
  const safeCount = Math.max(1, Number(count) || 1);
  if (unit === "week") return Math.round((7 * 86_400_000) / safeCount);
  if (unit === "month") return Math.round((30 * 86_400_000) / safeCount);
  return Math.round(86_400_000 / safeCount);
}

async function allocateNextQueueSlot(env: Env, count: number, unit: string): Promise<string> {
  const nowMs = Date.now();
  const interval = cadenceIntervalMs(count, unit);
  const lastRaw = await getRuntimeState(env.STORYFORGE_DB, env.STORYFORGE_VAULT, "last_queue_slot_utc", "");
  const lastMs = lastRaw ? Date.parse(lastRaw) : 0;
  const nextMs = Number.isFinite(lastMs) && lastMs + interval > nowMs ? lastMs + interval : nowMs;
  const nextIso = new Date(nextMs).toISOString();
  await setRuntimeState(env.STORYFORGE_DB, "last_queue_slot_utc", nextIso);
  return nextIso;
}

async function fetchGitHubRenderAssetStream(
  env: Env,
  part: TaskPartRow
): Promise<{ body: ReadableStream<Uint8Array>; sizeBytes: number }> {
  const repo = env.GITHUB_REPO || "motionssalt/storyforge";
  let assetApiUrl = part.asset_id
    ? `https://api.github.com/repos/${repo}/releases/assets/${part.asset_id}`
    : null;
  let sizeBytes = Number(part.size_bytes || 0);

  if (!assetApiUrl) {
    const relRes = await fetch(`https://api.github.com/repos/${repo}/releases/tags/renders`, {
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "StoryForge-Worker",
      },
    });
    if (!relRes.ok) throw new Error(`Renders release lookup failed (${relRes.status})`);
    const rel: any = await relRes.json();
    const asset = (rel.assets || []).find((a: any) => a.name === part.filename);
    if (!asset) throw new Error(`Render asset ${part.filename} not found in renders release`);
    assetApiUrl = asset.url;
    sizeBytes = Number(asset.size || sizeBytes);
  }

  // Follow redirect manually so Authorization header isn't sent to signed object storage
  const redir = await fetch(assetApiUrl!, {
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/octet-stream",
      "User-Agent": "StoryForge-Worker",
    },
    redirect: "manual",
  });

  if (redir.status >= 300 && redir.status < 400) {
    const loc = redir.headers.get("Location");
    if (!loc) throw new Error("GitHub asset redirect missing Location header");
    const dl = await fetch(loc);
    if (!dl.ok || !dl.body) throw new Error(`Signed asset download failed (${dl.status})`);
    const len = Number(dl.headers.get("Content-Length") || sizeBytes || 0);
    return { body: dl.body, sizeBytes: len };
  }

  if (!redir.ok || !redir.body) {
    throw new Error(`GitHub asset download failed (${redir.status})`);
  }
  const len = Number(redir.headers.get("Content-Length") || sizeBytes || 0);
  return { body: redir.body, sizeBytes: len };
}

async function deleteGitHubRenderAssetsForTask(env: Env, parts: TaskPartRow[]): Promise<string[]> {
  const repo = env.GITHUB_REPO || "motionssalt/storyforge";
  const deleted: string[] = [];
  const relRes = await fetch(`https://api.github.com/repos/${repo}/releases/tags/renders`, {
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "StoryForge-Worker",
    },
  });
  if (!relRes.ok) return deleted;
  const rel: any = await relRes.json();
  const assets: any[] = rel.assets || [];

  const targetNames = new Set<string>();
  const targetIds = new Set<number>();
  for (const p of parts) {
    if (p.asset_id) targetIds.add(Number(p.asset_id));
    if (p.filename) {
      targetNames.add(p.filename);
      const thumbName = p.filename.replace(/\.mp4$/i, ".jpg");
      targetNames.add(thumbName);
    }
  }

  for (const a of assets) {
    if (targetIds.has(Number(a.id)) || targetNames.has(String(a.name))) {
      const delRes = await fetch(`https://api.github.com/repos/${repo}/releases/assets/${a.id}`, {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "StoryForge-Worker",
        },
      });
      if (delRes.ok || delRes.status === 404) {
        deleted.push(String(a.name));
      }
    }
  }
  return deleted;
}

export async function cleanupPublishedTask(env: Env, taskId: string): Promise<{ deletedAssets: string[] }> {
  const full = await getTaskFull(env.STORYFORGE_DB, taskId);
  if (!full) return { deletedAssets: [] };
  const deletedAssets = await deleteGitHubRenderAssetsForTask(env, full.parts);
  await env.STORYFORGE_DB.batch([
    env.STORYFORGE_DB.prepare("DELETE FROM task_logs WHERE task_id = ?").bind(taskId),
    env.STORYFORGE_DB.prepare("DELETE FROM publish_targets WHERE task_id = ?").bind(taskId),
    env.STORYFORGE_DB.prepare("DELETE FROM task_parts WHERE task_id = ?").bind(taskId),
    env.STORYFORGE_DB.prepare("DELETE FROM tasks WHERE task_id = ?").bind(taskId),
  ]);
  try {
    await env.STORYFORGE_VAULT.delete(`job:${taskId}`);
  } catch {
    /* ignore */
  }
  return { deletedAssets };
}

export async function initializePublishingForTask(env: Env, taskId: string): Promise<void> {
  const db = env.STORYFORGE_DB;
  const full = await getTaskFull(db, taskId);
  if (!full) return;
  const { task, parts } = full;

  // Guard: never publish unless EVERY part rendered successfully
  if (!parts.length || parts.some((p) => p.render_status !== "done")) {
    return;
  }

  const settings = await getSettings(db);
  const keys = await listZernioKeys(db);
  const accounts = await listZernioAccounts(db);
  const nowIso = new Date().toISOString();

  await db
    .prepare("UPDATE tasks SET dry_run = ?, mock_mode = ?, updated_at = ? WHERE task_id = ?")
    .bind(settings.dry_run ? 1 : 0, settings.mock_mode ? 1 : 0, nowIso, taskId)
    .run();

  if (keys.length === 0 || accounts.length === 0) {
    await db
      .prepare("UPDATE tasks SET status = 'waiting_for_keys', current_stage = 'waiting_for_keys', updated_at = ? WHERE task_id = ?")
      .bind(nowIso, taskId)
      .run();
    await db
      .prepare("UPDATE task_parts SET publish_status = 'waiting_for_keys' WHERE task_id = ?")
      .bind(taskId)
      .run();
    await appendTaskLogs(db, taskId, [
      {
        ts: nowIso,
        level: "warn",
        stage: "publish",
        message:
          keys.length === 0
            ? "Render complete. No Zernio API keys configured — waiting for keys to start publishing automatically."
            : "Render complete. No connected social accounts cached — add/refresh accounts in Settings to start publishing.",
      },
    ]);
    return;
  }

  // Determine Part 1 initial eligibility timestamp
  let part1EligibleAt = nowIso;
  let initialStatus = "publishing";
  if (task.publish_mode === "schedule" && task.scheduled_for_utc) {
    part1EligibleAt = task.scheduled_for_utc;
    if (Date.parse(part1EligibleAt) > Date.now()) {
      initialStatus = "scheduled";
    }
  } else if (task.publish_mode === "queue") {
    part1EligibleAt = await allocateNextQueueSlot(env, settings.queue_cadence_count, settings.queue_cadence_unit);
    initialStatus = "queued_publish";
  }

  // Create publish_targets rows for every part & every active account
  const stmts: D1PreparedStatement[] = [];
  for (const part of parts) {
    const meta: PartMeta = {
      part_number: part.part_number,
      format: task.format,
      title: part.title || task.series_title || `Part ${part.part_number}`,
      description: part.description || part.title || "",
      hashtags: part.hashtags_json ? JSON.parse(part.hashtags_json) : [],
      attribution: part.attribution,
      duration_sec: Number(part.duration_sec || 0),
      size_bytes: Number(part.size_bytes || 0),
    };

    for (const acc of accounts) {
      const check = buildPlatformTarget(acc.platform, acc.account_id, meta);
      const status = check.compatible ? "pending" : "skipped";
      const err = check.compatible ? null : check.skipReason || "Incompatible with platform limits";
      const eligibleAt = check.compatible && part.part_number === 1 ? part1EligibleAt : null;

      stmts.push(
        db
          .prepare(
            `INSERT INTO publish_targets (task_id, part_number, key_id, account_id, platform, account_name, status, error, next_eligible_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT(task_id, part_number, key_id, account_id) DO NOTHING`
          )
          .bind(
            taskId,
            part.part_number,
            acc.key_id,
            acc.account_id,
            acc.platform,
            acc.account_name,
            status,
            err,
            eligibleAt,
            nowIso
          )
      );
    }
  }

  if (stmts.length > 0) await db.batch(stmts);

  await db
    .prepare("UPDATE tasks SET status = ?, scheduled_for_utc = COALESCE(scheduled_for_utc, ?), current_stage = ?, updated_at = ? WHERE task_id = ?")
    .bind(initialStatus, part1EligibleAt, initialStatus, nowIso, taskId)
    .run();
  await db
    .prepare("UPDATE task_parts SET publish_status = ? WHERE task_id = ? AND part_number = 1")
    .bind(initialStatus === "publishing" ? "pending" : initialStatus, taskId)
    .run();
  await db
    .prepare("UPDATE task_parts SET publish_status = 'waiting_part1' WHERE task_id = ? AND part_number > 1")
    .bind(taskId)
    .run();

  await appendTaskLogs(db, taskId, [
    {
      ts: nowIso,
      level: "info",
      stage: "publish",
      message: `Initialized publishing (${task.publish_mode}${settings.dry_run ? ", DRY RUN" : ""}) across ${accounts.length} account(s). Part 1 target time: ${part1EligibleAt}`,
    },
  ]);

  if (Date.parse(part1EligibleAt) <= Date.now()) {
    await processPublishingTick(env);
  }
}

export async function triggerWaitingTasksOnKeysReady(env: Env): Promise<number> {
  const db = env.STORYFORGE_DB;
  const { results } = await db
    .prepare("SELECT task_id FROM tasks WHERE status = 'waiting_for_keys' AND kind = 'render' ORDER BY created_at ASC")
    .all<{ task_id: string }>();
  let count = 0;
  for (const row of results || []) {
    await initializePublishingForTask(env, row.task_id);
    count++;
  }
  return count;
}

async function pollInFlightTargets(
  env: Env,
  adapter: ZernioAdapter,
  keyMap: Map<string, ZernioKeyRow>,
  part2DelayMin: number
): Promise<void> {
  const db = env.STORYFORGE_DB;
  const { results: inflight } = await db
    .prepare(
      `SELECT pt.*, t.dry_run
       FROM publish_targets pt
       JOIN tasks t ON t.task_id = pt.task_id
       WHERE pt.status IN ('publishing', 'uploading', 'processing') AND pt.zernio_post_id IS NOT NULL`
    )
    .all<PublishTargetRow & { dry_run: number }>();

  if (!inflight || !inflight.length) return;

  // Group by (key_id, zernio_post_id)
  const batches = new Map<string, (PublishTargetRow & { dry_run: number })[]>();
  for (const row of inflight) {
    if (row.dry_run === 1 || !row.zernio_post_id) continue;
    const k = `${row.key_id}::${row.zernio_post_id}`;
    const arr = batches.get(k) || [];
    arr.push(row);
    batches.set(k, arr);
  }

  const nowIso = new Date().toISOString();
  for (const [, rows] of batches) {
    const keyRow = keyMap.get(rows[0].key_id);
    if (!keyRow) continue;
    try {
      const resp = await adapter.getPost(keyRow.api_key, rows[0].zernio_post_id!);
      const byAcc = new Map<string, any>();
      for (const p of resp.post.platforms) {
        if (p.accountId) byAcc.set(p.accountId, p);
      }

      for (const target of rows) {
        const platRes = byAcc.get(target.account_id);
        if (!platRes) continue;
        if (platRes.status === "published") {
          const pubAt = platRes.publishedAt || nowIso;
          await db
            .prepare(
              "UPDATE publish_targets SET status = 'published', platform_post_url = COALESCE(?, platform_post_url), published_at = ?, error = NULL, updated_at = ? WHERE id = ?"
            )
            .bind(platRes.platformPostUrl || null, pubAt, nowIso, target.id)
            .run();

          // Schedule next part for this exact account relative to actual Part N live time!
          const nextElig = addMinutesIso(pubAt, part2DelayMin);
          await db
            .prepare(
              `UPDATE publish_targets SET next_eligible_at = ?, updated_at = ?
               WHERE task_id = ? AND part_number = ? AND key_id = ? AND account_id = ? AND status = 'pending'`
            )
            .bind(nextElig, nowIso, target.task_id, target.part_number + 1, target.key_id, target.account_id)
            .run();
          await db
            .prepare(
              `UPDATE task_parts SET planned_publish_at = COALESCE(planned_publish_at, ?) WHERE task_id = ? AND part_number = ?`
            )
            .bind(nextElig, target.task_id, target.part_number + 1)
            .run();

          await appendTaskLogs(db, target.task_id, [
            {
              ts: nowIso,
              level: "info",
              stage: "publish",
              message: `Part ${target.part_number} confirmed live on ${target.platform} (${target.account_name})${platRes.platformPostUrl ? `: ${platRes.platformPostUrl}` : ""}`,
            },
          ]);
        } else if (platRes.status === "failed" || platRes.status === "cancelled") {
          const errMsg = platRes.errorMessage || `Platform returned ${platRes.status}`;
          await db
            .prepare("UPDATE publish_targets SET status = 'failed', error = ?, updated_at = ? WHERE id = ?")
            .bind(errMsg, nowIso, target.id)
            .run();
          // Hold any later parts on this account so Part 2 never posts when Part 1 failed
          await db
            .prepare(
              `UPDATE publish_targets SET status = 'held', error = ?, updated_at = ?
               WHERE task_id = ? AND part_number > ? AND key_id = ? AND account_id = ? AND status = 'pending'`
            )
            .bind(`Held because Part ${target.part_number} failed on this account`, nowIso, target.task_id, target.part_number, target.key_id, target.account_id)
            .run();
          await appendTaskLogs(db, target.task_id, [
            {
              ts: nowIso,
              level: "error",
              stage: "publish",
              message: `Part ${target.part_number} failed on ${target.platform} (${target.account_name}): ${errMsg}`,
            },
          ]);
        }
      }
    } catch (e: any) {
      await appendTaskLogs(db, rows[0].task_id, [
        {
          ts: nowIso,
          level: "warn",
          stage: "publish",
          message: `Status poll error for Zernio post ${rows[0].zernio_post_id}: ${e?.message || e}`,
        },
      ]);
    }
  }
}

async function getSeriesBlockedAccounts(db: D1Database): Promise<Map<string, Set<string>>> {
  // SF-009: If a multi-part series has already started Part 1 and still has Part 2+ pending/publishing,
  // NO other queued post may go out on those occupied accounts until the series completes!
  const blocked = new Map<string, Set<string>>();
  const { results: activeSeries } = await db
    .prepare(
      `SELECT DISTINCT t.task_id
       FROM tasks t
       JOIN task_parts p1 ON p1.task_id = t.task_id AND p1.part_number = 1
       JOIN task_parts p2 ON p2.task_id = t.task_id AND p2.part_number > 1
       WHERE t.kind = 'render'
         AND t.status IN ('publishing', 'queued_publish', 'scheduled')
         AND EXISTS (SELECT 1 FROM publish_targets pt1 WHERE pt1.task_id = t.task_id AND pt1.part_number = 1 AND pt1.status IN ('published', 'publishing', 'uploading', 'processing'))
         AND EXISTS (SELECT 1 FROM publish_targets pt2 WHERE pt2.task_id = t.task_id AND pt2.part_number > 1 AND pt2.status IN ('pending', 'publishing', 'uploading', 'processing'))`
    )
    .all<{ task_id: string }>();

  for (const s of activeSeries || []) {
    const { results: accs } = await db
      .prepare("SELECT DISTINCT account_id FROM publish_targets WHERE task_id = ?")
      .bind(s.task_id)
      .all<{ account_id: string }>();
    const set = new Set<string>((accs || []).map((a) => a.account_id));
    blocked.set(s.task_id, set);
  }
  return blocked;
}

export async function processPublishingTick(env: Env): Promise<{ checked: number; cleaned: number }> {
  const db = env.STORYFORGE_DB;
  const settings = await getSettings(db);
  const adapter = getZernioAdapter(db, settings.mock_mode);
  const keys = await listZernioKeys(db);
  const keyMap = new Map<string, ZernioKeyRow>(keys.map((k) => [k.id, k]));

  // 1. Poll in-flight Zernio posts first
  await pollInFlightTargets(env, adapter, keyMap, settings.part2_delay_minutes);

  // 2. Determine queue lock per active series (SF-009)
  const activeSeriesBlocks = await getSeriesBlockedAccounts(db);

  // 3. Find eligible pending targets whose next_eligible_at <= now
  const nowIso = new Date().toISOString();
  const { results: pendingRows } = await db
    .prepare(
      `SELECT pt.*, t.format, t.publish_mode, t.dry_run, t.series_title
       FROM publish_targets pt
       JOIN tasks t ON t.task_id = pt.task_id
       WHERE pt.status = 'pending'
         AND pt.next_eligible_at IS NOT NULL
         AND pt.next_eligible_at <= ?
         AND t.kind = 'render'
         AND NOT EXISTS (SELECT 1 FROM task_parts tp WHERE tp.task_id = pt.task_id AND tp.render_status != 'done')
       ORDER BY t.created_at ASC, pt.part_number ASC`
    )
    .bind(nowIso)
    .all<PublishTargetRow & { format: string; publish_mode: string; dry_run: number; series_title: string }>();

  // Group into (task_id, part_number, key_id) batches
  const dispatchBatches = new Map<string, (PublishTargetRow & { format: string; publish_mode: string; dry_run: number; series_title: string })[]>();
  for (const row of pendingRows || []) {
    // SF-009: If this is a queued task's Part 1, check if another active series occupies this account
    if (row.publish_mode === "queue" && row.part_number === 1) {
      let isBlocked = false;
      for (const [seriesTaskId, accSet] of activeSeriesBlocks) {
        if (seriesTaskId !== row.task_id && accSet.has(row.account_id)) {
          isBlocked = true;
          break;
        }
      }
      if (isBlocked) continue;
    }

    // SF-009: If part_number > 1, double-check Part (N-1) is actually 'published' for this exact account
    if (row.part_number > 1) {
      const prev = await db
        .prepare(
          "SELECT status FROM publish_targets WHERE task_id = ? AND part_number = ? AND key_id = ? AND account_id = ?"
        )
        .bind(row.task_id, row.part_number - 1, row.key_id, row.account_id)
        .first<{ status: string }>();
      if (!prev || prev.status !== "published") continue;
    }

    const bKey = `${row.task_id}::${row.part_number}::${row.key_id}`;
    const arr = dispatchBatches.get(bKey) || [];
    arr.push(row);
    dispatchBatches.set(bKey, arr);
  }

  for (const [, batch] of dispatchBatches) {
    const first = batch[0];
    const taskFull = await getTaskFull(db, first.task_id);
    if (!taskFull) continue;
    const part = taskFull.parts.find((p) => p.part_number === first.part_number);
    if (!part) continue;
    const keyRow = keyMap.get(first.key_id);
    if (!keyRow) continue;

    const meta: PartMeta = {
      part_number: part.part_number,
      format: taskFull.task.format,
      title: part.title || taskFull.task.series_title || `Part ${part.part_number}`,
      description: part.description || part.title || "",
      hashtags: part.hashtags_json ? JSON.parse(part.hashtags_json) : [],
      attribution: part.attribution,
      duration_sec: Number(part.duration_sec || 0),
      size_bytes: Number(part.size_bytes || 0),
    };

    const platformEntries: CreatePostInput["platforms"] = [];
    let tiktokSettings: Record<string, any> | undefined;
    let youtubeTags: string[] | undefined;

    for (const target of batch) {
      const built = buildPlatformTarget(target.platform, target.account_id, meta);
      if (!built.compatible || !built.platformEntry) {
        await db
          .prepare("UPDATE publish_targets SET status = 'skipped', error = ?, updated_at = ? WHERE id = ?")
          .bind(built.skipReason || "Incompatible", nowIso, target.id)
          .run();
        continue;
      }
      platformEntries.push(built.platformEntry);
      if (built.tiktokSettings) tiktokSettings = built.tiktokSettings;
      if (built.youtubeTags?.length) youtubeTags = built.youtubeTags;
    }

    if (!platformEntries.length) continue;

    const baseTitle = ensurePartTitle(meta.title, meta.part_number, meta.format);
    const baseContent = `${baseTitle}\n\n${meta.description}`;
    const previewPayload: CreatePostInput = {
      title: baseTitle,
      content: baseContent,
      mediaItems: [
        {
          type: "video",
          url: first.dry_run ? `dry-run://${part.filename || "video.mp4"}` : "<uploaded-zernio-media-url>",
        },
      ],
      platforms: platformEntries,
      ...(youtubeTags ? { tags: youtubeTags } : {}),
      ...(tiktokSettings ? { tiktokSettings } : {}),
      publishNow: true,
      metadata: {
        storyforge_task_id: first.task_id,
        part_number: part.part_number,
      },
    };

    if (first.dry_run === 1) {
      const previewStr = JSON.stringify(previewPayload, null, 2);
      const nextElig = addMinutesIso(nowIso, settings.part2_delay_minutes);
      for (const target of batch) {
        await db
          .prepare(
            "UPDATE publish_targets SET status = 'published', platform_post_url = ?, published_at = ?, payload_preview = ?, error = NULL, updated_at = ? WHERE id = ?"
          )
          .bind(`dry-run://${first.task_id}/part${part.part_number}/${target.platform}`, nowIso, previewStr, nowIso, target.id)
          .run();
        await db
          .prepare(
            `UPDATE publish_targets SET next_eligible_at = ?, updated_at = ?
             WHERE task_id = ? AND part_number = ? AND key_id = ? AND account_id = ? AND status = 'pending'`
          )
          .bind(nextElig, nowIso, first.task_id, part.part_number + 1, target.key_id, target.account_id)
          .run();
      }
      await db
        .prepare("UPDATE task_parts SET planned_publish_at = COALESCE(planned_publish_at, ?) WHERE task_id = ? AND part_number = ?")
        .bind(nextElig, first.task_id, part.part_number + 1)
        .run();
      await appendTaskLogs(db, first.task_id, [
        {
          ts: nowIso,
          level: "info",
          stage: "publish",
          message: `[DRY RUN] Part ${part.part_number} payload for key "${keyRow.label}":\n${previewStr}`,
        },
      ]);
      continue;
    }

    // Live / Mock upload + createPost
    try {
      const { body, sizeBytes } = await fetchGitHubRenderAssetStream(env, part);
      const mediaUrl = await adapter.uploadVideoFromStream(
        keyRow.api_key,
        part.filename || `${first.task_id}_part${part.part_number}.mp4`,
        sizeBytes,
        body
      );
      const postPayload: CreatePostInput = {
        ...previewPayload,
        mediaItems: [{ type: "video", url: mediaUrl }],
      };
      const idempKey = `${first.task_id}_p${part.part_number}_${keyRow.id}_${Date.now()}`;
      const resp = await adapter.createPost(keyRow.api_key, postPayload, idempKey);

      const byAcc = new Map<string, any>();
      for (const p of resp.post.platforms) {
        if (p.accountId) byAcc.set(p.accountId, p);
      }

      for (const target of batch) {
        const resPlat = byAcc.get(target.account_id);
        const st = resPlat?.status || (resp.post.status === "published" ? "published" : "publishing");
        if (st === "published") {
          const pubAt = resPlat?.publishedAt || resp.post.publishedAt || nowIso;
          const nextElig = addMinutesIso(pubAt, settings.part2_delay_minutes);
          await db
            .prepare(
              `UPDATE publish_targets SET status = 'published', zernio_post_id = ?, platform_post_url = ?, published_at = ?, payload_preview = ?, error = NULL, updated_at = ? WHERE id = ?`
            )
            .bind(resp.post._id, resPlat?.platformPostUrl || null, pubAt, JSON.stringify(postPayload), nowIso, target.id)
            .run();
          await db
            .prepare(
              `UPDATE publish_targets SET next_eligible_at = ?, updated_at = ?
               WHERE task_id = ? AND part_number = ? AND key_id = ? AND account_id = ? AND status = 'pending'`
            )
            .bind(nextElig, nowIso, first.task_id, part.part_number + 1, target.key_id, target.account_id)
            .run();
          await db
            .prepare("UPDATE task_parts SET planned_publish_at = COALESCE(planned_publish_at, ?) WHERE task_id = ? AND part_number = ?")
            .bind(nextElig, first.task_id, part.part_number + 1)
            .run();
        } else if (st === "failed" || st === "cancelled") {
          const errMsg = resPlat?.errorMessage || resp.error || "Publish rejected by platform";
          await db
            .prepare(
              `UPDATE publish_targets SET status = 'failed', zernio_post_id = ?, error = ?, payload_preview = ?, updated_at = ? WHERE id = ?`
            )
            .bind(resp.post._id || null, errMsg, JSON.stringify(postPayload), nowIso, target.id)
            .run();
          await db
            .prepare(
              `UPDATE publish_targets SET status = 'held', error = ?, updated_at = ?
               WHERE task_id = ? AND part_number > ? AND key_id = ? AND account_id = ? AND status = 'pending'`
            )
            .bind(`Held because Part ${part.part_number} failed on this account`, nowIso, first.task_id, part.part_number, target.key_id, target.account_id)
            .run();
        } else {
          // uploading / processing / pending
          await db
            .prepare(
              `UPDATE publish_targets SET status = ?, zernio_post_id = ?, payload_preview = ?, updated_at = ? WHERE id = ?`
            )
            .bind(st, resp.post._id, JSON.stringify(postPayload), nowIso, target.id)
            .run();
        }
      }

      await appendTaskLogs(db, first.task_id, [
        {
          ts: nowIso,
          level: resp.httpStatus === 207 ? "warn" : "info",
          stage: "publish",
          message: `Dispatched Part ${part.part_number} via key "${keyRow.label}" -> Zernio post ${resp.post._id} (status: ${resp.post.status})`,
        },
      ]);
    } catch (e: any) {
      const msg = e?.message || String(e);
      for (const target of batch) {
        await db
          .prepare("UPDATE publish_targets SET status = 'failed', error = ?, updated_at = ? WHERE id = ?")
          .bind(msg.slice(0, 500), nowIso, target.id)
          .run();
        await db
          .prepare(
            `UPDATE publish_targets SET status = 'held', error = ?, updated_at = ?
             WHERE task_id = ? AND part_number > ? AND key_id = ? AND account_id = ? AND status = 'pending'`
          )
          .bind(`Held because Part ${part.part_number} failed on this account`, nowIso, first.task_id, part.part_number, target.key_id, target.account_id)
          .run();
      }
      await appendTaskLogs(db, first.task_id, [
        {
          ts: nowIso,
          level: "error",
          stage: "publish",
          message: `Part ${part.part_number} publish error for key "${keyRow.label}": ${msg}`,
        },
      ]);
    }
  }

  // 4. Recompute task/part statuses and run SF-007 cleanup for fully confirmed tasks
  const { results: activeTasks } = await db
    .prepare(
      "SELECT task_id, dry_run, publish_mode FROM tasks WHERE kind = 'render' AND status IN ('queued_publish', 'scheduled', 'publishing', 'publish_failed_partial', 'publish_failed')"
    )
    .all<{ task_id: string; dry_run: number; publish_mode: string }>();

  let checked = 0;
  let cleaned = 0;

  for (const row of activeTasks || []) {
    checked++;
    const full = await getTaskFull(db, row.task_id);
    if (!full) continue;
    const { parts, targets } = full;

    // Update each part's publish_status
    for (const part of parts) {
      const pt = targets.filter((x) => x.part_number === part.part_number);
      if (!pt.length) continue;
      const hasActive = pt.some((x) => ["pending", "publishing", "uploading", "processing", "scheduled"].includes(x.status));
      const hasFail = pt.some((x) => x.status === "failed");
      const hasHeld = pt.some((x) => x.status === "held");
      const hasPub = pt.some((x) => x.status === "published");
      let pStatus = "pending";
      if (hasActive) {
        pStatus = pt.some((x) => ["publishing", "uploading", "processing"].includes(x.status)) ? "publishing" : "scheduled";
      } else if (hasFail) {
        pStatus = hasPub ? "partial" : "failed";
      } else if (hasHeld && !hasPub) {
        pStatus = "held";
      } else if (pt.every((x) => x.status === "published" || x.status === "skipped")) {
        pStatus = "published";
      }
      const maxPubAt = pt
        .map((x) => x.published_at)
        .filter(Boolean)
        .sort()
        .slice(-1)[0] || null;
      await db
        .prepare("UPDATE task_parts SET publish_status = ?, published_at = COALESCE(published_at, ?) WHERE id = ?")
        .bind(pStatus, pStatus === "published" ? maxPubAt : null, part.id)
        .run();
    }

    const anyPendingOrActive = targets.some((x) =>
      ["pending", "publishing", "uploading", "processing", "scheduled"].includes(x.status)
    );
    const anyFailed = targets.some((x) => x.status === "failed");
    const anyPublished = targets.some((x) => x.status === "published");
    const allDoneAndConfirmed =
      targets.length > 0 &&
      targets.every((x) => x.status === "published" || x.status === "skipped");

    if (allDoneAndConfirmed) {
      await db
        .prepare("UPDATE tasks SET status = 'published', current_stage = 'published', updated_at = ? WHERE task_id = ?")
        .bind(nowIso, row.task_id)
        .run();
      if (row.dry_run === 0) {
        await cleanupPublishedTask(env, row.task_id);
        cleaned++;
      }
    } else if (anyPendingOrActive) {
      const anyStarted = targets.some((x) => ["published", "publishing", "uploading", "processing"].includes(x.status));
      const st = anyStarted
        ? "publishing"
        : row.publish_mode === "schedule"
        ? "scheduled"
        : row.publish_mode === "queue"
        ? "queued_publish"
        : "publishing";
      await db
        .prepare("UPDATE tasks SET status = ?, current_stage = ?, updated_at = ? WHERE task_id = ?")
        .bind(st, st, nowIso, row.task_id)
        .run();
    } else if (anyFailed) {
      const st = anyPublished ? "publish_failed_partial" : "publish_failed";
      await db
        .prepare("UPDATE tasks SET status = ?, current_stage = ?, updated_at = ? WHERE task_id = ?")
        .bind(st, st, nowIso, row.task_id)
        .run();
    }
  }

  return { checked, cleaned };
}

export async function retryFailedTargetsForTask(env: Env, taskId: string): Promise<{ retried: number }> {
  const db = env.STORYFORGE_DB;
  const full = await getTaskFull(db, taskId);
  if (!full) throw new Error("Task not found");
  const settings = await getSettings(db);
  const adapter = getZernioAdapter(db, settings.mock_mode);
  const keys = await listZernioKeys(db);
  const keyMap = new Map<string, ZernioKeyRow>(keys.map((k) => [k.id, k]));

  const failedRows = full.targets.filter((t) => t.status === "failed");
  if (!failedRows.length) return { retried: 0 };

  const nowIso = new Date().toISOString();
  let retried = 0;

  // Group by (part_number, key_id, zernio_post_id)
  const retryByPost = new Map<string, PublishTargetRow[]>();
  const reDispatchRows: PublishTargetRow[] = [];

  for (const row of failedRows) {
    if (row.zernio_post_id && full.task.dry_run === 0) {
      const k = `${row.key_id}::${row.zernio_post_id}`;
      const arr = retryByPost.get(k) || [];
      arr.push(row);
      retryByPost.set(k, arr);
    } else {
      reDispatchRows.push(row);
    }
  }

  // 1. Retry existing Zernio posts via POST /v1/posts/{postId}/retry
  for (const [, rows] of retryByPost) {
    const keyRow = keyMap.get(rows[0].key_id);
    if (!keyRow) continue;
    try {
      const resp = await adapter.retryPost(keyRow.api_key, rows[0].zernio_post_id!);
      const byAcc = new Map<string, any>();
      for (const p of resp.post.platforms) {
        if (p.accountId) byAcc.set(p.accountId, p);
      }
      for (const target of rows) {
        retried++;
        const platRes = byAcc.get(target.account_id);
        const st = platRes?.status || "publishing";
        if (st === "published") {
          const pubAt = platRes?.publishedAt || nowIso;
          const nextElig = addMinutesIso(pubAt, settings.part2_delay_minutes);
          await db
            .prepare("UPDATE publish_targets SET status = 'published', platform_post_url = ?, published_at = ?, error = NULL, updated_at = ? WHERE id = ?")
            .bind(platRes?.platformPostUrl || null, pubAt, nowIso, target.id)
            .run();
          // Unhold & schedule Part N+1 from the NEW Part N live time!
          await db
            .prepare(
              `UPDATE publish_targets SET status = 'pending', next_eligible_at = ?, error = NULL, updated_at = ?
               WHERE task_id = ? AND part_number = ? AND key_id = ? AND account_id = ? AND status IN ('held', 'pending')`
            )
            .bind(nextElig, nowIso, taskId, target.part_number + 1, target.key_id, target.account_id)
            .run();
        } else if (st === "failed") {
          await db
            .prepare("UPDATE publish_targets SET status = 'failed', error = ?, updated_at = ? WHERE id = ?")
            .bind(platRes?.errorMessage || "Retry failed", nowIso, target.id)
            .run();
        } else {
          await db
            .prepare("UPDATE publish_targets SET status = ?, error = NULL, updated_at = ? WHERE id = ?")
            .bind(st, nowIso, target.id)
            .run();
        }
      }
      await appendTaskLogs(db, taskId, [
        {
          ts: nowIso,
          level: "info",
          stage: "publish",
          message: `Retried failed targets on Zernio post ${rows[0].zernio_post_id} -> status: ${resp.post.status}`,
        },
      ]);
    } catch (e: any) {
      await appendTaskLogs(db, taskId, [
        {
          ts: nowIso,
          level: "error",
          stage: "publish",
          message: `Retry failed for Zernio post ${rows[0].zernio_post_id}: ${e?.message || e}`,
        },
      ]);
    }
  }

  // 2. Re-queue failed targets that never created a Zernio post (and unhold their Part 2)
  for (const target of reDispatchRows) {
    retried++;
    await db
      .prepare("UPDATE publish_targets SET status = 'pending', next_eligible_at = ?, error = NULL, updated_at = ? WHERE id = ?")
      .bind(nowIso, nowIso, target.id)
      .run();
    await db
      .prepare(
        `UPDATE publish_targets SET status = 'pending', next_eligible_at = NULL, error = NULL, updated_at = ?
         WHERE task_id = ? AND part_number > ? AND key_id = ? AND account_id = ? AND status = 'held'`
      )
      .bind(nowIso, taskId, target.part_number, target.key_id, target.account_id)
      .run();
  }

  await processPublishingTick(env);
  return { retried };
}
