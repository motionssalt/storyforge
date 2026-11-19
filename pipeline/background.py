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
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            rel = json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(
            "No 'backgrounds' GitHub Release found. YouTube blocks downloads from GitHub Actions IPs; "
            "run pipeline/upload_local_backgrounds.py locally or upload clips + manifest.json to the 'backgrounds' release."
        ) from exc

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


def generate_test_pattern_background(
    out_path: str,
    duration_sec: float,
    variant_index: int = 0,
) -> str:
    """
    Test-only procedural 9:16 background (used ONLY when test_background=true
    for pipeline smoke verification while 'backgrounds' release is blocked).
    Each variant_index produces a distinct visual color/pattern so multi-part
    background switching is visibly verifiable.
    """
    if variant_index % 2 == 0:
        src = f"testsrc2=size=1080x1920:rate=30:duration={duration_sec + 0.25:.3f}"
        vf = "hue=h=20:s=0.85,eq=brightness=-0.12:contrast=0.9,format=yuv420p"
    else:
        src = "life=size=540x960:rate=30:mold=10:ratio=0.3:seed=42:life_color=#38bdf8:death_color=#1e1b4b"
        vf = "scale=1080:1920:flags=neighbor,format=yuv420p"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            src,
            "-t",
            f"{duration_sec + 0.25:.3f}",
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            out_path,
        ],
        check=True,
    )
    return out_path
