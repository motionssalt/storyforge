"""
Final FFmpeg video compositor for StoryForge.
Combines:
  - 1080x1920 silent gameplay background
  - Transparent Reddit card PNGs with smooth fade-in/out over exact line time ranges
  - Burned-in ASS comic subtitles (1-3 words, active word highlighted)
  - Narration WAV encoded as AAC
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from tts_engine import LineTiming


def compute_card_windows(
    cards: list[dict[str, Any]],
    line_timings: dict[str, LineTiming],
    total_duration: float,
) -> list[tuple[int, float, float]]:
    """Return list of (card_index, start_sec, end_sec)."""
    windows: list[tuple[int, float, float]] = []
    for idx, item in enumerate(cards):
        from_id = str(item["from_line"])
        to_id = str(item["to_line"])
        st = line_timings[from_id].start if from_id in line_timings else 0.0
        en = line_timings[to_id].end if to_id in line_timings else min(total_duration, st + 3.0)

        if "duration_sec" in item and item["duration_sec"] is not None:
            en = min(total_duration, st + float(item["duration_sec"]))
        elif idx == 0:
            # First hook card: show roughly 2.5-2.9 seconds
            en = min(total_duration, st + 2.8)

        en = max(st + 0.5, min(total_duration, en))
        windows.append((idx, round(st, 3), round(en, 3)))
    return windows


def compose_video(
    bg_video_path: str,
    audio_wav_path: str,
    ass_path: str,
    fonts_dir: str,
    card_pngs: list[str],
    card_windows: list[tuple[int, float, float]],
    out_mp4_path: str,
    duration_sec: float,
) -> str:
    """Render final 1080x1920 30fps H.264 MP4."""
    cmd: list[str] = ["ffmpeg", "-y", "-i", bg_video_path, "-i", audio_wav_path]

    # Add each card PNG as a looped single-image stream trimmed to its active window
    for idx, (_, st, en) in enumerate(card_windows):
        dur = max(0.5, en - st)
        cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-itsoffset", f"{st:.3f}", "-i", card_pngs[idx]]

    filter_parts: list[str] = []
    last_v = "0:v"

    for i, (_, st, en) in enumerate(card_windows):
        in_idx = 2 + i
        dur = max(0.5, en - st)
        fade_d = min(0.18, dur / 4.0)
        fade_out_st = max(0.0, dur - fade_d)
        card_tag = f"card{i}"
        out_tag = f"v{i}"

        filter_parts.append(
            f"[{in_idx}:v]format=rgba,"
            f"fade=t=in:st=0:d={fade_d:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out_st:.3f}:d={fade_d:.3f}:alpha=1[{card_tag}]"
        )
        filter_parts.append(
            f"[{last_v}][{card_tag}]overlay=x=0:y=190:eof_action=pass:enable='between(t,{st:.3f},{en:.3f})'[{out_tag}]"
        )
        last_v = out_tag

    # Burn in ASS subtitles with bundled TTF fonts directory
    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:")
    fonts_esc = fonts_dir.replace("\\", "/").replace(":", "\\:")
    filter_parts.append(f"[{last_v}]ass='{ass_esc}':fontsdir='{fonts_esc}'[vfinal]")

    filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vfinal]",
        "-map",
        "1:a",
        "-t",
        f"{duration_sec:.3f}",
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        out_mp4_path,
    ]
    subprocess.run(cmd, check=True)
    return out_mp4_path
