"""Local helper for uploading clips from docs/backgrounds-to-upload.json.

Normalizes each clip to 1080x1920 30fps silent H.264 and uploads a .jpg thumbnail
+ updated manifest.json to the 'backgrounds' GitHub Release in motionssalt/storyforge.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

from background import generate_thumbnail, normalize_background_clip, probe_video_info

REPO = os.environ.get("GITHUB_REPOSITORY", "motionssalt/storyforge")


def ensure_release(token: str) -> dict:
    api = f"https://api.github.com/repos/{REPO}/releases"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "StoryForge",
    }
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(f"{api}/tags/backgrounds", headers=headers)).read())
    except Exception:
        payload = json.dumps({"tag_name": "backgrounds", "name": "backgrounds", "body": "Free-to-reuse gameplay background library"}).encode()
        return json.loads(urllib.request.urlopen(urllib.request.Request(api, data=payload, method="POST", headers={**headers, "Content-Type": "application/json"})).read())


def main() -> None:
    token = os.environ["GITHUB_TOKEN"]
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cands = json.load(open(os.path.join(root, "docs", "backgrounds-to-upload.json")))[:12]
    rel = ensure_release(token)
    upload_base = rel["upload_url"].split("{")[0]
    os.makedirs("/tmp/sf_bg_upload", exist_ok=True)
    manifest = []
    for i, c in enumerate(cands, 1):
        raw = f"/tmp/sf_bg_upload/raw_{c['id']}.mp4"
        fname = f"bg_{i:02d}_{c['id']}.mp4"
        tname = f"bg_{i:02d}_{c['id']}.jpg"
        out = f"/tmp/sf_bg_upload/{fname}"
        thumb = f"/tmp/sf_bg_upload/{tname}"
        subprocess.run([
            "yt-dlp", "-f", "bestvideo[height<=1080][ext=mp4]/best[height<=1080]",
            "--merge-output-format", "mp4", "-o", raw, c["url"],
        ], check=True)
        trimmed = f"/tmp/sf_bg_upload/trim_{c['id']}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-i", raw, "-t", "600", "-an", "-c:v", "copy", trimmed,
        ], check=True)
        os.remove(raw)
        normalize_background_clip(trimmed, out)
        os.remove(trimmed)
        generate_thumbnail(out, thumb)
        info = probe_video_info(out)
        for p, n, ctype in [(out, fname, "video/mp4"), (thumb, tname, "image/jpeg")]:
            subprocess.run([
                "curl", "-fsSL", "-X", "POST", f"{upload_base}?name={n}",
                "-H", f"Authorization: Bearer {token}", f"-H", f"Content-Type: {ctype}",
                "--data-binary", f"@{p}",
            ], check=True)
            os.remove(p)
        manifest.append({
            "filename": fname,
            "thumbnail": tname,
            "title": c["title"],
            "uploader": c["uploader"],
            "url": c["url"],
            "license_statement": c["license_statement"],
            "attribution": c["attribution"],
            "duration_sec": round(info["duration_sec"], 2),
            "size_bytes": info["size_bytes"],
            "width": info["width"],
            "height": info["height"],
            "fps": round(info["fps"], 2),
            "normalized": True,
        })
    man_path = "/tmp/sf_bg_upload/manifest.json"
    open(man_path, "w").write(json.dumps({"clips": manifest}, indent=2))
    subprocess.run([
        "curl", "-fsSL", "-X", "POST", f"{upload_base}?name=manifest.json",
        "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
        "--data-binary", f"@{man_path}",
    ], check=True)
    print(f"Uploaded {len(manifest)} normalized clips + thumbnails + manifest.json!")


if __name__ == "__main__":
    main()
