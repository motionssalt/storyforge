"""
Background gameplay footage fetcher & manifest builder for StoryForge.
Searches YouTube via yt-dlp, verifies Creative Commons license OR explicit
free-to-reuse statement in the video's description (title alone is rejected),
strips audio, caps at 1080p MP4, and uploads clips + manifest.json to the
GitHub Release named 'backgrounds'.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from typing import Any


SEARCH_QUERIES = [
    "ytsearch12:minecraft parkour gameplay no copyright free to use",
    "ytsearch10:gta 5 mega ramp gameplay no copyright free to use",
    "ytsearch10:subway surfers gameplay no copyright free to use",
    "ytsearch10:beamng drive gameplay no copyright free to use",
    "ytsearch10:forza horizon driving gameplay no copyright free to use",
]

FREE_REUSE_RE = re.compile(
    r"(free to use|free to reuse|no copyright|copyright[- ]free|royalty[- ]free|"
    r"creative commons|cc[- ]?by|public domain|free background gameplay|"
    r"you can use this (video|footage|gameplay)|feel free to use)",
    re.IGNORECASE,
)

CREDIT_LINE_RE = re.compile(r"(credit|attribution|mention|link).*$", re.IGNORECASE | re.MULTILINE)

YT_PLAYER_CLIENTS = [
    "android,web",
    "ios,web",
    "tv_embedded,web",
    "mweb",
    "web_creator",
]


def extract_free_statement(meta: dict[str, Any]) -> tuple[str | None, str]:
    """
    Returns (quoted_statement, attribution_text) if verified free-to-reuse via
    YouTube license metadata or creator's description. Returns (None, '') otherwise.
    """
    license_str = str(meta.get("license") or "").strip()
    desc = str(meta.get("description") or "").strip()
    title = str(meta.get("title") or "Gameplay Clip").strip()
    uploader = str(meta.get("uploader") or meta.get("channel") or "Creator").strip()
    url = str(meta.get("webpage_url") or f"https://www.youtube.com/watch?v={meta.get('id')}").strip()

    quoted: str | None = None
    if "creative commons" in license_str.lower():
        quoted = f"YouTube License: {license_str}"

    # Check description lines for explicit free-use statement
    for line in desc.splitlines():
        clean = line.strip()
        if clean and FREE_REUSE_RE.search(clean):
            if quoted:
                quoted = f"{quoted} | Description: \"{clean[:240]}\""
            else:
                quoted = f"Description: \"{clean[:240]}\""
            break

    if not quoted:
        return None, ""

    credit_req = ""
    m_cred = CREDIT_LINE_RE.search(desc)
    if m_cred:
        credit_req = f" ({m_cred.group(0).strip()[:160]})"

    attribution = f"Gameplay: \"{title}\" by {uploader} — {url} [{quoted}]{credit_req}"
    return quoted, attribution


def search_candidates() -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []

    for q in SEARCH_QUERIES:
        print(f"[bg] Searching: {q}")
        cmd = [
            "yt-dlp",
            "--ignore-errors",
            "--no-warnings",
            "--dump-json",
            "--no-download",
            "--extractor-args",
            "youtube:player_client=android,web",
            q,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                meta = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = meta.get("id")
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            dur = float(meta.get("duration") or 0)
            if not (240 <= dur <= 1200):  # 4-20 min clips
                continue
            quoted, attr = extract_free_statement(meta)
            if not quoted:
                continue
            candidates.append(
                {
                    "id": vid,
                    "title": meta.get("title"),
                    "uploader": meta.get("uploader") or meta.get("channel"),
                    "url": meta.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                    "duration_sec": int(dur),
                    "license_statement": quoted,
                    "attribution": attr,
                }
            )
    print(f"[bg] Found {len(candidates)} verified free-to-reuse candidates.")
    return candidates


def download_clip(url: str, raw_out: str) -> bool:
    for client in YT_PLAYER_CLIENTS:
        print(f"[bg] Trying yt-dlp client={client} for {url}")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--extractor-args",
            f"youtube:player_client={client}",
            "-f",
            "bestvideo[height<=1080][ext=mp4]/bestvideo[height<=1080]/best[height<=1080][ext=mp4]/best[height<=1080]",
            "--merge-output-format",
            "mp4",
            "-o",
            raw_out,
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(raw_out) and os.path.getsize(raw_out) > 100_000:
            return True
        print(f"[bg] client={client} failed: {proc.stderr[-300:]}")
    return False


def ensure_release(repo: str, tag: str, token: str) -> dict[str, Any]:
    api = f"https://api.github.com/repos/{repo}/releases"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "StoryForge-Pipeline",
    }
    try:
        req = urllib.request.Request(f"{api}/tags/{tag}", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        payload = json.dumps({"tag_name": tag, "name": tag, "body": "StoryForge background gameplay library"}).encode()
        req = urllib.request.Request(api, data=payload, method="POST", headers={**headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def upload_release_asset(upload_url_tmpl: str, file_path: str, asset_name: str, token: str, content_type: str) -> None:
    upload_base = upload_url_tmpl.split("{")[0]
    url = f"{upload_base}?name={asset_name}"
    subprocess.run(
        [
            "curl",
            "-fsSL",
            "-X",
            "POST",
            url,
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            f"Content-Type: {content_type}",
            "--data-binary",
            f"@{file_path}",
        ],
        check=True,
    )


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    target_count = int(os.environ.get("TARGET_CLIPS", "12"))
    work_dir = "/tmp/bg_build"
    os.makedirs(work_dir, exist_ok=True)

    candidates = search_candidates()
    if not candidates:
        print("[bg] ERROR: No verified candidates found (YouTube search/metadata may be blocked on this IP).", file=sys.stderr)
        sys.exit(2)

    rel = ensure_release(repo, "backgrounds", token)
    existing_assets = {a["name"]: a["id"] for a in rel.get("assets", [])}

    clips_manifest: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidates):
        if len(clips_manifest) >= target_count:
            break
        raw_path = os.path.join(work_dir, f"raw_{cand['id']}.mp4")
        clean_name = f"bg_{len(clips_manifest)+1:02d}_{cand['id']}.mp4"
        clean_path = os.path.join(work_dir, clean_name)

        if not download_clip(cand["url"], raw_path):
            continue

        # Strip audio and cap to <= 600s to keep total library size reasonable
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                raw_path,
                "-t",
                "600",
                "-an",
                "-c:v",
                "copy",
                "-movflags",
                "+faststart",
                clean_path,
            ],
            check=True,
        )
        os.remove(raw_path)

        if clean_name in existing_assets:
            subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "-X",
                    "DELETE",
                    f"https://api.github.com/repos/{repo}/releases/assets/{existing_assets[clean_name]}",
                    "-H",
                    f"Authorization: Bearer {token}",
                ],
                check=True,
            )

        upload_release_asset(rel["upload_url"], clean_path, clean_name, token, "video/mp4")
        os.remove(clean_path)

        clips_manifest.append(
            {
                "filename": clean_name,
                "title": cand["title"],
                "uploader": cand["uploader"],
                "url": cand["url"],
                "duration_sec": min(600, cand["duration_sec"]),
                "license_statement": cand["license_statement"],
                "attribution": cand["attribution"],
            }
        )

    if not clips_manifest:
        print("[bg] ERROR: YouTube blocked all clip downloads from GitHub Actions IP.", file=sys.stderr)
        sys.exit(3)

    manifest_path = os.path.join(work_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"clips": clips_manifest}, f, indent=2)

    if "manifest.json" in existing_assets:
        subprocess.run(
            [
                "curl",
                "-fsSL",
                "-X",
                "DELETE",
                f"https://api.github.com/repos/{repo}/releases/assets/{existing_assets['manifest.json']}",
                "-H",
                f"Authorization: Bearer {token}",
            ],
            check=True,
        )
    upload_release_asset(rel["upload_url"], manifest_path, "manifest.json", token, "application/json")
    print(f"[bg] Uploaded {len(clips_manifest)} background clips + manifest.json.")


if __name__ == "__main__":
    main()
