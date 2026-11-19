"""
Background gameplay footage selector and formatter for StoryForge.
Downloads the next clip sequentially from the GitHub Release 'backgrounds'
and crops/scales it to 1080x1920, 30fps, silent (no audio).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from typing import Any


def fetch_backgrounds_manifest(repo: str, github_token: str | None = None) -> dict[str, Any]:
    """Fetch manifest.json from the 'backgrounds' GitHub Release."""
    api_url = f"https://api.github.com/repos/{repo}/releases/tags/backgrounds"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "StoryForge-Pipeline"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    req = urllib.request.Request(api_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        rel = json.loads(resp.read())

    for asset in rel.get("assets", []):
        if asset["name"] == "manifest.json":
            dl_headers = {"User-Agent": "StoryForge-Pipeline"}
            if github_token:
                dl_headers["Authorization"] = f"Bearer {github_token}"
                dl_headers["Accept"] = "application/octet-stream"
                url = asset["url"]
            else:
                url = asset["browser_download_url"]
            dreq = urllib.request.Request(url, headers=dl_headers)
            with urllib.request.urlopen(dreq, timeout=60) as dresp:
                return json.loads(dresp.read())

    raise RuntimeError("manifest.json not found in 'backgrounds' GitHub Release")


def download_release_asset(
    repo: str,
    tag: str,
    asset_name: str,
    out_path: str,
    github_token: str | None = None,
) -> str:
    api_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "StoryForge-Pipeline"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    req = urllib.request.Request(api_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        rel = json.loads(resp.read())

    for asset in rel.get("assets", []):
        if asset["name"] == asset_name:
            cmd = ["curl", "-fsSL", "--retry", "3", "-o", out_path]
            if github_token:
                cmd += [
                    "-H", f"Authorization: Bearer {github_token}",
                    "-H", "Accept: application/octet-stream",
                    asset["url"],
                ]
            else:
                cmd += [asset["browser_download_url"]]
            subprocess.run(cmd, check=True)
            return out_path

    raise RuntimeError(f"Asset '{asset_name}' not found in release '{tag}'")


def prepare_background_clip(
    raw_clip_path: str,
    out_path: str,
    target_duration_sec: float,
) -> str:
    """
    Crop/scale gameplay video to 1080x1920, 30fps, silent, >= target_duration_sec.
    Loops smoothly if clip is shorter than narration.
    """
    # Crop center 9:16 from any aspect ratio, then scale to 1080x1920
    vf = (
        "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
        "scale=1080:1920:flags=lanczos,"
        "fps=30,"
        "format=yuv420p"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            raw_clip_path,
            "-t",
            f"{target_duration_sec + 0.25:.3f}",
            "-an",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            out_path,
        ],
        check=True,
    )
    return out_path
