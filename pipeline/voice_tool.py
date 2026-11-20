"""On-demand VoiceStudio helper for GitHub Actions:
1. design_preview: generate a normal 8-12s 128k MP3 preview for a custom Voice Design instruct+seed.
2. prepare_cloned: build & cache .pt VoiceClonePrompt(s) (with faster-whisper auto-transcription if blank)
   and generate 8-12s 128k MP3 previews for base + any uploaded per-emotion reference clips.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from progress import TaskReporter
from voicestudio import VoiceStudioEngine, _worker_request


def fetch_sample_config(worker_url: str, secret: str) -> tuple[str, float]:
    voices_cfg = json.loads((Path(__file__).resolve().parent.parent / "voices" / "voices.json").read_text(encoding="utf-8"))
    default_text = voices_cfg.get("sample_text_default") or (
        "I found the spare key taped inside the kitchen vent, right behind a stack of old takeout menus. "
        "For three weeks everyone swore nobody had been in that apartment, until I checked the hallway camera and saw the door open."
    )
    text = os.environ.get("SAMPLE_TEXT", "").strip() or default_text
    speedup = 1.25
    if worker_url and secret:
        status, body, _ = _worker_request(worker_url, secret, "/api/internal/voice-sample-config")
        if status == 200:
            cfg = json.loads(body.decode("utf-8"))
            if isinstance(cfg.get("sample_text"), str) and cfg["sample_text"].strip():
                text = cfg["sample_text"].strip()
            if cfg.get("voicestudio_speedup") is not None:
                speedup = max(1.0, min(2.0, float(cfg["voicestudio_speedup"])))
    return text, speedup


def upload_preview_mp3(worker_url: str, secret: str, qs_params: dict[str, str], wav_path: str, work_dir: str, tag: str) -> None:
    mp3_path = os.path.join(work_dir, f"{tag}.mp3")
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-i", wav_path,
            "-c:a", "libmp3lame", "-b:a", "128k",
            mp3_path,
        ],
        check=True,
    )
    mp3_bytes = Path(mp3_path).read_bytes()
    qs = urllib.parse.urlencode(qs_params)
    status, resp, _ = _worker_request(
        worker_url,
        secret,
        f"/api/internal/voice-preview?{qs}",
        method="POST",
        body=mp3_bytes,
        content_type="audio/mpeg",
        timeout=60,
    )
    if status >= 400:
        raise RuntimeError(f"Failed uploading preview MP3 ({status}): {resp.decode('utf-8', 'replace')[:300]}")


def run_design_preview(worker_url: str, secret: str, reporter: TaskReporter) -> None:
    instruct = os.environ["INSTRUCT"].strip()
    seed = int(os.environ.get("SEED") or "1001")
    preview_key = os.environ["PREVIEW_KEY"].strip()

    reporter.log("voice_sample", f"Generating Voice Design preview ({instruct}, seed={seed})...", progress_pct=15.0)
    sample_text, speedup = fetch_sample_config(worker_url, secret)
    engine = VoiceStudioEngine()
    with tempfile.TemporaryDirectory() as work_dir:
        res = engine.synthesize_lines(
            [{"id": "L1", "text": sample_text, "emotion": "calm"}],
            "draft_design",
            {
                "voicestudio_speedup": speedup,
                "voice_config": {"kind": "designed", "instruct": instruct, "seed": seed},
                "cancel_check": lambda: reporter.deleted,
            },
            work_dir,
            align=False,
        )
        if reporter.deleted:
            return
        reporter.log("upload", f"Uploading 128k MP3 preview ({res.duration_sec:.1f}s)...", progress_pct=85.0)
        upload_preview_mp3(worker_url, secret, {"key": preview_key}, res.audio_path, work_dir, "design_preview")
    reporter.update_progress("done", 100.0, "Voice sample preview ready.")
    reporter.flush(status="done")


def run_prepare_cloned(worker_url: str, secret: str, reporter: TaskReporter) -> None:
    voice_id = os.environ["VOICE_ID"].strip()
    qs = urllib.parse.urlencode({"voice_id": voice_id})
    status, body, _ = _worker_request(worker_url, secret, f"/api/internal/voice-config?{qs}")
    if status != 200:
        raise RuntimeError(f"Could not fetch voice config for {voice_id} (HTTP {status})")
    voice_cfg = json.loads(body.decode("utf-8"))
    slots: dict[str, Any] = voice_cfg.get("slots") or {}
    if "base" not in slots:
        raise RuntimeError(f"Cloned voice {voice_id} has no base reference clip.")

    sample_text, speedup = fetch_sample_config(worker_url, secret)
    slot_names = ["base"] + [k for k in slots.keys() if k != "base"]
    engine = VoiceStudioEngine()
    reporter.log("prepare", f"Preparing cloned voice '{voice_cfg.get('name', voice_id)}' ({len(slot_names)} clip slot(s))...", progress_pct=10.0)
    engine.prepare()

    with tempfile.TemporaryDirectory() as work_dir:
        prompt_cache = os.path.join(work_dir, "prompts")
        for idx, slot in enumerate(slot_names):
            if reporter.deleted:
                return
            pct_base = 15.0 + (idx / max(1, len(slot_names))) * 80.0
            reporter.log("prepare", f"Encoding & caching .pt clone prompt for slot '{slot}'...", progress_pct=pct_base)
            prompt_obj, transcript = engine.prepare_clone_prompt(
                voice_id=voice_id,
                slot=slot,
                slot_meta=slots[slot],
                cache_dir=prompt_cache,
                worker_url=worker_url,
                internal_secret=secret,
                force_recompute=True,
            )
            reporter.log("preview", f"Generating normal preview sample for slot '{slot}' (ref: {transcript[:60]!r})...", progress_pct=pct_base + 30.0 / max(1, len(slot_names)))
            slot_dir = os.path.join(work_dir, f"synth_{slot}")
            # Synthesize directly using the already-prepared prompt object
            import numpy as np
            import soundfile as sf
            import torch
            from tts_engine import TTSResult, VoiceStudioCoupledSpeedupHook

            with torch.inference_mode():
                wave = engine.model.generate(
                    text=sample_text,
                    voice_clone_prompt=prompt_obj,
                    num_step=16,
                )[0]
            wave = np.asarray(wave, dtype=np.float32).reshape(-1)
            os.makedirs(slot_dir, exist_ok=True)
            raw_24k = os.path.join(slot_dir, "raw.wav")
            nat_44k = os.path.join(slot_dir, "nat.wav")
            sf.write(raw_24k, wave, 24000, subtype="PCM_16")
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", raw_24k, "-ar", "44100", "-ac", "1", nat_44k], check=True)
            res = VoiceStudioCoupledSpeedupHook.apply_coupled_speedup(TTSResult(nat_44k, float(wave.size) / 24000.0), speedup)
            upload_preview_mp3(
                worker_url,
                secret,
                {"voice_id": voice_id, "slot": slot},
                res.audio_path,
                slot_dir,
                f"preview_{slot}",
            )
    reporter.update_progress("done", 100.0, "Cloned voice ready (prompt cached & preview generated).")
    reporter.flush(status="done")


def main() -> None:
    mode = os.environ["VOICE_TOOL_MODE"].strip()
    task_id = os.environ["TASK_ID"].strip()
    worker_url = os.environ["WORKER_URL"].strip()
    secret = os.environ["WORKER_INTERNAL_SECRET"].strip()
    reporter = TaskReporter(task_id=task_id, worker_url=worker_url, secret=secret)
    try:
        reporter.set_stage("running", 5.0, f"Voice tool started ({mode}).")
        if mode == "design_preview":
            run_design_preview(worker_url, secret, reporter)
        elif mode == "prepare_cloned":
            run_prepare_cloned(worker_url, secret, reporter)
        else:
            raise ValueError(f"Unsupported VOICE_TOOL_MODE: {mode}")
    except Exception as exc:
        err_short = str(exc)[:420]
        print("VOICE TOOL FAILED:", traceback.format_exc(), file=sys.stderr)
        try:
            reporter.log("failed", f"Voice tool failed: {exc}", level="error")
            reporter.flush(status="failed", error=err_short)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
