"""Main render orchestrator for StoryForge GitHub Actions workflow.

Supports:
  - Worker-dispatched jobs via WORKER_URL + JOB_ID + WORKER_INTERNAL_SECRET
  - Direct local test runs via --local-json <path> --local-bg <path>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import subprocess
import traceback
import urllib.request
from typing import Any

from background import (
    download_release_asset,
    fetch_backgrounds_manifest,
    generate_test_pattern_background,
    prepare_background_clip,
)
from compose import compose_scrolling_video, compress_final_video, probe_video_stats
from progress import TaskReporter
from render_cards import render_scrolling_post_assets, resolve_avatar_data_uri
from subtitles import generate_ass_subtitles
from audio_checks import verify_final_video_audio, verify_word_timing_coverage
from tts_engine import clean_spoken_text, get_tts_engine


def migrate_legacy_production_json(doc: dict[str, Any]) -> dict[str, Any]:
    """Use the exact same normalization as the Pages API."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    result = subprocess.run(["node", os.path.join(root, "pipeline", "normalize-production.mjs")],
                            input=json.dumps(doc), text=True, check=True, capture_output=True)
    normalized = json.loads(result.stdout)
    if normalized["errors"]:
        raise ValueError("; ".join(normalized["errors"]))
    return normalized["doc"]


def worker_get_job(worker_url: str, job_id: str, secret: str) -> dict[str, Any]:
    url = f"{worker_url.rstrip('/')}/api/internal/job?id={job_id}"
    req = urllib.request.Request(
        url,
        headers={"X-Internal-Secret": secret, "User-Agent": "StoryForge-Runner"},
    )
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
    import subprocess
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
                    "-H",
                    "User-Agent: StoryForge-Runner",
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
            "-H",
            "User-Agent: StoryForge-Runner",
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
    background_mode: str = "sequential",
    background_clip: str | None = None,
    tts_engine_name: str = "voicestudio",
    coupled_speedup_factor: float | None = None,
    voice_config: dict[str, Any] | None = None,
    worker_url: str | None = None,
    internal_secret: str | None = None,
    reporter: TaskReporter | None = None,
    stage_ref: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    def emit(stage: str, msg: str, pct: float | None = None, level: str = "info") -> None:
        if stage_ref is not None:
            stage_ref[0] = stage
        print(f"[{stage}] {msg}", flush=True)
        if reporter is not None:
            reporter.log(stage, msg, level=level, progress_pct=pct)

    def emit_pct(stage: str, pct: float) -> None:
        if stage_ref is not None:
            stage_ref[0] = stage
        if reporter is not None:
            reporter.update_progress(stage, pct)

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    production_json = migrate_legacy_production_json(production_json)
    from jsonschema import Draft202012Validator

    with open(os.path.join(root, "schema", "production.schema.json")) as schema_file:
        Draft202012Validator(json.load(schema_file)).validate(production_json)

    avatars_dir = os.path.join(root, "assets", "avatars")
    avatar_cache_dir = os.path.join(work_dir, "avatars_cache")
    fonts_dir = os.path.join(root, "assets", "fonts")
    engine = get_tts_engine(tts_engine_name)
    first_post = production_json["parts"][0]["post"]
    resolve_avatar_data_uri(first_post["username"], avatar_cache_dir, avatars_dir, first_post["avatar"])

    manifest_clips: list[dict[str, Any]] = []
    if not local_bg and not test_background:
        assert repo, "GITHUB_REPOSITORY is required when not using --local-bg"
        manifest = fetch_backgrounds_manifest(repo, github_token)
        manifest_clips = manifest.get("clips", [])
        if not manifest_clips:
            raise RuntimeError("No clips found in 'backgrounds' release manifest.json")

    # Resolve specific clip starting index if background_mode == 'specific'
    specific_idx: int | None = None
    if background_mode == "specific" and background_clip and manifest_clips:
        for i, c in enumerate(manifest_clips):
            if c.get("filename") == background_clip:
                specific_idx = i
                break
        if specific_idx is None:
            emit(
                "background",
                f"Requested default/specific clip '{background_clip}' not found in library; falling back to sequential index #{bg_index}.",
                level="warn",
            )

    rendered_parts: list[dict[str, Any]] = []
    cur_bg_idx = bg_index
    total_parts = len(production_json["parts"])
    # Reserve 0..88% for per-part rendering+compression, 88..98% for upload
    render_budget = 86.0

    for p_idx, part in enumerate(production_json["parts"]):
        if reporter is not None and reporter.deleted:
            raise RuntimeError("Task was cancelled/deleted during render.")
        p_num = int(part.get("part_number", p_idx + 1))
        part_tag = f"Part {p_num}/{total_parts}"
        part_base = 2.0 + (p_idx / total_parts) * render_budget
        part_span = render_budget / total_parts
        part_dir = os.path.join(work_dir, f"part_{p_num}")
        os.makedirs(part_dir, exist_ok=True)

        # 1. TTS + Word timings (0..18% of part span)
        engine_label = "VoiceStudio" if tts_engine_name == "voicestudio" else "Edge TTS"
        tts_detail = f"VoiceStudio {coupled_speedup_factor or 1.25:.2f}x" if tts_engine_name == "voicestudio" else f"Edge rate {voice_speed}"
        emit("tts", f"{part_tag}: Voice: {engine_label} / {voice_id} ({tts_detail})", part_base + part_span * 0.02)
        tts_res = engine.synthesize_lines(
            part["lines"],
            voice_id=voice_id,
            settings={
                "voice_speed": voice_speed,
                "voicestudio_speedup": coupled_speedup_factor or 1.25,
                "voice_config": voice_config,
                "worker_url": worker_url,
                "internal_secret": internal_secret,
                "cancel_check": (lambda: bool(reporter and reporter.deleted)),
            },
            out_dir=os.path.join(part_dir, "tts"),
        )
        emit(
            "tts",
            f"{part_tag}: Voice check: OK, {tts_res.chunk_count} chunks, {tts_res.retry_count} retries",
            part_base + part_span * 0.17,
        )
        emit(
            "tts",
            f"{part_tag}: Narration & word timings ready ({tts_res.duration_sec:.1f}s, {len(tts_res.word_timings)} words)",
            part_base + part_span * 0.18,
        )

        # 2. Cards (18..28% of part span)
        emit("cards", f"{part_tag}: Rendering persistent scrolling Reddit post card...", part_base + part_span * 0.19)
        post_assets = render_scrolling_post_assets(
            post=part["post"],
            lines=part["lines"],
            out_dir=os.path.join(part_dir, "cards"),
            avatars_dir=avatars_dir,
            avatar_cache_dir=avatar_cache_dir,
        )
        emit("cards", f"{part_tag}: Scrolling card layers rendered.", part_base + part_span * 0.28)

        # 3. Subtitles (28..31% of part span)
        emit("subtitles", f"{part_tag}: Generating ASS word subtitles...", part_base + part_span * 0.29)
        ass_path = os.path.join(part_dir, "subtitles.ass")
        generate_ass_subtitles(tts_res.word_timings, ass_path, font_name="Lilita One")
        expected_script = [
            (w, str(ln["id"]))
            for ln in part["lines"]
            for w in clean_spoken_text(str(ln.get("text", "")))[0].split()
        ]
        cov_stats = verify_word_timing_coverage(
            tts_res.word_timings,
            expected_script,
            tts_res.duration_sec,
            tts_res.intended_pauses,
            max_gap_sec=1.5,
        )
        emit(
            "subtitles",
            f"{part_tag}: Timing check: OK, {cov_stats['word_count']} words covered "
            f"(max gap {cov_stats['max_unintended_gap_sec']:.2f}s)",
            part_base + part_span * 0.305,
        )
        emit("subtitles", f"{part_tag}: Subtitles generated.", part_base + part_span * 0.31)

        # 4. Background footage selection & prep (31..43% of part span)
        emit("background", f"{part_tag}: Preparing background footage...", part_base + part_span * 0.32)
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
            if specific_idx is not None:
                clip_entry = manifest_clips[(specific_idx + p_idx) % len(manifest_clips)]
            else:
                clip_entry = manifest_clips[cur_bg_idx % len(manifest_clips)]
                cur_bg_idx += 1
            clip_name = clip_entry["filename"]
            attribution = clip_entry.get("attribution", "")
            raw_bg = os.path.join(part_dir, clip_name)
            emit("background", f"{part_tag}: Selected background '{clip_name}'", part_base + part_span * 0.34)
            download_release_asset(repo, "backgrounds", clip_name, raw_bg, github_token)
            prepare_background_clip(raw_bg, prep_bg, tts_res.duration_sec)
        emit("background", f"{part_tag}: Background '{clip_name}' prepared ({tts_res.duration_sec:.1f}s)", part_base + part_span * 0.43)

        # 5. Compose MP4 (43..75% of part span)
        raw_out_mp4 = os.path.join(part_dir, f"{job_id}_part{p_num}_raw.mp4")
        out_filename = f"{job_id}_part{p_num}.mp4"
        out_mp4 = os.path.join(part_dir, out_filename)
        emit("compose", f"{part_tag}: Composing 1080x1920 video + card + subtitles...", part_base + part_span * 0.44)
        compose_scrolling_video(
            bg_video_path=prep_bg,
            audio_wav_path=tts_res.audio_path,
            ass_path=ass_path,
            fonts_dir=fonts_dir,
            post_assets=post_assets,
            word_timings=tts_res.word_timings,
            line_timings=tts_res.line_timings,
            out_mp4_path=raw_out_mp4,
            duration_sec=tts_res.duration_sec,
            progress_cb=lambda frac, pb=part_base, ps=part_span: emit_pct("compose", pb + ps * (0.44 + 0.31 * frac)),
        )
        raw_stats = probe_video_stats(raw_out_mp4)
        emit(
            "compose",
            f"{part_tag}: Compose complete ({raw_stats['size_bytes']/(1024*1024):.1f} MB, {raw_stats['bitrate_kbps']:.0f} kbps)",
            part_base + part_span * 0.76,
        )

        # 6. SF-010 Final Compression (76..99% of part span)
        emit("compress", f"{part_tag}: Compressing MP4 (CRF 24, 2800k maxrate, AAC 128k)...", part_base + part_span * 0.77)
        c_stats = compress_final_video(
            raw_out_mp4,
            out_mp4,
            duration_sec=tts_res.duration_sec,
            progress_cb=lambda frac, pb=part_base, ps=part_span: emit_pct("compress", pb + ps * (0.77 + 0.22 * frac)),
        )
        try:
            os.remove(raw_out_mp4)
        except OSError:
            pass
        before_mb = c_stats["before_bytes"] / (1024 * 1024)
        after_mb = c_stats["after_bytes"] / (1024 * 1024)
        emit(
            "compress",
            f"{part_tag}: Compressed {before_mb:.1f} MB ({c_stats['before_kbps']:.0f} kbps) -> "
            f"{after_mb:.1f} MB ({c_stats['after_kbps']:.0f} kbps)",
            part_base + part_span * 0.985,
        )
        fa_stats = verify_final_video_audio(
            out_mp4,
            tts_res.word_timings,
            part["lines"],
            tts_res.intended_pauses,
            tts_res.duration_sec,
            part_tag=part_tag,
        )
        emit(
            "compress",
            f"{part_tag}: Final audio check: OK (dur={fa_stats['duration_sec']}s, "
            f"speech={fa_stats['avg_speech_ac_db']} dBFS, min_window={fa_stats['min_speech_window_db']} dBFS)",
            part_base + part_span * 0.995,
        )

        rendered_parts.append(
            {
                "part_number": p_num,
                "title": part["title"],
                "description": part.get("description", ""),
                "hashtags": part.get("hashtags", []),
                "filename": out_filename,
                "local_path": out_mp4,
                "duration_sec": tts_res.duration_sec,
                "background_clip": clip_name,
                "attribution": attribution,
                "size_bytes": c_stats["after_bytes"],
            }
        )
    return rendered_parts, cur_bg_idx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-json", help="Run locally from a production JSON file")
    parser.add_argument("--local-bg", help="Local MP4 background clip for offline testing")
    parser.add_argument("--engine", choices=["edge", "voicestudio"], default="voicestudio")
    parser.add_argument("--voicestudio-speedup", type=float, default=1.25)
    parser.add_argument("--voice", default="vs-harper")
    parser.add_argument("--speed", default="+34%")
    parser.add_argument("--out-dir", default="/tmp/sf_render")
    args = parser.parse_args()

    if args.local_json:
        prod = json.load(open(args.local_json, "r", encoding="utf-8"))
        parts, _ = render_all_parts(
            job_id="localtest",
            production_json=prod,
            voice_id=args.voice,
            tts_engine_name=args.engine,
            coupled_speedup_factor=args.voicestudio_speedup,
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

    reporter = TaskReporter(task_id=job_id, worker_url=worker_url, secret=secret)
    stage_ref = ["job_started"]

    try:
        gh_run_id = os.environ.get("GITHUB_RUN_ID")
        init_payload: dict[str, Any] = {"job_id": job_id, "status": "running"}
        if gh_run_id:
            init_payload["github_run_id"] = gh_run_id
        worker_post_status(worker_url, secret, init_payload)
        job = worker_get_job(worker_url, job_id, secret)

        prod_json = job["production_json"]
        bg_mode = str(job.get("background_mode") or "sequential")
        bg_clip = job.get("background_clip")
        eff_engine = str(job.get("voice_engine") or "voicestudio")
        engine_label = "VoiceStudio" if eff_engine == "voicestudio" else "Edge TTS"
        reporter.log(
            "job_started",
            f"Render started: \"{prod_json.get('series_title', job_id)}\" "
            f"({len(prod_json.get('parts', []))} part(s), Voice: {engine_label} / {job['voice_id']}, bg_mode={bg_mode}"
            f"{f' clip={bg_clip}' if bg_clip else ''})",
            progress_pct=1.5,
        )
        reporter.flush()
        if reporter.deleted:
            print("JOB DELETED BEFORE RENDER:", job_id)
            return

        if not test_bg and bg_mode == "sequential":
            req = urllib.request.Request(
                worker_url.rstrip("/") + "/api/internal/background-index",
                headers={"X-Internal-Secret": secret, "User-Agent": "StoryForge-Runner"},
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                job["background_index"] = json.loads(response.read())["index"]

        parts, next_bg_idx = render_all_parts(
            job_id=job_id,
            production_json=prod_json,
            voice_id=job["voice_id"],
            tts_engine_name=eff_engine,
            coupled_speedup_factor=job.get("voicestudio_speedup"),
            voice_config=job.get("voice_config"),
            worker_url=worker_url,
            internal_secret=secret,
            voice_speed=job.get("voice_speed", "+34%"),
            bg_index=int(job.get("background_index", 0)),
            work_dir=args.out_dir,
            repo=repo,
            github_token=gh_token,
            test_background=test_bg,
            background_mode=bg_mode,
            background_clip=bg_clip,
            reporter=reporter,
            stage_ref=stage_ref,
        )

        if reporter.deleted:
            print("JOB DELETED DURING RENDER; SKIPPING UPLOAD:", job_id)
            return

        stage_ref[0] = "upload"
        reporter.log("upload", f"Uploading {len(parts)} rendered MP4 asset(s) to GitHub Release 'renders'...", progress_pct=90.0)
        rel = ensure_renders_release(repo, gh_token)
        public_parts: list[dict[str, Any]] = []
        for idx, p in enumerate(parts):
            pct = 90.0 + ((idx + 1) / max(1, len(parts))) * 8.0
            asset = upload_mp4_to_release(repo, gh_token, rel, p["local_path"], p["filename"])
            reporter.log(
                "upload",
                f"Uploaded {p['filename']} ({asset['size'] / (1024 * 1024):.1f} MB)",
                progress_pct=pct,
            )
            public_parts.append(
                {
                    "part_number": p["part_number"],
                    "title": p["title"],
                    "description": p.get("description", ""),
                    "hashtags": p["hashtags"],
                    "filename": p["filename"],
                    "asset_id": asset["id"],
                    "size_bytes": asset["size"],
                    "duration_sec": p["duration_sec"],
                    "background_clip": p["background_clip"],
                    "attribution": p["attribution"],
                }
            )

        reporter.update_progress("render_done", 100.0, "Render & upload complete. Handing off to publishing...")
        reporter.flush(status="done")

        worker_post_status(
            worker_url,
            secret,
            {
                "job_id": job_id,
                "status": "done",
                "parts": public_parts,
                **({"next_background_index": next_bg_idx} if not test_bg and bg_mode == "sequential" else {}),
            },
        )
        print("JOB COMPLETE:", job_id)
    except Exception as exc:
        err_short = f"[{stage_ref[0]}] {str(exc)[:420]}"
        err_msg = f"{err_short}\n{traceback.format_exc()[-1000:]}"
        print("JOB FAILED:", err_msg, file=sys.stderr)
        try:
            reporter.log(stage_ref[0], f"Render failed: {exc}", level="error")
            reporter.flush(status="failed", error=err_short)
        except Exception:
            pass
        try:
            worker_post_status(
                worker_url,
                secret,
                {"job_id": job_id, "status": "failed", "stage": stage_ref[0], "error": err_short[:500]},
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
