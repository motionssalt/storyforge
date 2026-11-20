"""Unit and fault-injection tests for StoryForge audio validation, alignment repair, and final MP4 audio checks."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_checks import (  # noqa: E402
    WordTiming,
    align_whisper_words_to_chunk,
    normalize_text_for_retry,
    split_long_segment_for_tts,
    validate_tts_chunk,
    verify_and_repair_chunk_timings,
    verify_final_video_audio,
    verify_word_timing_coverage,
)


def _sine_speech(dur_sec: float, sr: int = 24000, amp: float = 0.2, dc: float = 0.0, freq: float = 220.0) -> list[float]:
    n = int(round(dur_sec * sr))
    return [dc + amp * math.sin(2.0 * math.pi * freq * (i / sr)) for i in range(n)]


class TestAudioChecks(unittest.TestCase):
    def test_split_long_segment(self) -> None:
        long_txt = (
            "I told him I wasn't asking him to love me. "
            "I wasn't even asking him to apologize. "
            "I just wanted him to stop pretending my mother had left me with nothing."
        )
        chunks = split_long_segment_for_tts(long_txt, max_words=18)
        self.assertEqual(len(chunks), 3)
        self.assertIn("love me.", chunks[0])
        self.assertIn("apologize.", chunks[1])
        self.assertIn("nothing.", chunks[2])

        short_txt = "She walked out the door without saying a word."
        self.assertEqual(split_long_segment_for_tts(short_txt, max_words=18), [short_txt])

    def test_normalize_text_for_retry(self) -> None:
        raw = "“Wait—what?” she asked… & then left (alone)."
        lvl1 = normalize_text_for_retry(raw, level=1)
        self.assertNotIn("—", lvl1)
        self.assertNotIn("“", lvl1)
        self.assertIn(" and ", lvl1)
        lvl2 = normalize_text_for_retry(raw, level=2)
        self.assertNotIn('"', lvl2)
        self.assertNotIn("(", lvl2)

    def test_per_chunk_validation_catches_faults(self) -> None:
        sr = 24000
        text = "Every night I kept that faded photograph beside my bed."

        # 1. Valid normal speech
        good = _sine_speech(3.2, sr=sr, amp=0.18)
        ok, reason, _ = validate_tts_chunk(good, sr, text, is_voicestudio=True, speedup_factor=1.25)
        self.assertTrue(ok, reason)

        # 2. Silent chunk
        silent = [0.0] * int(3.2 * sr)
        ok, reason, _ = validate_tts_chunk(silent, sr, text, is_voicestudio=True, speedup_factor=1.25)
        self.assertFalse(ok)
        self.assertIn("silent", reason.lower())

        # 3. Muffled low-energy chunk (-42 dBFS)
        muffled = _sine_speech(3.2, sr=sr, amp=0.01)
        ok, reason, _ = validate_tts_chunk(muffled, sr, text, is_voicestudio=True, speedup_factor=1.25)
        self.assertFalse(ok)
        self.assertIn("silent/muffled", reason.lower())

        # 4. Excessive raw DC offset (like sf_mwp1r544_c31aa8 part 1 & 2)
        ok, reason, _ = validate_tts_chunk(good, sr, text, raw_dc_offset=-0.24, is_voicestudio=True, speedup_factor=1.25)
        self.assertFalse(ok)
        self.assertIn("dc offset", reason.lower())

        # 5. Long internal silence (> 1.5s)
        with_hole = _sine_speech(1.2, sr=sr, amp=0.2) + [0.0] * int(2.0 * sr) + _sine_speech(1.2, sr=sr, amp=0.2)
        ok, reason, _ = validate_tts_chunk(with_hole, sr, text, is_voicestudio=True, speedup_factor=1.25)
        self.assertFalse(ok)
        self.assertIn("internal silence", reason.lower())

        # 6. Mid-chunk muffled collapse (2.2s at -36.5 dBFS inside active speech: above -38 silence threshold, below -35.5 window threshold)
        muffled_hole = _sine_speech(1.5, sr=sr, amp=0.2) + _sine_speech(2.2, sr=sr, amp=0.021) + _sine_speech(1.5, sr=sr, amp=0.2)
        ok, reason, _ = validate_tts_chunk(muffled_hole, sr, text, is_voicestudio=True, speedup_factor=1.25)
        self.assertFalse(ok)
        self.assertIn("muffled", reason.lower())

        # 7. Duration too short
        too_short = _sine_speech(0.4, sr=sr, amp=0.2)
        ok, reason, _ = validate_tts_chunk(too_short, sr, text, is_voicestudio=True, speedup_factor=1.25)
        self.assertFalse(ok)
        self.assertIn("duration", reason.lower())

        # 8. Garbled speech (Whisper returns 0 words)
        ok, reason, _ = validate_tts_chunk(
            good, sr, text, is_voicestudio=True, speedup_factor=1.25, whisper_transcribe_fn=lambda: []
        )
        self.assertFalse(ok)
        self.assertIn("0 words", reason.lower())

    def test_alignment_repair_fixes_missing_and_collapsed_regions(self) -> None:
        words_str = (
            "in.' So I stopped asking. I studied, took small jobs whenever I could, "
            "and kept one photograph of my mother beside my bed. Every night, I'd tell her about my day."
        ).split()
        chunk_words = [(w, "L10") for w in words_str]
        chunk_dur = 8.0

        # 1. Stray single Whisper match: align_whisper_words_to_chunk itself refuses to squash 31 words into 0.04s
        stray_whisper = [("in", 0.0, 0.04)]
        repaired, status = verify_and_repair_chunk_timings(
            chunk_words,
            stray_whisper,
            chunk_dur,
            re_align_fn=lambda: [],
            max_gap_sec=1.5,
        )
        self.assertEqual(len(repaired), len(chunk_words))
        self.assertGreater(repaired[-1].end, 7.5)
        cov = verify_word_timing_coverage(repaired, chunk_words, chunk_dur, [], max_gap_sec=1.5)
        self.assertLessEqual(cov["max_unintended_gap_sec"], 0.3)

        # 2. Collapsed Whisper anchors (multiple words jammed into 0.04s leaving a 6.5s gap)
        # Force a collapse in initial Whisper timestamps -> verify_and_repair_chunk_timings must re-align or fallback_even!
        collapsed_whisper = [("in", 0.0, 0.04), ("so", 0.04, 0.08), ("i", 0.08, 0.12), ("stopped", 0.12, 0.16), ("asking", 0.16, 0.20), ("day", 7.80, 7.95)]
        repaired_fallback, status_fb = verify_and_repair_chunk_timings(
            chunk_words,
            collapsed_whisper,
            chunk_dur,
            re_align_fn=lambda: collapsed_whisper,  # simulate re-run also returning collapsed timings
            max_gap_sec=1.5,
        )
        self.assertEqual(status_fb, "fallback_even")
        self.assertEqual(len(repaired_fallback), len(chunk_words))
        verify_word_timing_coverage(repaired_fallback, chunk_words, chunk_dur, [], max_gap_sec=1.5)

        # 3. Re-align succeeds on retry when initial Whisper missed the region
        recovered_whisper = [(w, i * 0.25, i * 0.25 + 0.22) for i, w in enumerate(words_str)]
        repaired2, status2 = verify_and_repair_chunk_timings(
            chunk_words,
            collapsed_whisper,
            chunk_dur,
            re_align_fn=lambda: recovered_whisper,
            max_gap_sec=1.5,
        )
        self.assertEqual(status2, "realigned")
        self.assertEqual(len(repaired2), len(chunk_words))
        verify_word_timing_coverage(repaired2, chunk_words, chunk_dur, [], max_gap_sec=1.5)

    def test_coverage_check_respects_intended_pauses_and_catches_bad_gaps(self) -> None:
        words = [("Wait", "L1"), ("listen", "L2")]
        wts = [WordTiming("Wait", 0.0, 0.4, "L1"), WordTiming("listen", 2.4, 2.9, "L2")]
        cov = verify_word_timing_coverage(wts, words, 3.0, [(0.45, 2.25)], max_gap_sec=1.5)
        self.assertLessEqual(cov["max_unintended_gap_sec"], 0.25)

        with self.assertRaises(RuntimeError):
            verify_word_timing_coverage(wts, words, 3.0, [], max_gap_sec=1.5)

    def test_final_mp4_audio_check_pass_and_dropout_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good_mp4 = os.path.join(tmp, "good.mp4")
            bad_mp4 = os.path.join(tmp, "dropout.mp4")

            # Generate 4.0s clean MP4 (lavfi sine base is 0.125 peak, volume=1.5 -> ~0.19 peak / -17.4 dBFS RMS)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=160x160:r=10:d=4.0",
                    "-f", "lavfi", "-i", "sine=f=240:r=44100:d=4.0,volume=1.5",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    good_mp4,
                ],
                check=True,
            )
            # Generate 4.0s MP4 with a 2.0s muted dropout (1.0s..3.0s at -55 dBFS)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=160x160:r=10:d=4.0",
                    "-f", "lavfi", "-i", "sine=f=240:r=44100:d=4.0,volume='if(between(t,1.0,3.0),0.01,1.5)':eval=frame",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    bad_mp4,
                ],
                check=True,
            )
            wts = [
                WordTiming("Word1", 0.1, 0.9, "L1"),
                WordTiming("Word2", 1.0, 1.9, "L1"),
                WordTiming("Word3", 2.0, 2.9, "L1"),
                WordTiming("Word4", 3.0, 3.8, "L1"),
            ]
            lines = [{"id": "L1", "emotion": "calm"}]

            res = verify_final_video_audio(good_mp4, wts, lines, [], 4.0, part_tag="Test")
            self.assertGreater(res["min_speech_window_db"], -22.0)

            with self.assertRaises(RuntimeError) as ctx:
                verify_final_video_audio(bad_mp4, wts, lines, [], 4.0, part_tag="Test")
            self.assertIn("low/missing voice energy", str(ctx.exception))
            self.assertRegex(str(ctx.exception), r"1\.\d\ds-2\.\d\ds")




try:
    import numpy as _np  # noqa: F401
    import soundfile as _sf  # noqa: F401
    import torch as _torch  # noqa: F401
    HAS_VS_DEPS = True
except ImportError:
    HAS_VS_DEPS = False


class TestVoiceStudioFaultInjection(unittest.TestCase):
    @unittest.skipUnless(HAS_VS_DEPS, "torch/numpy/soundfile not installed in this container")
    def test_voicestudio_chunk_retry_and_missing_alignment_repair(self) -> None:
        import numpy as np
        from voicestudio import VoiceStudioEngine

        sr = 24000
        engine = VoiceStudioEngine()

        class FakeModel:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, **kwargs):
                self.calls += 1
                txt = kwargs.get("text", "")
                if self.calls == 1:
                    # Fault 1: silent zeros chunk
                    return [np.zeros(int(2.2 * sr), dtype=np.float32)]
                if self.calls == 2:
                    # Fault 2: massive DC offset + muffled low AC energy
                    t = np.arange(int(2.2 * sr), dtype=np.float32) / sr
                    return [-0.30 + 0.005 * np.sin(2 * np.pi * 220 * t)]
                # Attempt 3: clean speech-like signal
                dur = 2.4 if "first" in txt.lower() else 2.2
                t = np.arange(int(dur * sr), dtype=np.float32) / sr
                wave = 0.22 * np.sin(2 * np.pi * 220 * t)
                return [wave.astype(np.float32)]

        class FakeWhisperHelper:
            def _transcribe_with_faster_whisper(self, audio_input, **kwargs):
                # Inject missing alignment on chunk 2 (returns 0 words on re-align too -> forces even fallback),
                # while returning words for chunk 1 after attempt 3!
                return []

        fake_model = FakeModel()
        engine.model = fake_model
        # Patch _get_whisper_helper so attempt 1 & 2 already fail on energy/DC before Whisper,
        # and on attempt 3 we simulate Whisper recognizing words for chunk 1 and missing chunk 2.
        calls_w = {"n": 0}

        class SmartWhisperHelper:
            def _transcribe_with_faster_whisper(self, audio_input, **kwargs):
                calls_w["n"] += 1
                if calls_w["n"] == 1:
                    # Chunk 1 attempt 3 succeeds Whisper check
                    return [
                        ("the", 0.05, 0.2),
                        ("first", 0.2, 0.5),
                        ("line", 0.5, 0.8),
                        ("worked", 0.8, 1.2),
                        ("cleanly", 1.2, 1.6),
                    ]
                # Simulate missing Whisper alignment for chunk 2 (only 1 short word, or empty on re-align)
                return []

        engine._whisper_helper = SmartWhisperHelper()

        # Verify: when Whisper returns 0 words on chunk 2 (>=4 words), per-chunk validation catches & rejects after 3 tries!
        lines_bad = [
            {"id": "L1", "text": "The first line worked cleanly.", "emotion": "calm"},
            {"id": "L2", "text": "This second chunk has missing alignment words.", "emotion": "calm"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                engine.synthesize_lines(lines_bad, "vs-harper", {"voicestudio_speedup": 1.25}, tmp, align=True)
            self.assertIn("failed validation at L2 after 3 attempts", str(ctx.exception))

        # Now test successful retry recovery (2 failures -> 3rd try succeeds) + alignment repair when re-align drops words!
        fake_model2 = FakeModel()
        engine.model = fake_model2
        calls_w2 = {"n": 0}

        class RecoverWhisperHelper:
            def _transcribe_with_faster_whisper(self, audio_input, **kwargs):
                calls_w2["n"] += 1
                if calls_w2["n"] == 1:
                    # Only 1 word initially -> triggers alignment repair on L1 (Wait: validation requires >=0.35 recall for >=5 words)
                    return [("first", 0.05, 0.2), ("line", 0.2, 0.4), ("ok", 0.4, 0.6)]
                return []

        engine._whisper_helper = RecoverWhisperHelper()
        lines_ok = [{"id": "L1", "text": "First line ok.", "emotion": "calm"}]
        with tempfile.TemporaryDirectory() as tmp:
            res = engine.synthesize_lines(lines_ok, "vs-harper", {"voicestudio_speedup": 1.25}, tmp, align=True)
            self.assertEqual(res.chunk_count, 1)
            self.assertEqual(res.retry_count, 2)  # 2 retries before 3rd attempt succeeded!
            self.assertEqual(len(res.word_timings), 3)
            verify_word_timing_coverage(
                res.word_timings,
                [("First", "L1"), ("line", "L1"), ("ok.", "L1")],
                res.duration_sec,
                res.intended_pauses,
                max_gap_sec=1.5,
            )

if __name__ == "__main__":
    unittest.main(verbosity=2)
