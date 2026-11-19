import { getRuntimeState, setRuntimeState } from "./db";

export interface ZernioAccountInfo {
  accountId: string;
  platform: string;
  accountName: string;
  username: string;
  profileId: string | null;
  isActive: boolean;
  raw: any;
}

export interface ZernioPlatformResult {
  platform: string;
  accountId: string;
  status: "pending" | "processing" | "uploading" | "published" | "failed" | "cancelled";
  platformPostId?: string | null;
  platformPostUrl?: string | null;
  publishedAt?: string | null;
  errorMessage?: string | null;
  errorCategory?: string | null;
}

export interface ZernioPostResponse {
  httpStatus: number;
  post: {
    _id: string;
    status: "draft" | "pending_approval" | "scheduled" | "publishing" | "published" | "partial" | "failed" | "cancelled";
    scheduledFor?: string | null;
    publishedAt?: string | null;
    platforms: ZernioPlatformResult[];
  };
  message?: string;
  error?: string;
  warnings?: string[];
}

export interface CreatePostInput {
  title?: string;
  content: string;
  mediaItems: Array<{ type: "video" | "image" | "gif" | "document"; url: string; thumbnail?: string }>;
  platforms: Array<{
    platform: string;
    accountId: string;
    customContent?: string;
    platformSpecificData?: Record<string, any>;
  }>;
  tags?: string[];
  tiktokSettings?: Record<string, any>;
  publishNow?: boolean;
  scheduledFor?: string;
  timezone?: string;
  metadata?: Record<string, any>;
}

export interface ZernioAdapter {
  readonly isMock: boolean;
  listAccounts(apiKey: string, keyId?: string): Promise<{ ok: boolean; accounts: ZernioAccountInfo[]; error?: string }>;
  uploadVideoFromStream(
    apiKey: string,
    filename: string,
    sizeBytes: number,
    stream: ReadableStream<Uint8Array> | ArrayBuffer
  ): Promise<string>;
  createPost(apiKey: string, payload: CreatePostInput, idempotencyKey: string): Promise<ZernioPostResponse>;
  getPost(apiKey: string, postId: string): Promise<ZernioPostResponse>;
  retryPost(apiKey: string, postId: string): Promise<ZernioPostResponse>;
}

const ZERNIO_BASE = "https://zernio.com/api/v1";

function normalizeAccount(raw: any): ZernioAccountInfo {
  const id = String(raw?._id || raw?.id || raw?.accountId || "");
  const platform = String(raw?.platform || "unknown").toLowerCase();
  const username = String(raw?.username || raw?.handle || "");
  const displayName = String(raw?.displayName || raw?.name || username || id);
  const active =
    raw?.isActive === false ||
    raw?.status === "disconnected" ||
    raw?.status === "inactive"
      ? false
      : true;
  return {
    accountId: id,
    platform,
    accountName: displayName,
    username,
    profileId: raw?.profileId ? String(raw.profileId) : null,
    isActive: active,
    raw,
  };
}

function normalizePlatformEntry(p: any): ZernioPlatformResult {
  return {
    platform: String(p?.platform || "").toLowerCase(),
    accountId: String(p?.accountId?._id || p?.accountId || ""),
    status: (p?.status || "pending") as ZernioPlatformResult["status"],
    platformPostId: p?.platformPostId || null,
    platformPostUrl: p?.platformPostUrl || null,
    publishedAt: p?.publishedAt || null,
    errorMessage: p?.errorMessage || p?.error || null,
    errorCategory: p?.errorCategory || null,
  };
}

export class LiveZernioAdapter implements ZernioAdapter {
  readonly isMock = false;

  async listAccounts(apiKey: string): Promise<{ ok: boolean; accounts: ZernioAccountInfo[]; error?: string }> {
    const accounts: ZernioAccountInfo[] = [];
    let cursor: string | null = null;
    try {
      for (let page = 0; page < 10; page++) {
        const qs = new URLSearchParams({ excludeHidden: "true" });
        if (cursor) {
          qs.set("cursor", cursor);
          qs.set("limit", "500");
        }
        const res = await fetch(`${ZERNIO_BASE}/accounts?${qs.toString()}`, {
          headers: {
            Authorization: `Bearer ${apiKey}`,
            Accept: "application/json",
          },
        });
        const data: any = await res.json().catch(() => ({}));
        if (!res.ok) {
          return {
            ok: false,
            accounts: [],
            error: data?.error || data?.message || `Zernio HTTP ${res.status}`,
          };
        }
        const list = Array.isArray(data?.accounts) ? data.accounts : [];
        for (const a of list) accounts.push(normalizeAccount(a));
        cursor = data?.pagination?.nextCursor || null;
        if (!cursor) break;
      }
      return { ok: true, accounts };
    } catch (e: any) {
      return { ok: false, accounts: [], error: e?.message || "Network error contacting Zernio" };
    }
  }

  async uploadVideoFromStream(
    apiKey: string,
    filename: string,
    sizeBytes: number,
    stream: ReadableStream<Uint8Array> | ArrayBuffer
  ): Promise<string> {
    const presignRes = await fetch(`${ZERNIO_BASE}/media/presign`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        filename,
        contentType: "video/mp4",
        size: sizeBytes,
      }),
    });
    const presignData: any = await presignRes.json().catch(() => ({}));
    if (!presignRes.ok || !presignData?.uploadUrl || !presignData?.publicUrl) {
      throw new Error(`Zernio media presign failed (${presignRes.status}): ${presignData?.error || JSON.stringify(presignData)}`);
    }

    const putRes = await fetch(presignData.uploadUrl, {
      method: "PUT",
      headers: {
        "Content-Type": "video/mp4",
        ...(sizeBytes > 0 ? { "Content-Length": String(sizeBytes) } : {}),
      },
      body: stream,
    });
    if (!putRes.ok) {
      const txt = await putRes.text().catch(() => "");
      throw new Error(`Zernio media PUT upload failed (${putRes.status}): ${txt.slice(0, 200)}`);
    }
    return String(presignData.publicUrl);
  }

  async createPost(apiKey: string, payload: CreatePostInput, idempotencyKey: string): Promise<ZernioPostResponse> {
    const res = await fetch(`${ZERNIO_BASE}/posts`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(payload),
    });
    const data: any = await res.json().catch(() => ({}));
    if (!res.ok && res.status !== 207) {
      throw new Error(`Zernio createPost failed (${res.status}): ${data?.error || data?.message || JSON.stringify(data).slice(0, 300)}`);
    }
    const post = data?.post || {};
    return {
      httpStatus: res.status,
      post: {
        _id: String(post._id || ""),
        status: post.status || "failed",
        scheduledFor: post.scheduledFor || null,
        publishedAt: post.publishedAt || null,
        platforms: (Array.isArray(post.platforms) ? post.platforms : []).map(normalizePlatformEntry),
      },
      message: data?.message,
      error: data?.error,
      warnings: data?.warnings,
    };
  }

  async getPost(apiKey: string, postId: string): Promise<ZernioPostResponse> {
    const res = await fetch(`${ZERNIO_BASE}/posts/${encodeURIComponent(postId)}`, {
      headers: {
        Authorization: `Bearer ${apiKey}`,
        Accept: "application/json",
      },
    });
    const data: any = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(`Zernio getPost failed (${res.status}): ${data?.error || JSON.stringify(data).slice(0, 200)}`);
    }
    const post = data?.post || {};
    return {
      httpStatus: res.status,
      post: {
        _id: String(post._id || postId),
        status: post.status || "publishing",
        scheduledFor: post.scheduledFor || null,
        publishedAt: post.publishedAt || null,
        platforms: (Array.isArray(post.platforms) ? post.platforms : []).map(normalizePlatformEntry),
      },
    };
  }

  async retryPost(apiKey: string, postId: string): Promise<ZernioPostResponse> {
    const res = await fetch(`${ZERNIO_BASE}/posts/${encodeURIComponent(postId)}/retry`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        Accept: "application/json",
      },
    });
    const data: any = await res.json().catch(() => ({}));
    if (!res.ok && res.status !== 207) {
      throw new Error(`Zernio retryPost failed (${res.status}): ${data?.error || JSON.stringify(data).slice(0, 200)}`);
    }
    const post = data?.post || {};
    return {
      httpStatus: res.status,
      post: {
        _id: String(post._id || postId),
        status: post.status || "failed",
        scheduledFor: post.scheduledFor || null,
        publishedAt: post.publishedAt || null,
        platforms: (Array.isArray(post.platforms) ? post.platforms : []).map(normalizePlatformEntry),
      },
      message: data?.message,
      error: data?.error,
    };
  }
}

export class MockZernioAdapter implements ZernioAdapter {
  readonly isMock = true;
  constructor(private db: D1Database) {}

  async listAccounts(_apiKey: string, keyId = "mock_key"): Promise<{ ok: boolean; accounts: ZernioAccountInfo[]; error?: string }> {
    if (_apiKey.includes("invalid")) {
      return { ok: false, accounts: [], error: "401 invalid_credentials: API key is invalid or revoked (mock)" };
    }
    const suffix = keyId.slice(-4) || "0001";
    const platforms = [
      { platform: "tiktok", name: "StoryForge TikTok", handle: `@storyforge_tt_${suffix}` },
      { platform: "youtube", name: "StoryForge Shorts", handle: `@storyforge_yt_${suffix}` },
      { platform: "instagram", name: "StoryForge Reels", handle: `@storyforge_ig_${suffix}` },
      { platform: "facebook", name: "StoryForge Page", handle: `StoryForge FB ${suffix}` },
      { platform: "threads", name: "StoryForge Threads", handle: `@storyforge_th_${suffix}` },
      { platform: "twitter", name: "StoryForge X", handle: `@storyforge_x_${suffix}` },
    ];
    return {
      ok: true,
      accounts: platforms.map((p) => ({
        accountId: `mock_${p.platform}_${suffix}`,
        platform: p.platform,
        accountName: p.name,
        username: p.handle,
        profileId: `mock_prof_${suffix}`,
        isActive: true,
        raw: { mock: true },
      })),
    };
  }

  async uploadVideoFromStream(
    _apiKey: string,
    filename: string,
    _sizeBytes: number,
    _stream: ReadableStream<Uint8Array> | ArrayBuffer
  ): Promise<string> {
    // Drain stream if ReadableStream so fetch body completes cleanly
    if (_stream && typeof (_stream as any).cancel === "function") {
      try { await (_stream as ReadableStream<Uint8Array>).cancel(); } catch { /* ignore */ }
    }
    return `https://mock-media.zernio.local/temp/${Date.now()}_${encodeURIComponent(filename)}`;
  }

  async createPost(_apiKey: string, payload: CreatePostInput, _idempotencyKey: string): Promise<ZernioPostResponse> {
    const postId = `mock_post_${crypto.randomUUID().slice(0, 12)}`;
    const nowIso = new Date().toISOString();
    const failCfgRaw = await getRuntimeState(this.db, undefined, "mock_behavior", "{}");
    let failCfg: { failPlatforms?: string[]; delayPlatforms?: string[] } = {};
    try { failCfg = JSON.parse(failCfgRaw); } catch { /* ignore */ }
    const failPlats = new Set((failCfg.failPlatforms || []).map((s) => s.toLowerCase()));
    const delayPlats = new Set((failCfg.delayPlatforms || []).map((s) => s.toLowerCase()));

    const platforms: ZernioPlatformResult[] = payload.platforms.map((entry) => {
      const plat = entry.platform.toLowerCase();
      if (failPlats.has(plat)) {
        return {
          platform: plat,
          accountId: entry.accountId,
          status: "failed",
          errorMessage: `Mock failure simulated for ${plat} (${entry.accountId})`,
          errorCategory: "platform_rejected",
        };
      }
      if (delayPlats.has(plat)) {
        return {
          platform: plat,
          accountId: entry.accountId,
          status: "uploading",
          platformPostId: null,
          platformPostUrl: null,
          publishedAt: null,
        };
      }
      return {
        platform: plat,
        accountId: entry.accountId,
        status: "published",
        platformPostId: `mock_${plat}_${postId.slice(-6)}`,
        platformPostUrl: `https://mock.social/${plat}/post/${postId}`,
        publishedAt: nowIso,
      };
    });

    const anyFail = platforms.some((p) => p.status === "failed");
    const anyPub = platforms.some((p) => p.status === "published");
    const anyPend = platforms.some((p) => p.status === "uploading" || p.status === "processing");
    const postStatus = anyPend ? "publishing" : anyFail && anyPub ? "partial" : anyFail ? "failed" : "published";

    const stored = {
      _id: postId,
      status: postStatus,
      publishedAt: postStatus === "published" ? nowIso : null,
      platforms,
      pollCount: 0,
    };
    await setRuntimeState(this.db, `mock_post:${postId}`, JSON.stringify(stored));

    return {
      httpStatus: anyFail ? 207 : 201,
      post: stored as ZernioPostResponse["post"],
      ...(anyFail ? { message: "Post created with partial/failed mock targets" } : {}),
    };
  }

  async getPost(_apiKey: string, postId: string): Promise<ZernioPostResponse> {
    const raw = await getRuntimeState(this.db, undefined, `mock_post:${postId}`, "");
    if (!raw) throw new Error(`Mock post ${postId} not found`);
    const stored = JSON.parse(raw);
    stored.pollCount = (stored.pollCount || 0) + 1;
    const nowIso = new Date().toISOString();
    let changed = false;
    for (const p of stored.platforms as ZernioPlatformResult[]) {
      if (p.status === "uploading" || p.status === "processing") {
        p.status = "published";
        p.platformPostId = `mock_${p.platform}_${postId.slice(-6)}`;
        p.platformPostUrl = `https://mock.social/${p.platform}/post/${postId}`;
        p.publishedAt = nowIso;
        changed = true;
      }
    }
    if (changed) {
      const anyFail = stored.platforms.some((p: any) => p.status === "failed");
      const anyPub = stored.platforms.some((p: any) => p.status === "published");
      stored.status = anyFail && anyPub ? "partial" : anyFail ? "failed" : "published";
      if (stored.status === "published") stored.publishedAt = nowIso;
    }
    await setRuntimeState(this.db, `mock_post:${postId}`, JSON.stringify(stored));
    return { httpStatus: 200, post: stored };
  }

  async retryPost(_apiKey: string, postId: string): Promise<ZernioPostResponse> {
    const raw = await getRuntimeState(this.db, undefined, `mock_post:${postId}`, "");
    if (!raw) throw new Error(`Mock post ${postId} not found`);
    const stored = JSON.parse(raw);
    const nowIso = new Date().toISOString();
    for (const p of stored.platforms as ZernioPlatformResult[]) {
      if (p.status === "failed") {
        p.status = "published";
        p.platformPostId = `mock_${p.platform}_${postId.slice(-6)}`;
        p.platformPostUrl = `https://mock.social/${p.platform}/post/${postId}`;
        p.publishedAt = nowIso;
        p.errorMessage = null;
      }
    }
    stored.status = "published";
    stored.publishedAt = nowIso;
    await setRuntimeState(this.db, `mock_post:${postId}`, JSON.stringify(stored));
    return { httpStatus: 200, post: stored, message: "Mock retry succeeded" };
  }
}

export function getZernioAdapter(db: D1Database, useMock: boolean): ZernioAdapter {
  return useMock ? new MockZernioAdapter(db) : new LiveZernioAdapter();
}
