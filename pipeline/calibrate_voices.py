"""
Measure real WPM at +0% rate for each voice in voices/voices.json,
compute default_rate to hit target WPM (default 250), and generate
two static sample MP3 clips (neutral + emotional) in web/public/samples/.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import edge_tts

CALIBRATION_PASSAGE = (
    "At nine fifteen on Tuesday morning, my manager walked into the conference room "
    "holding a printed spreadsheet and asked why the client numbers looked different. "
    "I opened my laptop, pulled up the timestamped folder from last Friday, and showed "
    "everyone at the table who had actually signed off on the revision. Nobody spoke "
    "for ten seconds while the regional director scrolled straight down to the bottom "
    "of the page and read the audit note aloud."
)
CALIBRATION_WORDS = len(CALIBRATION_PASSAGE.split())

SAMPLE_NEUTRAL = (
    "When I opened the envelope on my kitchen counter, I realized nobody in the building "
    "had told me the whole story."
)
SAMPLE_EMOTIONAL = (
    "Wait—you told everyone I signed that contract last Friday? Look at the date on the receipt again!"
)


def probe_duration(path: str) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        text=True,
    ).strip()
    return float(out)


async def process_voice(voice: dict, target_wpm: int, max_rate: int, samples_dir: str, tmp_dir: str) -> dict:
    vid = voice["id"]
    cal_mp3 = os.path.join(tmp_dir, f"{vid}_cal.mp3")
    await edge_tts.Communicate(CALIBRATION_PASSAGE, vid, rate="+0%").save(cal_mp3)
    dur = probe_duration(cal_mp3)
    base_wpm = round(CALIBRATION_WORDS / (dur / 60.0), 1)
    needed_pct = round(((target_wpm / base_wpm) - 1.0) * 100.0)
    clamped_pct = max(0, min(max_rate, needed_pct))
    default_rate = f"+{clamped_pct}%"

    neutral_mp3 = os.path.join(samples_dir, f"{vid}_neutral.mp3")
    emotional_mp3 = os.path.join(samples_dir, f"{vid}_emotional.mp3")
    await edge_tts.Communicate(SAMPLE_NEUTRAL, vid, rate=default_rate).save(neutral_mp3)
    await edge_tts.Communicate(SAMPLE_EMOTIONAL, vid, rate=f"+{min(max_rate, clamped_pct + 5)}%").save(emotional_mp3)

    voice["base_wpm"] = base_wpm
    voice["default_rate"] = default_rate
    voice["sample_neutral"] = f"/samples/{vid}_neutral.mp3"
    voice["sample_emotional"] = f"/samples/{vid}_emotional.mp3"
    print(f"{vid}: base_wpm={base_wpm}, default_rate={default_rate}")
    return voice


async def main() -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    voices_path = os.path.join(root, "voices", "voices.json")
    samples_dir = os.path.join(root, "web", "public", "samples")
    tmp_dir = os.path.join(root, ".tmp_cal")
    os.makedirs(samples_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    cfg = json.load(open(voices_path, "r", encoding="utf-8"))
    target_wpm = int(cfg.get("target_wpm_default", 250))
    max_rate = int(cfg.get("max_rate_pct", 65))

    updated = []
    for v in cfg["voices"]:
        updated.append(await process_voice(v, target_wpm, max_rate, samples_dir, tmp_dir))

    cfg["voices"] = updated
    with open(voices_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    # Also copy voices.json into web/public/voices.json so the Cloudflare site serves it statically
    with open(os.path.join(root, "web", "public", "voices.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print("Calibration and sample generation complete.")


if __name__ == "__main__":
    asyncio.run(main())
