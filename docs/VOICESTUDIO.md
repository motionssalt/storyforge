# VoiceStudio in StoryForge

VoiceStudio is an [AGPL-3.0 application](https://github.com/debpalash/voicestudio) and defaults to [OmniVoice](https://github.com/k2-fsa/OmniVoice). The runner imports OmniVoice directly, without Electron or an HTTP service. The code is Apache-2.0; the [pinned model](https://huggingface.co/k2-fsa/OmniVoice/tree/3fc979d1cd72492cd679c5ac2611e0d7ac047ba1) is **CC BY-NC** per its model card. Do not use this configuration for commercial work without separately establishing permission; keep Edge as the default. Voice presets are designs from supported speaker attributes, not real-person clones.

The TTS weights are 2,450,344,112 bytes; the audio tokenizer weights are 805,665,628 bytes (3,256,009,740 bytes combined, excluding tokenizers/config and alignment). Those are registry file sizes, not transfer measurements. Actions installs CPU-only PyTorch plus the pinned OmniVoice package, restores the Hugging Face hub cache, takes a pinned snapshot and loads one model per process. Job timeout is 100 minutes; a long runner benchmark is needed before adding chunk jobs. A runner cache can be evicted: sample readiness is not a guarantee of a cache hit. Canceling/deleting uses the existing Actions cancel/tombstone path; VoiceStudio does not silently retry as Edge.

| Input | OmniVoice behavior |
|---|---|
| `calm` | Preset voice with natural text prosody |
| `whisper` | Supported `whisper` voice design control |
| `tense`, `angry`, `shocked`, `sad`, `sarcastic`, `excited` | Preset voice with natural text prosody; no direct supported control for these labels |
| `[pause]` | 380 ms of raw silence at the marker |
| `[sigh]`, `[laugh]` | 160 ms of raw silence; no generated sound effect |

Speech is generated at natural speed, resampled to 44.1 kHz, then passed through `asetrate=44100*F,aresample=44100` (default 1.25). The entire narration including marker gaps is sped up with no compensation; the background is unaffected. OmniVoice has no word timestamps, so faster-whisper aligns the final (already sped-up) signal. No second division is applied to those timestamps. Edge keeps its existing behavior, including faster-whisper fallback when necessary.

The sample workflow publishes an MP3 per Edge voice and both a calm and whisper MP3 per VoiceStudio voice, with a manifest in `voice-samples`. The site uses its authenticated Release proxy. Because the repo itself is public, GitHub’s direct Release URLs are public even though the site's proxy requires login.
