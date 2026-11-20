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

from audio_checks import (
    clean_omnivoice_wave,
    normalize_text_for_retry,
    split_long_segment_for_tts,
    validate_tts_chunk,
    verify_and_repair_chunk_timings,
    verify_word_timing_coverage,
)
from tts_engine import (
    EdgeTTSEngine,
    LineTiming,
    TTSResult,
    VoiceStudioCoupledSpeedupHook,
    WordTiming,
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
INTER_SENTENCE_GAP_SEC = 0.14
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
    _whisper_helper: EdgeTTSEngine | None = None

    def _get_whisper_helper(self) -> EdgeTTSEngine:
        if self._whisper_helper is None:
            self._whisper_helper = EdgeTTSEngine()
        return self._whisper_helper

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

        rate = 24000

        def _to_16k_fast(wave_24k: np.ndarray) -> np.ndarray:
            n_in = int(wave_24k.size)
            n_out = max(1, int(round(n_in * 16000.0 / (float(rate) * factor))))
            if n_in == 1:
                return np.repeat(wave_24k, n_out).astype(np.float32)
            x_old = np.arange(n_in, dtype=np.float64)
            x_new = np.linspace(0.0, float(n_in - 1), n_out)
            return np.interp(x_new, x_old, wave_24k).astype(np.float32)

        script: list[tuple[str, str]] = []
        parts: list[Any] = []
        raw_sample_cursor = 0
        raw_pauses: list[tuple[int, int]] = []
        chunk_records: list[dict[str, Any]] = []
        total_chunks = 0
        total_retries = 0
        started = time.monotonic()

        def _append_silence(sec: float) -> None:
            nonlocal raw_sample_cursor
            sil_n = round(rate * sec)
            if sil_n <= 0:
                return
            parts.append(np.zeros(sil_n, dtype=np.float32))
            raw_pauses.append((raw_sample_cursor, raw_sample_cursor + sil_n))
            raw_sample_cursor += sil_n

        for idx, line in enumerate(lines):
            if callable(cancel_check) and cancel_check():
                raise TaskCancelledError("Task was cancelled/deleted during VoiceStudio synthesis.")

            lid = str(line["id"])
            raw_text = str(line["text"])
            clean, inline_markers = clean_spoken_text(raw_text)
            if not re.search(r"\w", clean) and "pause" not in inline_markers and "pause" not in [
                str(m).strip().lower() for m in (line.get("markers") or [])
            ]:
                continue

            emotion = str(line.get("emotion") or "").strip().lower()
            explicit_markers = [str(m).strip().lower() for m in (line.get("markers") or []) if m]
            extra_sound_tags = [
                ("laughter" if m == "laugh" else m)
                for m in explicit_markers
                if m != "pause" and m not in inline_markers and ("laughter" if m == "laugh" else m) in OMNIVOICE_SOUND_TAGS
            ]

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
                    _append_silence(PAUSE_GAP_SEC)
                    ended_with_pause = True
                seg_clean, _ = clean_spoken_text(seg)
                if not re.search(r"\w", seg_clean):
                    continue

                sub_chunks = split_long_segment_for_tts(seg, max_words=18)
                for sub_i, sub_text in enumerate(sub_chunks):
                    if sub_i > 0:
                        _append_silence(INTER_SENTENCE_GAP_SEC)
                        ended_with_pause = True

                    sub_clean, sub_tags = clean_spoken_text(sub_text)
                    sub_words = [(w, lid) for w in sub_clean.split()]
                    if not sub_words:
                        continue
                    script.extend(sub_words)

                    sub_with_tags = sub_text
                    sub_tag_count = len([t for t in sub_tags if t != "pause"])
                    if seg_i == last_spoken_idx and sub_i == len(sub_chunks) - 1 and extra_sound_tags:
                        sub_with_tags = sub_with_tags + " " + " ".join(f"[{t}]" for t in extra_sound_tags)
                        sub_tag_count += len(extra_sound_tags)

                    chosen_wave = None
                    chosen_16k = None
                    chosen_whisper_words: list[tuple[str, float, float]] = []
                    last_reason = "unknown"

                    for attempt in range(3):
                        if callable(cancel_check) and cancel_check():
                            raise TaskCancelledError("Task was cancelled/deleted during VoiceStudio synthesis.")

                        if attempt == 0:
                            cand_text = normalize_omnivoice_text(sub_with_tags)
                            cand_seed = active_seed
                            cand_steps = num_step
                        elif attempt == 1:
                            cand_text = normalize_omnivoice_text(normalize_text_for_retry(sub_with_tags, level=1))
                            cand_seed = (active_seed if active_seed is not None else 1001) + 101
                            cand_steps = num_step
                        else:
                            cand_text = normalize_omnivoice_text(normalize_text_for_retry(sub_with_tags, level=2))
                            cand_seed = (active_seed if active_seed is not None else 1001) + 202
                            cand_steps = max(num_step, 20)

                        if not re.search(r"\w", clean_spoken_text(cand_text)[0]):
                            cand_text = normalize_omnivoice_text(sub_with_tags)

                        try:
                            with torch.inference_mode():
                                if cand_seed is not None:
                                    torch.manual_seed(int(cand_seed))
                                gen_kwargs: dict[str, Any] = {"text": cand_text, "num_step": cand_steps}
                                if active_prompt is not None:
                                    gen_kwargs["voice_clone_prompt"] = active_prompt
                                else:
                                    gen_kwargs["instruct"] = active_instruct
                                raw_wave = model.generate(**gen_kwargs)[0]
                            raw_wave = np.asarray(raw_wave, dtype=np.float32).reshape(-1)
                            if not raw_wave.size or not np.isfinite(raw_wave).all():
                                raise ValueError("empty or non-finite audio")
                        except Exception as exc:
                            last_reason = f"synthesis error: {exc}"
                            print(f"[voicestudio] chunk generation failed ({lid} try {attempt+1}/3): {last_reason}", flush=True)
                            continue

                        cleaned_wave, raw_dc = clean_omnivoice_wave(raw_wave, rate)
                        wave_16k = _to_16k_fast(cleaned_wave) if align else None
                        fast_dur = (cleaned_wave.size / float(rate)) / factor

                        def _whisper_fn(arr16k=wave_16k, mdur=fast_dur) -> list[tuple[str, float, float]]:
                            return self._get_whisper_helper()._transcribe_with_faster_whisper(
                                arr16k,
                                beam_size=1,
                                condition_on_previous_text=False,
                                pad_sec=0.25,
                                max_dur_sec=mdur,
                            )

                        ok, reason, stats = validate_tts_chunk(
                            cleaned_wave,
                            rate,
                            sub_clean,
                            sound_tag_count=sub_tag_count,
                            raw_dc_offset=raw_dc,
                            is_whisper_emotion=(emotion == "whisper"),
                            is_voicestudio=True,
                            speedup_factor=factor,
                            whisper_transcribe_fn=(_whisper_fn if align else None),
                        )
                        if ok:
                            chosen_wave = cleaned_wave
                            chosen_16k = wave_16k
                            chosen_whisper_words = stats.get("whisper_words", [])
                            total_chunks += 1
                            total_retries += attempt
                            break

                        last_reason = reason
                        print(
                            f"[voicestudio] chunk validation failed ({lid} chunk #{total_chunks + 1} try {attempt+1}/3): {reason}",
                            flush=True,
                        )

                    if chosen_wave is None:
                        raise RuntimeError(
                            f"OmniVoice synthesis failed validation at {lid} after 3 attempts: {last_reason}"
                        )

                    c_start_sample = raw_sample_cursor
                    parts.append(chosen_wave)
                    raw_sample_cursor += int(chosen_wave.size)
                    c_end_sample = raw_sample_cursor
                    ended_with_pause = False

                    chunk_records.append(
                        {
                            "lid": lid,
                            "words": sub_words,
                            "start_sample": c_start_sample,
                            "end_sample": c_end_sample,
                            "whisper_words": chosen_whisper_words,
                            "wave_16k": chosen_16k,
                        }
                    )

            if "pause" in explicit_markers and "pause" not in inline_markers:
                _append_silence(PAUSE_GAP_SEC)
                ended_with_pause = True
            elif not ended_with_pause and idx < len(lines) - 1:
                _append_silence(INTER_LINE_GAP_SEC)

        if not parts or not script:
            raise ValueError("No narration was generated.")

        raw_path = os.path.join(out_dir, "omnivoice-24k.wav")
        natural = os.path.join(out_dir, "narration.wav")
        sf.write(raw_path, np.concatenate(parts), rate, subtype="PCM_16")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", raw_path, "-ar", "44100", "-ac", "1", natural],
            check=True,
        )
        raw_total_dur = raw_sample_cursor / float(rate)
        result = VoiceStudioCoupledSpeedupHook.apply_coupled_speedup(
            TTSResult(natural, raw_total_dur),
            factor,
        )
        print(
            f"[voicestudio] generation and resampling {time.monotonic()-started:.2f}s; "
            f"{len(script)} words, {total_chunks} chunks ({total_retries} retries), final {result.duration_sec:.2f}s",
            flush=True,
        )

        result.chunk_count = total_chunks
        result.retry_count = total_retries

        if align:
            started_align = time.monotonic()
            scale = result.duration_sec / max(0.001, raw_total_dur)
            realign_count = 0
            fallback_count = 0
            word_timings: list[WordTiming] = []

            for rec in chunk_records:
                c_start = (rec["start_sample"] / float(rate)) * scale
                c_end = (rec["end_sample"] / float(rate)) * scale
                c_dur = max(0.04 * len(rec["words"]), c_end - c_start)

                def _realign_chunk(arr16k=rec["wave_16k"], mdur=c_dur) -> list[tuple[str, float, float]]:
                    if arr16k is None:
                        return []
                    return self._get_whisper_helper()._transcribe_with_faster_whisper(
                        arr16k,
                        beam_size=5,
                        condition_on_previous_text=False,
                        pad_sec=0.25,
                        max_dur_sec=mdur,
                    )

                aligned_local, status = verify_and_repair_chunk_timings(
                    rec["words"],
                    rec["whisper_words"],
                    c_dur,
                    re_align_fn=_realign_chunk,
                    max_gap_sec=1.5,
                )
                if status == "realigned":
                    realign_count += 1
                elif status == "fallback_even":
                    fallback_count += 1

                for wt in aligned_local:
                    word_timings.append(
                        WordTiming(
                            word=wt.word,
                            start=round(c_start + wt.start, 3),
                            end=round(c_start + wt.end, 3),
                            line_id=wt.line_id,
                        )
                    )

            intended_pauses = [
                (round((s / float(rate)) * scale, 3), round((e / float(rate)) * scale, 3))
                for s, e in raw_pauses
            ]
            cov = verify_word_timing_coverage(
                word_timings,
                script,
                result.duration_sec,
                intended_pauses,
                max_gap_sec=1.5,
            )

            result.word_timings = word_timings
            result.intended_pauses = intended_pauses
            result.max_unintended_gap_sec = cov["max_unintended_gap_sec"]
            for word in result.word_timings:
                if word.line_id not in result.line_timings:
                    result.line_timings[word.line_id] = LineTiming(word.line_id, word.start, word.end)
                else:
                    result.line_timings[word.line_id].end = word.end
            print(
                f"[voicestudio] chunk-anchored alignment {time.monotonic()-started_align:.2f}s; "
                f"Voice check: OK, {total_chunks} chunks, {total_retries} retries "
                f"(realigned={realign_count}, even_fallback={fallback_count}, max_gap={cov['max_unintended_gap_sec']:.2f}s)",
                flush=True,
            )

        return result
