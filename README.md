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
- `assets/` — Bundled OFL comic fonts (`assets/fonts/`) and 60 offline SVG avatars (`assets/avatars/`).
- `web/` — Password-protected Cloudflare Pages + Pages Functions web app.
- `.github/workflows/` — `render.yml`, `generate-voice-samples.yml`, and `build-backgrounds.yml`.
