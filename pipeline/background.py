"""
Background gameplay footage selector, normalizer, thumbnailer, and formatter for StoryForge.
Downloads the next clip sequentially from the GitHub Release 'backgrounds'
and ensures it is 1080x1920, 30fps, silent (no audio).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from typing import Any, Callable

from progress import run_ffmpeg_with_progress


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
            "No 'backgrounds' GitHub Release found. Add clips via Settings -> Background Video Manager "
            "or upload clips + manifest.json to the 'backgrounds' release."
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
    api_url = f"https://api.github.com/repos/{repo}/releases/tags/backgrounds" if tag == "backgrounds" else f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
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


def probe_video_info(video_path: str) -> dict[str, Any]:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt",
            "-of",
            "json",
            video_path,
        ],
        text=True,
    )
    info = json.loads(out)
    fmt = info.get("format", {})
    size_bytes = os.path.getsize(video_path)
    dur = float(fmt.get("duration") or 0.0)
    bitrate_bps = int(fmt.get("bit_rate") or 0)
    if bitrate_bps <= 0 and dur > 0:
        bitrate_bps = int((size_bytes * 8) / dur)

    vstream = None
    has_audio = False
    for st in info.get("streams", []):
        if st.get("codec_type") == "video" and vstream is None:
            vstream = st
        elif st.get("codec_type") == "audio":
            has_audio = True

    if not vstream:
        raise RuntimeError(f"No video stream found in {video_path}")

    fps_raw = str(vstream.get("r_frame_rate") or "30/1")
    fps = 30.0
    if "/" in fps_raw:
        num, den = fps_raw.split("/", 1)
        if float(den or 1) > 0:
            fps = float(num) / float(den)
    else:
        fps = float(fps_raw or 30.0)

    return {
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "codec": str(vstream.get("codec_name") or ""),
        "pix_fmt": str(vstream.get("pix_fmt") or ""),
        "fps": round(fps, 2),
        "has_audio": has_audio,
        "duration_sec": dur,
        "size_bytes": size_bytes,
        "bitrate_kbps": round(bitrate_bps / 1000.0, 1),
    }


def generate_thumbnail(video_path: str, out_jpg_path: str, timestamp_sec: float = 3.0) -> str:
    info = probe_video_info(video_path)
    dur = info["duration_sec"]
    seek_t = min(timestamp_sec, max(0.2, dur * 0.12)) if dur > 0 else 1.0
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{seek_t:.2f}",
            "-i",
            video_path,
            "-vframes",
            "1",
            "-vf",
            "scale=360:-1",
            "-q:v",
            "4",
            out_jpg_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out_jpg_path


def normalize_background_clip(
    raw_path: str,
    out_path: str,
    progress_cb: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """
    SF-010 library normalization:
    - 1080x1920 vertical (9:16 center crop + lanczos scale)
    - 30 fps, yuv420p, audio stripped (-an)
    - Moderate capped H.264 bitrate (CRF 23, maxrate 3500k)
    """
    info = probe_video_info(raw_path)
    dur = max(0.5, info["duration_sec"])
    vf = (
        "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
        "scale=1080:1920:flags=lanczos,"
        "fps=30,"
        "format=yuv420p,"
        "setsar=1"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        raw_path,
        "-an",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-maxrate",
        "3500k",
        "-bufsize",
        "7000k",
        "-movflags",
        "+faststart",
        out_path,
    ]
    run_ffmpeg_with_progress(cmd=cmd, duration_sec=dur, on_progress=progress_cb)
    after = probe_video_info(out_path)
    return {
        "before_bytes": info["size_bytes"],
        "before_kbps": info["bitrate_kbps"],
        "after_bytes": after["size_bytes"],
        "after_kbps": after["bitrate_kbps"],
        "duration_sec": after["duration_sec"],
    }


def prepare_background_clip(
    raw_clip_path: str,
    out_path: str,
    target_duration_sec: float,
) -> str:
    """
    Crop/scale gameplay video to 1080x1920, 30fps, silent, >= target_duration_sec.
    Loops smoothly if clip is shorter than narration.
    """
    try:
        info = probe_video_info(raw_clip_path)
        is_normalized = (
            info["width"] == 1080
            and info["height"] == 1920
            and info["codec"] == "h264"
            and not info["has_audio"]
            and abs(info["fps"] - 30.0) < 0.5
            and info["duration_sec"] >= target_duration_sec + 0.5
        )
    except Exception:
        is_normalized = False

    if is_normalized:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                raw_clip_path,
                "-t",
                f"{target_duration_sec + 0.25:.3f}",
                "-an",
                "-c:v",
                "copy",
                out_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return out_path

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
            "22",
            "-maxrate",
            "3500k",
            "-bufsize",
            "7000k",
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
    for pipeline smoke verification).
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
