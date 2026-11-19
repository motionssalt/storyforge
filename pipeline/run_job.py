"""
Main render orchestrator for StoryForge GitHub Actions workflow.
Supports:
  - Worker-dispatched jobs via WORKER_URL + JOB_ID + WORKER_INTERNAL_SECRET
  - Direct local test runs via --local-json <path> --local-bg <path>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
import urllib.request
from typing import Any

from background import (
    download_release_asset,
    fetch_backgrounds_manifest,
    generate_test_pattern_background,
    prepare_background_clip,
)
from compose import compose_video, compute_card_windows
from render_cards import render_cards_to_pngs
from subtitles import generate_ass_subtitles
from tts_engine import VoiceStudioCoupledSpeedupHook, get_tts_engine


def worker_get_job(worker_url: str, job_id: str, secret: str) -> dict[str, Any]:
    url = f"{worker_url.rstrip('/')}/api/internal/job?id={job_id}"
    req = urllib.request.Request(url, headers={"X-Internal-Secret": secret, "User-Agent": "StoryForge-Runner"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def worker_post_status(worker_url: str, secret: str, payload: dict[str, Any]) -> None:
    url = f"{worker_url.rstrip('/')}/api/internal/job-status"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "X-Internal-Secret": secret,
            "Content-Type": "application/json",
            "User-Agent": "StoryForge-Runner",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def ensure_renders_release(repo: str, token: str) -> dict[str, Any]:
    api = f"https://api.github.com/repos/{repo}/releases"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "StoryForge-Runner",
    }
    try:
        req = urllib.request.Request(f"{api}/tags/renders", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        payload = json.dumps({"tag_name": "renders", "name": "renders", "body": "StoryForge rendered output videos"}).encode()
        req = urllib.request.Request(api, data=payload, method="POST", headers={**headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def upload_mp4_to_release(repo: str, token: str, release: dict[str, Any], mp4_path: str, asset_name: str) -> dict[str, Any]:
    for a in release.get("assets", []):
        if a["name"] == asset_name:
            subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "-X",
                    "DELETE",
                    f"https://api.github.com/repos/{repo}/releases/assets/{a['id']}",
                    "-H",
                    f"Authorization: Bearer {token}",
                ],
                check=True,
            )
    upload_base = release["upload_url"].split("{")[0]
    res = subprocess.check_output(
        [
            "curl",
            "-fsSL",
            "-X",
            "POST",
            f"{upload_base}?name={asset_name}",
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Content-Type: video/mp4",
            "--data-binary",
            f"@{mp4_path}",
        ],
        text=True,
    )
    return json.loads(res)


def render_all_parts(
    job_id: str,
    production_json: dict[str, Any],
    voice_id: str,
    voice_speed: str,
    bg_index: int,
    work_dir: str,
    repo: str | None,
    github_token: str | None,
    local_bg: str | None = None,
    test_background: bool = False,
    tts_engine_name: str = "edge",
    coupled_speedup_factor: float | None = None,
) -> tuple[list[dict[str, Any]], int]:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    from jsonschema import Draft202012Validator
    with open(os.path.join(root, "schema", "production.schema.json")) as schema_file:
        Draft202012Validator(json.load(schema_file)).validate(production_json)
    avatars_dir = os.path.join(root, "assets", "avatars")
    fonts_dir = os.path.join(root, "assets", "fonts")
    engine = get_tts_engine(tts_engine_name)

    manifest_clips: list[dict[str, Any]] = []
    if not local_bg and not test_background:
        assert repo, "GITHUB_REPOSITORY is required when not using --local-bg"
        manifest = fetch_backgrounds_manifest(repo, github_token)
        manifest_clips = manifest.get("clips", [])
        if not manifest_clips:
            raise RuntimeError("No clips found in 'backgrounds' release manifest.json")

    rendered_parts: list[dict[str, Any]] = []
    cur_bg_idx = bg_index

    for p_idx, part in enumerate(production_json["parts"]):
        p_num = int(part.get("part_number", p_idx + 1))
        part_dir = os.path.join(work_dir, f"part_{p_num}")
        os.makedirs(part_dir, exist_ok=True)

        # 1. TTS + Word timings
        tts_res = engine.synthesize_lines(
            part["lines"],
            voice_id=voice_id,
            settings={"voice_speed": voice_speed},
            out_dir=os.path.join(part_dir, "tts"),
        )
        if coupled_speedup_factor and tts_engine_name != "edge":
            tts_res = VoiceStudioCoupledSpeedupHook.apply_coupled_speedup(tts_res, factor=coupled_speedup_factor)

        # 2. Reddit cards -> transparent PNGs
        card_pngs = render_cards_to_pngs(
            part["cards"],
            out_dir=os.path.join(part_dir, "cards"),
            avatars_dir=avatars_dir,
            seed=f"{job_id}-part{p_num}",
        )
        card_windows = compute_card_windows(part["cards"], tts_res.line_timings, tts_res.duration_sec)

        # 3. ASS subtitles
        ass_path = os.path.join(part_dir, "subtitles.ass")
        generate_ass_subtitles(tts_res.word_timings, ass_path, font_name="Lilita One")

        # 4. Background footage (sequential per video)
        prep_bg = os.path.join(part_dir, "bg_1080x1920.mp4")
        if local_bg:
            clip_name = os.path.basename(local_bg)
            attribution = "Local test background clip"
            prepare_background_clip(local_bg, prep_bg, tts_res.duration_sec)
        elif test_background:
            clip_name = f"test_pattern_variant_{cur_bg_idx % 2}.mp4"
            attribution = (
                f"Test background #{cur_bg_idx} (procedural verification pattern — "
                "upload real YouTube gameplay clips to the 'backgrounds' GitHub Release for production)"
            )
            generate_test_pattern_background(prep_bg, tts_res.duration_sec, variant_index=cur_bg_idx)
            cur_bg_idx += 1
        else:
            clip_entry = manifest_clips[cur_bg_idx % len(manifest_clips)]
            cur_bg_idx += 1
            clip_name = clip_entry["filename"]
            attribution = clip_entry.get("attribution", "")
            raw_bg = os.path.join(part_dir, clip_name)
            download_release_asset(repo, "backgrounds", clip_name, raw_bg, github_token)
            prepare_background_clip(raw_bg, prep_bg, tts_res.duration_sec)

        # 5. Compose final MP4
        out_filename = f"{job_id}_part{p_num}.mp4"
        out_mp4 = os.path.join(part_dir, out_filename)
        compose_video(
            bg_video_path=prep_bg,
            audio_wav_path=tts_res.audio_path,
            ass_path=ass_path,
            fonts_dir=fonts_dir,
            card_pngs=card_pngs,
            card_windows=card_windows,
            out_mp4_path=out_mp4,
            duration_sec=tts_res.duration_sec,
        )

        rendered_parts.append(
            {
                "part_number": p_num,
                "title": part["title"],
                "hashtags": part.get("hashtags", []),
                "filename": out_filename,
                "local_path": out_mp4,
                "duration_sec": tts_res.duration_sec,
                "background_clip": clip_name,
                "attribution": attribution,
            }
        )

    return rendered_parts, cur_bg_idx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-json", help="Run locally from a production JSON file")
    parser.add_argument("--local-bg", help="Local MP4 background clip for offline testing")
    parser.add_argument("--voice", default="en-US-AndrewMultilingualNeural")
    parser.add_argument("--speed", default="+34%")
    parser.add_argument("--out-dir", default="/tmp/sf_render")
    args = parser.parse_args()

    if args.local_json:
        prod = json.load(open(args.local_json, "r", encoding="utf-8"))
        parts, _ = render_all_parts(
            job_id="localtest",
            production_json=prod,
            voice_id=args.voice,
            voice_speed=args.speed,
            bg_index=0,
            work_dir=args.out_dir,
            repo=os.environ.get("GITHUB_REPOSITORY"),
            github_token=os.environ.get("GITHUB_TOKEN"),
            local_bg=args.local_bg,
            test_background=(not args.local_bg),
        )
        print("LOCAL RENDER COMPLETE:", json.dumps(parts, indent=2))
        return

    worker_url = os.environ["WORKER_URL"]
    secret = os.environ["WORKER_INTERNAL_SECRET"]
    job_id = os.environ["JOB_ID"]
    repo = os.environ["GITHUB_REPOSITORY"]
    gh_token = os.environ["GITHUB_TOKEN"]
    test_bg = os.environ.get("TEST_BACKGROUND", "false").lower() == "true"

    try:
        worker_post_status(worker_url, secret, {"job_id": job_id, "status": "running"})
        job = worker_get_job(worker_url, job_id, secret)
        if not test_bg and job.get("background_mode", "sequential") == "sequential":
            req = urllib.request.Request(worker_url.rstrip("/") + "/api/internal/background-index", headers={"X-Internal-Secret": secret, "User-Agent": "StoryForge-Runner"})
            with urllib.request.urlopen(req, timeout=30) as response:
                job["background_index"] = json.loads(response.read())["index"]

        parts, next_bg_idx = render_all_parts(
            job_id=job_id,
            production_json=job["production_json"],
            voice_id=job["voice_id"],
            voice_speed=job.get("voice_speed", "+34%"),
            bg_index=int(job.get("background_index", 0)),
            work_dir=args.out_dir,
            repo=repo,
            github_token=gh_token,
            test_background=test_bg,
        )

        rel = ensure_renders_release(repo, gh_token)
        public_parts: list[dict[str, Any]] = []
        for p in parts:
            asset = upload_mp4_to_release(repo, gh_token, rel, p["local_path"], p["filename"])
            public_parts.append(
                {
                    "part_number": p["part_number"],
                    "title": p["title"],
                    "hashtags": p["hashtags"],
                    "filename": p["filename"],
                    "asset_id": asset["id"],
                    "size_bytes": asset["size"],
                    "duration_sec": p["duration_sec"],
                    "background_clip": p["background_clip"],
                    "attribution": p["attribution"],
                }
            )

        worker_post_status(
            worker_url,
            secret,
            {
                "job_id": job_id,
                "status": "done",
                "parts": public_parts,
                **({"next_background_index": next_bg_idx} if not test_bg else {}),
            },
        )
        print("JOB COMPLETE:", job_id)
    except Exception as exc:
        err_msg = f"{exc}\n{traceback.format_exc()[-1000:]}"
        print("JOB FAILED:", err_msg, file=sys.stderr)
        try:
            worker_post_status(worker_url, secret, {"job_id": job_id, "status": "failed", "error": str(exc)[:500]})
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
