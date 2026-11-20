"""Headless VoiceStudio default engine: OmniVoice, CPU only."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from tts_engine import (EdgeTTSEngine, LineTiming, TTSResult, MARKER_PATTERN,
                        VoiceStudioCoupledSpeedupHook, align_whisper_words_to_script,
                        clean_spoken_text)

MODEL = 'k2-fsa/OmniVoice'
REVISION = '3fc979d1cd72492cd679c5ac2611e0d7ac047ba1'
PRESETS = {
    'vs-harper': ('Harper', 'Female · American · lower register', 'female, low pitch, american accent'),
    'vs-rowan': ('Rowan', 'Male · British · lower register', 'male, low pitch, british accent'),
    'vs-morgan': ('Morgan', 'Female · British · mid register', 'female, british accent'),
}
MARKER_GAPS = {'pause': 0.38, 'sigh': 0.16, 'laugh': 0.16}


class VoiceStudioEngine:
    model = None

    def prepare(self):
        import torch
        from omnivoice import OmniVoice
        from huggingface_hub import snapshot_download

        if self.model is None:
            ts = time.monotonic()
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
            path = snapshot_download(MODEL, revision=REVISION)
            total = sum(p.stat().st_size for p in Path(path).rglob('*') if p.is_file())
            print(f'[voicestudio] snapshot {total} bytes, available after {time.monotonic()-ts:.2f}s', flush=True)
            ts = time.monotonic()
            self.model = OmniVoice.from_pretrained(path, device_map='cpu', dtype=torch.float32).eval()
            print(f'[voicestudio] CPU model load {time.monotonic()-ts:.2f}s', flush=True)
        return self.model

    def synthesize_lines(self, lines, voice_id, settings, out_dir, *, align=True):
        import numpy as np
        import soundfile as sf
        import subprocess
        import torch

        if voice_id not in PRESETS:
            raise ValueError('Unknown VoiceStudio voice preset. Choose one from the voice picker.')
        model = self.prepare()
        os.makedirs(out_dir, exist_ok=True)
        factor = float(settings.get('voicestudio_speedup', 1.25))
        if not 1.0 <= factor <= 2.0:
            raise ValueError('VoiceStudio speed-up must be between 1.0 and 2.0.')
        script = []
        parts = []
        rate = 24000
        started = time.monotonic()
        for line in lines:
            lid = str(line['id'])
            text = str(line['text'])
            clean, markers = clean_spoken_text(text)
            script.extend((word, lid) for word in clean.split())
            prompt = PRESETS[voice_id][2]
            if line.get('emotion') == 'whisper':
                prompt += ', whisper'
            # OmniVoice's controlled vocabulary supports whisper, not arbitrary emotion labels.
            for i, piece in enumerate(MARKER_PATTERN.split(text)):
                if i % 2:
                    parts.append(np.zeros(round(rate * MARKER_GAPS[piece.lower()]), dtype=np.float32))
                    continue
                if not re.search(r'\w', piece):
                    continue
                try:
                    with torch.inference_mode():
                        wave = model.generate(text=piece.strip(), instruct=prompt, num_step=16)[0]
                    wave = np.asarray(wave, dtype=np.float32).reshape(-1)
                    if not wave.size or not np.isfinite(wave).all():
                        raise ValueError('empty or invalid audio')
                    parts.append(wave)
                except Exception as exc:
                    raise RuntimeError(f'OmniVoice synthesis failed at {lid}: {exc}') from exc
            for marker in dict.fromkeys(line.get('markers', [])):
                if marker in MARKER_GAPS and marker not in markers:
                    parts.append(np.zeros(round(rate * MARKER_GAPS[marker]), dtype=np.float32))
        if not parts or not script:
            raise ValueError('No narration was generated.')
        raw = os.path.join(out_dir, 'omnivoice-24k.wav')
        natural = os.path.join(out_dir, 'narration.wav')
        sf.write(raw, np.concatenate(parts), rate, subtype='PCM_16')
        subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-i',raw,'-ar','44100',natural], check=True)
        result = VoiceStudioCoupledSpeedupHook.apply_coupled_speedup(TTSResult(natural, sum(a.size for a in parts)/rate), factor)
        print(f'[voicestudio] generation and resampling {time.monotonic()-started:.2f}s; {len(script)} words, final {result.duration_sec:.2f}s', flush=True)
        if align:
            started = time.monotonic()
            words = EdgeTTSEngine()._transcribe_with_faster_whisper(result.audio_path)
            result.word_timings = align_whisper_words_to_script(script, words, result.duration_sec)
            for word in result.word_timings:
                if word.line_id not in result.line_timings:
                    result.line_timings[word.line_id] = LineTiming(word.line_id, word.start, word.end)
                else:
                    result.line_timings[word.line_id].end = word.end
            print(f'[voicestudio] final-audio alignment {time.monotonic()-started:.2f}s', flush=True)
        return result
