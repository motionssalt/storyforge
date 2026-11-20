# VoiceStudio in StoryForge

VoiceStudio runs headlessly via [OmniVoice](https://github.com/k2-fsa/OmniVoice) (informed by [VoiceStudio / OmniVoice-Studio](https://github.com/debpalash/voicestudio)) on GitHub Actions CPU runners, with no Electron UI or local HTTP daemon. The OmniVoice code is Apache-2.0; the pinned model weights ([`k2-fsa/OmniVoice` @ `3fc979d1cd72492cd679c5ac2611e0d7ac047ba1`](https://huggingface.co/k2-fsa/OmniVoice/tree/3fc979d1cd72492cd679c5ac2611e0d7ac047ba1)) are **CC BY-NC** (non-commercial). Do not use those weights commercially without separately obtaining commercial rights.

**Default engine:** **VoiceStudio** is the default narration engine across Settings, project setup, Worker defaults, and the render pipeline (`Edge TTS` remains available as the second option in Settings and on the Create view). Selecting or switching the engine updates the active setting and filters the project-setup voice picker immediately. On VoiceStudio failure, the job fails clearly and never silently falls back to Edge TTS.

## Model, Caching & Speed-Up

- **Weights & cache:** The pinned Hugging Face snapshot is **3,267,465,820 bytes** (~3.04 GiB; `2,450,344,112` bytes TTS + `805,665,628` bytes audio tokenizer + config/tokenizers), cached by GitHub Actions under `~/.cache/huggingface/hub` (`storyforge-omni-3fc979d1-hub-v1`, **2,865,808,620 bytes** compressed).
- **Speed-up & subtitle sync:** Speech is generated at natural speed (`24 kHz`, `num_step=16`), resampled to `44.1 kHz` mono PCM, and sped up via `asetrate=44100*F,aresample=44100` (default `F = 1.25`, configurable `1.0`–`2.0` in Settings). `faster-whisper` (`base.en`, CPU `int8`) then aligns words directly on the already sped-up audio, so subtitle and card-scroll timestamps match the final audio with no double division. Background video/music is unaffected.
- **Cancellation:** Deleting/cancelling a task tombstones it in D1, cancels the active GitHub Actions run (`render.yml` or `voice-tool.yml`), and triggers `cancel_check` between VoiceStudio synthesis lines.

## Built-In Voice Design Library & Custom Voice Design

- **Built-in library (35 curated voices):** `voices/voices.json` (synced to `web/public/voices.json`) ships **35** fixed-seed Voice Design presets across male/female genders, young adult/adult/middle-aged/elderly ages, very low to very high pitch, and American, British, Australian, Canadian, Indian, and Russian accents. During sample QC (faster-whisper transcript match + RMS + duration), 3 unstable candidate voices (`vs-hannah`, `vs-finn`, `vs-miles`) were dropped, leaving 35 voices with `0.938–1.000` Whisper match.
- **Create a voice (Voice Design):** On the Voices page, enter a comma-separated OmniVoice `instruct` string (validated against supported gender, age, pitch, accent, and `whisper` attributes) and a fixed `seed`, trigger an on-demand `voice-tool.yml` (`design_preview`) run to hear an 8–12s normal preview, and save it to D1 (`custom_voice:vsc_*`) + KV (`voice_preview:*`). Saved voices appear in the picker and can be deleted.

## My Voices (Private Voice Cloning & Per-Emotion References)

- **Privacy & storage choice:** Because `motionssalt/storyforge` is a **public** GitHub repository (and GitHub Releases on a public repo are always public), user voice clips, `.pt` clone prompts, and cloned previews are stored **exclusively in the private Cloudflare KV vault** (`STORYFORGE_VAULT`), chunked in 4 MiB segments (up to 20 MiB per upload) under `voice_clip:<voice_id>:<slot>`, `voice_prompt:<voice_id>:<slot>`, and `voice_preview:<voice_id>:<slot>`. Voice clips are never committed to the repo or uploaded to any GitHub Release.
- **Clip preparation & caching:** Uploading a 3–10s clip (`.mp3`, `.wav`, `.m4a`) with an optional transcript dispatches `voice-tool.yml` (`prepare_cloned`), which normalizes to 24 kHz mono WAV (trimming clips >15s to 12s), auto-transcribes blank transcripts via `faster-whisper` (`base.en`), computes and caches `VoiceClonePrompt` `.pt` tensors once per voice/slot (`create_voice_clone_prompt(..., preprocess_prompt=True)`), and generates an 8–12s normal `128k` MP3 preview at the active speed-up `F`.
- **Per-emotion reference clips:** Each cloned voice requires a `base` clip and optionally accepts clips for `calm`, `tense`, `angry`, `shocked`, `sad`, `sarcastic`, and `excited`. During rendering, each line's `emotion` picks the matching cached `.pt` prompt if uploaded, falling back to `base` otherwise.

## Emotion & Marker Mapping

OmniVoice has no general emotion slider; expressiveness comes from natural phrasing, per-emotion reference clips (on cloned voices), native OmniVoice sound tags, and real silence gaps:

| StoryForge Input | Designed Voice Behavior | Cloned ("My voice") Behavior | On-Screen Card & Subtitles |
|---|---|---|---|
| `emotion: "calm" \| "tense" \| "angry" \| "shocked" \| "sad" \| "sarcastic" \| "excited"` | Fixed preset `instruct` + natural text prosody (nothing invented) | Uses that emotion's cached `.pt` reference clip if uploaded; otherwise falls back to `base` clip | Unchanged |
| `emotion: "whisper"` | Appends `whisper` to `instruct` (supported OmniVoice control) | Falls back to `base` reference clip | Unchanged |
| `[pause]` (inline or in `markers`) | `380 ms` raw silence (`0.38s` at 24 kHz prior to speed-up `F`) | `380 ms` raw silence | Stripped |
| `[sigh]`, `[laughter]` (and `[laugh]` alias) | Passed inline to OmniVoice as `[sigh]` / `[laughter]` | Passed inline to OmniVoice as `[sigh]` / `[laughter]` | Stripped |
| `[confirmation-en]`, `[question-en]`, `[question-ah]`, `[question-oh]`, `[question-ei]`, `[question-yi]`, `[surprise-ah]`, `[surprise-oh]`, `[surprise-wa]`, `[surprise-yo]`, `[dissatisfaction-hnn]` | Passed inline to OmniVoice | Passed inline to OmniVoice | Stripped |

## Voice Previews

- Each built-in voice has **one main normal preview**: an 8–12s Reddit-style narration sample (`sample_text` configurable in Settings and on the Voices page), generated at `num_step=16` with speed-up `F` applied and encoded as `128 kbps` MP3 (`44.1 kHz` mono). Legacy `whisper` samples were removed from main previews.
- `generate-voice-samples.yml` regenerates all Edge and built-in VoiceStudio samples plus the `voices.json` manifest into the `voice-samples` Release, served through the authenticated Worker proxy (`/api/voice-samples/*`). Only one preview plays at a time across the UI.

## Measured Runner Numbers

- **Long 2-part VoiceStudio CPU render (`44735421938`, ~2.58 min finished audio, `vs-harper`, 1.25x):**
  - Warm Hugging Face cache hit (`storyforge-omni-3fc979d1-hub-v1`): snapshot available in **0.05s** (`3,267,465,820` bytes); CPU model load **1.61s**.
  - Part 1 (`307` words): **778.55s** generation/resampling + **9.09s** alignment -> **76.86s** finished MP4.
  - Part 2 (`296` words): **780.16s** generation/resampling + **9.46s** alignment -> **78.24s** finished MP4.
  - Total finished video: **155.10s** (~2.59 min) in a single job (~31.8 min total workflow time, well under the 100-min timeout; chunked jobs not required).
- **Voice cloning & prepare (`44735615086`, `prepare_cloned` with `base` + `tense`):** snapshot **0.07s**, CPU model load **1.58s**, Whisper auto-transcription + `.pt` creation **2.44s** (`base`, 11,432 bytes) and **2.06s** (`tense`, 10,991 bytes).
- **Cloned render (`44736711038`, `sf_clone_test`):** snapshot **0.11s**, CPU model load **1.13s**, loaded cached `.pt` prompts directly (`11,432` + `10,991` bytes, zero recompute), **120.27s** generation/resampling + **1.96s** alignment -> **5.82s** MP4.
- **On-demand Voice Design preview (`44736714248`, `design_preview`):** snapshot **0.09s**, CPU model load **1.23s**, **81.78s** generation/resampling -> **10.38s** `128k` MP3 preview.
