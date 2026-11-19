"""
Reddit-style card renderer for StoryForge (Appendix A spec).
Uses headless Playwright Chromium to render each card to a transparent PNG
at 1080p vertical video scale (card width = 810px inside 1080px frame).
"""

from __future__ import annotations

import base64
import json
import os
import random
from typing import Any


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
      padding: 56px 135px 80px 135px;
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

    function renderAwards(awards) {
      if (!awards || awards.length === 0) return "";
      const itemsHtml = awards.map((aw) => {
        let iconSvg = ICONS.awardGold;
        if (aw.type === "helpful") iconSvg = ICONS.awardHelpful;
        if (aw.type === "heart") iconSvg = ICONS.awardHeart;
        const label = aw.label ? esc(aw.label) : (aw.count ? esc(aw.count) : "1");
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
        return `
          <article class="r-card r-card--post ${themeClass}">
            <div class="r-post-header">
              ${renderAvatar(data.subreddit_icon_url, data.subreddit || "AskReddit", "r-sub-icon")}
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


def _load_avatar_pool(avatars_dir: str) -> list[str]:
    if not os.path.isdir(avatars_dir):
        return []
    files = sorted(
        os.path.join(avatars_dir, fn)
        for fn in os.listdir(avatars_dir)
        if fn.lower().endswith((".svg", ".png", ".jpg", ".webp"))
    )
    urls: list[str] = []
    for p in files:
        raw = open(p, "rb").read()
        mime = "image/svg+xml" if p.lower().endswith(".svg") else "image/png"
        b64 = base64.b64encode(raw).decode("ascii")
        urls.append(f"data:{mime};base64,{b64}")
    return urls


def render_cards_to_pngs(
    cards: list[dict[str, Any]],
    out_dir: str,
    avatars_dir: str,
    seed: str | None = None,
) -> list[str]:
    """Render each card dict in `cards` (timeline items' `.card` payloads) to transparent PNGs."""
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    pool = _load_avatar_pool(avatars_dir)
    rng = random.Random(seed)

    def pick_avatar() -> str:
        return rng.choice(pool) if pool else ""

    png_paths: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        page.set_content(CARD_HTML_TEMPLATE)

        for idx, item in enumerate(cards):
            card_data = json.loads(json.dumps(item["card"]))
            # Assign random avatar from local pool (no network needed)
            if card_data.get("type") == "post":
                if not card_data.get("subreddit_icon_url"):
                    card_data["subreddit_icon_url"] = pick_avatar()
            else:
                card_data["avatar_url"] = pick_avatar()
                if card_data.get("type") == "reply" and isinstance(card_data.get("reply"), dict):
                    card_data["reply"]["avatar_url"] = pick_avatar()

            page.evaluate("(d) => window.setCardData(d)", card_data)
            page.wait_for_timeout(40)
            out_png = os.path.join(out_dir, f"card_{idx:02d}.png")
            stage = page.locator("#stage")
            stage.screenshot(path=out_png, omit_background=True)
            png_paths.append(out_png)

        browser.close()
    return png_paths
