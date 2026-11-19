"""
Local helper for Victor (runs on phone/Termux or PC where YouTube is not bot-blocked).
Reads docs/backgrounds-to-upload.json, downloads each verified Creative Commons /
free-to-reuse clip at <=1080p, strips audio, builds manifest.json, and uploads
them to the 'backgrounds' GitHub Release in motionssalt/storyforge.

Usage:
  export GITHUB_TOKEN="your_pat"
  pip install yt-dlp
  python pipeline/upload_local_backgrounds.py
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request


REPO = os.environ.get("GITHUB_REPOSITORY", "motionssalt/storyforge")


def ensure_release(token: str) -> dict:
    api = f"https://api.github.com/repos/{REPO}/releases"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "StoryForge"}
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
        out = f"/tmp/sf_bg_upload/{fname}"
        subprocess.run([
            "yt-dlp", "-f", "bestvideo[height<=1080][ext=mp4]/best[height<=1080]",
            "--merge-output-format", "mp4", "-o", raw, c["url"]
        ], check=True)
        subprocess.run([
            "ffmpeg", "-y", "-i", raw, "-t", "600", "-an", "-c:v", "copy", "-movflags", "+faststart", out
        ], check=True)
        os.remove(raw)
        subprocess.run([
            "curl", "-fsSL", "-X", "POST", f"{upload_base}?name={fname}",
            "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: video/mp4",
            "--data-binary", f"@{out}"
        ], check=True)
        os.remove(out)
        manifest.append({
            "filename": fname,
            "title": c["title"],
            "uploader": c["uploader"],
            "url": c["url"],
            "license_statement": c["license_statement"],
            "attribution": c["attribution"],
        })

    man_path = "/tmp/sf_bg_upload/manifest.json"
    open(man_path, "w").write(json.dumps({"clips": manifest}, indent=2))
    subprocess.run([
        "curl", "-fsSL", "-X", "POST", f"{upload_base}?name=manifest.json",
        "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
        "--data-binary", f"@{man_path}"
    ], check=True)
    print(f"Uploaded {len(manifest)} clips + manifest.json to backgrounds release!")


if __name__ == "__main__":
    main()
