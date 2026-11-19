# StoryForge

Automated vertical (9:16, 1080x1920) Reddit-story short video pipeline powered by Cloudflare Pages/Workers and GitHub Actions.

## Architecture
- `schema/` — Production JSON schema (`production.schema.json`) and single / two-part examples.
- `pipeline/` — Modular Python render pipeline:
  - `tts_engine.py`: Swappable TTS interface (Phase 1: Edge TTS + WordBoundary / `faster-whisper` word alignment; Phase 2 coupled-speedup hook).
  - `render_cards.py`: Playwright HTML-to-transparent-PNG renderer for Reddit post, comment, reply, and branded cards (810px card width at 1080p scale).
  - `subtitles.py`: ASS comic-font word-by-word subtitle generator (`Lilita One` / `Bangers`, active word highlighted).
  - `background.py`: Sequential gameplay footage fetcher & 9:16 center-cropper reading from the `backgrounds` GitHub Release.
  - `compose.py`: FFmpeg final compositor (background + timed card PNG overlays + burned-in subtitles + AAC narration).
  - `calibrate_voices.py` & `build_backgrounds.py`: Voice WPM calibration/sample generator and free-to-reuse YouTube gameplay collector.
- `voices/` — Curated English Edge TTS voice catalog (`voices.json`).
- `assets/` — Bundled comic fonts (`assets/fonts/`; OFL for Bangers/Lilita One, Apache 2.0 for Luckiest Guy, notices included) and 60 offline SVG avatars (`assets/avatars/`).
- `web/` — Password-protected Cloudflare Pages + Pages Functions web app.
- `.github/workflows/` — `render.yml`, `generate-voice-samples.yml`, and `build-backgrounds.yml`.

## Phase 1 status and first use

Live site: https://storyforge-sfb.pages.dev . The password is a Cloudflare Pages secret, never a repository file. The public login shell contains no private research or prompt template. Authenticated API responses, voice catalog and samples require the site password/session.

The technical two-part smoke run succeeded: https://github.com/motionssalt/storyforge/actions/runs/44466718014 . Its two MP4s are in https://github.com/motionssalt/storyforge/releases/tag/renders and the site's jobs list. These are explicitly **TEST PATTERNS, NOT GAMEPLAY**. They verify rendering and protected downloads, not production-ready footage. `docs/verification.json` records actual checks and limits; `BUILD_STATE.json` is the resumable checkpoint.

**Gameplay rendering remains blocked.** YouTube bot-verification blocked GitHub Actions after multiple client attempts; do not repeatedly retry or substitute unapproved footage. Normal Render requests return an actionable missing-library error until the footage is uploaded.

### Exact gameplay upload procedure

Open https://github.com/motionssalt/storyforge/releases/tag/backgrounds and edit that release to attach files. On a device/network permitted to download the approved source videos, prepare the clips listed in `docs/backgrounds-to-upload.json`. Each clip should be a silent H.264 MP4 at up to 1080p, about 5–15 minutes long. Keep the original creator's reuse-permission statement and requested credit. Aim for 10–20 clips and only a few GB total. Reconfirm permission if the source terms have changed.

`docs/background-upload-manifest.template.json` gives the exact numbered filenames and source/attribution metadata for the first 12 candidates. Upload each clip under its matching `filename`. Save the template as **manifest.json**, remove entries for any clips you did not upload, and attach that file to the same `backgrounds` release. An empty manifest is not a usable library. The manifest format is `{ "clips": [{ "filename": "bg_01_VIDEO_ID.mp4", "title": "...", "uploader": "...", "url": "...", "license_statement": "Exact source permission quote", "attribution": "Creator credit to paste into description" }] }`.

The optional helper `pipeline/upload_local_backgrounds.py` performs downloading, audio stripping and upload from a non-blocked Python/FFmpeg environment using the `GITHUB_TOKEN` environment variable. Do not place that token in a file committed to git. YouTube access failure is a stop condition, not permission to bypass restrictions.

After upload, log in, preview a voice, paste `schema/examples/two-part-series.json`, validate, and press Render. Verify actual gameplay, different sequential clip choices, attribution, narration pace and both downloads before marking phases 6 and 7 complete. The smaller `schema/examples/smoke-two-part.json` is for quick technical checks only.

### Maintenance

In `web/`, `npm ci` then `npm run deploy` compiles the schema validator and deploys Pages using Cloudflare credentials from environment variables. Schema validation is compiled ahead of time because Workers disallow runtime eval. `npm run build` builds the Functions bundle without deploying.

The voice calibration/sample workflow regenerates the 16-voice catalog and 32 samples in one run; deploy Pages afterward to publish changed static assets. Phase 1 uses Edge TTS only. Dramatic-text samples do not imply an emotional voice model. Edge speed is controlled by its native rate setting; no pitch-changing post-processing is applied.

Render workflow concurrency serializes executions. KV remains eventually consistent; stronger transactional allocation/queuing is needed before multi-user operation or multiple pending jobs. Edge is an external service and may still reject requests after bounded retries. Historical private commits may remain fetchable by exact SHA until GitHub garbage-collects them, even though the reachable orphan history is clean. No emotional engine, Android app, or UI refinement work is included.
