"""
Swappable TTS Engine interface for StoryForge.

Fixed interface:
  synthesize_lines(lines, voice_id, settings, out_dir) -> TTSResult
    - audio_path: str (final WAV path)
    - duration_sec: float
    - word_timings: list[WordTiming(word, start, end, line_id)]
    - line_timings: dict[str, LineTiming(line_id, start, end)]
"""

from __future__ import annotations

import asyncio
import inspect
import os
import random
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

from audio_checks import (
    LineTiming,
    WordTiming,
    align_whisper_words_to_chunk,
    load_wav_float32,
    normalize_text_for_retry,
    validate_tts_chunk,
    verify_and_repair_chunk_timings,
    verify_word_timing_coverage,
)
from text_norm import (
    map_spoken_timings_to_display,
    prepare_alignment_expansion,
)


MARKER_PATTERN = re.compile(
    r"\[(pause|sigh|laughter|laugh|confirmation-en|question-en|question-ah|"
    r"question-oh|question-ei|question-yi|surprise-ah|surprise-oh|surprise-wa|"
    r"surprise-yo|dissatisfaction-hnn)\]",
    re.IGNORECASE,
)

# Subtle per-emotion Edge TTS rate/pitch tweaks (kept gentle for natural sound)
EMOTION_EDGE_TWEAKS: dict[str, tuple[int, int]] = {
    "calm": (0, 0),
    "tense": (3, 1),
    "angry": (6, 2),
    "shocked": (4, 3),
    "sad": (-6, -3),
    "sarcastic": (-2, 1),
    "whisper": (-8, -4),
    "excited": (7, 3),
}


@dataclass
class TTSResult:
    audio_path: str
    duration_sec: float
    word_timings: list[WordTiming] = field(default_factory=list)
    line_timings: dict[str, LineTiming] = field(default_factory=dict)
    chunk_count: int = 0
    retry_count: int = 0
    intended_pauses: list[tuple[float, float]] = field(default_factory=list)
    max_unintended_gap_sec: float = 0.0


class TTSEngine(Protocol):
    def synthesize_lines(
        self,
        lines: list[dict[str, Any]],
        voice_id: str,
        settings: dict[str, Any],
        out_dir: str,
    ) -> TTSResult:
        ...


def parse_rate_pct(rate_str: str | int | float | None, default: int = 35) -> int:
    if rate_str is None:
        return default
    if isinstance(rate_str, (int, float)):
        return int(round(rate_str))
    m = re.search(r"([+-]?\d+)", str(rate_str))
    return int(m.group(1)) if m else default


def format_rate(pct: int) -> str:
    pct = max(-50, min(65, int(pct)))
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


def format_pitch(hz: int) -> str:
    hz = max(-20, min(20, int(hz)))
    return f"+{hz}Hz" if hz >= 0 else f"{hz}Hz"


def probe_duration(path: str) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        text=True,
    ).strip()
    return float(out)


def make_silence_wav(out_path: str, duration_sec: float, sample_rate: int = 44100) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=mono",
            "-t",
            f"{duration_sec:.3f}",
            "-c:a",
            "pcm_s16le",
            out_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def clean_spoken_text(text: str) -> tuple[str, list[str]]:
    found_markers = [m.lower() for m in MARKER_PATTERN.findall(text)]
    cleaned = MARKER_PATTERN.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, found_markers


def align_whisper_words_to_script(
    script_words: list[tuple[str, str]],
    whisper_words: list[tuple[str, float, float]],
    total_duration: float,
) -> list[WordTiming]:
    """Align recognized Whisper word timestamps onto exact script words."""
    if not script_words:
        return []
    if not whisper_words:
        raise RuntimeError("No word boundaries or aligned transcription available; refusing to fabricate caption timings.")
    return align_whisper_words_to_chunk(script_words, whisper_words, total_duration)


class EdgeTTSEngine:
    """Phase 1 TTS engine using Microsoft Edge TTS + WordBoundary / faster-whisper fallback."""

    def __init__(self) -> None:
        self._whisper_model: Any = None

    def _get_whisper_model(self) -> Any:
        if self._whisper_model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError("faster-whisper is not installed.") from exc
            self._whisper_model = WhisperModel("base.en", device="cpu", compute_type="int8")
        return self._whisper_model

    def _transcribe_with_faster_whisper(
        self,
        audio_input: Any,
        *,
        beam_size: int = 1,
        condition_on_previous_text: bool = False,
        pad_sec: float = 0.0,
        max_dur_sec: float | None = None,
    ) -> list[tuple[str, float, float]]:
        model = self._get_whisper_model()
        transcribe_target = audio_input
        if pad_sec > 0.0 and not isinstance(audio_input, str):
            import numpy as np

            arr = np.asarray(audio_input, dtype=np.float32).reshape(-1)
            pad_n = int(round(pad_sec * 16000))
            if pad_n > 0:
                zeros = np.zeros(pad_n, dtype=np.float32)
                transcribe_target = np.concatenate([zeros, arr, zeros])

        segments, _ = model.transcribe(
            transcribe_target,
            word_timestamps=True,
            language="en",
            vad_filter=False,
            beam_size=beam_size,
            condition_on_previous_text=condition_on_previous_text,
        )
        out: list[tuple[str, float, float]] = []
        for seg in segments:
            if not seg.words:
                continue
            for w in seg.words:
                txt = (w.word or "").strip()
                if not txt:
                    continue
                st = float(w.start) - pad_sec
                en = float(w.end) - pad_sec
                if max_dur_sec is not None:
                    if en <= 0.0 or st >= max_dur_sec:
                        continue
                    st = max(0.0, min(max_dur_sec - 0.02, st))
                    en = max(st + 0.02, min(max_dur_sec, en))
                else:
                    st = max(0.0, st)
                    en = max(st + 0.02, en)
                out.append((txt, st, en))
        return out

    def synthesize_lines(
        self,
        lines: list[dict[str, Any]],
        voice_id: str,
        settings: dict[str, Any],
        out_dir: str,
    ) -> TTSResult:
        return asyncio.run(self._synthesize_async(lines, voice_id, settings, out_dir))

    async def _synthesize_async(
        self,
        lines: list[dict[str, Any]],
        voice_id: str,
        settings: dict[str, Any],
        out_dir: str,
    ) -> TTSResult:
        import edge_tts

        os.makedirs(out_dir, exist_ok=True)
        base_rate_pct = parse_rate_pct(settings.get("voice_speed"), default=35)
        supports_boundary_kw = "boundary" in inspect.signature(edge_tts.Communicate.__init__).parameters

        wav_segments: list[str] = []
        line_timings: dict[str, LineTiming] = {}
        script_words: list[tuple[str, str]] = []
        chunk_records: list[dict[str, Any]] = []
        intended_pauses: list[tuple[float, float]] = []

        total_chunks = 0
        total_retries = 0
        cursor = 0.0

        for idx, line in enumerate(lines):
            lid = str(line["id"])
            emotion = str(line.get("emotion", "calm")).lower()
            explicit_markers = [str(m).lower() for m in line.get("markers", [])]
            clean_text, inline_markers = clean_spoken_text(str(line["text"]))
            all_markers = set(explicit_markers + inline_markers)

            if not clean_text:
                continue

            display_words = [(w, lid) for w in clean_text.split()]
            spoken_words_line, disp_counts = prepare_alignment_expansion(display_words)
            spoken_text = " ".join(w for w, _ in spoken_words_line)

            dr, _ = EMOTION_EDGE_TWEAKS.get(emotion, (0, 0))
            eff_rate = format_rate(base_rate_pct + dr)

            mp3_path = os.path.join(out_dir, f"line_{idx:03d}_{lid}.mp3")
            wav_path = os.path.join(out_dir, f"line_{idx:03d}_{lid}.wav")

            line_wb: list[tuple[str, float, float]] = []
            line_dur = 0.0
            val_ok = False
            last_val_reason = "unknown"

            for val_try in range(3):
                if val_try == 0:
                    cand_text = spoken_text
                    cand_rate = eff_rate
                elif val_try == 1:
                    cand_text = normalize_text_for_retry(spoken_text, level=1) or spoken_text
                    cand_rate = eff_rate
                else:
                    cand_text = normalize_text_for_retry(spoken_text, level=2) or spoken_text
                    cand_rate = "+0%"

                last_exc: Exception | None = None
                attempts = []
                if supports_boundary_kw:
                    attempts.append({"rate": cand_rate, "boundary": "WordBoundary"})
                attempts.extend({"rate": cand_rate} for _ in range(5))

                for attempt_idx, kwargs in enumerate(attempts):
                    if attempt_idx:
                        await asyncio.sleep(min(16, 2 ** attempt_idx) + random.uniform(0, 1))
                    line_wb = []
                    try:
                        comm = edge_tts.Communicate(cand_text, voice_id, **kwargs)
                        audio_bytes = 0
                        with open(mp3_path, "wb") as f:
                            async for chunk in comm.stream():
                                ctype = chunk.get("type")
                                if ctype == "audio":
                                    f.write(chunk["data"])
                                    audio_bytes += len(chunk["data"])
                                elif ctype == "WordBoundary":
                                    offset_s = float(chunk["offset"]) / 10_000_000.0
                                    dur_s = float(chunk["duration"]) / 10_000_000.0
                                    w_txt = str(chunk.get("text", "")).strip()
                                    if w_txt:
                                        line_wb.append((w_txt, offset_s, offset_s + dur_s))
                        if audio_bytes <= 0:
                            raise RuntimeError(f"Edge returned empty audio for {lid}")
                        last_exc = None
                        break
                    except Exception as exc:
                        last_exc = exc
                        print(f"[tts] Attempt {attempt_idx+1} failed for {lid} ({kwargs}): {exc}")

                if last_exc is not None:
                    raise RuntimeError(f"Edge TTS failed for {lid} after {len(attempts)} attempts: {last_exc}") from last_exc
                await asyncio.sleep(0.4)

                # Convert to standard 44.1kHz mono PCM WAV
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        mp3_path,
                        "-ar",
                        "44100",
                        "-ac",
                        "1",
                        "-c:a",
                        "pcm_s16le",
                        wav_path,
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                samples, sr = load_wav_float32(wav_path)
                val_ok, last_val_reason, stats = validate_tts_chunk(
                    samples,
                    sr,
                    spoken_text,
                    sound_tag_count=0,
                    is_whisper_emotion=(emotion == "whisper"),
                    is_voicestudio=False,
                    speedup_factor=1.0,
                )
                if val_ok:
                    line_dur = stats["duration_sec"]
                    total_chunks += 1
                    total_retries += val_try
                    break
                print(
                    f"[tts] Edge chunk validation failed ({lid} try {val_try + 1}/3): {last_val_reason}",
                    flush=True,
                )

            if not val_ok:
                raise RuntimeError(
                    f"Edge TTS chunk failed validation at {lid} after 3 attempts: {last_val_reason}"
                )

            wav_segments.append(wav_path)
            line_start = cursor
            line_end = cursor + line_dur
            line_timings[lid] = LineTiming(line_id=lid, start=round(line_start, 3), end=round(line_end, 3))

            words_in_line = display_words
            script_words.extend(words_in_line)
            chunk_records.append(
                {
                    "lid": lid,
                    "wav_path": wav_path,
                    "start": line_start,
                    "end": line_end,
                    "dur": line_dur,
                    "words": words_in_line,
                    "spoken_words": spoken_words_line,
                    "disp_counts": disp_counts,
                    "wb": list(line_wb),
                }
            )
            cursor = line_end

            # Optional pause/breath after line
            gap_sec = 0.0
            if "pause" in all_markers:
                gap_sec = 0.38
            elif "sigh" in all_markers or "laugh" in all_markers or "laughter" in all_markers:
                gap_sec = 0.16
            elif idx < len(lines) - 1:
                gap_sec = 0.08

            if gap_sec > 0:
                sil_path = os.path.join(out_dir, f"sil_{idx:03d}.wav")
                make_silence_wav(sil_path, gap_sec, sample_rate=44100)
                wav_segments.append(sil_path)
                intended_pauses.append((round(cursor, 3), round(cursor + gap_sec, 3)))
                cursor += gap_sec

        # Concatenate all WAVs
        concat_list = os.path.join(out_dir, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for seg in wav_segments:
                f.write(f"file '{os.path.abspath(seg)}'\n")

        final_wav = os.path.join(out_dir, "narration.wav")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list,
                "-c",
                "copy",
                final_wav,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        total_dur = probe_duration(final_wav)

        # Chunk-anchored alignment per line
        word_timings: list[WordTiming] = []
        realign_count = 0
        fallback_count = 0

        for rec in chunk_records:
            c_words = rec["words"]
            c_spoken = rec["spoken_words"]
            c_counts = rec["disp_counts"]
            c_dur = rec["dur"]
            c_start = rec["start"]
            c_wb = rec["wb"]
            if len(c_wb) >= max(1, int(len(c_spoken) * 0.75)):
                initial_raw = c_wb
            else:
                initial_raw = self._transcribe_with_faster_whisper(rec["wav_path"], beam_size=1)

            def _realign_chunk(wp: str = rec["wav_path"]) -> list[tuple[str, float, float]]:
                return self._transcribe_with_faster_whisper(
                    wp, beam_size=5, condition_on_previous_text=False
                )

            aligned_spoken, status = verify_and_repair_chunk_timings(
                c_spoken,
                initial_raw,
                c_dur,
                re_align_fn=_realign_chunk,
                max_gap_sec=1.5,
            )
            if status == "realigned":
                realign_count += 1
            elif status == "fallback_even":
                fallback_count += 1
            aligned_local = map_spoken_timings_to_display(c_words, aligned_spoken, c_counts)

            for wt in aligned_local:
                word_timings.append(
                    WordTiming(
                        word=wt.word,
                        start=round(c_start + wt.start, 3),
                        end=round(c_start + wt.end, 3),
                        line_id=wt.line_id,
                    )
                )

        cov = verify_word_timing_coverage(
            word_timings,
            script_words,
            total_dur,
            intended_pauses,
            max_gap_sec=1.5,
        )
        print(
            f"[tts] Voice check: OK, {total_chunks} chunks, {total_retries} retries "
            f"(realigned={realign_count}, even_fallback={fallback_count}, max_gap={cov['max_unintended_gap_sec']:.2f}s)",
            flush=True,
        )

        return TTSResult(
            audio_path=final_wav,
            duration_sec=round(total_dur, 3),
            word_timings=word_timings,
            line_timings=line_timings,
            chunk_count=total_chunks,
            retry_count=total_retries,
            intended_pauses=intended_pauses,
            max_unintended_gap_sec=cov["max_unintended_gap_sec"],
        )



class VoiceStudioCoupledSpeedupHook:
    """
    Phase 2 hook (OFF for Edge TTS):
    Applies coupled pitch+speed speedup via asetrate=44100*F,aresample=44100
    and divides all word/line/pause timestamps by F.
    """

    @staticmethod
    def apply_coupled_speedup(result: TTSResult, factor: float = 1.25) -> TTSResult:
        if factor == 1.0:
            return result
        src = result.audio_path
        dst = os.path.join(os.path.dirname(src), "narration_fast.wav")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                src,
                "-af",
                f"asetrate=44100*{factor:.4f},aresample=44100",
                "-c:a",
                "pcm_s16le",
                dst,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        new_dur = probe_duration(dst)
        scaled_words = [
            WordTiming(
                word=w.word,
                start=round(w.start / factor, 3),
                end=round(w.end / factor, 3),
                line_id=w.line_id,
            )
            for w in result.word_timings
        ]
        scaled_lines = {
            lid: LineTiming(
                line_id=lt.line_id,
                start=round(lt.start / factor, 3),
                end=round(lt.end / factor, 3),
            )
            for lid, lt in result.line_timings.items()
        }
        scaled_pauses = [
            (round(s / factor, 3), round(e / factor, 3))
            for s, e in result.intended_pauses
        ]
        return TTSResult(
            audio_path=dst,
            duration_sec=round(new_dur, 3),
            word_timings=scaled_words,
            line_timings=scaled_lines,
            chunk_count=result.chunk_count,
            retry_count=result.retry_count,
            intended_pauses=scaled_pauses,
            max_unintended_gap_sec=round(result.max_unintended_gap_sec / factor, 3),
        )


def get_tts_engine(engine_name: str = "voicestudio") -> TTSEngine:
    if engine_name == "edge":
        return EdgeTTSEngine()
    if engine_name == "voicestudio":
        from voicestudio import VoiceStudioEngine
        return VoiceStudioEngine()
    raise ValueError(f"Unsupported TTS engine: {engine_name}")
