"""Headless VoiceStudio engine: OmniVoice, CPU only (designed + cloned voices)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from tts_engine import (
    EdgeTTSEngine,
    LineTiming,
    MARKER_PATTERN,
    TTSResult,
    VoiceStudioCoupledSpeedupHook,
    align_whisper_words_to_script,
    clean_spoken_text,
    probe_duration,
)

MODEL = "k2-fsa/OmniVoice"
REVISION = "3fc979d1cd72492cd679c5ac2611e0d7ac047ba1"

OMNIVOICE_SOUND_TAGS = {
    "laughter",
    "sigh",
    "confirmation-en",
    "question-en",
    "question-ah",
    "question-oh",
    "question-ei",
    "question-yi",
    "surprise-ah",
    "surprise-oh",
    "surprise-wa",
    "surprise-yo",
    "dissatisfaction-hnn",
}
PAUSE_SPLIT_RE = re.compile(r"\[pause\]", re.IGNORECASE)
LAUGH_ALIAS_RE = re.compile(r"\[laugh\]", re.IGNORECASE)
NON_PAUSE_TAG_RE = re.compile(
    r"\[(sigh|laughter|laugh|confirmation-en|question-en|question-ah|"
    r"question-oh|question-ei|question-yi|surprise-ah|surprise-oh|surprise-wa|"
    r"surprise-yo|dissatisfaction-hnn)\]",
    re.IGNORECASE,
)

PAUSE_GAP_SEC = 0.38
INTER_LINE_GAP_SEC = 0.08

_ROOT = Path(__file__).resolve().parent.parent


class TaskCancelledError(RuntimeError):
    """Raised when the StoryForge task was deleted/cancelled mid-render."""


def load_builtin_presets() -> dict[str, dict[str, Any]]:
    voices_file = _ROOT / "voices" / "voices.json"
    data = json.loads(voices_file.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for v in data.get("voicestudio_voices", []):
        vid = str(v.get("id", "")).strip()
        if not vid:
            continue
        out[vid] = {
            "id": vid,
            "kind": "designed",
            "name": str(v.get("name", vid)),
            "descriptor": str(v.get("descriptor", "")),
            "instruct": str(v.get("instruct", "")),
            "seed": int(v.get("seed", 1001)),
        }
    return out


# Keep module-level PRESETS dict for backward compatibility
PRESETS: dict[str, tuple[str, str, str]] = {
    vid: (item["name"], item["descriptor"], item["instruct"])
    for vid, item in load_builtin_presets().items()
}


def normalize_omnivoice_text(text: str) -> str:
    """Normalize inline StoryForge markers for OmniVoice (e.g. [laugh] -> [laughter])."""
    txt = LAUGH_ALIAS_RE.sub("[laughter]", str(text or ""))
    # Canonicalize supported tag names to lowercase
    def _canon(m: re.Match[str]) -> str:
        tag = m.group(1).lower()
        if tag == "laugh":
            tag = "laughter"
        return f"[{tag}]"

    txt = NON_PAUSE_TAG_RE.sub(_canon, txt)
    txt = re.sub(r"\s{2,}", " ", txt).strip()
    return txt


def _worker_request(
    worker_url: str,
    secret: str,
    path_and_query: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 60,
) -> tuple[int, bytes, dict[str, str]]:
    url = worker_url.rstrip("/") + path_and_query
    headers = {
        "X-Internal-Secret": secret,
        "User-Agent": "StoryForge-Runner",
    }
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in (exc.headers or {}).items()}


def transcribe_clip_with_whisper(wav_path: str) -> str:
    from faster_whisper import WhisperModel

    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(wav_path, language="en", vad_filter=False)
    text = " ".join((seg.text or "").strip() for seg in segments).strip()
    return re.sub(r"\s{2,}", " ", text).strip()


class VoiceStudioEngine:
    model = None

    def prepare(self):
        import torch
        from huggingface_hub import snapshot_download
        from omnivoice import OmniVoice

        if self.model is None:
            ts = time.monotonic()
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
            path = snapshot_download(MODEL, revision=REVISION)
            total = sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())
            print(f"[voicestudio] snapshot {total} bytes, available after {time.monotonic()-ts:.2f}s", flush=True)
            ts = time.monotonic()
            self.model = OmniVoice.from_pretrained(path, device_map="cpu", dtype=torch.float32).eval()
            print(f"[voicestudio] CPU model load {time.monotonic()-ts:.2f}s", flush=True)
        return self.model

    def prepare_clone_prompt(
        self,
        *,
        voice_id: str,
        slot: str,
        slot_meta: dict[str, Any],
        cache_dir: str,
        worker_url: str,
        internal_secret: str,
        force_recompute: bool = False,
    ) -> tuple[Any, str]:
        from omnivoice.models.omnivoice import VoiceClonePrompt

        model = self.prepare()
        os.makedirs(cache_dir, exist_ok=True)
        local_pt = os.path.join(cache_dir, f"{voice_id}_{slot}.pt")
        ref_text = str(slot_meta.get("ref_text") or "").strip()

        if not force_recompute and os.path.isfile(local_pt):
            prompt = VoiceClonePrompt.load(local_pt, map_location="cpu")
            return prompt, str(prompt.ref_text or ref_text)

        if not force_recompute and worker_url and internal_secret:
            qs = urllib.parse.urlencode({"voice_id": voice_id, "slot": slot})
            status, data, _ = _worker_request(worker_url, internal_secret, f"/api/internal/voice-prompt?{qs}")
            if status == 200 and len(data) > 64:
                with open(local_pt, "wb") as f:
                    f.write(data)
                prompt = VoiceClonePrompt.load(local_pt, map_location="cpu")
                print(f"[voicestudio] loaded cached .pt clone prompt for {voice_id}/{slot} ({len(data)} bytes)", flush=True)
                return prompt, str(prompt.ref_text or ref_text)

        # Download raw clip from private Worker KV store
        if not worker_url or not internal_secret:
            raise RuntimeError(f"Worker credentials missing; cannot fetch private voice clip {voice_id}/{slot}.")
        qs = urllib.parse.urlencode({"voice_id": voice_id, "slot": slot})
        status, clip_bytes, _ = _worker_request(worker_url, internal_secret, f"/api/internal/voice-clip?{qs}", timeout=90)
        if status != 200 or not clip_bytes:
            raise RuntimeError(f"Private voice clip {voice_id}/{slot} unavailable (HTTP {status}).")

        raw_path = os.path.join(cache_dir, f"{voice_id}_{slot}_raw.bin")
        wav_24k = os.path.join(cache_dir, f"{voice_id}_{slot}_24k.wav")
        with open(raw_path, "wb") as f:
            f.write(clip_bytes)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", raw_path, "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", wav_24k],
            check=True,
        )
        dur = probe_duration(wav_24k)
        if dur < 1.0:
            raise ValueError(f"Reference clip {voice_id}/{slot} is too short ({dur:.2f}s). Use a 3-10 second clip.")
        if dur > 15.0:
            print(f"[voicestudio] warn: reference clip {voice_id}/{slot} is {dur:.1f}s (>10s slows inference); trimming to 12s.", flush=True)
            trimmed = os.path.join(cache_dir, f"{voice_id}_{slot}_24k_trim.wav")
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", wav_24k, "-t", "12.0", "-c:a", "pcm_s16le", trimmed],
                check=True,
            )
            os.replace(trimmed, wav_24k)

        auto_transcribed = False
        if not ref_text:
            print(f"[voicestudio] auto-transcribing {voice_id}/{slot} with faster-whisper...", flush=True)
            ref_text = transcribe_clip_with_whisper(wav_24k)
            auto_transcribed = True
            if not ref_text:
                raise ValueError(f"Could not transcribe reference clip {voice_id}/{slot}. Enter a manual transcript.")
            print(f"[voicestudio] transcribed {voice_id}/{slot}: {ref_text!r}", flush=True)

        ts = time.monotonic()
        prompt = model.create_voice_clone_prompt(ref_audio=wav_24k, ref_text=ref_text, preprocess_prompt=True)
        prompt.save(local_pt)
        pt_bytes = Path(local_pt).read_bytes()
        print(f"[voicestudio] built .pt clone prompt for {voice_id}/{slot} in {time.monotonic()-ts:.2f}s ({len(pt_bytes)} bytes)", flush=True)

        # Cache .pt back into Worker KV so it is computed only once
        if worker_url and internal_secret:
            up_qs = urllib.parse.urlencode({
                "voice_id": voice_id,
                "slot": slot,
                "ref_text": prompt.ref_text or ref_text,
                "auto_transcribed": "1" if auto_transcribed else "0",
            })
            up_status, up_resp, _ = _worker_request(
                worker_url,
                internal_secret,
                f"/api/internal/voice-prompt?{up_qs}",
                method="POST",
                body=pt_bytes,
                content_type="application/octet-stream",
                timeout=60,
            )
            if up_status >= 400:
                print(f"[voicestudio] warn: failed caching .pt prompt to KV ({up_status}): {up_resp[:200]!r}", flush=True)

        return prompt, str(prompt.ref_text or ref_text)

    def _resolve_voice_spec(self, voice_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        cfg = settings.get("voice_config")
        if isinstance(cfg, dict) and cfg.get("kind") in ("designed", "cloned"):
            return cfg
        builtins = load_builtin_presets()
        if voice_id in builtins:
            return builtins[voice_id]
        raise ValueError(f"Unknown VoiceStudio voice '{voice_id}'. Choose a voice from the voice picker.")

    def synthesize_lines(self, lines, voice_id, settings, out_dir, *, align=True):
        import numpy as np
        import soundfile as sf
        import torch

        voice_spec = self._resolve_voice_spec(str(voice_id), settings or {})
        model = self.prepare()
        os.makedirs(out_dir, exist_ok=True)
        factor = float((settings or {}).get("voicestudio_speedup", 1.25))
        if not 1.0 <= factor <= 2.0:
            raise ValueError("VoiceStudio speed-up must be between 1.0 and 2.0.")

        cancel_check = (settings or {}).get("cancel_check")
        worker_url = str((settings or {}).get("worker_url") or os.environ.get("WORKER_URL") or "").strip()
        internal_secret = str((settings or {}).get("internal_secret") or os.environ.get("WORKER_INTERNAL_SECRET") or "").strip()
        num_step = int((settings or {}).get("num_step", 16))

        # Pre-load or lazily load clone prompts per slot
        clone_prompts: dict[str, Any] = {}
        is_cloned = voice_spec.get("kind") == "cloned"
        slots_map = voice_spec.get("slots") if is_cloned else {}
        if is_cloned:
            if not isinstance(slots_map, dict) or "base" not in slots_map:
                raise ValueError(f"Cloned voice '{voice_id}' is missing its base reference clip.")
            prompt_cache_dir = os.path.join(out_dir, "clone_prompts")
            needed_slots = {"base"}
            for line in lines:
                emo = str(line.get("emotion") or "").strip().lower()
                if emo and emo in slots_map:
                    needed_slots.add(emo)
            for slot in sorted(needed_slots):
                prompt_obj, _ = self.prepare_clone_prompt(
                    voice_id=str(voice_id),
                    slot=slot,
                    slot_meta=slots_map[slot],
                    cache_dir=prompt_cache_dir,
                    worker_url=worker_url,
                    internal_secret=internal_secret,
                )
                clone_prompts[slot] = prompt_obj

        script: list[tuple[str, str]] = []
        parts: list[Any] = []
        rate = 24000
        started = time.monotonic()

        for idx, line in enumerate(lines):
            if callable(cancel_check) and cancel_check():
                raise TaskCancelledError("Task was cancelled/deleted during VoiceStudio synthesis.")

            lid = str(line["id"])
            raw_text = str(line["text"])
            clean, inline_markers = clean_spoken_text(raw_text)
            script.extend((word, lid) for word in clean.split())

            emotion = str(line.get("emotion") or "").strip().lower()
            explicit_markers = [str(m).strip().lower() for m in (line.get("markers") or []) if m]
            extra_sound_tags = [
                ("laughter" if m == "laugh" else m)
                for m in explicit_markers
                if m != "pause" and m not in inline_markers and ("laughter" if m == "laugh" else m) in OMNIVOICE_SOUND_TAGS
            ]

            # Split at [pause] so [pause] becomes true raw silence; keep OmniVoice sound tags inline!
            segments = PAUSE_SPLIT_RE.split(raw_text)
            spoken_indices = [i for i, seg in enumerate(segments) if re.search(r"\w", clean_spoken_text(seg)[0])]
            last_spoken_idx = spoken_indices[-1] if spoken_indices else -1

            if is_cloned:
                chosen_slot = emotion if emotion in clone_prompts else "base"
                active_prompt = clone_prompts[chosen_slot]
                active_instruct = None
                active_seed = None
            else:
                active_prompt = None
                active_instruct = str(voice_spec.get("instruct") or "").strip()
                if emotion == "whisper" and "whisper" not in active_instruct.lower():
                    active_instruct = f"{active_instruct}, whisper" if active_instruct else "whisper"
                active_seed = int(voice_spec.get("seed", 1001))

            ended_with_pause = False
            for seg_i, seg in enumerate(segments):
                if seg_i > 0:
                    parts.append(np.zeros(round(rate * PAUSE_GAP_SEC), dtype=np.float32))
                    ended_with_pause = True
                seg_clean, _ = clean_spoken_text(seg)
                if not re.search(r"\w", seg_clean):
                    continue
                omni_text = normalize_omnivoice_text(seg)
                if seg_i == last_spoken_idx and extra_sound_tags:
                    omni_text = omni_text + " " + " ".join(f"[{t}]" for t in extra_sound_tags)
                try:
                    with torch.inference_mode():
                        if active_seed is not None:
                            torch.manual_seed(active_seed)
                        gen_kwargs: dict[str, Any] = {"text": omni_text, "num_step": num_step}
                        if active_prompt is not None:
                            gen_kwargs["voice_clone_prompt"] = active_prompt
                        else:
                            gen_kwargs["instruct"] = active_instruct
                        wave = model.generate(**gen_kwargs)[0]
                    wave = np.asarray(wave, dtype=np.float32).reshape(-1)
                    if not wave.size or not np.isfinite(wave).all():
                        raise ValueError("empty or invalid audio")
                    parts.append(wave)
                    ended_with_pause = False
                except Exception as exc:
                    raise RuntimeError(f"OmniVoice synthesis failed at {lid}: {exc}") from exc

            if "pause" in explicit_markers and "pause" not in inline_markers:
                parts.append(np.zeros(round(rate * PAUSE_GAP_SEC), dtype=np.float32))
                ended_with_pause = True
            elif not ended_with_pause and idx < len(lines) - 1:
                parts.append(np.zeros(round(rate * INTER_LINE_GAP_SEC), dtype=np.float32))

        if not parts or not script:
            raise ValueError("No narration was generated.")

        raw = os.path.join(out_dir, "omnivoice-24k.wav")
        natural = os.path.join(out_dir, "narration.wav")
        sf.write(raw, np.concatenate(parts), rate, subtype="PCM_16")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", raw, "-ar", "44100", "-ac", "1", natural],
            check=True,
        )
        result = VoiceStudioCoupledSpeedupHook.apply_coupled_speedup(
            TTSResult(natural, sum(a.size for a in parts) / rate),
            factor,
        )
        print(
            f"[voicestudio] generation and resampling {time.monotonic()-started:.2f}s; "
            f"{len(script)} words, final {result.duration_sec:.2f}s",
            flush=True,
        )
        if align:
            started = time.monotonic()
            words = EdgeTTSEngine()._transcribe_with_faster_whisper(result.audio_path)
            result.word_timings = align_whisper_words_to_script(script, words, result.duration_sec)
            for word in result.word_timings:
                if word.line_id not in result.line_timings:
                    result.line_timings[word.line_id] = LineTiming(word.line_id, word.start, word.end)
                else:
                    result.line_timings[word.line_id].end = word.end
            print(f"[voicestudio] final-audio alignment {time.monotonic()-started:.2f}s", flush=True)
        return result
