"""
Background gameplay footage fetcher & manifest builder for StoryForge.
Searches YouTube, verifies Creative Commons license OR explicit free-to-reuse
statement in the creator's description (title alone is rejected), tries multiple
yt-dlp player clients without cookies, strips audio, caps at 1080p MP4, and
uploads clips + manifest.json to the GitHub Release named 'backgrounds'.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from typing import Any


SEARCH_QUERIES = [
    "minecraft parkour gameplay no copyright free to use",
    "gta 5 mega ramp gameplay no copyright free to use",
    "subway surfers gameplay no copyright free to use",
    "beamng drive gameplay no copyright free to use",
    "forza horizon driving gameplay no copyright free to use",
]

FREE_REUSE_RE = re.compile(
    r"(free to use|free to reuse|no copyright|copyright[- ]free|royalty[- ]free|"
    r"creative commons|cc[- ]?by|public domain|free background gameplay|"
    r"you can use this (video|footage|gameplay)|feel free to use)",
    re.IGNORECASE,
)

CREDIT_LINE_RE = re.compile(r"(credit|attribution|mention|link).*$", re.IGNORECASE | re.MULTILINE)

YT_PLAYER_CLIENTS = [
    "tv_embedded",
    "tv_simply",
    "android_vr",
    "ios",
    "mweb",
    "web_safari",
    "android_sdkless",
    "default",
]


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_free_statement(
    title: str,
    uploader: str,
    url: str,
    desc: str,
    has_cc_license: bool,
) -> tuple[str | None, str]:
    """
    Returns (quoted_statement, attribution_text) if verified free-to-reuse via
    YouTube Creative Commons license metadata OR creator's description.
    Title alone is NEVER sufficient.
    """
    quoted: str | None = None
    if has_cc_license:
        quoted = "YouTube License: Creative Commons Attribution license (reuse allowed)"

    for line in desc.splitlines():
        clean = line.strip()
        if clean and FREE_REUSE_RE.search(clean):
            snippet = f'Description: "{clean[:240]}"'
            quoted = f"{quoted} | {snippet}" if quoted else snippet
            break

    if not quoted:
        return None, ""

    credit_req = ""
    m_cred = CREDIT_LINE_RE.search(desc)
    if m_cred:
        credit_req = f" ({m_cred.group(0).strip()[:160]})"

    attribution = f'Gameplay: "{title}" by {uploader} — {url} [{quoted}]{credit_req}'
    return quoted, attribution


def search_candidates(max_per_query: int = 8) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []

    for q in SEARCH_QUERIES:
        print(f"[bg] Searching YouTube: {q}")
        try:
            search_html = _fetch_html(
                "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)
            )
        except Exception as exc:
            print(f"[bg] Search failed for '{q}': {exc}")
            continue

        ids = list(dict.fromkeys(re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', search_html)))
        checked = 0
        for vid in ids:
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            if checked >= max_per_query:
                break
            checked += 1

            url = f"https://www.youtube.com/watch?v={vid}"
            try:
                whtml = _fetch_html(url)
            except Exception:
                continue

            has_cc = "Creative Commons Attribution" in whtml
            title = ""
            uploader = ""
            desc = ""

            m_data = re.search(r"var ytInitialData\s*=\s*(\{.+?\});</script>", whtml)
            if m_data:
                txt = m_data.group(1)
                tm = re.search(r'"videoPrimaryInfoRenderer":\{"title":\{"runs":\[\{"text":"([^"]+)"\}', txt)
                if tm:
                    title = tm.group(1)
                um = re.search(r'"videoOwnerRenderer":\{.*?"title":\{"runs":\[\{"text":"([^"]+)"', txt)
                if um:
                    uploader = um.group(1)
                dm = re.search(r'"attributedDescription":\{"content":"((?:\\.|[^"\\])*)"', txt)
                if dm:
                    try:
                        desc = json.loads('"' + dm.group(1) + '"')
                    except Exception:
                        desc = dm.group(1)

            if not title:
                om = re.search(r'<title>(.*?)</title>', whtml)
                title = (om.group(1).replace(" - YouTube", "").strip() if om else vid)
            if not uploader:
                uploader = "YouTube Creator"

            quoted, attr = extract_free_statement(title, uploader, url, desc, has_cc)
            if not quoted:
                continue

            print(f"[bg] Verified candidate {vid}: {title[:60]} ({quoted[:80]})")
            candidates.append(
                {
                    "id": vid,
                    "title": title,
                    "uploader": uploader,
                    "url": url,
                    "license_statement": quoted,
                    "attribution": attr,
                }
            )

    print(f"[bg] Found {len(candidates)} verified free-to-reuse candidates.")
    return candidates


def download_clip(url: str, raw_out: str) -> tuple[bool, str]:
    last_err = ""
    for client in YT_PLAYER_CLIENTS:
        print(f"[bg] Trying yt-dlp client={client} for {url}")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--js-runtimes",
            "node",
            "--extractor-args",
            f"youtube:player_client={client}",
            "-f",
            "bestvideo[height<=1080][ext=mp4]/bestvideo[height<=1080]/best[height<=1080][ext=mp4]/best[height<=1080]",
            "--merge-output-format",
            "mp4",
            "-o",
            raw_out,
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(raw_out) and os.path.getsize(raw_out) > 100_000:
            return True, ""
        last_err = (proc.stderr or proc.stdout or "").strip()[-350:]
        print(f"[bg] client={client} failed: {last_err}")
    return False, last_err


def ensure_release(repo: str, tag: str, token: str) -> dict[str, Any]:
    api = f"https://api.github.com/repos/{repo}/releases"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "StoryForge-Pipeline",
    }
    try:
        req = urllib.request.Request(f"{api}/tags/{tag}", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        payload = json.dumps({"tag_name": tag, "name": tag, "body": "StoryForge background gameplay library"}).encode()
        req = urllib.request.Request(api, data=payload, method="POST", headers={**headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def upload_release_asset(upload_url_tmpl: str, file_path: str, asset_name: str, token: str, content_type: str) -> None:
    upload_base = upload_url_tmpl.split("{")[0]
    url = f"{upload_base}?name={asset_name}"
    subprocess.run(
        [
            "curl",
            "-fsSL",
            "-X",
            "POST",
            url,
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            f"Content-Type: {content_type}",
            "--data-binary",
            f"@{file_path}",
        ],
        check=True,
    )


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    target_count = int(os.environ.get("TARGET_CLIPS", "12"))
    work_dir = "/tmp/bg_build"
    os.makedirs(work_dir, exist_ok=True)

    candidates = search_candidates()
    if not candidates:
        print("[bg] ERROR: No verified candidates found.", file=sys.stderr)
        sys.exit(2)

    clips_manifest: list[dict[str, Any]] = []
    rel = None
    existing_assets: dict[str, int] = {}
    last_dl_err = ""

    for cand in candidates:
        if len(clips_manifest) >= target_count:
            break
        raw_path = os.path.join(work_dir, f"raw_{cand['id']}.mp4")
        clean_name = f"bg_{len(clips_manifest)+1:02d}_{cand['id']}.mp4"
        clean_path = os.path.join(work_dir, clean_name)

        ok, err = download_clip(cand["url"], raw_path)
        if not ok:
            last_dl_err = err
            if len(clips_manifest) == 0 and len(candidates) > 3:
                # Try first 3 candidates across all clients before bailing on IP block
                if candidates.index(cand) >= 2:
                    break
            continue

        if rel is None:
            rel = ensure_release(repo, "backgrounds", token)
            existing_assets = {a["name"]: a["id"] for a in rel.get("assets", [])}

        # Strip audio and cap to <= 600s
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                raw_path,
                "-t",
                "600",
                "-an",
                "-c:v",
                "copy",
                "-movflags",
                "+faststart",
                clean_path,
            ],
            check=True,
        )
        os.remove(raw_path)

        if clean_name in existing_assets:
            subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "-X",
                    "DELETE",
                    f"https://api.github.com/repos/{repo}/releases/assets/{existing_assets[clean_name]}",
                    "-H",
                    f"Authorization: Bearer {token}",
                ],
                check=True,
            )

        upload_release_asset(rel["upload_url"], clean_path, clean_name, token, "video/mp4")
        os.remove(clean_path)

        clips_manifest.append(
            {
                "filename": clean_name,
                "title": cand["title"],
                "uploader": cand["uploader"],
                "url": cand["url"],
                "license_statement": cand["license_statement"],
                "attribution": cand["attribution"],
            }
        )

    if not clips_manifest:
        print(f"[bg] BLOCKED: YouTube blocked downloads from GitHub Actions IP. Last error: {last_dl_err}", file=sys.stderr)
        print("[bg] VERIFIED_CANDIDATES_JSON=" + json.dumps(candidates[:15]))
        sys.exit(3)

    manifest_path = os.path.join(work_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"clips": clips_manifest}, f, indent=2)

    assert rel is not None
    if "manifest.json" in existing_assets:
        subprocess.run(
            [
                "curl",
                "-fsSL",
                "-X",
                "DELETE",
                f"https://api.github.com/repos/{repo}/releases/assets/{existing_assets['manifest.json']}",
                "-H",
                f"Authorization: Bearer {token}",
            ],
            check=True,
        )
    upload_release_asset(rel["upload_url"], manifest_path, "manifest.json", token, "application/json")
    print(f"[bg] Uploaded {len(clips_manifest)} background clips + manifest.json.")


if __name__ == "__main__":
    main()
