# StoryForge Photo & Cartoon Avatars

## Bundled Pool & Provenance
`assets/photo_avatars/manifest.json` records the source page, license, remote URLs (where applicable), and local cropped filename for every bundled asset. The bundled 256x256 pool contains **38 verified images** across all four real-photo styles:

- **`photo_person` (8 images — 4 `male`, 4 `female`)**: 2 explicitly synthetic public-domain portraits from Wikimedia Commons + 6 project-owned AI-generated synthetic everyday portraits (`photo_person-*-synth*.jpg`). Zero scraped photos of real private individuals and zero public figures.
- **`photo_object` (10 images)**: 7 CC0 Wikimedia Commons everyday objects (`coffee`, `watch`) + 3 AI-generated logo-free everyday object photos (`camera`, `book`, `guitar`).
- **`photo_animal` (9 images)**: CC0 Wikimedia Commons pet/animal photos (`dog`, `cat`).
- **`photo_scenery` (11 images)**: CC0 Wikimedia Commons landscapes (`mountain`, `waterfall`).

Before adding any new asset, confirm its exact license (CC0 / Public Domain / synthetic AI-generated), confirm zero real private individuals or public figures are depicted for `photo_person`, and confirm the 256x256 crop has no prominent brand logos or watermarks.

## Resolution & Fallback Chain
At the start of a render job (`pipeline/run_job.py`), the poster's avatar is resolved **once**, saved to disk as a 256x256 PNG (`avatar_<sha256[:16]>.png`), embedded into the Chromium card HTML as a `data:image/png;base64,...` URI, and awaited (`img.complete` + `img.decode()`) before layout measurement and screenshots. Both Part 1 and Part 2 share the same canonical username seed and cached file, guaranteeing identical avatars across parts and re-renders.

For photo styles (`photo_person`, `photo_object`, `photo_animal`, `photo_scenery`), the fallback chain is:
1. **Primary online provider (Wikimedia Commons pinned CC0 thumbnail CDN)** (`thumb.wikimedia.org`) for Wikimedia-backed entries, selected deterministically by hashing the normalized username (filtering by optional `gender` or `keyword` hint when matched).
2. **Secondary online endpoint (Wikimedia Commons original upload host)** (`upload.wikimedia.org`) for the same pinned asset so a CDN hiccup does not swap poster identity.
3. **Bundled local pool (`assets/photo_avatars/`)** — the 38 reviewed 256x256 local crops committed to the repo (also used directly as the deterministic source for the bundled AI-generated synthetic portraits/objects).
4. **Independent online scenery fallback (Lorem Picsum seeded endpoint)** (`https://picsum.photos/seed/{digest[:16]}/256/256`) for `photo_scenery` if the local pool file is unavailable.
5. **Cartoon fallback (DiceBear v9 `avataaars`)** (`https://api.dicebear.com/9.x/avataaars/png?seed=...&size=256`) -> bundled SVG cartoon pool (`assets/avatars/`).
6. **Letter circle last resort** (colored CSS circle with the poster's initial).

For `style: "cartoon"`, the chain starts at step 5 (DiceBear -> bundled SVG pool -> letter circle). A network failure never breaks a render.

## Evaluated Free / No-Key Candidate Providers & License Notes

| Provider | Tested Endpoint | Result | Decision & License Notes |
|---|---|---|---|
| **Wikimedia Commons (CC0 / PD)** | `thumb.wikimedia.org` / `upload.wikimedia.org` (pinned file URLs) | Works reliably (HTTP 200 for pinned manifest files; requires explicit User-Agent) | **Chosen (primary for CC0 photos).** Every file's individual Commons file page license (CC0 1.0 / Public Domain) is recorded in `assets/photo_avatars/manifest.json`. See [Wikimedia Commons reuse guidance](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia) and [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). |
| **Bundled AI-generated synthetic assets** | Local `assets/photo_avatars/*-synth*.jpg` | Offline, instant, 100% deterministic | **Chosen (primary for `photo_person` variety + extra `photo_object`).** Fictional everyday people and logo-free objects; zero real individuals, zero third-party license risk. |
| **Lorem Picsum** | `https://picsum.photos/seed/{seed}/256/256` | HTTP 200, `image/jpeg`, ~11 KB, ~140 ms, no key | **Secondary fallback for `photo_scenery` only.** Free Unsplash-backed seeded photos (Unsplash License allows free use without attribution, no redistribution as a competing stock service). Not used as primary for people/animals/objects because its seeded catalog mixes unrelated categories and occasional real faces. |
| **RandomUser.me** | `https://randomuser.me/api/portraits/men/12.jpg` | HTTP 200, `image/jpeg`, ~5.6 KB, ~64 ms | **Declined.** Portraits are small thumbnails whose underlying per-person commercial/video release and consent provenance are not verifiable; we do not use photos of real private individuals without clear public-domain/synthetic provenance. |
| **Pravatar (`i.pravatar.cc`)** | `https://i.pravatar.cc/256?u={seed}` | HTTP 200, `image/jpeg`, ~11 KB, ~338 ms | **Declined.** Serves unattributed portraits of real people from third-party photo pools without explicit model releases or license guarantees for video redistribution. |
| **DiceBear v9 (`avataaars`)** | `https://api.dicebear.com/9.x/avataaars/png?seed={seed}&size=256` | HTTP 200, `image/png`, ~9.6 KB, ~88 ms, no key | **Chosen for `cartoon` style and online fallback.** Open-source avatar library (`avataaars` style by Pablo Stanley, free for personal & commercial use; DiceBear HTTP API is free with rate limits, backed by our local `assets/avatars/` SVG pool). |
