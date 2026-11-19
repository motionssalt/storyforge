export interface PartMeta {
  part_number: number;
  format?: string | null;
  title: string;
  description: string;
  hashtags: string[];
  attribution?: string | null;
  duration_sec: number;
  size_bytes: number;
}

export interface PlatformBuildResult {
  compatible: boolean;
  skipReason?: string;
  platformEntry?: {
    platform: string;
    accountId: string;
    customContent: string;
    platformSpecificData: Record<string, any>;
  };
  youtubeTags?: string[];
  tiktokSettings?: Record<string, any>;
}

const MB = 1024 * 1024;
const GB = 1024 * MB;

function cleanHashtags(hashtags: string[], maxCount: number): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of hashtags || []) {
    const clean = "#" + String(raw || "").replace(/^#+/, "").replace(/[^A-Za-z0-9_]/g, "");
    if (clean.length > 1 && !seen.has(clean.toLowerCase())) {
      seen.add(clean.toLowerCase());
      out.push(clean);
      if (out.length >= maxCount) break;
    }
  }
  return out;
}

function truncateText(text: string, maxLen: number): string {
  const s = String(text || "").trim();
  if (s.length <= maxLen) return s;
  if (maxLen <= 3) return s.slice(0, maxLen);
  return s.slice(0, maxLen - 1).trimEnd() + "…";
}

function twitterWeightedLength(text: string): number {
  // Approximate X's character counting: emojis / non-BMP count as 2, URLs as 23
  const urlRe = /https?:\/\/\S+/g;
  let len = 0;
  let lastIdx = 0;
  for (const m of text.matchAll(urlRe)) {
    const idx = m.index ?? 0;
    const chunk = text.slice(lastIdx, idx);
    for (const ch of chunk) {
      len += ch.codePointAt(0)! > 0xffff ? 2 : 1;
    }
    len += 23;
    lastIdx = idx + m[0].length;
  }
  for (const ch of text.slice(lastIdx)) {
    len += ch.codePointAt(0)! > 0xffff ? 2 : 1;
  }
  return len;
}

function trimToTwitterLimit(text: string, limit = 278): string {
  if (twitterWeightedLength(text) <= limit) return text;
  const chars = Array.from(text);
  let out = "";
  for (const ch of chars) {
    const next = out + ch;
    if (twitterWeightedLength(next + "…") > limit) break;
    out = next;
  }
  return out.trimEnd() + "…";
}

export function ensurePartTitle(title: string, partNumber: number, format?: string | null): string {
  const clean = String(title || "").trim() || `Part ${partNumber}`;
  const hasPartMention = new RegExp(`\\bpart\\s*${partNumber}\\b`, "i").test(clean);
  if (hasPartMention) return clean;
  if (format === "two_part" || partNumber > 1) {
    return `[PART ${partNumber}] ${clean}`;
  }
  return clean;
}

export function buildYouTubeTags(hashtags: string[]): string[] {
  const out: string[] = [];
  let totalChars = 0;
  const seen = new Set<string>();
  for (const raw of hashtags || []) {
    const tag = String(raw || "").replace(/^#+/, "").trim().slice(0, 100);
    if (!tag || seen.has(tag.toLowerCase())) continue;
    if (totalChars + tag.length + (out.length > 0 ? 1 : 0) > 480) break;
    seen.add(tag.toLowerCase());
    out.push(tag);
    totalChars += tag.length + (out.length > 1 ? 1 : 0);
  }
  return out;
}

export function buildPlatformTarget(platformRaw: string, accountId: string, meta: PartMeta): PlatformBuildResult {
  const platform = String(platformRaw || "").toLowerCase();
  const dur = Number(meta.duration_sec || 0);
  const bytes = Number(meta.size_bytes || 0);
  const titleWithPart = ensurePartTitle(meta.title, meta.part_number, meta.format);
  const desc = String(meta.description || "").trim();
  const attr = meta.attribution ? `\n\n${meta.attribution.trim()}` : "";

  if (platform === "tiktok") {
    if (dur < 3) return { compatible: false, skipReason: `Video too short for TikTok (${dur.toFixed(1)}s < 3s min)` };
    if (dur > 600) return { compatible: false, skipReason: `Video too long for TikTok (${dur.toFixed(0)}s > 600s max)` };
    if (bytes > 4 * GB) return { compatible: false, skipReason: `File exceeds TikTok 4 GB limit` };

    const tags = cleanHashtags(meta.hashtags, 8).join(" ");
    const full = [titleWithPart, desc + attr, tags].filter(Boolean).join("\n\n");
    const tiktokSettings = {
      privacy_level: "PUBLIC_TO_EVERYONE",
      allow_comment: true,
      allow_duet: true,
      allow_stitch: true,
      content_preview_confirmed: true,
      express_consent_given: true,
      video_cover_timestamp_ms: 2000,
    };
    return {
      compatible: true,
      tiktokSettings,
      platformEntry: {
        platform: "tiktok",
        accountId,
        customContent: truncateText(full, 2200),
        platformSpecificData: { tiktokSettings },
      },
    };
  }

  if (platform === "youtube") {
    if (dur < 1) return { compatible: false, skipReason: `Video too short for YouTube (${dur.toFixed(1)}s < 1s min)` };
    if (bytes > 256 * GB) return { compatible: false, skipReason: `File exceeds YouTube 256 GB limit` };

    const tags = cleanHashtags(meta.hashtags, 12).join(" ");
    const ytTitle = truncateText(titleWithPart, 100);
    const ytDesc = truncateText([`Part ${meta.part_number}: ${titleWithPart}`, desc + attr, tags].filter(Boolean).join("\n\n"), 5000);
    return {
      compatible: true,
      youtubeTags: buildYouTubeTags(meta.hashtags),
      platformEntry: {
        platform: "youtube",
        accountId,
        customContent: ytDesc,
        platformSpecificData: {
          title: ytTitle,
          visibility: "public",
          madeForKids: false,
          categoryId: "24",
        },
      },
    };
  }

  if (platform === "instagram") {
    if (dur < 3) return { compatible: false, skipReason: `Video too short for Instagram Reels (${dur.toFixed(1)}s < 3s min)` };
    if (dur > 900) return { compatible: false, skipReason: `Video too long for Instagram Reels (${dur.toFixed(0)}s > 900s max)` };
    if (bytes > 300 * MB) return { compatible: false, skipReason: `File exceeds Instagram Reels 300 MB limit (${(bytes / MB).toFixed(1)} MB)` };

    const tags = cleanHashtags(meta.hashtags, 12).join(" ");
    const full = [titleWithPart, desc + attr, tags].filter(Boolean).join("\n\n");
    return {
      compatible: true,
      platformEntry: {
        platform: "instagram",
        accountId,
        customContent: truncateText(full, 2200),
        platformSpecificData: {
          shareToFeed: true,
          thumbOffset: 2000,
        },
      },
    };
  }

  if (platform === "facebook") {
    if (dur < 1) return { compatible: false, skipReason: `Video too short for Facebook (${dur.toFixed(1)}s < 1s min)` };
    if (dur > 240 * 60) return { compatible: false, skipReason: `Video exceeds Facebook 240-minute limit` };
    if (bytes > 4 * GB) return { compatible: false, skipReason: `File exceeds Facebook 4 GB limit` };

    const tags = cleanHashtags(meta.hashtags, 8).join(" ");
    const full = [titleWithPart, desc + attr, tags].filter(Boolean).join("\n\n");
    // Per Zernio docs: Facebook Reels (contentType: "reel") cap at 60s; >60s publishes as standard vertical Feed video (max 240m)
    const useReel = dur >= 3 && dur <= 60;
    return {
      compatible: true,
      platformEntry: {
        platform: "facebook",
        accountId,
        customContent: truncateText(full, 5000),
        platformSpecificData: useReel
          ? { contentType: "reel", title: truncateText(titleWithPart, 255) }
          : {},
      },
    };
  }

  if (platform === "threads") {
    if (dur > 300) return { compatible: false, skipReason: `Video too long for Threads (${dur.toFixed(0)}s > 300s / 5m max)` };
    if (bytes > 1 * GB) return { compatible: false, skipReason: `File exceeds Threads 1 GB limit` };

    const tags = cleanHashtags(meta.hashtags, 3).join(" ");
    let text = `${titleWithPart}\n\n${desc}`;
    if (tags) text += `\n\n${tags}`;
    return {
      compatible: true,
      platformEntry: {
        platform: "threads",
        accountId,
        customContent: truncateText(text, 500),
        platformSpecificData: {},
      },
    };
  }

  if (platform === "twitter") {
    if (dur < 0.5) return { compatible: false, skipReason: `Video too short for X (${dur.toFixed(1)}s < 0.5s min)` };
    if (bytes > 512 * MB) return { compatible: false, skipReason: `File exceeds X 512 MB limit (${(bytes / MB).toFixed(1)} MB)` };

    const tags = cleanHashtags(meta.hashtags, 2).join(" ");
    const candidate = tags ? `${titleWithPart}\n\n${tags}` : titleWithPart;
    const fitted =
      twitterWeightedLength(candidate) <= 278
        ? candidate
        : trimToTwitterLimit(titleWithPart, 278);
    return {
      compatible: true,
      platformEntry: {
        platform: "twitter",
        accountId,
        customContent: fitted,
        platformSpecificData: {},
      },
    };
  }

  // Generic fallback for any other platform connected on Zernio
  const tags = cleanHashtags(meta.hashtags, 5).join(" ");
  const full = [titleWithPart, desc + attr, tags].filter(Boolean).join("\n\n");
  return {
    compatible: true,
    platformEntry: {
      platform: platform,
      accountId,
      customContent: truncateText(full, 2000),
      platformSpecificData: {},
    },
  };
}
