# Zernio First Live Test Checklist

StoryForge's social publishing integration (SF-005 & SF-009) was built from the official Zernio API documentation (`https://docs.zernio.com`) and verified using StoryForge's `MockZernioAdapter` and `Dry Run` mode while no Zernio API key was available.

Follow this checklist when adding the first real Zernio API key.

## 1. Pre-flight Configuration (in StoryForge -> Settings)

1. Turn **OFF** `Mock Zernio adapter`.
2. Turn **ON** `Dry run mode` for the first test.
3. Set `Queue cadence` (e.g. `3` per `day`) and `Part 2 delay` (e.g. `20` minutes; use `2` minutes for a quick first test if desired).
4. Add your Zernio API key under **Zernio API Keys & Connected Accounts**, give it a label (e.g. teammate name), and click **Add & Test Key**.
   - Verify status shows `OK` and all connected social accounts (TikTok, YouTube, Instagram, Facebook, Threads, X) appear in **Connected Social Accounts**.
   - Confirm only masked keys (`sk_…1234`) are visible in UI/responses.
5. Verify Cloudflare Cron Worker (`storyforge-cron`, `* * * * *`) is deployed and active in your Cloudflare dashboard.

## 2. Dry-Run Verification (no live posts)

1. Keep `Dry run mode` **ON**.
2. Submit a short single-part test video (or two-part series) using **Add to queue**.
3. Wait for render completion -> verify task shows `Payload previews` under each target account:
   - **TikTok**: `tiktokSettings` includes `privacy_level: "PUBLIC_TO_EVERYONE"`, `allow_comment: true`, `allow_duet: true`, `allow_stitch: true`, `content_preview_confirmed: true`, `express_consent_given: true`.
   - **YouTube**: `platformSpecificData.title` <= 100 chars with `[PART N]`, `visibility: "public"`, `madeForKids: false`.
   - **Instagram**: `shareToFeed: true`, caption <= 2,200 chars, file <= 300 MB.
   - **Facebook**: `contentType: "reel"` when duration is 3–60s; omitted (standard Feed video) when > 60s.
   - **Threads**: text <= 500 chars.
   - **X (Twitter)**: weighted length <= 280 chars.
4. Confirm `.mp4` assets are **NOT deleted** during Dry Run.

## 3. First Live Publish Test

1. Turn **OFF** `Dry run mode` and save Settings.
2. Render a short test video (20–45s is cheapest/fastest) with **Publish immediately** (or a two-part series with short `Part 2 delay`).
3. Watch task logs in StoryForge Task Queue:
   - Presigned upload (`POST /v1/media/presign` + binary stream `PUT`) succeeds cleanly for your `.mp4` size.
   - `POST /v1/posts` returns `201` (or `207` if partial).
   - Cron Worker polls `GET /v1/posts/{postId}` every minute until platforms leave `uploading` / `processing` state.
   - Live post URLs appear in the Task Queue target table.
   - Once **all** targets in all parts reach `published` (or `skipped`), verify the `.mp4` asset is automatically removed from the `renders` GitHub Release and the completed task is purged from D1.

## 4. Documented Assumptions to Confirm Against Live Zernio

The items below come from Zernio's documentation and have **not** been tested live yet:

1. **TikTok Privacy & Consent Settings**: Some unverified/private TikTok Creator Studio developer apps force `SELF_ONLY` visibility until approved by TikTok. If Zernio returns `privacy_level` validation errors for `PUBLIC_TO_EVERYONE`, check the Zernio account's Creator Info / app audit status.
2. **X (Twitter) Video Aspect Ratio / Dimensions**: Zernio's `twitter/media` doc states *"Max dimensions: 1920 x 1200 px"*. StoryForge outputs `1080 x 1920` (9:16 portrait, total pixels 2.07M vs 2.30M landscape max). Confirm in live X upload that Zernio/X accepts `1080x1920` portrait MP4 without dimension rejection; if X requires `<=1200` height, add a platform-specific check or toggle in `web/shared/platforms.ts`.
3. **Zernio Streaming Presign Put from Cloudflare Worker**: `LiveZernioAdapter.uploadVideoFromStream` passes `Content-Length` and the GitHub signed asset `ReadableStream` directly to `PUT uploadUrl`. Confirm R2/S3 presigned URL accepts chunkless fixed `Content-Length` streaming from Cloudflare Workers edge runtime (under free CPU time limits for ~30–45 MB files).
4. **Partial Failure (`HTTP 207`) Shape & `POST /v1/posts/{postId}/retry`**: Confirm live Zernio `207` response includes per-account `platforms[].status` (`published` vs `failed`) and `retry` re-attempts only failed targets.
5. **YouTube Tag Combined Limit**: Zernio passes `tags[]`; StoryForge strips `#` and caps combined tag characters <= 480 and individual tags <= 100 chars. Confirm live acceptance.
