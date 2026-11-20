"""
Reddit-style card renderer for StoryForge.
Supports:
  - SF-003: Persistent auto-scrolling Reddit post card (header + title + narration body)
  - Legacy card types (comment, reply, branded) preserved for future reuse
  - SF-002: Username-seeded cartoon profile pictures fetched once from DiceBear,
    cached to disk as PNG, embedded as data URIs, with deterministic bundled
    avatar pool fallback and letter-circle last resort.
"""

from __future__ import annotations

import base64
import hashlib
import json
import io
import math
import os
import re
import urllib.parse
import urllib.request
from typing import Any


MARKER_PATTERN = re.compile(
    r"\[(pause|sigh|laughter|laugh|confirmation-en|question-en|question-ah|"
    r"question-oh|question-ei|question-yi|surprise-ah|surprise-oh|surprise-wa|"
    r"surprise-yo|dissatisfaction-hnn)\]",
    re.IGNORECASE,
)


def strip_emotion_markers(text: str) -> str:
    cleaned = MARKER_PATTERN.sub("", str(text or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def normalize_avatar_seed(username: str | None) -> str:
    raw = str(username or "").strip()
    cleaned = re.sub(r"^(u/|r/|@)", "", raw, flags=re.IGNORECASE).strip().lower()
    return cleaned or "storyteller"


def _load_fallback_avatar_pool(avatars_dir: str) -> list[str]:
    if not os.path.isdir(avatars_dir):
        return []
    files = sorted(
        os.path.join(avatars_dir, fn)
        for fn in os.listdir(avatars_dir)
        if fn.lower().endswith((".svg", ".png", ".jpg", ".webp"))
    )
    preferred: list[str] = []
    all_urls: list[str] = []
    for p in files:
        raw = open(p, "rb").read()
        mime = "image/svg+xml" if p.lower().endswith(".svg") else "image/png"
        b64 = base64.b64encode(raw).decode("ascii")
        uri = f"data:{mime};base64,{b64}"
        all_urls.append(uri)
        # Prefer actual character avatars over simple emoji blobs
        if b"Fun Emoji Set" not in raw[:1200]:
            preferred.append(uri)
    return preferred if preferred else all_urls


def resolve_avatar_data_uri(
    username: str | None,
    cache_dir: str | None,
    avatars_dir: str,
    avatar: dict[str, Any] | None = None,
) -> str:
    """
    Resolve a deterministic cartoon profile picture for `username`:
      1. Reuse cached PNG on disk if already fetched in this job.
      2. Fetch once from DiceBear (avataaars cartoon style, seeded by username) and save to disk.
      3. Fall back to bundled character avatar pool hashed by username.
      4. Return "" to fall back to HTML letter-circle.
    """
    seed = normalize_avatar_seed(username)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"avatar_{digest[:16]}.png")
        if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 100:
            raw = open(cache_path, "rb").read()
            return f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"
    else:
        cache_path = None

    def remember(data: bytes) -> str:
        if cache_path:
            with open(cache_path, "wb") as f:
                f.write(data)
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")

    avatar = avatar or {}
    style = avatar.get("style", "cartoon")
    if style != "cartoon":
        from PIL import Image, ImageOps
        pool_dir = os.path.join(os.path.dirname(avatars_dir), "photo_avatars")
        try:
            with open(os.path.join(pool_dir, "manifest.json"), encoding="utf-8") as f:
                rows = [row for row in json.load(f) if row["style"] == style]
            hint = str(avatar.get("keyword", "")).lower().strip() if style != "photo_person" else avatar.get("gender")
            hinted = [row for row in rows if row["hint"] == hint]
            rows = hinted or rows
            row = rows[int(digest, 16) % len(rows)]
            # Pin the asset, not merely the source. A provider outage must not change the poster.
            sources = [row.get("thumb"), row.get("url"), os.path.join(pool_dir, row["file"])]
            if style == "photo_scenery":
                sources.append(f"https://picsum.photos/seed/{digest[:16]}/256/256")
            for source in sources:
                if not source:
                    continue
                try:
                    if source.startswith(("https://thumb.wikimedia.org/", "https://upload.wikimedia.org/", "https://picsum.photos/")):
                        req = urllib.request.Request(source, headers={"User-Agent": "StoryForge-Renderer/4.0 (github.com/motionssalt/storyforge)"})
                        with urllib.request.urlopen(req, timeout=4) as response:
                            data = response.read(12_000_001)
                        if len(data) > 12_000_000:
                            continue
                        source = io.BytesIO(data)
                    with Image.open(source) as img:
                        out = io.BytesIO()
                        ImageOps.fit(img.convert("RGB"), (256, 256)).save(out, "PNG")
                        return remember(out.getvalue())
                except (OSError, ValueError, Image.DecompressionBombError):
                    continue
        except (OSError, KeyError, ValueError, IndexError):
            pass
        print("[avatar] No usable curated photo; using cartoon fallback.")

    url = (
        "https://api.dicebear.com/9.x/avataaars/png"
        f"?seed={urllib.parse.quote(seed)}&size=256"
        "&backgroundColor=b6e3f4,c0aede,d1d4f9,ffd5dc,ffdfbf"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "StoryForge-Renderer/2.0", "Accept": "image/png"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        if resp.status == 200 and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 100:
            if cache_path:
                with open(cache_path, "wb") as f:
                    f.write(data)
            return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
    except Exception as exc:
        print(f"[avatar] External DiceBear fetch failed for '{seed}', using fallback pool: {exc}")

    pool = _load_fallback_avatar_pool(avatars_dir)
    if pool:
        idx = int(digest, 16) % len(pool)
        return pool[idx]
    return ""


CARD_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      width: 1080px;
      background: transparent !important;
      overflow: hidden;
    }
    :root {
      --video-width: 1080px;
      --card-width: 810px;
      --scale-factor: 1;
      --accent-verified: #1d9bf0;
      --accent-nsfw-bg: #ff2e93;
      --accent-nsfw-text: #ffffff;
      --accent-upvote: #ff4500;
      --accent-downvote: #7193ff;
      --accent-award-gold: #ffb000;
      --accent-award-blue: #24a0ed;
      --accent-award-pink: #ff585b;
      --accent-heart: #f91880;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    .card-theme-light {
      --card-bg: #ffffff;
      --card-border: #e5e7eb;
      --card-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
      --text-primary: #0f1a1c;
      --text-body: #1a282d;
      --text-meta: #576f76;
      --text-username: #0045ac;
      --pill-bg: #f2f4f5;
      --pill-border: #e2e7e9;
      --thread-line: #d6d9dc;
      --icon-stroke: #1a282d;
    }
    .card-theme-dark {
      --card-bg: #161618;
      --card-border: #2c2c30;
      --card-shadow: 0 28px 64px rgba(0, 0, 0, 0.65);
      --text-primary: #f2f4f5;
      --text-body: #d7dadc;
      --text-meta: #818384;
      --text-username: #4fbcff;
      --pill-bg: #242528;
      --pill-border: #323338;
      --thread-line: #343536;
      --icon-stroke: #d7dadc;
    }
    #stage {
      width: 1080px;
      padding: 80px 135px 160px 135px;
      background: transparent;
      display: inline-block;
    }
    .r-card {
      width: var(--card-width);
      background-color: var(--card-bg);
      border: 1.5px solid var(--card-border);
      box-shadow: var(--card-shadow);
      border-radius: 24px;
      color: var(--text-primary);
      font-family: var(--font-sans);
      overflow: hidden;
    }
    .r-avatar-wrap {
      position: relative;
      flex-shrink: 0;
      border-radius: 50%;
      overflow: hidden;
      display: flex;
      align-items: center;
      justify-content: center;
      background-color: #3b82f6;
      color: #ffffff;
      font-weight: 700;
      user-select: none;
    }
    .r-avatar-wrap img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .r-avatar-fallback {
      width: 100%;
      height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 24px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .r-verified {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      color: var(--accent-verified);
      flex-shrink: 0;
      vertical-align: middle;
    }
    .r-awards-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      margin-top: 14px;
    }
    .r-award-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background-color: var(--pill-bg);
      border: 1px solid var(--pill-border);
      border-radius: 999px;
      padding: 5px 12px 5px 8px;
      font-size: 21px;
      font-weight: 600;
      color: var(--text-meta);
      line-height: 1;
    }
    .r-award-chip svg { width: 24px; height: 24px; flex-shrink: 0; }
    .r-nsfw-row {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-top: 14px;
    }
    .r-nsfw-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background-color: var(--accent-nsfw-bg);
      color: var(--accent-nsfw-text);
      font-size: 20px;
      font-weight: 800;
      letter-spacing: 0.04em;
      padding: 5px 12px;
      border-radius: 8px;
      line-height: 1;
    }
    .r-nsfw-18 {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: rgba(0, 0, 0, 0.22);
      border-radius: 4px;
      padding: 2px 5px;
      font-size: 17px;
      font-weight: 900;
    }
    .r-card--post { padding: 32px 36px 28px 36px; }
    .r-post-header { display: flex; align-items: center; gap: 16px; }
    .r-sub-icon { width: 52px; height: 52px; }
    .r-post-meta { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .r-sub-name { font-size: 25px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }
    .r-author-line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 24px; line-height: 1.2; }
    .r-username-blue { color: var(--text-username); font-weight: 600; word-break: break-all; }
    .r-dot-age { color: var(--text-meta); font-weight: 400; }
    .r-post-title {
      margin-top: 18px;
      font-size: 42px;
      font-weight: 700;
      line-height: 1.24;
      letter-spacing: -0.015em;
      color: var(--text-primary);
    }
    .r-post-footer { margin-top: 26px; display: flex; align-items: center; flex-wrap: wrap; gap: 14px; }
    .r-action-pill {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      background-color: var(--pill-bg);
      border-radius: 999px;
      padding: 11px 18px;
      font-size: 24px;
      font-weight: 700;
      color: var(--text-primary);
      line-height: 1;
    }
    .r-action-pill svg { width: 28px; height: 28px; flex-shrink: 0; }
    .r-vote-score { color: var(--accent-upvote); }
    .r-vote-divider { width: 1.5px; height: 22px; background-color: var(--card-border); margin: 0 2px; }

    .r-card--comment { padding: 28px 34px; }
    .r-comment-head { display: flex; align-items: center; gap: 16px; }
    .r-comment-avatar { width: 54px; height: 54px; }
    .r-comment-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
    .r-comment-user { font-size: 25px; font-weight: 700; color: var(--text-primary); word-break: break-all; }
    .r-comment-age { font-size: 24px; color: var(--text-meta); font-weight: 400; }
    .r-comment-body {
      margin-top: 16px;
      font-size: 31px;
      line-height: 1.44;
      font-weight: 400;
      color: var(--text-body);
      letter-spacing: -0.005em;
    }
    .r-comment-body p + p { margin-top: 20px; }
    .r-comment-body ul { margin-top: 16px; padding-left: 34px; display: flex; flex-direction: column; gap: 12px; }
    .r-comment-body li { padding-left: 6px; }
    .r-comment-footer {
      margin-top: 22px;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 22px;
      color: var(--text-meta);
      font-size: 23px;
      font-weight: 700;
    }
    .r-comment-action { display: inline-flex; align-items: center; gap: 8px; }
    .r-comment-action svg { width: 26px; height: 26px; }

    .r-thread-wrapper { display: flex; flex-direction: column; gap: 0; }
    .r-thread-body-with-line { position: relative; padding-left: 70px; }
    .r-thread-line {
      position: absolute;
      left: 25px;
      top: 12px;
      bottom: 8px;
      width: 3.5px;
      background-color: var(--thread-line);
      border-radius: 4px;
    }
    .r-reply-block { margin-top: 24px; padding-top: 22px; border-top: 1px solid var(--card-border); }

    .r-card--branded { border-radius: 36px; padding: 36px 40px 32px 40px; }
    .r-branded-header { display: flex; align-items: center; gap: 18px; }
    .r-branded-avatar { width: 68px; height: 68px; }
    .r-branded-identity { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .r-branded-handle-row { display: flex; align-items: center; gap: 8px; }
    .r-branded-handle { font-size: 29px; font-weight: 800; color: var(--text-primary); letter-spacing: -0.01em; }
    .r-branded-emojis { display: flex; align-items: center; gap: 8px; font-size: 30px; line-height: 1.1; margin-top: 4px; }
    .r-branded-headline {
      margin-top: 22px;
      font-size: 44px;
      font-weight: 800;
      line-height: 1.25;
      letter-spacing: -0.02em;
      color: var(--text-primary);
    }
    .r-branded-footer {
      margin-top: 28px;
      display: flex;
      align-items: center;
      gap: 28px;
      color: var(--text-meta);
      font-size: 25px;
      font-weight: 600;
    }
    .r-branded-stat { display: inline-flex; align-items: center; gap: 9px; }
    .r-branded-stat svg { width: 28px; height: 28px; }
  </style>
</head>
<body>
  <div id="stage"></div>
  <script>
    const ICONS = {
      verified: `<span class="r-verified" title="Verified"><svg viewBox="0 0 24 24" width="28" height="28" fill="currentColor" aria-hidden="true"><path d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.91-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91c-1.31.67-2.2 1.91-2.2 3.34s.89 2.67 2.2 3.34c-.46 1.39-.21 2.9.8 3.91s2.52 1.26 3.91.81c.67 1.31 1.91 2.19 3.34 2.19s2.68-.88 3.34-2.19c1.39.45 2.9.2 3.91-.81s1.27-2.52.81-3.91c1.31-.67 2.19-1.91 2.19-3.34zm-11.71 4.2L6.8 12.46l1.41-1.42 2.26 2.26 4.8-5.23 1.47 1.36-6.2 6.77z"/></svg></span>`,
      upvote: `<svg viewBox="0 0 24 24" fill="#FF4500" aria-hidden="true"><path d="M12 3.2L3.5 12.5h5.2V20h6.6v-7.5h5.2L12 3.2z"/></svg>`,
      downvote: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M12 20.8l8.5-9.3h-5.2V4H8.7v7.5H3.5l8.5 9.3z"/></svg>`,
      comment: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>`,
      share: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>`,
      awardOutline: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="6"/><path d="M15.477 12.89L17 22l-5-3-5 3 1.523-9.11"/></svg>`,
      awardGold: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="#FFB000"/><polygon points="12,5.5 13.9,9.6 18.4,10.2 15.1,13.3 15.9,17.8 12,15.6 8.1,17.8 8.9,13.3 5.6,10.2 10.1,9.6" fill="#FFF8DC"/></svg>`,
      awardHelpful: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="#24A0ED"/><path d="M9.5 12.5l2 2 4-4.5" stroke="#FFFFFF" stroke-width="2.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
      awardHeart: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="#FF585B"/><path d="M12 16.8s-4.8-3.1-4.8-6.4A2.7 2.7 0 0 1 12 8.7a2.7 2.7 0 0 1 4.8 1.7c0 3.3-4.8 6.4-4.8 6.4z" fill="#FFFFFF"/></svg>`,
      heartOutline: `<svg viewBox="0 0 24 24" fill="#F91880" aria-hidden="true"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>`,
      brandedShare: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 2L11 13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`
    };

    function getAvatarColor(seedStr) {
      const palette = ["#2563EB", "#D97706", "#059669", "#7C3AED", "#DB2777", "#DC2626", "#0891B2"];
      let hash = 0;
      const str = seedStr || "user";
      for (let i = 0; i < str.length; i++) {
        hash = str.charCodeAt(i) + ((hash << 5) - hash);
      }
      return palette[Math.abs(hash) % palette.length];
    }

    function esc(str) {
      if (str === null || str === undefined) return "";
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function renderAvatar(url, name, sizeClass) {
      const cleanName = (name || "?").replace(/^(r\/|u\/|@)/i, "");
      const initial = cleanName.charAt(0).toUpperCase() || "?";
      const bg = getAvatarColor(cleanName);
      if (!url) {
        return `<div class="r-avatar-wrap ${sizeClass}" style="background-color:${bg};"><span class="r-avatar-fallback">${esc(initial)}</span></div>`;
      }
      return `<div class="r-avatar-wrap ${sizeClass}" style="background-color:${bg};"><img src="${esc(url)}" alt="${esc(cleanName)}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';" /><span class="r-avatar-fallback" style="display:none;">${esc(initial)}</span></div>`;
    }

    function formatCount(value) {
      const match = String(value ?? '').toLowerCase().match(/^([0-9.]+)([kmb]?)$/);
      if (!match) return '';
      const n = Number(match[1]) * ({k:1e3,m:1e6,b:1e9}[match[2]] ?? 1);
      const [unit, suffix] = [[1e9,'B'],[1e6,'M'],[1e3,'K'],[1,'']].find(([u]) => n >= u) ?? [1,''];
      return (unit === 1 ? Math.round(n) : Number((n / unit).toFixed(1))) + suffix;
    }

    function renderAwards(awards) {
      if (!Array.isArray(awards)) awards = Number.parseFloat(awards) > 0 ? [{type:'gold', count:awards}] : [];
      if (!awards || awards.length === 0) return "";
      const itemsHtml = awards.map((aw) => {
        let iconSvg = ICONS.awardGold;
        if (aw.type === "helpful") iconSvg = ICONS.awardHelpful;
        if (aw.type === "heart") iconSvg = ICONS.awardHeart;
        const label = aw.label ? esc(aw.label) : (aw.count != null ? esc(formatCount(aw.count)) : "");
        return `<span class="r-award-chip">${iconSvg}<span>${label}</span></span>`;
      }).join("");
      return `<div class="r-awards-row">${itemsHtml}</div>`;
    }

    function renderRichBody(body) {
      if (!body) return "";
      if (Array.isArray(body)) {
        return body.map((block) => {
          if (typeof block === "object" && Array.isArray(block.bullets)) {
            const lis = block.bullets.map((b) => `<li>${esc(b)}</li>`).join("");
            return `<ul>${lis}</ul>`;
          }
          return `<p>${esc(block)}</p>`;
        }).join("");
      }
      return String(body).split(/\n\n+/).map((p) => `<p>${esc(p).replace(/\n/g, "<br/>")}</p>`).join("");
    }

    function renderCard(data) {
      const themeClass = data.theme === "light" ? "card-theme-light" : "card-theme-dark";
      const type = data.type || "post";

      if (type === "post") {
        const verifiedHtml = data.verified ? ICONS.verified : "";
        const awardsHtml = renderAwards(data.awards);
        const nsfwHtml = data.nsfw ? `<div class="r-nsfw-row"><span class="r-nsfw-pill">NSFW <span class="r-nsfw-18">18+</span></span></div>` : "";
        const footerHtml = data.show_footer !== false ? `
          <div class="r-post-footer">
            <div class="r-action-pill">${ICONS.upvote}<span class="r-vote-score">${esc(data.upvotes || "14.2K")}</span><span class="r-vote-divider"></span>${ICONS.downvote}</div>
            <div class="r-action-pill">${ICONS.comment}<span>${esc(data.comments_count || "2.8K")}</span></div>
            <div class="r-action-pill">${ICONS.share}<span>Share</span></div>
            <div class="r-action-pill">${ICONS.awardOutline}</div>
          </div>` : "";
        const avatarSrc = data.avatar_url || data.subreddit_icon_url;
        const avatarName = data.username || data.subreddit || "storyteller";
        return `
          <article class="r-card r-card--post ${themeClass}">
            <div class="r-post-header">
              ${renderAvatar(avatarSrc, avatarName, "r-sub-icon")}
              <div class="r-post-meta">
                <div class="r-sub-name">${esc(data.subreddit || "r/AskReddit")}</div>
                <div class="r-author-line">
                  <span class="r-username-blue">${esc(data.username || "u/storyteller")}</span>
                  ${verifiedHtml}
                  <span class="r-dot-age">· ${esc(data.age || "1y")}</span>
                </div>
              </div>
            </div>
            ${awardsHtml}
            ${nsfwHtml}
            <h3 class="r-post-title">${esc(data.title)}</h3>
            ${footerHtml}
          </article>`;
      }

      if (type === "comment") {
        const verifiedHtml = data.verified ? ICONS.verified : "";
        const awardsHtml = renderAwards(data.awards);
        const footerHtml = data.show_footer ? `
          <div class="r-comment-footer">
            <span class="r-comment-action">${ICONS.awardOutline} Award</span>
            <span class="r-comment-action">${ICONS.upvote} <span>${esc(data.upvotes || "9.4K")}</span></span>
          </div>` : "";
        return `
          <article class="r-card r-card--comment ${themeClass}">
            <div class="r-comment-head">
              ${renderAvatar(data.avatar_url, data.username, "r-comment-avatar")}
              <div class="r-comment-meta">
                <span class="r-comment-user">${esc(data.username)}</span>
                ${verifiedHtml}
                <span class="r-comment-age">· ${esc(data.age || "4y")}</span>
              </div>
            </div>
            ${awardsHtml}
            <div class="r-comment-body">${renderRichBody(data.body)}</div>
            ${footerHtml}
          </article>`;
      }

      if (type === "reply") {
        const verifiedParent = data.verified ? ICONS.verified : "";
        const awardsParent = renderAwards(data.awards);
        const rep = data.reply || {};
        const verifiedReply = rep.verified ? ICONS.verified : "";
        const awardsReply = renderAwards(rep.awards);
        return `
          <article class="r-card r-card--comment ${themeClass}">
            <div class="r-thread-wrapper">
              <div class="r-comment-head">
                ${renderAvatar(data.avatar_url, data.username, "r-comment-avatar")}
                <div class="r-comment-meta">
                  <span class="r-comment-user">${esc(data.username)}</span>
                  ${verifiedParent}
                  <span class="r-comment-age">· ${esc(data.age || "4y")}</span>
                </div>
              </div>
              <div class="r-thread-body-with-line">
                <div class="r-thread-line" aria-hidden="true"></div>
                ${awardsParent}
                <div class="r-comment-body">${renderRichBody(data.body)}</div>
                <div class="r-reply-block">
                  <div class="r-comment-head">
                    ${renderAvatar(rep.avatar_url, rep.username, "r-comment-avatar")}
                    <div class="r-comment-meta">
                      <span class="r-comment-user">${esc(rep.username || "reply_user")}</span>
                      ${verifiedReply}
                      <span class="r-comment-age">· ${esc(rep.age || "4y")}</span>
                    </div>
                  </div>
                  ${awardsReply}
                  <div class="r-comment-body">${renderRichBody(rep.body)}</div>
                </div>
              </div>
            </div>
          </article>`;
      }

      if (type === "branded") {
        const verifiedHtml = data.verified !== false ? ICONS.verified : "";
        const emojis = Array.isArray(data.emoji_row) ? data.emoji_row.join(" ") : (data.emoji_row || "😭💀🔥");
        return `
          <article class="r-card r-card--branded ${themeClass}">
            <div class="r-branded-header">
              ${renderAvatar(data.avatar_url, data.handle, "r-branded-avatar")}
              <div class="r-branded-identity">
                <div class="r-branded-handle-row">
                  <span class="r-branded-handle">${esc(data.handle || "@YourChannelHandle")}</span>
                  ${verifiedHtml}
                </div>
                <div class="r-branded-emojis">${esc(emojis)}</div>
              </div>
            </div>
            <h3 class="r-branded-headline">${esc(data.title)}</h3>
            <div class="r-branded-footer">
              <span class="r-branded-stat">${ICONS.heartOutline}<span>${esc(data.likes || "876.4K+")}</span></span>
              <span class="r-branded-stat">${ICONS.brandedShare}<span>${esc(data.shares || "42.1K+")}</span></span>
            </div>
          </article>`;
      }
      return "";
    }

    window.setCardData = function(data) {
      document.getElementById("stage").innerHTML = renderCard(data);
    };
  </script>
</body>
</html>
"""


SCROLLING_POST_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      width: 1080px;
      background: transparent !important;
    }
    :root {
      --card-width: 860px;
      --accent-verified: #1d9bf0;
      --accent-nsfw-bg: #ff2e93;
      --accent-nsfw-text: #ffffff;
      --accent-upvote: #ff4500;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    .card-theme-light {
      --card-bg: #ffffff;
      --card-border: #e5e7eb;
      --card-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
      --text-primary: #0f1a1c;
      --text-body-dim: #4b5c63;
      --text-body-bright: #0f1a1c;
      --active-bg: rgba(15, 26, 28, 0.06);
      --active-bar: #ff4500;
      --text-meta: #576f76;
      --text-username: #0045ac;
      --pill-bg: #f2f4f5;
      --pill-border: #e2e7e9;
    }
    .card-theme-dark {
      --card-bg: #161618;
      --card-border: #2c2c30;
      --card-shadow: 0 28px 64px rgba(0, 0, 0, 0.65);
      --text-primary: #f2f4f5;
      --text-body-dim: #a1a6ad;
      --text-body-bright: #ffffff;
      --active-bg: rgba(255, 255, 255, 0.08);
      --active-bar: #ff4500;
      --text-meta: #818384;
      --text-username: #4fbcff;
      --pill-bg: #242528;
      --pill-border: #323338;
    }
    #stage {
      width: 1080px;
      padding: 80px 110px 160px 110px;
      background: transparent;
      display: inline-block;
    }
    .r-card {
      width: var(--card-width);
      background-color: var(--card-bg);
      border: 1.5px solid var(--card-border);
      box-shadow: var(--card-shadow);
      border-radius: 26px;
      color: var(--text-primary);
      font-family: var(--font-sans);
      padding: 28px 34px 24px 34px;
      display: flex;
      flex-direction: column;
    }
    .r-post-header { display: flex; align-items: center; gap: 16px; }
    .r-avatar-wrap {
      position: relative;
      flex-shrink: 0;
      width: 58px;
      height: 58px;
      border-radius: 50%;
      overflow: hidden;
      display: flex;
      align-items: center;
      justify-content: center;
      background-color: #3b82f6;
      color: #ffffff;
      font-weight: 700;
      user-select: none;
    }
    .r-avatar-wrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .r-avatar-fallback {
      width: 100%; height: 100%;
      display: flex; align-items: center; justify-content: center;
      font-size: 26px; font-weight: 800; text-transform: uppercase;
    }
    .r-post-meta { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
    .r-sub-name { font-size: 24px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }
    .r-author-line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 22px; line-height: 1.2; }
    .r-username-blue { color: var(--text-username); font-weight: 600; word-break: break-all; }
    .r-dot-age { color: var(--text-meta); font-weight: 400; }
    .r-verified {
      display: inline-flex; align-items: center; justify-content: center;
      width: 24px; height: 24px; color: var(--accent-verified); flex-shrink: 0;
    }
    .r-awards-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; }
    .r-award-chip {
      display: inline-flex; align-items: center; gap: 5px;
      background-color: var(--pill-bg); border: 1px solid var(--pill-border);
      border-radius: 999px; padding: 4px 10px 4px 7px;
      font-size: 19px; font-weight: 600; color: var(--text-meta); line-height: 1;
    }
    .r-award-chip svg { width: 20px; height: 20px; flex-shrink: 0; }
    .r-nsfw-row { display: inline-flex; align-items: center; gap: 8px; margin-top: 12px; }
    .r-nsfw-pill {
      display: inline-flex; align-items: center; gap: 6px;
      background-color: var(--accent-nsfw-bg); color: var(--accent-nsfw-text);
      font-size: 18px; font-weight: 800; letter-spacing: 0.04em;
      padding: 4px 10px; border-radius: 7px; line-height: 1;
    }
    .r-nsfw-18 {
      display: inline-flex; align-items: center; justify-content: center;
      background: rgba(0, 0, 0, 0.22); border-radius: 4px;
      padding: 2px 5px; font-size: 15px; font-weight: 900;
    }
    .r-post-title {
      margin-top: 14px;
      font-size: 35px;
      font-weight: 700;
      line-height: 1.24;
      letter-spacing: -0.015em;
      color: var(--text-primary);
    }
    .r-divider {
      height: 1.5px;
      background-color: var(--card-border);
      margin-top: 16px;
      margin-bottom: 6px;
    }
    #body-viewport {
      width: 100%;
      background-color: var(--card-bg);
      position: relative;
      overflow: hidden;
    }
    .r-post-footer { margin-top: 14px; display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
    .r-action-pill {
      display: inline-flex; align-items: center; gap: 8px;
      background-color: var(--pill-bg); border-radius: 999px;
      padding: 9px 15px; font-size: 21px; font-weight: 700;
      color: var(--text-primary); line-height: 1;
    }
    .r-action-pill svg { width: 24px; height: 24px; flex-shrink: 0; }
    .r-vote-score { color: var(--accent-upvote); }
    .r-vote-divider { width: 1.5px; height: 18px; background-color: var(--card-border); margin: 0 2px; }

    #body-tall-wrap {
      display: none;
      background: transparent;
      font-family: var(--font-sans);
    }
    #body-tall {
      padding: 18px 0 26px 0;
      display: flex;
      flex-direction: column;
      gap: 10px;
      background: transparent;
    }
    .s-line {
      font-size: 28px;
      line-height: 1.44;
      letter-spacing: -0.004em;
      padding: 8px 14px;
      border-radius: 10px;
      border-left: 4px solid transparent;
    }
    .mode-dim .s-line {
      color: var(--text-body-dim);
      font-weight: 450;
      background: transparent;
      border-left-color: transparent;
    }
    .mode-bright .s-line {
      color: var(--text-body-bright);
      font-weight: 500;
      background: var(--active-bg);
      border-left-color: var(--active-bar);
    }
    .s-w {
      display: inline;
    }
  </style>
</head>
<body>
  <div id="stage"></div>
  <div id="body-tall-wrap"></div>
  <script>
    const ICONS = {
      verified: `<span class="r-verified" title="Verified"><svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor" aria-hidden="true"><path d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81c-.66-1.31-1.91-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91c-1.31.67-2.2 1.91-2.2 3.34s.89 2.67 2.2 3.34c-.46 1.39-.21 2.9.8 3.91s2.52 1.26 3.91.81c.67 1.31 1.91 2.19 3.34 2.19s2.68-.88 3.34-2.19c1.39.45 2.9.2 3.91-.81s1.27-2.52.81-3.91c1.31-.67 2.19-1.91 2.19-3.34zm-11.71 4.2L6.8 12.46l1.41-1.42 2.26 2.26 4.8-5.23 1.47 1.36-6.2 6.77z"/></svg></span>`,
      upvote: `<svg viewBox="0 0 24 24" fill="#FF4500" aria-hidden="true"><path d="M12 3.2L3.5 12.5h5.2V20h6.6v-7.5h5.2L12 3.2z"/></svg>`,
      downvote: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M12 20.8l8.5-9.3h-5.2V4H8.7v7.5H3.5l8.5 9.3z"/></svg>`,
      comment: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>`,
      share: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>`,
      awardOutline: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="6"/><path d="M15.477 12.89L17 22l-5-3-5 3 1.523-9.11"/></svg>`,
      awardGold: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="#FFB000"/><polygon points="12,5.5 13.9,9.6 18.4,10.2 15.1,13.3 15.9,17.8 12,15.6 8.1,17.8 8.9,13.3 5.6,10.2 10.1,9.6" fill="#FFF8DC"/></svg>`,
      awardHelpful: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="#24A0ED"/><path d="M9.5 12.5l2 2 4-4.5" stroke="#FFFFFF" stroke-width="2.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
      awardHeart: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="#FF585B"/><path d="M12 16.8s-4.8-3.1-4.8-6.4A2.7 2.7 0 0 1 12 8.7a2.7 2.7 0 0 1 4.8 1.7c0 3.3-4.8 6.4-4.8 6.4z" fill="#FFFFFF"/></svg>`
    };

    function getAvatarColor(seedStr) {
      const palette = ["#2563EB", "#D97706", "#059669", "#7C3AED", "#DB2777", "#DC2626", "#0891B2"];
      let hash = 0;
      const str = seedStr || "user";
      for (let i = 0; i < str.length; i++) {
        hash = str.charCodeAt(i) + ((hash << 5) - hash);
      }
      return palette[Math.abs(hash) % palette.length];
    }

    function esc(str) {
      if (str === null || str === undefined) return "";
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function renderAvatar(url, name) {
      const cleanName = (name || "?").replace(/^(r\/|u\/|@)/i, "");
      const initial = cleanName.charAt(0).toUpperCase() || "?";
      const bg = getAvatarColor(cleanName);
      if (!url) {
        return `<div class="r-avatar-wrap" style="background-color:${bg};"><span class="r-avatar-fallback">${esc(initial)}</span></div>`;
      }
      return `<div class="r-avatar-wrap" style="background-color:${bg};"><img src="${esc(url)}" alt="${esc(cleanName)}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';" /><span class="r-avatar-fallback" style="display:none;">${esc(initial)}</span></div>`;
    }

    function formatCount(value) {
      const match = String(value ?? '').toLowerCase().match(/^([0-9.]+)([kmb]?)$/);
      if (!match) return '';
      const n = Number(match[1]) * ({k:1e3,m:1e6,b:1e9}[match[2]] ?? 1);
      const [unit, suffix] = [[1e9,'B'],[1e6,'M'],[1e3,'K'],[1,'']].find(([u]) => n >= u) ?? [1,''];
      return (unit === 1 ? Math.round(n) : Number((n / unit).toFixed(1))) + suffix;
    }

    function renderAwards(awards) {
      if (!Array.isArray(awards)) awards = Number.parseFloat(awards) > 0 ? [{type:'gold', count:awards}] : [];
      if (!awards || awards.length === 0) return "";
      const itemsHtml = awards.map((aw) => {
        let iconSvg = ICONS.awardGold;
        if (aw.type === "helpful") iconSvg = ICONS.awardHelpful;
        if (aw.type === "heart") iconSvg = ICONS.awardHeart;
        const label = aw.label ? esc(aw.label) : (aw.count != null ? esc(formatCount(aw.count)) : "");
        return `<span class="r-award-chip">${iconSvg}<span>${label}</span></span>`;
      }).join("");
      return `<div class="r-awards-row">${itemsHtml}</div>`;
    }

    function buildLinesHtml(lines) {
      return lines.map((ln) => {
        const wordsHtml = ln.words.map((w, wi) => `<span class="s-w" data-lid="${esc(ln.id)}" data-wi="${wi}">${esc(w)}</span>`).join(" ");
        return `<div class="s-line" data-lid="${esc(ln.id)}">${wordsHtml}</div>`;
      }).join("");
    }

    window.setupScrollingPost = async function(payload) {
      const { post, lines, maxCardHeight } = payload;
      const themeClass = post.theme === "light" ? "card-theme-light" : "card-theme-dark";
      const verifiedHtml = post.verified ? ICONS.verified : "";
      const awardsHtml = renderAwards(post.awards);
      const nsfwHtml = post.nsfw ? `<div class="r-nsfw-row"><span class="r-nsfw-pill">NSFW <span class="r-nsfw-18">18+</span></span></div>` : "";
      const footerHtml = post.show_footer !== false ? `
        <div class="r-post-footer">
          <div class="r-action-pill">${ICONS.upvote}<span class="r-vote-score">${esc(formatCount(post.upvotes))}</span><span class="r-vote-divider"></span>${ICONS.downvote}</div>
          <div class="r-action-pill">${ICONS.comment}<span>${esc(formatCount(post.comments))}</span></div>
          <div class="r-action-pill">${ICONS.share}<span>${esc(formatCount(post.shares))}</span></div>
          <div class="r-action-pill">${ICONS.awardOutline}</div>
        </div>` : "";

      const stage = document.getElementById("stage");
      stage.innerHTML = `
        <article id="post-card" class="r-card ${themeClass}">
          <div class="r-post-header">
            ${renderAvatar(post.avatar_url, post.username)}
            <div class="r-post-meta">
              <div class="r-sub-name">${esc(post.subreddit)}</div>
              <div class="r-author-line">
                <span class="r-username-blue">${esc(post.username)}</span>
                ${verifiedHtml}
                <span class="r-dot-age">· ${esc(post.age)} · ${esc(formatCount(post.karma ?? post.follower_count))} ${post.karma != null ? "karma" : "followers"}</span>
              </div>
            </div>
          </div>
          ${post.flair ? `<div class="r-awards-row"><span class="r-award-chip">${esc(post.flair)}</span></div>` : ""}
          ${awardsHtml}
          ${nsfwHtml}
          <h3 class="r-post-title">${esc(post.title)}</h3>
          <div class="r-divider"></div>
          <div id="body-viewport"></div>
          ${footerHtml}
        </article>`;

      // Ensure images are loaded and decoded before measuring/screenshotting
      await Promise.all(
        Array.from(document.images).map(async (img) => {
          if (!img.complete) {
            await new Promise((res) => {
              img.addEventListener("load", res, { once: true });
              img.addEventListener("error", res, { once: true });
            });
          }
          if (img.naturalWidth > 0 && typeof img.decode === "function") {
            await img.decode().catch(() => {});
          }
        })
      );

      const cardEl = document.getElementById("post-card");
      const vpEl = document.getElementById("body-viewport");
      const vpWidth = Math.round(vpEl.getBoundingClientRect().width);
      const chromeHeight = Math.round(cardEl.getBoundingClientRect().height);
      const availVpHeight = Math.max(220, Math.round(maxCardHeight - chromeHeight));

      // Render tall body to measure natural height
      const wrap = document.getElementById("body-tall-wrap");
      wrap.className = `${themeClass} mode-dim`;
      wrap.style.width = `${vpWidth}px`;
      wrap.innerHTML = `<div id="body-tall">${buildLinesHtml(lines)}</div>`;
      wrap.style.display = "block";

      const tallEl = document.getElementById("body-tall");
      const naturalBodyHeight = Math.ceil(tallEl.getBoundingClientRect().height);
      const vpHeight = Math.min(availVpHeight, Math.max(180, naturalBodyHeight));

      // Hide tall body while taking card_shell screenshot
      wrap.style.display = "none";
      vpEl.style.height = `${vpHeight}px`;

      const stageRect = stage.getBoundingClientRect();
      const vpRect = vpEl.getBoundingClientRect();

      return {
        vp_x_in_stage: Math.round(vpRect.left - stageRect.left),
        vp_y_in_stage: Math.round(vpRect.top - stageRect.top),
        vp_w: Math.round(vpRect.width),
        vp_h: Math.round(vpRect.height),
        stage_w: Math.round(stageRect.width),
        stage_h: Math.round(stageRect.height),
        natural_body_h: naturalBodyHeight,
      };
    };

    window.prepareBodyPass = function(modeClass) {
      document.getElementById("stage").style.display = "none";
      const wrap = document.getElementById("body-tall-wrap");
      const isLight = wrap.classList.contains("card-theme-light");
      wrap.className = `${isLight ? "card-theme-light" : "card-theme-dark"} ${modeClass}`;
      wrap.style.display = "block";

      const tallEl = document.getElementById("body-tall");
      const tallRect = tallEl.getBoundingClientRect();
      const lineRects = {};
      for (const el of tallEl.querySelectorAll(".s-line")) {
        const lid = el.getAttribute("data-lid");
        const r = el.getBoundingClientRect();
        lineRects[lid] = [
          Math.max(0, Math.floor(r.top - tallRect.top)),
          Math.min(Math.ceil(tallRect.height), Math.ceil(r.bottom - tallRect.top))
        ];
      }
      const wordYs = [];
      for (const el of tallEl.querySelectorAll(".s-w")) {
        const r = el.getBoundingClientRect();
        wordYs.push({
          line_id: el.getAttribute("data-lid"),
          word: el.textContent || "",
          y_mid: (r.top + r.bottom) / 2.0 - tallRect.top,
        });
      }
      return {
        body_h: Math.ceil(tallRect.height),
        line_rects: lineRects,
        word_ys: wordYs,
      };
    };
  </script>
</body>
</html>
"""


def render_scrolling_post_assets(
    post: dict[str, Any],
    lines: list[dict[str, Any]],
    out_dir: str,
    avatars_dir: str,
    avatar_cache_dir: str | None = None,
    stage_screen_y: int = 35,
    max_card_height: int = 900,
) -> dict[str, Any]:
    """
    Render the persistent Reddit post card shell + dim/bright tall body PNGs
    plus exact DOM coordinates for smooth narration-synced scrolling.
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    avatar_uri = resolve_avatar_data_uri(
        post.get("username"),
        cache_dir=avatar_cache_dir,
        avatars_dir=avatars_dir,
        avatar=post.get("avatar"),
    )

    post_payload = dict(post)
    post_payload["avatar_url"] = avatar_uri

    clean_lines: list[dict[str, Any]] = []
    for ln in lines:
        cleaned = strip_emotion_markers(ln.get("text", ""))
        words = cleaned.split()
        if words:
            clean_lines.append({"id": str(ln["id"]), "words": words})

    shell_png = os.path.join(out_dir, "card_shell.png")
    dim_png = os.path.join(out_dir, "body_dim.png")
    bright_png = os.path.join(out_dir, "body_bright.png")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.url.startswith(("http:", "https:", "file:"))
            else route.continue_(),
        )
        page.set_content(SCROLLING_POST_HTML_TEMPLATE)

        meta = page.evaluate(
            "(payload) => window.setupScrollingPost(payload)",
            {"post": post_payload, "lines": clean_lines, "maxCardHeight": max_card_height},
        )
        page.locator("#stage").screenshot(path=shell_png, omit_background=True)

        # Ensure viewport is tall enough to screenshot the full body without clipping
        needed_h = max(1920, int(math.ceil(meta["natural_body_h"])) + 200)
        page.set_viewport_size({"width": 1080, "height": needed_h})

        body_info = page.evaluate("() => window.prepareBodyPass('mode-dim')")
        page.locator("#body-tall").screenshot(path=dim_png, omit_background=True)

        page.evaluate("() => window.prepareBodyPass('mode-bright')")
        page.locator("#body-tall").screenshot(path=bright_png, omit_background=True)

        browser.close()

    theme = "light" if post.get("theme") == "light" else "dark"
    card_bg_rgb = (255, 255, 255) if theme == "light" else (22, 22, 24)

    return {
        "card_shell_png": shell_png,
        "body_dim_png": dim_png,
        "body_bright_png": bright_png,
        "theme": theme,
        "card_bg_rgb": card_bg_rgb,
        "stage_screen_y": stage_screen_y,
        "viewport_screen_x": int(meta["vp_x_in_stage"]),
        "viewport_screen_y": stage_screen_y + int(meta["vp_y_in_stage"]),
        "viewport_w": int(meta["vp_w"]),
        "viewport_h": int(meta["vp_h"]),
        "body_h": int(body_info["body_h"]),
        "line_rects": body_info["line_rects"],
        "word_ys": body_info["word_ys"],
    }


def _load_avatar_pool(avatars_dir: str) -> list[str]:
    return _load_fallback_avatar_pool(avatars_dir)


def render_cards_to_pngs(
    cards: list[dict[str, Any]],
    out_dir: str,
    avatars_dir: str,
    seed: str | None = None,
    avatar_cache_dir: str | None = None,
) -> list[str]:
    """Legacy card renderer (post, comment, reply, branded) preserved for optional reuse."""
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    png_paths: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        page.route("**/*", lambda route: route.abort() if route.request.url.startswith(("http:", "https:", "file:")) else route.continue_())
        page.set_content(CARD_HTML_TEMPLATE)

        for idx, item in enumerate(cards):
            card_data = json.loads(json.dumps(item["card"]))
            uname = card_data.get("username") or card_data.get("handle") or f"user_{seed or idx}"
            avatar_uri = resolve_avatar_data_uri(uname, cache_dir=avatar_cache_dir, avatars_dir=avatars_dir)
            if card_data.get("type") == "post":
                card_data["avatar_url"] = avatar_uri
                card_data["subreddit_icon_url"] = avatar_uri
            else:
                card_data["avatar_url"] = avatar_uri
                if card_data.get("type") == "reply" and isinstance(card_data.get("reply"), dict):
                    rep_name = card_data["reply"].get("username") or f"reply_{idx}"
                    card_data["reply"]["avatar_url"] = resolve_avatar_data_uri(
                        rep_name, cache_dir=avatar_cache_dir, avatars_dir=avatars_dir
                    )

            page.evaluate("(d) => window.setCardData(d)", card_data)
            page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
            page.evaluate("() => Promise.all(Array.from(document.images).map(i => i.decode().catch(() => {})))")
            height = page.locator(".r-card").evaluate("el => el.getBoundingClientRect().height")
            if height > 1050:
                raise ValueError(f"Card {idx + 1} is {height:.0f}px tall; shorten its text so it fits above captions (maximum 1050px).")
            out_png = os.path.join(out_dir, f"card_{idx:02d}.png")
            stage = page.locator("#stage")
            stage.screenshot(path=out_png, omit_background=True)
            png_paths.append(out_png)

        browser.close()
    return png_paths

