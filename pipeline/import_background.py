"""Import, normalize, and thumbnail background gameplay clips into GitHub Release 'backgrounds'."""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request
from typing import Any

from background import (
    download_release_asset,
    generate_thumbnail,
    normalize_background_clip,
    probe_video_info,
)
from progress import TaskReporter


def gh_headers(token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "StoryForge-Pipeline",
    }
    if extra:
        h.update(extra)
    return h


def ensure_backgrounds_release(repo: str, token: str) -> dict[str, Any]:
    api = f"https://api.github.com/repos/{repo}/releases"
    try:
        req = urllib.request.Request(f"{api}/tags/backgrounds", headers=gh_headers(token))
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        payload = json.dumps({
            "tag_name": "backgrounds",
            "name": "backgrounds",
            "body": "StoryForge background gameplay library (1080x1920 30fps silent H.264)",
        }).encode("utf-8")
        req = urllib.request.Request(
            api,
            data=payload,
            method="POST",
            headers=gh_headers(token, {"Content-Type": "application/json"}),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def load_manifest_from_release(release: dict[str, Any], token: str) -> dict[str, Any]:
    for asset in release.get("assets", []):
        if asset.get("name") == "manifest.json":
            req = urllib.request.Request(
                asset["url"],
                headers=gh_headers(token, {"Accept": "application/octet-stream"}),
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
    return {"clips": []}


def delete_release_asset_if_exists(repo: str, token: str, release: dict[str, Any], name: str) -> None:
    for a in release.get("assets", []):
        if a.get("name") == name:
            subprocess.run(
                [
                    "curl", "-fsSL", "-X", "DELETE",
                    f"https://api.github.com/repos/{repo}/releases/assets/{a['id']}",
                    "-H", f"Authorization: Bearer {token}",
                    "-H", "User-Agent: StoryForge-Pipeline",
                ],
                check=True,
            )


def upload_release_asset(
    repo: str,
    token: str,
    release: dict[str, Any],
    file_path: str,
    asset_name: str,
    content_type: str,
) -> dict[str, Any]:
    delete_release_asset_if_exists(repo, token, release, asset_name)
    upload_base = release["upload_url"].split("{")[0]
    res = subprocess.check_output(
        [
            "curl", "-fsSL", "-X", "POST",
            f"{upload_base}?name={urllib.parse.quote(asset_name)}",
            "-H", f"Authorization: Bearer {token}",
            "-H", f"Content-Type: {content_type}",
            "-H", "User-Agent: StoryForge-Pipeline",
            "--data-binary", f"@{file_path}",
        ],
        text=True,
    )
    return json.loads(res)


def worker_post_status(worker_url: str, secret: str, payload: dict[str, Any]) -> None:
    if not worker_url or not secret:
        return
    url = f"{worker_url.rstrip('/')}/api/internal/job-status"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "X-Internal-Secret": secret,
            "Content-Type": "application/json",
            "User-Agent": "StoryForge-Runner",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def extract_gdrive_file_id(url: str) -> str | None:
    patterns = [
        r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)",
        r"drive\.google\.com/open\?[^#]*id=([A-Za-z0-9_-]+)",
        r"drive\.google\.com/uc\?[^#]*id=([A-Za-z0-9_-]+)",
        r"drive\.usercontent\.google\.com/download\?[^#]*id=([A-Za-z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def download_source_url(url: str, out_path: str, reporter: TaskReporter) -> None:
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise RuntimeError("Source URL must begin with http:// or https://")

    cookie_jar = "/tmp/sf_bg_cookies.txt"
    gdrive_id = extract_gdrive_file_id(url)
    target_url = (
        f"https://drive.usercontent.google.com/download?id={gdrive_id}&export=download&confirm=t"
        if gdrive_id
        else url
    )

    reporter.log("download", f"Downloading source video from {'Google Drive' if gdrive_id else 'URL'}...", 8.0)
    subprocess.run(
        [
            "curl", "-fsSL", "--retry", "3",
            "-A", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "-b", cookie_jar, "-c", cookie_jar,
            "-o", out_path,
            target_url,
        ],
        check=True,
    )

    # Check if Google Drive returned an HTML virus-scan warning page instead of video bytes
    if os.path.exists(out_path) and os.path.getsize(out_path) < 2_000_000:
        head = open(out_path, "rb").read(1024).decode("utf-8", errors="ignore").lower()
        if "<html" in head or "<!doctype html" in head:
            full_html = open(out_path, "r", encoding="utf-8", errors="ignore").read()
            if gdrive_id:
                action_m = re.search(r'<form[^>]+id="download-form"[^>]+action="([^"]+)"', full_html)
                if action_m:
                    action_url = html.unescape(action_m.group(1))
                    inputs = dict(re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', full_html))
                    qs = urllib.parse.urlencode(inputs)
                    sep = "&" if "?" in action_url else "?"
                    confirm_url = f"{action_url}{sep}{qs}" if qs else action_url
                    reporter.log("download", "Handling Google Drive large-file confirmation page...", 12.0)
                    subprocess.run(
                        [
                            "curl", "-fsSL", "--retry", "3",
                            "-A", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                            "-b", cookie_jar, "-c", cookie_jar,
                            "-o", out_path,
                            confirm_url,
                        ],
                        check=True,
                    )
                    head = open(out_path, "rb").read(1024).decode("utf-8", errors="ignore").lower()
            if "<html" in head or "<!doctype html" in head:
                raise RuntimeError(
                    "URL returned an HTML webpage instead of a direct video file. "
                    "Make sure the link is publicly readable (Google Drive: 'Anyone with the link') or a direct MP4 URL."
                )


def next_clip_slug(existing_clips: list[dict[str, Any]], title: str) -> str:
    max_num = 0
    for c in existing_clips:
        m = re.match(r"^bg_(\d+)_", str(c.get("filename", "")))
        if m:
            max_num = max(max_num, int(m.group(1)))
    num = max_num + 1
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:28] or "clip"
    return f"bg_{num:02d}_{slug}"


def run_normalize_existing(
    repo: str,
    token: str,
    reporter: TaskReporter,
    work_dir: str,
) -> dict[str, Any]:
    rel = ensure_backgrounds_release(repo, token)
    manifest = load_manifest_from_release(rel, token)
    clips: list[dict[str, Any]] = manifest.get("clips", [])
    if not clips:
        raise RuntimeError("No clips in manifest.json to normalize.")

    assets_by_name = {a["name"]: a for a in rel.get("assets", [])}
    total = len(clips)
    updated_clips: list[dict[str, Any]] = []

    for idx, entry in enumerate(clips):
        fname = entry["filename"]
        stem = os.path.splitext(fname)[0]
        thumb_name = f"{stem}.jpg"
        base_pct = (idx / total) * 90.0
        span = 90.0 / total
        reporter.update_progress("probe", base_pct + span * 0.05, f"[{idx + 1}/{total}] Checking {fname}...")

        raw_path = os.path.join(work_dir, f"raw_{fname}")
        norm_path = os.path.join(work_dir, fname)
        thumb_path = os.path.join(work_dir, thumb_name)

        download_release_asset(repo, "backgrounds", fname, raw_path, token)
        info = probe_video_info(raw_path)

        needs_reencode = not (
            info["width"] == 1080
            and info["height"] == 1920
            and info["codec"] == "h264"
            and not info["has_audio"]
            and abs(info["fps"] - 30.0) < 0.5
            and info["bitrate_kbps"] <= 4200
        )

        if needs_reencode:
            reporter.log(
                "normalize",
                f"[{idx + 1}/{total}] Normalizing {fname} ({info['width']}x{info['height']}, "
                f"{info['bitrate_kbps']:.0f} kbps, {info['size_bytes'] / (1024*1024):.1f} MB)...",
                base_pct + span * 0.15,
            )
            stats = normalize_background_clip(
                raw_path,
                norm_path,
                progress_cb=lambda frac, b=base_pct, s=span: reporter.update_progress(
                    "normalize", b + s * (0.15 + 0.60 * frac)
                ),
            )
            reporter.log(
                "normalize",
                f"[{idx + 1}/{total}] Normalized {fname}: "
                f"{stats['before_bytes']/(1024*1024):.1f} MB ({stats['before_kbps']:.0f} kbps) -> "
                f"{stats['after_bytes']/(1024*1024):.1f} MB ({stats['after_kbps']:.0f} kbps)",
                base_pct + span * 0.78,
            )
            # Refresh release object so upload uses fresh asset list
            rel = ensure_backgrounds_release(repo, token)
            upload_release_asset(repo, token, rel, norm_path, fname, "video/mp4")
            video_for_thumb = norm_path
            final_info = probe_video_info(norm_path)
        else:
            reporter.log(
                "normalize",
                f"[{idx + 1}/{total}] {fname} already matches 1080x1920 30fps silent H.264 ({info['bitrate_kbps']:.0f} kbps).",
                base_pct + span * 0.78,
            )
            video_for_thumb = raw_path
            final_info = info

        if thumb_name not in assets_by_name or needs_reencode:
            reporter.log("thumbnail", f"[{idx + 1}/{total}] Generating thumbnail {thumb_name}...", base_pct + span * 0.85)
            generate_thumbnail(video_for_thumb, thumb_path)
            rel = ensure_backgrounds_release(repo, token)
            upload_release_asset(repo, token, rel, thumb_path, thumb_name, "image/jpeg")

        for p in (raw_path, norm_path, thumb_path):
            if os.path.exists(p):
                os.remove(p)

        updated_entry = dict(entry)
        updated_entry["thumbnail"] = thumb_name
        updated_entry["duration_sec"] = round(final_info["duration_sec"], 2)
        updated_entry["size_bytes"] = final_info["size_bytes"]
        updated_entry["width"] = final_info["width"]
        updated_entry["height"] = final_info["height"]
        updated_entry["fps"] = round(final_info["fps"], 2)
        updated_entry["normalized"] = True
        updated_clips.append(updated_entry)

    manifest_path = os.path.join(work_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"clips": updated_clips}, f, indent=2)
    rel = ensure_backgrounds_release(repo, token)
    upload_release_asset(repo, token, rel, manifest_path, "manifest.json", "application/json")
    reporter.log("manifest", f"Updated manifest.json for {len(updated_clips)} background clip(s).", 98.0)
    return {"normalized_count": len(updated_clips)}


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    worker_url = os.environ.get("WORKER_URL", "")
    secret = os.environ.get("WORKER_INTERNAL_SECRET", "")
    task_id = os.environ.get("TASK_ID", "bg_import_local")
    mode = os.environ.get("IMPORT_MODE", "url").strip()
    source_url = os.environ.get("SOURCE_URL", "").strip()
    pending_asset_name = os.environ.get("PENDING_ASSET_NAME", "").strip()
    clip_title = os.environ.get("CLIP_TITLE", "").strip()
    clip_note = os.environ.get("CLIP_NOTE", "").strip()
    clip_attribution = os.environ.get("CLIP_ATTRIBUTION", "").strip()

    reporter = TaskReporter(task_id=task_id, worker_url=worker_url, secret=secret)
    work_dir = f"/tmp/sf_bg_import/{task_id}"
    os.makedirs(work_dir, exist_ok=True)
    current_stage = "init"

    try:
        worker_post_status(worker_url, secret, {"job_id": task_id, "status": "running"})
        reporter.log("init", f"Starting background library job ({mode})", 2.0)
        reporter.flush()

        if mode == "normalize_existing":
            current_stage = "normalize"
            res = run_normalize_existing(repo, token, reporter, work_dir)
            reporter.update_progress("done", 100.0, f"Background library normalization finished ({res['normalized_count']} clip(s)).")
            reporter.flush(status="done")
            worker_post_status(worker_url, secret, {"job_id": task_id, "status": "done"})
            return

        rel = ensure_backgrounds_release(repo, token)
        manifest = load_manifest_from_release(rel, token)
        clips: list[dict[str, Any]] = manifest.get("clips", [])

        raw_path = os.path.join(work_dir, "raw_input.mp4")
        if mode == "pending_asset":
            if not pending_asset_name:
                raise RuntimeError("Missing PENDING_ASSET_NAME for pending_asset mode")
            current_stage = "download"
            reporter.log("download", f"Fetching staged upload {pending_asset_name} from GitHub Release...", 10.0)
            download_release_asset(repo, "backgrounds", pending_asset_name, raw_path, token)
            reporter.log("download", f"Downloaded staged upload ({os.path.getsize(raw_path)/(1024*1024):.1f} MB)", 20.0)
        elif mode == "url":
            if not source_url:
                raise RuntimeError("Missing SOURCE_URL")
            current_stage = "download"
            download_source_url(source_url, raw_path, reporter)
            reporter.log("download", f"Downloaded source ({os.path.getsize(raw_path)/(1024*1024):.1f} MB)", 20.0)
        else:
            raise RuntimeError(f"Unsupported IMPORT_MODE: {mode}")

        current_stage = "probe"
        try:
            raw_info = probe_video_info(raw_path)
        except Exception as exc:
            raise RuntimeError(f"Uploaded/linked file is not a readable video: {exc}") from exc
        if raw_info["width"] <= 0 or raw_info["height"] <= 0 or raw_info["duration_sec"] < 1.0:
            raise RuntimeError("Video must have valid dimensions and be at least 1 second long.")

        reporter.log(
            "probe",
            f"Probed source: {raw_info['width']}x{raw_info['height']} @ {raw_info['fps']:.1f} fps, "
            f"{raw_info['duration_sec']:.1f}s, audio={'yes' if raw_info['has_audio'] else 'no'}",
            25.0,
        )

        title = clip_title or (
            os.path.splitext(re.sub(r"^pending_upload_[^_]+_", "", pending_asset_name))[0]
            if pending_asset_name
            else f"Background clip {len(clips) + 1}"
        )
        stem = next_clip_slug(clips, title)
        final_mp4_name = f"{stem}.mp4"
        final_thumb_name = f"{stem}.jpg"
        norm_path = os.path.join(work_dir, final_mp4_name)
        thumb_path = os.path.join(work_dir, final_thumb_name)

        current_stage = "normalize"
        reporter.log("normalize", f"Normalizing to 1080x1920 30fps silent H.264 ({final_mp4_name})...", 30.0)
        stats = normalize_background_clip(
            raw_path,
            norm_path,
            progress_cb=lambda frac: reporter.update_progress("normalize", 30.0 + 45.0 * frac),
        )
        final_info = probe_video_info(norm_path)
        reporter.log(
            "normalize",
            f"Normalized: {stats['before_bytes']/(1024*1024):.1f} MB -> {stats['after_bytes']/(1024*1024):.1f} MB "
            f"({stats['after_kbps']:.0f} kbps)",
            76.0,
        )

        current_stage = "thumbnail"
        reporter.log("thumbnail", f"Generating preview thumbnail {final_thumb_name}...", 78.0)
        generate_thumbnail(norm_path, thumb_path)

        current_stage = "upload"
        reporter.log("upload", f"Uploading {final_mp4_name} & {final_thumb_name} to 'backgrounds' release...", 82.0)
        rel = ensure_backgrounds_release(repo, token)
        upload_release_asset(repo, token, rel, norm_path, final_mp4_name, "video/mp4")
        rel = ensure_backgrounds_release(repo, token)
        upload_release_asset(repo, token, rel, thumb_path, final_thumb_name, "image/jpeg")

        if mode == "pending_asset" and pending_asset_name:
            rel = ensure_backgrounds_release(repo, token)
            delete_release_asset_if_exists(repo, token, rel, pending_asset_name)

        clips.append(
            {
                "filename": final_mp4_name,
                "thumbnail": final_thumb_name,
                "title": title,
                "note": clip_note,
                "uploader": clip_note or "User Library",
                "url": source_url or "Uploaded via StoryForge",
                "license_statement": clip_note or "User-supplied background footage",
                "attribution": clip_attribution,
                "duration_sec": round(final_info["duration_sec"], 2),
                "size_bytes": final_info["size_bytes"],
                "width": final_info["width"],
                "height": final_info["height"],
                "fps": round(final_info["fps"], 2),
                "normalized": True,
            }
        )
        manifest_path = os.path.join(work_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"clips": clips}, f, indent=2)
        rel = ensure_backgrounds_release(repo, token)
        upload_release_asset(repo, token, rel, manifest_path, "manifest.json", "application/json")

        reporter.update_progress("done", 100.0, f"Added background clip {final_mp4_name} to library.")
        reporter.flush(status="done")
        worker_post_status(
            worker_url,
            secret,
            {
                "job_id": task_id,
                "status": "done",
                "background_clip": final_mp4_name,
            },
        )
    except Exception as exc:
        err_msg = f"[{current_stage}] {exc}"
        print("BG IMPORT FAILED:", err_msg, traceback.format_exc(), file=sys.stderr)
        reporter.log("error", err_msg, level="error")
        reporter.flush(status="failed", error=err_msg[:500])
        try:
            worker_post_status(worker_url, secret, {"job_id": task_id, "status": "failed", "error": err_msg[:500]})
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
