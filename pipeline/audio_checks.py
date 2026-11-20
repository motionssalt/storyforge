"""
Audio validation, chunk-anchored word alignment, and final MP4 voice checks for StoryForge.

Guards against:
1. Silent, muffled, DC-drifted, truncated, or garbled TTS chunks (per-chunk validation + retries).
2. Missing or collapsed word-timing spans (chunk-anchored alignment + coverage check + even fallback).
3. Final compressed MP4 audio dropouts or DC collapse compared against expected word timings.
"""

from __future__ import annotations

import array
import difflib
import math
import re
import subprocess
import wave
from dataclasses import dataclass
from typing import Any, Callable, Sequence


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


_ABBREV_RE = re.compile(
    r"(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|U\.S|U\.K|a\.m|p\.m)\.$",
    re.IGNORECASE,
)

_RETRY_CHAR_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2013": ", ",
        "\u2014": ", ",
        "\u2026": "...",
        "\u00a0": " ",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\ufeff": "",
    }
)


def norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def normalize_text_for_retry(text: str, level: int = 1) -> str:
    """Normalize odd punctuation/characters while keeping StoryForge [tags] intact."""
    txt = str(text or "").translate(_RETRY_CHAR_MAP)
    txt = txt.replace("&", " and ")
    # Remove unusual symbols/emojis that can confuse TTS models while keeping ASCII + [tags] + basic punctuation
    txt = re.sub(r"[^\w\s.,!?'\":;\-\[\]()/]", " ", txt)
    if level >= 2:
        txt = txt.replace('"', "").replace("(", ", ").replace(")", ", ")
        txt = txt.replace(";", ",").replace(":", ",")
        txt = re.sub(r"\s*-\s*", " ", txt)
    txt = re.sub(r"\s{2,}", " ", txt).strip()
    return txt


def split_long_segment_for_tts(seg_text: str, max_words: int = 18) -> list[str]:
    """
    Split multi-sentence segments exceeding `max_words` into sentence-sized sub-chunks
    so OmniVoice does not suffer late-chunk vocoder drift / DC offset / muffled collapse.
    """
    raw = str(seg_text or "").strip()
    if not raw:
        return []
    plain = re.sub(r"\[[a-z0-9_-]+\]", "", raw, flags=re.IGNORECASE).strip()
    words = plain.split()
    if len(words) <= max_words and len(plain) <= 150:
        return [raw]

    # Split at sentence boundaries while avoiding common abbreviations
    parts: list[str] = []
    cur_start = 0
    for m in re.finditer(r"([.!?][\"'\u201d\u2019\)]*)\s+(?=[A-Z0-9\"'\u201c\u2018(\[])", raw):
        prefix = raw[cur_start : m.end(1)].strip()
        last_tok = prefix.split()[-1] if prefix.split() else ""
        if _ABBREV_RE.search(last_tok):
            continue
        if prefix:
            parts.append(prefix)
        cur_start = m.end()
    tail = raw[cur_start:].strip()
    if tail:
        parts.append(tail)

    if len(parts) <= 1:
        parts = [raw]

    # If any single sentence is still very long (>26 words), split once near midpoint at clause punctuation
    refined: list[str] = []
    for p in parts:
        p_words = re.sub(r"\[[a-z0-9_-]+\]", "", p, flags=re.IGNORECASE).split()
        if len(p_words) <= 26:
            refined.append(p)
            continue
        mid = len(p) // 2
        best_pos = None
        best_dist = 10**9
        for cm in re.finditer(r"([,;:\u2014]|\s-\s)\s+", p):
            left_w = len(re.sub(r"\[[a-z0-9_-]+\]", "", p[: cm.start()], flags=re.IGNORECASE).split())
            right_w = len(re.sub(r"\[[a-z0-9_-]+\]", "", p[cm.end() :], flags=re.IGNORECASE).split())
            if left_w >= 6 and right_w >= 6:
                d = abs(cm.start() - mid)
                if d < best_dist:
                    best_dist = d
                    best_pos = cm
        if best_pos is not None:
            refined.append(p[: best_pos.end(1)].strip())
            refined.append(p[best_pos.end() :].strip())
        else:
            refined.append(p)

    # Merge tiny trailing fragments (<= 2 spoken words) back into the previous sub-chunk
    merged: list[str] = []
    for p in refined:
        p_words = re.sub(r"\[[a-z0-9_-]+\]", "", p, flags=re.IGNORECASE).split()
        if merged and len(p_words) <= 2:
            merged[-1] = f"{merged[-1]} {p}".strip()
        else:
            merged.append(p)
    return [m for m in merged if m]


def clean_omnivoice_wave(wave_arr: Any, sample_rate: int = 24000) -> tuple[Any, float]:
    """
    Remove DC bias and sub-sonic rumble (<65 Hz) from OmniVoice output,
    soft-limit peaks before PCM_16 encoding, and apply 3ms edge fades.
    Returns (cleaned_float32_ndarray, raw_dc_offset).
    """
    import numpy as np

    w = np.asarray(wave_arr, dtype=np.float32).reshape(-1)
    if w.size == 0:
        return w, 0.0
    raw_dc = float(np.mean(w))
    w = w - raw_dc

    # Zero-phase FFT high-pass filter (45 Hz .. 75 Hz raised-cosine transition)
    n = int(w.size)
    if n >= int(sample_rate * 0.08):
        freqs = np.fft.rfftfreq(n, d=1.0 / float(sample_rate))
        spec = np.fft.rfft(w)
        gain = np.ones_like(freqs, dtype=np.float32)
        low_cut = 45.0
        pass_freq = 75.0
        gain[freqs <= low_cut] = 0.0
        trans = (freqs > low_cut) & (freqs < pass_freq)
        if np.any(trans):
            u = (freqs[trans] - low_cut) / (pass_freq - low_cut)
            gain[trans] = (0.5 * (1.0 - np.cos(np.pi * u))).astype(np.float32)
        w = np.fft.irfft(spec * gain, n=n).astype(np.float32)
        w = w - float(np.mean(w))

    peak = float(np.max(np.abs(w))) if w.size else 0.0
    if peak > 0.96:
        w = (w * (0.96 / peak)).astype(np.float32)

    fade_n = min(n // 4, max(1, int(round(sample_rate * 0.003))))
    if fade_n > 1:
        ramp = (0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n, dtype=np.float32) / float(fade_n)))).astype(np.float32)
        w[:fade_n] *= ramp
        w[-fade_n:] *= ramp[::-1]

    return w, raw_dc


def load_wav_float32(wav_path: str) -> tuple[list[float], int]:
    """Read a mono/stereo PCM_16 WAV into mono float32 list in [-1, 1]."""
    with wave.open(wav_path, "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    if sw != 2:
        raise ValueError(f"Expected 16-bit PCM WAV at {wav_path}, got sample width {sw}")
    arr = array.array("h")
    arr.frombytes(raw)
    if ch > 1:
        mono = [
            sum(arr[i + c] for c in range(ch)) / (ch * 32768.0)
            for i in range(0, len(arr), ch)
        ]
    else:
        mono = [x / 32768.0 for x in arr]
    return mono, sr


def _to_float_list(samples: Any) -> list[float]:
    if hasattr(samples, "tolist"):
        return [float(x) for x in samples.reshape(-1).tolist()]
    return [float(x) for x in samples]


def analyze_chunk_energy(
    samples: Any,
    sample_rate: int,
    *,
    sil_thresh_db: float = -38.0,
    active_thresh_db: float = -40.0,
    speedup_factor: float = 1.0,
) -> dict[str, Any]:
    """Compute AC RMS, peak, active coverage, max internal silence, and worst 1.2s window."""
    if hasattr(samples, "dtype") and hasattr(samples, "reshape"):
        import numpy as np

        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        n = int(arr.size)
        if n == 0:
            return {
                "duration_sec": 0.0,
                "raw_dc": 0.0,
                "ac_rms_db": -180.0,
                "peak_db": -180.0,
                "active_frame_ratio": 0.0,
                "max_internal_silence_sec": 0.0,
                "worst_window_db": -180.0,
                "worst_window_range": (0.0, 0.0),
                "active_span_sec": 0.0,
            }
        raw_dc = float(np.mean(arr))
        ac = arr - raw_dc
        ac_rms = float(np.sqrt(np.mean(ac * ac)))
        peak = float(np.max(np.abs(ac)))
        ac_rms_db = 20.0 * math.log10(max(ac_rms, 1e-9))
        peak_db = 20.0 * math.log10(max(peak, 1e-9))

        frame_n = max(1, int(round(sample_rate * 0.05)))
        n_frames = n // frame_n
        if n_frames == 0:
            frame_dbs = [ac_rms_db]
        else:
            trimmed = ac[: n_frames * frame_n].reshape(n_frames, frame_n)
            f_means = np.mean(trimmed, axis=1, keepdims=True)
            f_ac = trimmed - f_means
            f_rms = np.sqrt(np.mean(f_ac * f_ac, axis=1))
            frame_dbs = (20.0 * np.log10(np.maximum(f_rms, 1e-9))).tolist()
    else:
        vals = _to_float_list(samples)
        n = len(vals)
        if n == 0:
            return {
                "duration_sec": 0.0,
                "raw_dc": 0.0,
                "ac_rms_db": -180.0,
                "peak_db": -180.0,
                "active_frame_ratio": 0.0,
                "max_internal_silence_sec": 0.0,
                "worst_window_db": -180.0,
                "worst_window_range": (0.0, 0.0),
                "active_span_sec": 0.0,
            }
        raw_dc = sum(vals) / n
        ac_vals = [x - raw_dc for x in vals]
        ac_rms = math.sqrt(sum(x * x for x in ac_vals) / n)
        peak = max(abs(x) for x in ac_vals)
        ac_rms_db = 20.0 * math.log10(max(ac_rms, 1e-9))
        peak_db = 20.0 * math.log10(max(peak, 1e-9))

        frame_n = max(1, int(round(sample_rate * 0.05)))
        frame_dbs = []
        for i in range(0, n - frame_n + 1, frame_n):
            seg = ac_vals[i : i + frame_n]
            m = sum(seg) / frame_n
            r = math.sqrt(sum((x - m) ** 2 for x in seg) / frame_n)
            frame_dbs.append(20.0 * math.log10(max(r, 1e-9)))
        if not frame_dbs:
            frame_dbs = [ac_rms_db]

    dur = n / float(max(1, sample_rate))
    active_indices = [idx for idx, db in enumerate(frame_dbs) if db >= active_thresh_db]
    active_ratio = len(active_indices) / float(max(1, len(frame_dbs)))

    max_sil_sec = 0.0
    worst_win_db = ac_rms_db
    worst_win_range = (0.0, dur)
    active_span_sec = 0.0

    if active_indices:
        first_a = active_indices[0]
        last_a = active_indices[-1]
        active_span_sec = (last_a - first_a + 1) * 0.05
        cur_sil = 0
        for idx in range(first_a, last_a + 1):
            if frame_dbs[idx] < sil_thresh_db:
                cur_sil += 1
                if cur_sil * 0.05 > max_sil_sec:
                    max_sil_sec = cur_sil * 0.05
            else:
                cur_sil = 0

        # Check sliding 1.2s*factor window inside active span for muffled collapse
        win_frames = max(1, int(round((1.2 * speedup_factor) / 0.05)))
        if last_a - first_a + 1 >= win_frames:
            worst_win_db = 0.0
            for idx in range(first_a, last_a - win_frames + 2):
                # Mean-square energy across frames in window
                ms = sum((10.0 ** (frame_dbs[j] / 10.0)) for j in range(idx, idx + win_frames)) / win_frames
                w_db = 10.0 * math.log10(max(ms, 1e-18))
                if w_db < worst_win_db:
                    worst_win_db = w_db
                    worst_win_range = (round(idx * 0.05, 2), round((idx + win_frames) * 0.05, 2))

    return {
        "duration_sec": dur,
        "raw_dc": raw_dc,
        "ac_rms_db": ac_rms_db,
        "peak_db": peak_db,
        "active_frame_ratio": active_ratio,
        "max_internal_silence_sec": max_sil_sec,
        "worst_window_db": worst_win_db,
        "worst_window_range": worst_win_range,
        "active_span_sec": active_span_sec,
    }


def validate_tts_chunk(
    samples: Any,
    sample_rate: int,
    spoken_text: str,
    *,
    sound_tag_count: int = 0,
    raw_dc_offset: float = 0.0,
    is_whisper_emotion: bool = False,
    is_voicestudio: bool = True,
    speedup_factor: float = 1.0,
    whisper_transcribe_fn: Callable[[], list[tuple[str, float, float]]] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Validate a single synthesized TTS chunk immediately after generation.
    Returns (ok, failure_reason, details_dict_with_whisper_words).
    """
    word_tokens = [norm_word(w) for w in spoken_text.split()]
    word_tokens = [w for w in word_tokens if w]
    n_words = len(word_tokens) if word_tokens else len(spoken_text.split())

    sil_thresh = -42.0 if is_whisper_emotion else -38.0
    act_thresh = -44.0 if is_whisper_emotion else -40.0
    stats = analyze_chunk_energy(
        samples,
        sample_rate,
        sil_thresh_db=sil_thresh,
        active_thresh_db=act_thresh,
        speedup_factor=speedup_factor,
    )
    dur = stats["duration_sec"]
    if dur <= 0.02:
        return False, "empty audio (0s)", stats

    if abs(raw_dc_offset) > 0.15:
        return False, f"excessive raw DC offset ({raw_dc_offset:+.3f})", stats

    # Duration bounds against expected speaking rate
    if is_voicestudio:
        min_dur = max(0.25, n_words * 0.16)
        max_dur = max(3.8, n_words * 0.80 + 2.4 + 1.5 * sound_tag_count)
    else:
        min_dur = max(0.20, n_words * 0.11)
        max_dur = max(3.5, n_words * 0.70 + 2.2 + 1.2 * sound_tag_count)

    if dur < min_dur or dur > max_dur:
        return (
            False,
            f"duration {dur:.2f}s outside expected [{min_dur:.2f}s, {max_dur:.2f}s] for {n_words} words",
            stats,
        )

    min_ac_rms = -37.0 if is_whisper_emotion else -33.0
    min_peak = -26.0 if is_whisper_emotion else -22.0
    if stats["ac_rms_db"] < min_ac_rms:
        return False, f"silent/muffled audio (AC RMS {stats['ac_rms_db']:.1f} dBFS < {min_ac_rms:.1f})", stats
    if stats["peak_db"] < min_peak:
        return False, f"low peak amplitude ({stats['peak_db']:.1f} dBFS < {min_peak:.1f})", stats
    if dur >= 0.5 and stats["active_frame_ratio"] < 0.25:
        return False, f"too little active speech ({stats['active_frame_ratio']*100:.0f}% frames active)", stats

    max_int_sil = 1.20 * speedup_factor
    if stats["max_internal_silence_sec"] > max_int_sil:
        return (
            False,
            f"internal silence {stats['max_internal_silence_sec']:.2f}s exceeds {max_int_sil:.2f}s",
            stats,
        )

    min_win_db = -39.5 if is_whisper_emotion else -35.5
    if stats["active_span_sec"] >= 1.35 * speedup_factor and stats["worst_window_db"] < min_win_db:
        t0, t1 = stats["worst_window_range"]
        return (
            False,
            f"muffled/low-energy stretch ({stats['worst_window_db']:.1f} dBFS) at {t0:.2f}s-{t1:.2f}s",
            stats,
        )

    whisper_words: list[tuple[str, float, float]] = []
    if whisper_transcribe_fn is not None:
        whisper_words = whisper_transcribe_fn()
        got_tokens = [norm_word(w) for w, _, _ in whisper_words]
        got_tokens = [w for w in got_tokens if w]
        if n_words >= 4 and len(got_tokens) == 0:
            stats["whisper_words"] = whisper_words
            return False, "Whisper recognized 0 words (garbled/inaudible)", stats
        if n_words >= 5:
            seq_ratio = difflib.SequenceMatcher(None, word_tokens, got_tokens, autojunk=False).ratio()
            got_set = set(got_tokens)
            recall = sum(1 for t in word_tokens if t in got_set) / float(len(word_tokens))
            if max(seq_ratio, recall) < 0.35:
                stats["whisper_words"] = whisper_words
                return (
                    False,
                    f"low speech intelligibility (match={seq_ratio:.2f}, recall={recall:.2f})",
                    stats,
                )

    stats["whisper_words"] = whisper_words
    return True, "ok", stats


def distribute_words_across_duration(
    chunk_words: Sequence[tuple[str, str]],
    chunk_dur_sec: float,
) -> list[WordTiming]:
    """Distribute word timings smoothly across [0, chunk_dur_sec] weighted by word length."""
    n = len(chunk_words)
    if n == 0:
        return []
    dur = max(0.04 * n, float(chunk_dur_sec))
    weights = [max(1, len(norm_word(w))) + 2.0 for w, _ in chunk_words]
    total_w = sum(weights)
    out: list[WordTiming] = []
    cur = 0.0
    for idx, ((word_str, lid), wt) in enumerate(zip(chunk_words, weights)):
        span = dur * (wt / total_w)
        st = cur
        en = dur if idx == n - 1 else min(dur, cur + span)
        en = max(st + 0.03, en)
        cur = en
        out.append(WordTiming(word=word_str, start=round(st, 3), end=round(en, 3), line_id=lid))
    return out


def align_whisper_words_to_chunk(
    chunk_words: Sequence[tuple[str, str]],
    whisper_words: Sequence[tuple[str, float, float]],
    chunk_dur_sec: float,
) -> list[WordTiming]:
    """
    Align Whisper word timestamps onto `chunk_words` strictly inside [0, chunk_dur_sec].
    Never squashes many unmatched script words into a tiny 0.04s window.
    """
    n = len(chunk_words)
    if n == 0:
        return []
    dur = max(0.04 * n, float(chunk_dur_sec))
    if not whisper_words:
        return distribute_words_across_duration(chunk_words, dur)

    s_norm = [norm_word(w) for w, _ in chunk_words]
    w_norm = [norm_word(w) for w, _, _ in whisper_words]

    sm = difflib.SequenceMatcher(None, s_norm, w_norm, autojunk=False)
    assigned: list[tuple[float, float] | None] = [None] * n

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                _, st, en = whisper_words[j1 + k]
                st = max(0.0, min(dur - 0.04, float(st)))
                en = max(st + 0.04, min(dur, float(en)))
                assigned[i1 + k] = (st, en)
        elif tag == "replace":
            cnt_s = i2 - i1
            cnt_w = j2 - j1
            if cnt_s <= 0 or cnt_w <= 0:
                continue
            span_start = max(0.0, min(dur - 0.04, float(whisper_words[j1][1])))
            span_end = max(span_start + 0.04, min(dur, float(whisper_words[j2 - 1][2])))
            # Only anchor a replace span directly if counts are comparable and words aren't crushed!
            if cnt_s <= max(3, cnt_w * 2) and (span_end - span_start) / cnt_s >= 0.06:
                step = (span_end - span_start) / cnt_s
                for k in range(cnt_s):
                    st = span_start + k * step
                    assigned[i1 + k] = (st, st + step)

    # Interpolate any unassigned spans smoothly between surrounding anchors (or 0..dur)
    i = 0
    while i < n:
        if assigned[i] is not None:
            i += 1
            continue
        gap_start = i
        while i < n and assigned[i] is None:
            i += 1
        gap_end = i
        prev_t = assigned[gap_start - 1][1] if gap_start > 0 else 0.0
        next_t = assigned[gap_end][0] if gap_end < n else dur
        if next_t <= prev_t + 0.03 * (gap_end - gap_start):
            # Not enough room before next anchor: clear colliding anchor if tail was squeezed,
            # or fall back to clean weighted distribution later.
            next_t = min(dur, prev_t + 0.08 * (gap_end - gap_start))
        step = (next_t - prev_t) / (gap_end - gap_start)
        for k in range(gap_start, gap_end):
            st = prev_t + (k - gap_start) * step
            assigned[k] = (st, st + step)

    out: list[WordTiming] = []
    last_end = 0.0
    for idx, (word_str, lid) in enumerate(chunk_words):
        st, en = assigned[idx]  # type: ignore[misc]
        st = max(last_end, min(dur, st))
        en = max(st + 0.04, min(dur + 0.02, en))
        last_end = en
        out.append(WordTiming(word=word_str, start=round(st, 3), end=round(en, 3), line_id=lid))
    return out


def _chunk_timings_valid(
    timings: Sequence[WordTiming],
    expected_count: int,
    chunk_dur_sec: float,
    max_gap_sec: float = 1.5,
) -> tuple[bool, str]:
    if len(timings) != expected_count:
        return False, f"word count mismatch ({len(timings)} != {expected_count})"
    if expected_count == 0:
        return True, "ok"
    if timings[0].start > max_gap_sec:
        return False, f"leading gap {timings[0].start:.2f}s > {max_gap_sec:.2f}s"
    if chunk_dur_sec - timings[-1].end > max_gap_sec:
        return False, f"trailing gap {chunk_dur_sec - timings[-1].end:.2f}s > {max_gap_sec:.2f}s"
    for i in range(len(timings) - 1):
        gap = timings[i + 1].start - timings[i].end
        if gap > max_gap_sec:
            return False, f"inter-word gap {gap:.2f}s > {max_gap_sec:.2f}s"
        if timings[i + 1].start < timings[i].start - 0.002:
            return False, "non-monotonic timestamps"

    # Detect collapsed word bursts (e.g. >= 4 words squashed to <=0.05s when chunk has normal pace)
    avg_dur = chunk_dur_sec / float(expected_count)
    if expected_count >= 4 and avg_dur > 0.10:
        tiny_run = 0
        for wt in timings:
            if (wt.end - wt.start) <= 0.055:
                tiny_run += 1
                if tiny_run >= 4:
                    return False, "collapsed burst of <=0.05s words"
            else:
                tiny_run = 0
    return True, "ok"


def verify_and_repair_chunk_timings(
    chunk_words: Sequence[tuple[str, str]],
    whisper_words: Sequence[tuple[str, float, float]],
    chunk_dur_sec: float,
    *,
    re_align_fn: Callable[[], list[tuple[str, float, float]]] | None = None,
    max_gap_sec: float = 1.5,
) -> tuple[list[WordTiming], str]:
    """
    Align chunk words, verify full coverage & no gaps > max_gap_sec.
    If invalid: re-run alignment once (if re_align_fn provided); as last resort
    distribute timings evenly across the chunk's real audio duration.
    """
    timings = align_whisper_words_to_chunk(chunk_words, whisper_words, chunk_dur_sec)
    ok, _ = _chunk_timings_valid(timings, len(chunk_words), chunk_dur_sec, max_gap_sec=max_gap_sec)
    if ok:
        return timings, "ok"

    if re_align_fn is not None:
        fresh_words = re_align_fn()
        timings2 = align_whisper_words_to_chunk(chunk_words, fresh_words, chunk_dur_sec)
        ok2, _ = _chunk_timings_valid(timings2, len(chunk_words), chunk_dur_sec, max_gap_sec=max_gap_sec)
        if ok2:
            return timings2, "realigned"

    return distribute_words_across_duration(chunk_words, chunk_dur_sec), "fallback_even"


def _intended_pause_overlap(
    t0: float,
    t1: float,
    intended_pauses: Sequence[tuple[float, float]],
) -> float:
    if t1 <= t0 or not intended_pauses:
        return 0.0
    total = 0.0
    for ps, pe in intended_pauses:
        ov = min(t1, pe) - max(t0, ps)
        if ov > 0.0:
            total += ov
    return total


def verify_word_timing_coverage(
    word_timings: Sequence[WordTiming],
    script_words: Sequence[tuple[str, str]],
    duration_sec: float,
    intended_pauses: Sequence[tuple[float, float]] = (),
    *,
    max_gap_sec: float = 1.5,
) -> dict[str, Any]:
    """Verify 100% script word coverage and no gap > max_gap_sec outside intended pauses."""
    if len(word_timings) != len(script_words):
        raise RuntimeError(
            f"Word-timing coverage check failed: {len(word_timings)} timed words vs {len(script_words)} script words."
        )
    if not word_timings:
        raise RuntimeError("Word-timing coverage check failed: 0 timed words.")

    max_unintended_gap = 0.0
    # Leading gap
    lead_gap = max(0.0, word_timings[0].start - _intended_pause_overlap(0.0, word_timings[0].start, intended_pauses))
    max_unintended_gap = max(max_unintended_gap, lead_gap)
    if lead_gap > max_gap_sec:
        raise RuntimeError(
            f"Word-timing coverage check failed: leading gap {lead_gap:.2f}s at 0.00s-{word_timings[0].start:.2f}s"
        )

    for i in range(len(word_timings)):
        wt = word_timings[i]
        exp_w, exp_lid = script_words[i]
        if wt.word != exp_w or wt.line_id != exp_lid:
            raise RuntimeError(
                f"Word-timing coverage check failed: mismatch at word #{i} ({wt.word!r}/{wt.line_id} != {exp_w!r}/{exp_lid})"
            )
        if wt.end <= wt.start:
            raise RuntimeError(f"Word-timing coverage check failed: invalid word span at {wt.word!r} ({wt.start:.3f}..{wt.end:.3f})")
        if i + 1 < len(word_timings):
            nxt = word_timings[i + 1]
            if nxt.start < wt.end - 0.005:
                raise RuntimeError(
                    f"Word-timing coverage check failed: overlapping words at {wt.start:.2f}s ({wt.word!r} -> {nxt.word!r})"
                )
            raw_gap = max(0.0, nxt.start - wt.end)
            unintended = max(0.0, raw_gap - _intended_pause_overlap(wt.end, nxt.start, intended_pauses))
            max_unintended_gap = max(max_unintended_gap, unintended)
            if unintended > max_gap_sec:
                raise RuntimeError(
                    f"Word-timing coverage check failed: unintended {unintended:.2f}s gap at {wt.end:.2f}s-{nxt.start:.2f}s"
                )

    tail_gap = max(
        0.0,
        duration_sec - word_timings[-1].end - _intended_pause_overlap(word_timings[-1].end, duration_sec, intended_pauses),
    )
    max_unintended_gap = max(max_unintended_gap, tail_gap)
    if tail_gap > max_gap_sec:
        raise RuntimeError(
            f"Word-timing coverage check failed: trailing gap {tail_gap:.2f}s at {word_timings[-1].end:.2f}s-{duration_sec:.2f}s"
        )

    return {
        "word_count": len(word_timings),
        "max_unintended_gap_sec": round(max_unintended_gap, 3),
    }


def extract_mp4_audio_16k(mp4_path: str) -> tuple[list[float], int]:
    """Decode audio stream from an MP4 file to 16 kHz mono float32 list via ffmpeg."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        mp4_path,
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "s16le",
        "pipe:1",
    ]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 or not res.stdout:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Could not extract audio from '{mp4_path}': {err or 'empty audio stream'}")
    arr = array.array("h")
    usable = len(res.stdout) - (len(res.stdout) % 2)
    arr.frombytes(res.stdout[:usable])
    return [x / 32768.0 for x in arr], 16000


def verify_final_video_audio(
    mp4_path: str,
    word_timings: Sequence[WordTiming],
    lines: Sequence[dict[str, Any]],
    intended_pauses: Sequence[tuple[float, float]],
    expected_duration_sec: float,
    *,
    part_tag: str = "Part 1",
    low_energy_db_threshold: float = -36.0,
    whisper_low_energy_db_threshold: float = -40.0,
    min_dropout_sec: float = 1.2,
    max_dc_offset: float = 0.15,
) -> dict[str, Any]:
    """
    Scan the finished MP4's actual audio track against expected word-timing speech regions.
    Fails loudly with exact time range if any stretch during expected speech has low/missing
    AC energy or severe DC offset.
    """
    samples, sr = extract_mp4_audio_16k(mp4_path)
    audio_dur = len(samples) / float(sr)
    if abs(audio_dur - expected_duration_sec) > 0.75:
        raise RuntimeError(
            f"Final audio check failed ({part_tag}): audio duration {audio_dur:.2f}s differs from expected {expected_duration_sec:.2f}s"
        )
    if not word_timings:
        raise RuntimeError(f"Final audio check failed ({part_tag}): no word timings present.")

    line_emotions = {
        str(ln.get("id")): str(ln.get("emotion") or "").strip().lower()
        for ln in lines
    }

    # Build merged expected-speech spans (merging consecutive words with gap <= 0.45s)
    speech_spans: list[tuple[float, float, bool]] = []
    for wt in word_timings:
        is_whisp = line_emotions.get(wt.line_id, "") == "whisper"
        if not speech_spans:
            speech_spans.append((wt.start, wt.end, is_whisp))
        else:
            ps, pe, pw = speech_spans[-1]
            # Split span if an intended pause sits between words or gap > 0.45s
            gap = wt.start - pe
            has_pause = _intended_pause_overlap(pe, wt.start, intended_pauses) > 0.05
            if gap <= 0.45 and not has_pause:
                speech_spans[-1] = (ps, max(pe, wt.end), pw or is_whisp)
            else:
                speech_spans.append((wt.start, wt.end, is_whisp))

    win_n = int(round(min_dropout_sec * sr))
    hop_n = int(round(0.2 * sr))

    worst_speech_db = 0.0
    speech_dbs: list[float] = []
    max_abs_dc = 0.0

    # Check DC offset across the entire audio track in 1.0s windows
    dc_win = int(round(1.0 * sr))
    for i in range(0, max(1, len(samples) - dc_win + 1), hop_n):
        seg = samples[i : i + dc_win]
        if len(seg) < dc_win // 2:
            continue
        dc = abs(sum(seg) / len(seg))
        if dc > max_abs_dc:
            max_abs_dc = dc
        if dc > max_dc_offset:
            t0 = i / float(sr)
            t1 = (i + len(seg)) / float(sr)
            raise RuntimeError(
                f"Final audio check failed ({part_tag}): severe DC offset ({dc:.3f}) at {t0:.2f}s-{t1:.2f}s"
            )

    # Scan each expected speech span >= min_dropout_sec
    for sp_start, sp_end, has_whisper in speech_spans:
        if sp_end - sp_start < min_dropout_sec:
            continue
        thresh = whisper_low_energy_db_threshold if has_whisper else low_energy_db_threshold
        i_start = max(0, int(round(sp_start * sr)))
        i_end = min(len(samples), int(round(sp_end * sr)))
        if i_end - i_start < win_n:
            continue
        bad_start: float | None = None
        bad_end: float = 0.0
        bad_worst_db: float = 0.0
        for i in range(i_start, i_end - win_n + 1, hop_n):
            seg = samples[i : i + win_n]
            mean = sum(seg) / len(seg)
            ac_rms = math.sqrt(sum((x - mean) ** 2 for x in seg) / len(seg))
            ac_db = 20.0 * math.log10(max(ac_rms, 1e-9))
            speech_dbs.append(ac_db)
            if ac_db < worst_speech_db:
                worst_speech_db = ac_db
            t0 = i / float(sr)
            t1 = (i + win_n) / float(sr)
            if ac_db < thresh:
                if bad_start is None:
                    bad_start = t0
                    bad_worst_db = ac_db
                bad_end = t1
                bad_worst_db = min(bad_worst_db, ac_db)
            else:
                if bad_start is not None:
                    raise RuntimeError(
                        f"Final audio check failed ({part_tag}): low/missing voice energy ({bad_worst_db:.1f} dBFS AC RMS) "
                        f"during expected speech at {bad_start:.2f}s-{bad_end:.2f}s"
                    )
        if bad_start is not None:
            raise RuntimeError(
                f"Final audio check failed ({part_tag}): low/missing voice energy ({bad_worst_db:.1f} dBFS AC RMS) "
                f"during expected speech at {bad_start:.2f}s-{bad_end:.2f}s"
            )

    avg_db = (sum(speech_dbs) / len(speech_dbs)) if speech_dbs else -22.0
    return {
        "duration_sec": round(audio_dur, 2),
        "avg_speech_ac_db": round(avg_db, 1),
        "min_speech_window_db": round(worst_speech_db, 1),
        "max_abs_dc": round(max_abs_dc, 4),
    }
