"""Build normal 8-12s full-quality Release previews for Edge TTS and VoiceStudio.
No cloned or real-person voices are ever published to the public Release."""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import edge_tts

from tts_engine import probe_duration
from voicestudio import VoiceStudioEngine


def fetch_sample_config(default_text: str) -> tuple[str, float]:
    env_text = os.environ.get("SAMPLE_TEXT", "").strip()
    env_speed = os.environ.get("VOICESTUDIO_SPEEDUP", "").strip()
    worker_url = os.environ.get("WORKER_URL", "").strip()
    secret = os.environ.get("WORKER_INTERNAL_SECRET", "").strip()
    text = env_text or default_text
    speedup = 1.25
    if env_speed:
        try:
            speedup = max(1.0, min(2.0, float(env_speed)))
        except ValueError:
            pass
    elif worker_url and secret:
        try:
            req = urllib.request.Request(
                worker_url.rstrip("/") + "/api/internal/voice-sample-config",
                headers={"X-Internal-Secret": secret, "User-Agent": "StoryForge-Runner"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                cfg = json.loads(resp.read().decode("utf-8"))
                if isinstance(cfg.get("sample_text"), str) and cfg["sample_text"].strip():
                    text = cfg["sample_text"].strip()
                if cfg.get("voicestudio_speedup") is not None:
                    speedup = max(1.0, min(2.0, float(cfg["voicestudio_speedup"])))
        except Exception as exc:
            print(f"[samples] warn: could not fetch Worker sample config ({exc}); using repo default", flush=True)
    return text, speedup


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def quality_check_wav(wav_path: str, expected_text: str, whisper_model: Any | None) -> dict[str, Any]:
    import numpy as np
    import soundfile as sf

    audio, sr = sf.read(wav_path, dtype="float32")
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    dur = float(len(audio) / max(1, sr))
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    rms_db = float(20.0 * np.log10(max(rms, 1e-9)))
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    qc_ratio = 1.0
    transcript = ""
    if whisper_model is not None:
        segs, _ = whisper_model.transcribe(wav_path, language="en", vad_filter=False)
        transcript = " ".join((s.text or "").strip() for s in segs).strip()
        exp_w = _norm_words(expected_text)
        got_w = _norm_words(transcript)
        qc_ratio = difflib.SequenceMatcher(None, exp_w, got_w).ratio() if exp_w else 0.0
    ok = (4.0 <= dur <= 22.0) and (rms_db > -36.0) and (qc_ratio >= 0.72)
    return {
        "ok": ok,
        "duration_sec": round(dur, 2),
        "rms_db": round(rms_db, 1),
        "peak": round(peak, 3),
        "whisper_ratio": round(qc_ratio, 3),
        "transcript": transcript,
    }


async def _synth_edge_sample(sample_text: str, vid: str, rate: str, raw_mp3: Path) -> str:
    attempts = [rate, rate, rate, rate, "+0%", "+0%"]
    last_exc: Exception | None = None
    for idx, eff_rate in enumerate(attempts):
        if idx > 0:
            await asyncio.sleep(min(16.0, 2 ** idx) + random.uniform(0.0, 1.0))
        try:
            comm = edge_tts.Communicate(sample_text, vid, rate=eff_rate)
            audio_bytes = 0
            with open(raw_mp3, "wb") as f:
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio":
                        f.write(chunk["data"])
                        audio_bytes += len(chunk["data"])
            if audio_bytes <= 0:
                raise RuntimeError(f"Edge returned empty audio for {vid}")
            return eff_rate
        except Exception as exc:
            last_exc = exc
            print(f"[samples] Edge {vid} attempt {idx + 1}/{len(attempts)} (rate={eff_rate}) failed: {exc}", flush=True)
    raise RuntimeError(f"Edge sample failed for {vid}: {last_exc}") from last_exc


def main() -> None:
    output = Path(os.environ.get("SAMPLE_OUTPUT", "voice-sample-output"))
    output.mkdir(parents=True, exist_ok=True)
    voices_cfg = json.loads(Path("voices/voices.json").read_text(encoding="utf-8"))
    default_text = voices_cfg.get("sample_text_default") or (
        "I found the spare key taped inside the kitchen vent, right behind a stack of old takeout menus. "
        "For three weeks everyone swore nobody had been in that apartment, until I checked the hallway camera and saw the door open."
    )
    sample_text, speedup = fetch_sample_config(default_text)
    print(f"[samples] sample_text ({len(sample_text.split())} words), speedup={speedup:.2f}x", flush=True)

    edge_voices = voices_cfg.get("voices", [])
    vs_voices = voices_cfg.get("voicestudio_voices", [])
    vs_manifest: list[dict[str, Any]] = []
    edge_manifest: list[dict[str, Any]] = []

    whisper_model = None
    if os.environ.get("QC_WHISPER", "1") == "1" and os.environ.get("EDGE_ONLY") != "1":
        try:
            from faster_whisper import WhisperModel

            whisper_model = WhisperModel("base.en", device="cpu", compute_type="int8")
        except Exception as exc:
            print(f"[samples] warn: faster-whisper QC disabled ({exc})", flush=True)

    with tempfile.TemporaryDirectory() as work:
        # Run fast Edge TTS samples first so transient issues fail fast before 33m VoiceStudio run
        if os.environ.get("VS_ONLY") != "1":
            for v in edge_voices:
                vid = v["id"]
                raw_mp3 = Path(work) / f"{vid}_raw.mp3"
                mp3_name = f"{vid}.mp3"
                used_rate = asyncio.run(
                    _synth_edge_sample(sample_text, vid, v.get("default_rate", "+34%"), raw_mp3)
                )
                subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                        "-i", str(raw_mp3),
                        "-ar", "44100", "-ac", "1",
                        "-c:a", "libmp3lame", "-b:a", "128k",
                        str(output / mp3_name),
                    ],
                    check=True,
                )
                dur = round(probe_duration(str(output / mp3_name)), 2)
                print(f"[samples] Edge {vid} (rate={used_rate}): dur={dur}s", flush=True)
                edge_manifest.append(
                    {
                        "id": vid,
                        "engine": "edge",
                        "name": v["name"],
                        "descriptor": f"{v['gender']} · {v['accent']}",
                        "file": mp3_name,
                        "sample_file": mp3_name,
                        "duration_sec": dur,
                    }
                )
                time.sleep(0.7)

        if os.environ.get("EDGE_ONLY") != "1":
            engine = VoiceStudioEngine()
            for v in vs_voices:
                vid = v["id"]
                v_dir = Path(work) / vid
                v_dir.mkdir(parents=True, exist_ok=True)
                result = engine.synthesize_lines(
                    [{"id": "L1", "text": sample_text, "emotion": "calm"}],
                    vid,
                    {
                        "voicestudio_speedup": speedup,
                        "voice_config": {
                            "kind": "designed",
                            "id": vid,
                            "name": v["name"],
                            "instruct": v["instruct"],
                            "seed": int(v["seed"]),
                        },
                    },
                    str(v_dir),
                    align=False,
                )
                qc = quality_check_wav(result.audio_path, sample_text, whisper_model)
                print(
                    f"[samples] QC {vid} ({v['instruct']}): dur={qc['duration_sec']}s "
                    f"rms={qc['rms_db']}dB match={qc['whisper_ratio']} ok={qc['ok']}",
                    flush=True,
                )
                mp3_name = f"{vid}.mp3"
                subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                        "-i", result.audio_path,
                        "-c:a", "libmp3lame", "-b:a", "128k",
                        str(output / mp3_name),
                    ],
                    check=True,
                )
                vs_manifest.append(
                    {
                        "id": vid,
                        "engine": "voicestudio",
                        "name": v["name"],
                        "descriptor": v["descriptor"],
                        "instruct": v["instruct"],
                        "seed": int(v["seed"]),
                        "file": mp3_name,
                        "sample_file": mp3_name,
                        "duration_sec": qc["duration_sec"],
                        "qc_ok": qc["ok"],
                        "qc_whisper_ratio": qc["whisper_ratio"],
                        "qc_rms_db": qc["rms_db"],
                    }
                )

    manifest = vs_manifest + edge_manifest
    (output / "voices.json").write_text(
        json.dumps(
            {
                "sample_text": sample_text,
                "voicestudio_speedup": speedup,
                "voices": manifest,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[samples] Wrote {len(manifest)} samples ({len(vs_manifest)} VoiceStudio + {len(edge_manifest)} Edge) and manifest.", flush=True)


if __name__ == "__main__":
    main()
