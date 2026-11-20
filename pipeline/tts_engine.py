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
import difflib
import inspect
import random
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol


MARKER_PATTERN = re.compile(r"\[(pause|sigh|laugh)\]", re.IGNORECASE)

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
class WordTiming:
    word: str
    start: float
    end: float
    line_id: str


@dataclass
class LineTiming:
    line_id: str
    start: float
    end: float


@dataclass
class TTSResult:
    audio_path: str
    duration_sec: float
    word_timings: list[WordTiming] = field(default_factory=list)
    line_timings: dict[str, LineTiming] = field(default_factory=dict)


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


def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


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

    s_norm = [_norm_word(w) for w, _ in script_words]
    w_norm = [_norm_word(w) for w, _, _ in whisper_words]

    sm = difflib.SequenceMatcher(None, s_norm, w_norm, autojunk=False)
    assigned: list[tuple[float, float] | None] = [None] * len(script_words)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                _, st, en = whisper_words[j1 + k]
                assigned[i1 + k] = (st, max(st + 0.04, en))
        elif tag == "replace":
            span_start = whisper_words[j1][1]
            span_end = whisper_words[j2 - 1][2]
            cnt = max(1, i2 - i1)
            step = max(0.04, (span_end - span_start) / cnt)
            for k in range(cnt):
                st = span_start + k * step
                assigned[i1 + k] = (st, st + step)

    # Interpolate any remaining gaps smoothly between neighbors
    n = len(script_words)
    i = 0
    while i < n:
        if assigned[i] is not None:
            i += 1
            continue
        gap_start_idx = i
        while i < n and assigned[i] is None:
            i += 1
        gap_end_idx = i  # exclusive

        prev_t = assigned[gap_start_idx - 1][1] if gap_start_idx > 0 else 0.0
        next_t = assigned[gap_end_idx][0] if gap_end_idx < n else total_duration
        if next_t <= prev_t:
            next_t = prev_t + 0.08 * (gap_end_idx - gap_start_idx)
        step = (next_t - prev_t) / (gap_end_idx - gap_start_idx)
        for k in range(gap_start_idx, gap_end_idx):
            st = prev_t + (k - gap_start_idx) * step
            assigned[k] = (st, st + step)

    result: list[WordTiming] = []
    last_end = 0.0
    for idx, (word_str, lid) in enumerate(script_words):
        st, en = assigned[idx]  # type: ignore[misc]
        st = max(last_end, st)
        en = max(st + 0.04, en)
        last_end = en
        result.append(WordTiming(word=word_str, start=round(st, 3), end=round(en, 3), line_id=lid))
    return result


class EdgeTTSEngine:
    """Phase 1 TTS engine using Microsoft Edge TTS + WordBoundary / faster-whisper fallback."""

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
        raw_word_timings: list[WordTiming] = []
        script_words: list[tuple[str, str]] = []
        line_word_ranges: list[tuple[str, float, float, list[str]]] = []

        cursor = 0.0
        for idx, line in enumerate(lines):
            lid = str(line["id"])
            emotion = str(line.get("emotion", "calm")).lower()
            explicit_markers = [str(m).lower() for m in line.get("markers", [])]
            clean_text, inline_markers = clean_spoken_text(str(line["text"]))
            all_markers = set(explicit_markers + inline_markers)

            if not clean_text:
                continue

            dr, _ = EMOTION_EDGE_TWEAKS.get(emotion, (0, 0))
            eff_rate = format_rate(base_rate_pct + dr)

            mp3_path = os.path.join(out_dir, f"line_{idx:03d}_{lid}.mp3")
            wav_path = os.path.join(out_dir, f"line_{idx:03d}_{lid}.wav")

            line_wb: list[tuple[str, float, float]] = []
            last_exc: Exception | None = None
            attempts = []
            if supports_boundary_kw:
                attempts.append({"rate": eff_rate, "boundary": "WordBoundary"})
            attempts.extend({"rate": eff_rate} for _ in range(5))

            for attempt_idx, kwargs in enumerate(attempts):
                if attempt_idx:
                    await asyncio.sleep(min(16, 2 ** attempt_idx) + random.uniform(0, 1))
                line_wb = []
                try:
                    comm = edge_tts.Communicate(clean_text, voice_id, **kwargs)
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
            await asyncio.sleep(0.6)

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
            line_dur = probe_duration(wav_path)
            wav_segments.append(wav_path)

            line_start = cursor
            line_end = cursor + line_dur
            line_timings[lid] = LineTiming(line_id=lid, start=round(line_start, 3), end=round(line_end, 3))

            words_in_line = clean_text.split()
            for w in words_in_line:
                script_words.append((w, lid))
            line_word_ranges.append((lid, line_start, line_end, words_in_line))

            for w_txt, wst, wen in line_wb:
                raw_word_timings.append(
                    WordTiming(
                        word=w_txt,
                        start=round(cursor + wst, 3),
                        end=round(cursor + wen, 3),
                        line_id=lid,
                    )
                )

            cursor = line_end

            # Optional pause/breath after line
            gap_sec = 0.0
            if "pause" in all_markers:
                gap_sec = 0.38
            elif "sigh" in all_markers or "laugh" in all_markers:
                gap_sec = 0.16
            elif idx < len(lines) - 1:
                gap_sec = 0.08

            if gap_sec > 0:
                sil_path = os.path.join(out_dir, f"sil_{idx:03d}.wav")
                make_silence_wav(sil_path, gap_sec, sample_rate=44100)
                wav_segments.append(sil_path)
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

        # Decide whether Edge WordBoundary gave us full word coverage
        expected_words = len(script_words)
        if expected_words > 0 and len(raw_word_timings) >= int(expected_words * 0.75):
            # Align Edge word timings cleanly to exact script words (preserves punctuation)
            edge_pairs = [(wt.word, wt.start, wt.end) for wt in raw_word_timings]
            word_timings = align_whisper_words_to_script(script_words, edge_pairs, total_dur)
        else:
            print(
                f"[tts] Edge WordBoundary returned {len(raw_word_timings)}/{expected_words} words; "
                "falling back to faster-whisper CPU alignment."
            )
            whisper_words = self._transcribe_with_faster_whisper(final_wav)
            word_timings = align_whisper_words_to_script(script_words, whisper_words, total_dur)

        return TTSResult(
            audio_path=final_wav,
            duration_sec=round(total_dur, 3),
            word_timings=word_timings,
            line_timings=line_timings,
        )

    def _transcribe_with_faster_whisper(self, audio_path: str) -> list[tuple[str, float, float]]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("Edge returned no word timings and faster-whisper is not installed.") from exc

        model = WhisperModel("base.en", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, word_timestamps=True, language="en")
        out: list[tuple[str, float, float]] = []
        for seg in segments:
            if not seg.words:
                continue
            for w in seg.words:
                txt = (w.word or "").strip()
                if txt:
                    out.append((txt, float(w.start), float(w.end)))
        return out


class VoiceStudioCoupledSpeedupHook:
    """
    Phase 2 hook (OFF for Edge TTS):
    Applies coupled pitch+speed speedup via asetrate=44100*F,aresample=44100
    and divides all word/line/card timestamps by F.
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
        return TTSResult(
            audio_path=dst,
            duration_sec=round(new_dur, 3),
            word_timings=scaled_words,
            line_timings=scaled_lines,
        )


def get_tts_engine(engine_name: str = "edge") -> TTSEngine:
    if engine_name == "edge":
        return EdgeTTSEngine()
    if engine_name == "voicestudio":
        from voicestudio import VoiceStudioEngine
        return VoiceStudioEngine()
    raise ValueError(f"Unsupported TTS engine: {engine_name}")
